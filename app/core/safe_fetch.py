"""只允許 http / https 的下載包裝。

## 為什麼

`urllib.request.urlopen()` **什麼 scheme 都吃** —— `file://` 會讀本機檔案、
`ftp://` 會連出去。全站有六處在下載東西（版本檢查、語言包、WinSW、VC++
runtime、健康檢查），其中**有幾處的網址是變數**：管理員設定的鏡像、
從設定檔讀來的來源。那些地方如果被設成 `file:///etc/passwd`，
`urlopen` 會老老實實把檔案讀給你。

bandit 的 B310 指的就是這件事。**擋法是白名單，不是黑名單** ——
只放行 http 與 https，其餘一律拒絕。

## 為什麼不直接關掉 bandit 的這條

把門檻從 Medium 調到 High 就看不到這 6 個了 —— 那是把問題藏起來，不是解決。
"""
from __future__ import annotations

import urllib.request
from typing import Any, Optional
from urllib.parse import urlparse

_ALLOWED = ("http", "https")


class UnsafeUrlScheme(ValueError):
    """網址用了 http / https 以外的協定。"""


def _check(url: str) -> str:
    scheme = (urlparse(url).scheme or "").lower()
    if scheme not in _ALLOWED:
        raise UnsafeUrlScheme(
            f"只接受 http / https 的網址（收到 {scheme or '(沒有協定)'}）")
    return url


def urlopen(target: Any, timeout: Optional[float] = None):
    """`urllib.request.urlopen` 的白名單版本。

    `target` 可以是網址字串，也可以是 `Request` 物件（版本檢查那邊要帶
    User-Agent 標頭，所以要收得下 Request）。
    """
    url = target if isinstance(target, str) else getattr(target, "full_url", "")
    _check(url)
    # scheme 已在上面用白名單擋過 —— 這裡是全站唯一真正呼叫 urlopen 的地方
    return urllib.request.urlopen(target, timeout=timeout)  # nosec B310


def urlopen_direct(target: Any, timeout: Optional[float] = None):
    """同上，但**不經過任何代理伺服器**。

    `urlopen` 會照 `http_proxy` / `https_proxy` 環境變數走。探測自己這台機器
    的 `healthz` 時那是錯的：企業環境的管理員 shell 常設著代理，而
    `no_proxy` 不見得列了 127.0.0.1 —— 於是「連自己」被送去代理伺服器、
    失敗，看起來就像服務沒起來。**本機探測一律直連。**
    """
    url = target if isinstance(target, str) else getattr(target, "full_url", "")
    _check(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(target, timeout=timeout)


def urlretrieve(url: str, filename: str):
    """`urllib.request.urlretrieve` 的白名單版本。"""
    _check(url)
    # scheme 已在上面用白名單擋過
    return urllib.request.urlretrieve(url, filename)  # nosec B310


# ---------------------------------------------------------------------------
# 管理員填的對外網址：**只准公開網路**（SSRF 防護）
# ---------------------------------------------------------------------------
#
# 上面那幾支只擋協定 —— 它們的網址是我們自己寫死的（GitHub、微軟的下載點），
# 或是**刻意要連內網**的（健康檢查連自己、遠端 OCR 伺服器本來就在區網）。
#
# `fetch_public()` 給的是另一種情境：**管理員在設定頁貼一個網址，伺服器照著
# 去抓**（公文撰擬的範本與地址簿，2026-10-07）。那個欄位只要被填成
# `http://127.0.0.1:8765/admin/...`、`http://169.254.169.254/`（雲端中繼資料）
# 或內網某台機器，伺服器就會替填的人去連一個他自己連不到的地方。所以：
#
# 1. **協定白名單**：只收 http / https（同上）。網址不可以夾帶帳號密碼。
# 2. **位址判斷看解析出來的 IP，不看字面**：`localhost`、`0x7f.1`、
#    `2130706433`、`[::ffff:127.0.0.1]` 這些寫法字面上都不像內網。
#    任何一個解析結果不是公開位址就整個拒絕（多筆紀錄只要混一筆內網就擋 ——
#    DNS rebinding 常用這招）。
# 3. **連線時再解析一次、而且直接連那個驗過的 IP**：只在送出前驗一次的話，
#    驗的時候回公開位址、連的時候回 127.0.0.1（DNS rebinding）就繞過去了。
#    TLS 的 SNI 與憑證仍然用主機名稱，不受影響。
# 4. **每一次重新導向都重驗**：公開網址 302 到內網是最常見的繞法。
# 5. **大小與時間都有上限**，超過就停（Content-Length 先看一次、邊讀邊數）。
#
# **代理伺服器**：企業環境常要經代理才連得出去。連「環境變數設定的代理」本身
# 不算 SSRF（那是部署設定，不是使用者填的），所以放行；目的地仍然先用本機
# DNS 驗過。只能經代理上網的機器常常**解析不到外部網域**，那種情況下只放行
# 「看起來是公開網域」的名稱（有點號、不是 .local / .lan / .internal 這類）
# —— 剩下的判斷交給代理。這是已知的限制，寫在這裡讓下一個人知道。

import http.client
import ipaddress
import socket
import ssl
import time
import urllib.error
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote, urlsplit, urlunsplit


class BlockedDestination(ValueError):
    """網址指向本機 / 內部網路 / 雲端中繼資料位址，或格式不允許。

    訊息是**給管理員看的中文**（我們自己寫的，不含對方回應的內容）。"""


class FetchFailed(RuntimeError):
    """下載失敗：連不上、逾時、HTTP 錯誤、檔案太大。訊息同樣是給人看的中文。"""


@dataclass
class FetchResult:
    data: bytes
    final_url: str
    content_type: str


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_INTERNAL_SUFFIXES = (".local", ".localdomain", ".localhost", ".lan", ".internal",
                      ".intranet", ".corp", ".home", ".home.arpa", ".test",
                      ".invalid", ".example")
_USER_AGENT = "Mozilla/5.0 (compatible; jt-doc-tools)"
_MAX_URL_LEN = 2048


def ip_is_public(ip) -> bool:
    """這個位址是不是**公開網路**上的位址。

    `is_global` 已經排除私有、迴路、鏈路本地（含 169.254.169.254）、
    CGNAT（100.64/10）、保留與未指定位址；另外擋群播。IPv6 裡**夾帶 IPv4** 的
    幾種寫法（IPv4-mapped、6to4、Teredo、NAT64）要拿夾帶的那個再判一次 ——
    `64:ff9b::7f00:1` 在 Python 眼裡是 global，實際上是 127.0.0.1。
    """
    if isinstance(ip, str):
        ip = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = [ip.ipv4_mapped, ip.sixtofour]
        if ip.teredo:
            embedded.append(ip.teredo[1])
        if ip in _NAT64:
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        for e in embedded:
            if e is not None and not ip_is_public(e):
                return False
    return bool(ip.is_global and not ip.is_multicast
                and not ip.is_unspecified and not ip.is_reserved)


def _ip_allowed(ip) -> bool:
    """連線前最後一道判斷。**全站只有這一個入口**（測試可以替換它，
    只放行假伺服器那一個位址；正式環境就是 `ip_is_public`）。"""
    return ip_is_public(ip)


def normalize_public_url(url: str) -> str:
    """把管理員貼的網址整理成可以送出的形式，順便擋掉格式不允許的。

    * 只收 http / https，不可以夾帶帳號密碼、不可以有控制字元。
    * 主機名稱轉 IDNA、路徑與查詢字串裡的非 ASCII 字元（例如網址裡直接寫
      「Web版公文製作系統表單範本.zip」）照 UTF-8 百分比編碼 —— 已經編好的
      `%E7%89%88` 不會被再編一次。`#` 之後的片段丟掉（不會送到伺服器）。

    **這裡不解析 DNS**（存設定時不該因為這台暫時連不上 DNS 就存不進去），
    位址判斷在 `check_public_destination()`。
    """
    if not isinstance(url, str):
        raise BlockedDestination("網址格式不正確")
    u = url.strip()
    if not u:
        raise BlockedDestination("網址是空的")
    if len(u) > _MAX_URL_LEN:
        raise BlockedDestination("網址太長")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in u):
        raise BlockedDestination("網址裡有控制字元")
    try:
        p = urlsplit(u)
    except ValueError:
        raise BlockedDestination("網址格式不正確") from None
    scheme = (p.scheme or "").lower()
    if scheme not in _ALLOWED:
        raise UnsafeUrlScheme(
            f"只接受 http / https 的網址（收到 {scheme or '(沒有協定)'}）")
    if p.username is not None or p.password is not None:
        raise BlockedDestination("網址不可以夾帶帳號密碼")
    host = (p.hostname or "").strip()
    if not host:
        raise BlockedDestination("網址少了主機名稱")
    try:
        port = p.port
    except ValueError:
        raise BlockedDestination("網址的連接埠不正確") from None
    try:
        ipaddress.ip_address(host)
        host_enc = f"[{host}]" if ":" in host else host
    except ValueError:
        try:
            host_enc = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise BlockedDestination("網址的主機名稱不正確") from None
        if not all(c.isalnum() or c in "-." for c in host_enc):
            raise BlockedDestination("網址的主機名稱不正確")
    netloc = host_enc + (f":{port}" if port is not None else "")
    path = quote(p.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(p.query, safe="=&%/:?@!$'()*+,;-._~")
    return urlunsplit((scheme, netloc, path, query, ""))


def _looks_like_public_name(host: str) -> bool:
    h = host.lower().rstrip(".")
    if "." not in h or h == "localhost":
        return False
    if h.endswith(_INTERNAL_SUFFIXES):
        return False
    tld = h.rsplit(".", 1)[-1]
    return tld.isalpha() or tld.startswith("xn--")


def _proxy_for(url: str) -> Optional[str]:
    """這個網址會不會經代理（照 urllib 自己的規則：環境變數 ＋ no_proxy）。"""
    p = urlsplit(url)
    proxies = urllib.request.getproxies()
    proxy = proxies.get((p.scheme or "").lower())
    if not proxy:
        return None
    try:
        if p.hostname and urllib.request.proxy_bypass(p.hostname):
            return None
    except Exception:
        pass
    return proxy


def _proxy_endpoints() -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    for scheme, proxy in urllib.request.getproxies().items():
        if scheme == "no" or not proxy:
            continue
        try:
            pp = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
            if pp.hostname:
                out.add((pp.hostname.lower(),
                         pp.port or (443 if pp.scheme == "https" else 80)))
        except ValueError:
            continue
    return out


def _resolve_checked(host: str, port: int) -> list:
    """解析主機並確認**每一個**結果都是公開位址；回 `getaddrinfo` 的結果。"""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise FetchFailed(f"找不到主機 {host}（DNS 解析失敗）") from None
    if not infos:
        raise FetchFailed(f"找不到主機 {host}（DNS 解析失敗）")
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(str(addr).split("%", 1)[0])
        except ValueError:
            raise BlockedDestination(f"{host} 解析出無法判斷的位址，已拒絕") from None
        if not _ip_allowed(ip):
            raise BlockedDestination(
                f"{host} 指向本機或內部網路位址（{ip}），為了安全不會連過去。"
                "請改用公開網路上的網址，或把檔案下載下來改用「上傳檔案」。")
    return infos


def check_public_destination(url: str, *, proxied: bool = False) -> str:
    """整理網址並確認它指向公開網路；回整理好的網址。"""
    norm = normalize_public_url(url)
    p = urlsplit(norm)
    host = p.hostname or ""
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        ipaddress.ip_address(host)
        is_literal = True
    except ValueError:
        is_literal = False
    h = host.lower().rstrip(".")
    if not is_literal and (h == "localhost" or h.endswith(_INTERNAL_SUFFIXES)):
        raise BlockedDestination(f"{host} 看起來是內部網路的主機名稱，為了安全不會連過去。")
    try:
        _resolve_checked(host, port)
    except FetchFailed:
        # 只能經代理上網的機器常常解析不到外部網域 —— 那就交給代理，
        # 但只放行「看起來是公開網域」的名稱（見模組說明）。
        if proxied and not is_literal and _looks_like_public_name(host):
            return norm
        raise
    return norm


def _pinned_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                              source_address=None, *args, **kwargs):
    """`socket.create_connection` 的替身：**直接連驗過的 IP**。

    連的是環境變數設定的代理伺服器時照常連（部署設定，不是使用者填的）。
    """
    host, port = address[0], int(address[1])
    if (str(host).lower(), port) in _proxy_endpoints():
        return socket.create_connection((host, port), timeout, source_address)
    infos = _resolve_checked(str(host), port)
    last: Optional[BaseException] = None
    for family, socktype, proto, _canon, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as e:
            last = e
            sock.close()
    raise last or OSError("connect failed")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _pinned_create_connection


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _pinned_create_connection


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PinnedHTTPConnection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """每一次重新導向都重驗目的地（公開網址 302 到內網是最常見的繞法）。"""
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        norm = check_public_destination(newurl, proxied=_proxy_for(newurl) is not None)
        return super().redirect_request(req, fp, code, msg, headers, norm)


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _opener() -> urllib.request.OpenerDirector:
    """**自己組 opener，不用 `build_opener()`** —— 後者會順手裝上 file:// 與
    ftp:// 的處理器。這裡只有 http / https 兩條路。"""
    od = urllib.request.OpenerDirector()
    for h in (urllib.request.ProxyHandler(), urllib.request.UnknownHandler(),
              urllib.request.HTTPDefaultErrorHandler(), _CheckedRedirect(),
              urllib.request.HTTPErrorProcessor(), _PinnedHTTPHandler(),
              _PinnedHTTPSHandler(context=_tls_context())):
        od.add_handler(h)
    return od


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def fetch_public(url: str, *, max_bytes: int, timeout: float = 30.0,
                 deadline_s: float = 900.0,
                 progress: Optional[Callable[[int, Optional[int]], None]] = None,
                 ) -> FetchResult:
    """下載一個**公開網路上**的檔案（管理員填的網址專用）。

    失敗一律丟 `BlockedDestination` / `UnsafeUrlScheme`（網址本身不允許）或
    `FetchFailed`（連不上、逾時、HTTP 錯誤、太大）—— 訊息都是給人看的中文，
    不夾帶對方回應的內容。
    """
    norm = check_public_destination(url, proxied=_proxy_for(url) is not None)
    req = urllib.request.Request(norm, headers={"User-Agent": _USER_AGENT,
                                                "Accept": "*/*"})
    started = time.monotonic()
    try:
        with _opener().open(req, timeout=timeout) as resp:
            cl = (resp.headers.get("Content-Length") or "").strip()
            total = int(cl) if cl.isdigit() else None
            if total is not None and total > max_bytes:
                raise FetchFailed(
                    f"檔案太大（{_mb(total)}，上限 {_mb(max_bytes)}），已停止下載")
            buf = bytearray()
            while True:
                if time.monotonic() - started > deadline_s:
                    raise FetchFailed(
                        f"下載超過 {int(deadline_s // 60)} 分鐘仍未完成，已停止")
                chunk = resp.read(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > max_bytes:
                    raise FetchFailed(
                        f"檔案太大（超過上限 {_mb(max_bytes)}），已停止下載")
                if progress is not None:
                    try:
                        progress(len(buf), total)
                    except Exception:
                        pass
            return FetchResult(bytes(buf), resp.geturl() or norm,
                               resp.headers.get_content_type() or "")
    except (BlockedDestination, UnsafeUrlScheme, FetchFailed):
        raise
    except urllib.error.HTTPError as e:
        raise FetchFailed(f"對方伺服器回應 HTTP {e.code}") from None
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (BlockedDestination, UnsafeUrlScheme, FetchFailed)):
            raise reason from None
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise FetchFailed("對方的 TLS 憑證驗證失敗（若是企業網路檢查換了憑證，"
                              "請把企業 CA 加進系統信任庫）") from None
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise FetchFailed("連線逾時") from None
        if isinstance(reason, ConnectionRefusedError):
            raise FetchFailed("對方拒絕連線") from None
        raise FetchFailed("連不上對方伺服器") from None
    except ssl.SSLCertVerificationError:
        raise FetchFailed("對方的 TLS 憑證驗證失敗") from None
    except (TimeoutError, socket.timeout):
        raise FetchFailed("連線逾時") from None
    except (http.client.HTTPException, OSError):
        raise FetchFailed("連線中斷，沒有收到完整的檔案") from None
