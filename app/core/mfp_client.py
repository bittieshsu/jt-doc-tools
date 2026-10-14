"""事務機取件：連到事務機的網路用戶端與「連線測試」的逐步診斷。

## 為什麼診斷要這麼細

我們手上沒有設備。客戶現場跑一次連線測試的結果，就是我們了解那台機器（網頁結構、
登入流程、TLS 設定）的主要來源，所以每一步都記下「做了什麼、結果、耗時、依據」，
畫面上看得到、也能下載成報告（使用者 2026-10-13 交代，見 docs-share/mfp-fetch/decisions.md）。

## 安全邊界（這支會讓伺服器去連使用者指定的位址，等於一條 SSRF 管道）

* **只准內網位址**：私有位址（RFC 1918、IPv6 ULA）。回送、鏈路本地（含雲端 metadata
  169.254.169.254）、群播、未指定、保留位址、公網位址、**伺服器自己的位址**一律拒絕；
  IPv4 對映的 IPv6 先還原成 IPv4 再判斷。
* **只准網頁連接埠**（`ALLOWED_PORTS`）—— 不能拿來掃描任意服務。
* **DNS 解析到的每一個位址都要過**，之後**固定連那個 IP**（不再解析第二次），防 DNS rebinding。
* **只發 GET、不帶任何帳密或 Cookie**；轉址只跟同一個主機＋連接埠＋協定；內容最多讀
  `MAX_BODY`；整次測試有時間預算。不用環境變數裡的 proxy。
* TLS 預設驗證。驗證失敗時**記下確切原因**，另外用不驗證的連線取得憑證內容與首頁做辨識
  —— 這條連線**不送任何帳密**，畫面上標明「未驗證」。之後真正的登入與取件不會走未驗證的連線。

回傳的報告已經過 `mfp_redact`（畫面、下載、紀錄檔共用一份遮蔽規則）。
"""
from __future__ import annotations

import errno
import hashlib
import http.client
import ipaddress
import logging
import re
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutTimeout
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import quote, urljoin, urlsplit

from . import mfp_redact as red

logger = logging.getLogger("app.mfp_fetch")

#: 允許測試的連接埠（事務機的網頁介面常見的幾個）。不是任意埠 —— 否則這支會變成內網掃描器。
ALLOWED_PORTS = (80, 443, 8080, 8443)
DNS_TIMEOUT = 5.0
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0
#: 一次連線測試的總時間預算（秒）。
TOTAL_BUDGET = 45.0
#: 每個網頁最多讀多少（位元組）。事務機首頁很小，超過就是不對勁。
MAX_BODY = 256 * 1024
#: 每個協定最多發幾個請求（首頁＋轉址＋框架頁）。
MAX_REQUESTS = 6
#: 摘要文字最多幾個字。
SUMMARY_CHARS = 500
USER_AGENT = "jt-doc-tools (mfp-fetch connection test)"

#: 固定的訊息（翻譯檔裡有英日文；畫面用 `tr()`）。**不夾帶變數** —— 變數放在 detail 裡另外顯示。
MESSAGES: dict[str, str] = {
    "HOST_INVALID": "位址格式不正確：請輸入 IP 位址或主機名稱，不要加 http:// 或路徑",
    "PORT_NOT_ALLOWED": "這個連接埠不在允許的範圍（只能測事務機網頁介面常用的 80、443、8080、8443）",
    "SCHEME_INVALID": "協定只能是 http 或 https",
    "DNS_FAILED": "查不到這個主機名稱的位址",
    "DNS_TIMEOUT": "查詢主機名稱逾時",
    "ADDR_LOOPBACK": "不可以連到本機回送位址",
    "ADDR_LINK_LOCAL": "不可以連到鏈路本地位址（含雲端主機的中繼資料位址）",
    "ADDR_MULTICAST": "不可以連到群播位址",
    "ADDR_RESERVED": "不可以連到保留或未指定的位址",
    "ADDR_PUBLIC": "只能連到內部網路的位址（私有 IP）；公網位址不允許",
    "ADDR_SELF": "這是本服務所在主機自己的位址，不可以連",
    "ADDR_OK": "位址通過檢查（內部網路）",
    "TCP_OK": "TCP 連線成功",
    "TCP_TIMEOUT": "連線逾時：設備沒有回應（可能沒開機、IP 不對，或中間有防火牆擋住）",
    "TCP_REFUSED": "設備拒絕連線：這個連接埠沒有服務在聽（網頁介面可能沒開，或用的是別的連接埠）",
    "TCP_UNREACHABLE": "無法連到這個網段：伺服器到設備之間沒有路由",
    "TCP_ERROR": "連線失敗",
    "TLS_OK": "TLS 憑證驗證通過",
    "TLS_VERIFY_FAILED": "TLS 憑證沒有通過驗證（下面列出原因與憑證內容）；連線測試改用未驗證的連線讀首頁，不會傳送任何帳密",
    "TLS_HANDSHAKE_FAILED": "TLS 交握失敗（設備可能只支援舊版 TLS，或這個連接埠不是 HTTPS）",
    "HTTP_OK": "網頁回應正常",
    "HTTP_REDIRECT_FOLLOWED": "設備要求轉址到同一台設備的另一個頁面，已跟隨",
    "HTTP_REDIRECT_NOT_FOLLOWED": "設備要求轉址到不同的主機、連接埠或協定，連線測試不跟隨（下面列出目的地）",
    "HTTP_ERROR_STATUS": "網頁回應了錯誤狀態碼",
    "HTTP_TIMEOUT": "讀取網頁逾時",
    "HTTP_BAD_RESPONSE": "回應不是正常的 HTTP（這個連接埠可能不是網頁服務）",
    "HTTP_TOO_LARGE": "網頁內容超過讀取上限，只看了前面一段",
    "HTTP_INCOMPLETE": "設備沒有送完整個頁面就關閉連線，用已經收到的部分辨識",
    "ESCL_OK": "支援從本站掃描（eSCL）",
    "ESCL_NONE": "這台沒有 eSCL 掃描介面，不能從本站掃描",
    "ESCL_BAD": "eSCL 掃描介面回應的格式不正確",
    "BUDGET_EXCEEDED": "連線測試超過時間預算，後面的步驟沒有做",
    "SKIPPED_AFTER_FAILURE": "前一步失敗，這一步沒有做",
}


class PolicyError(Exception):
    def __init__(self, code: str, detail: Optional[dict] = None):
        super().__init__(code)
        self.code = code
        self.message = MESSAGES.get(code, code)
        self.detail = detail or {}


# --------------------------------------------------------------------------- 輸入與目的地政策

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$")


def parse_host(raw: str) -> str:
    """使用者輸入的「IP 或主機名稱」。不收網址、帳密、路徑、連接埠（連接埠另一欄）。"""
    h = (raw or "").strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    try:
        return str(ipaddress.ip_address(h))
    except ValueError:
        pass
    if not h or not _HOSTNAME_RE.match(h) or h.replace(".", "").isdigit():
        raise PolicyError("HOST_INVALID", {"input": (raw or "")[:100]})
    return h.rstrip(".").lower()


def parse_port_scheme(port: Any, scheme: Any) -> list[tuple[str, int]]:
    """要嘗試的（協定, 連接埠）。都沒給 → HTTPS 443 與 HTTP 80 都測（各自獨立報告）。"""
    sc = (scheme or "").strip().lower() or None
    if sc not in (None, "http", "https"):
        raise PolicyError("SCHEME_INVALID", {"input": str(scheme)[:20]})
    p: Optional[int] = None
    if port not in (None, ""):
        try:
            p = int(port)
        except (TypeError, ValueError):
            raise PolicyError("PORT_NOT_ALLOWED", {"input": str(port)[:20],
                                                   "allowed": list(ALLOWED_PORTS)}) from None
        if p not in ALLOWED_PORTS:
            raise PolicyError("PORT_NOT_ALLOWED", {"input": p, "allowed": list(ALLOWED_PORTS)})
    if p is None and sc is None:
        return [("https", 443), ("http", 80)]
    if p is None:
        return [(sc, 443 if sc == "https" else 80)]
    if sc is None:
        sc = "https" if p in (443, 8443) else "http"
    return [(sc, p)]


_SELF_CACHE: tuple[float, frozenset[str]] = (0.0, frozenset())
_SELF_LOCK = threading.Lock()


def _self_addresses() -> frozenset[str]:
    """這台主機自己的位址（每張網卡）。快取 60 秒。"""
    global _SELF_CACHE
    with _SELF_LOCK:
        ts, addrs = _SELF_CACHE
        if time.time() - ts < 60:
            return addrs
        out: set[str] = set()
        try:
            import psutil
            for _nic, lst in psutil.net_if_addrs().items():
                for a in lst:
                    if a.family in (socket.AF_INET, socket.AF_INET6):
                        try:
                            out.add(str(ipaddress.ip_address(a.address.split("%", 1)[0])))
                        except ValueError:
                            pass
        except Exception:  # noqa: BLE001 — 拿不到就只靠其他規則（回送位址照樣擋）
            logger.warning("mfp: 無法列出本機網卡位址")
        _SELF_CACHE = (time.time(), frozenset(out))
        return _SELF_CACHE[1]


def check_address(ip_text: str) -> Optional[str]:
    """位址不可以連 → 回原因代碼；可以 → None。"""
    ip = ipaddress.ip_address(ip_text)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "ADDR_LOOPBACK"
    if ip.is_link_local:
        return "ADDR_LINK_LOCAL"
    if ip.is_multicast:
        return "ADDR_MULTICAST"
    if ip.is_unspecified or ip.is_reserved or (
            isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("0.0.0.0/8")):
        return "ADDR_RESERVED"
    if not ip.is_private:
        return "ADDR_PUBLIC"
    if str(ip) in _self_addresses():
        return "ADDR_SELF"
    return None


_DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mfp-dns")


def device_key(raw_host: str) -> str:
    """排隊用的設備鍵：解析後、**每一個**位址都通過檢查的 IP（IPv4 優先）。

    同一台被不同人各自加進清單（一個寫 IP、一個寫主機名稱）也會排在同一條隊伍。
    不合格丟 `PolicyError`（不連線）—— 跟連線測試同一套規則。"""
    host = parse_host(raw_host)
    addrs = resolve(host, 80)
    verdicts = [{"ip": a, "code": check_address(a) or "ADDR_OK"} for a in addrs]
    bad = [v for v in verdicts if v["code"] != "ADDR_OK"]
    if bad:
        for v in verdicts:
            v["message"] = MESSAGES.get(v["code"], v["code"])
        raise PolicyError(bad[0]["code"], {"host": host, "addresses": verdicts})
    return sorted(addrs, key=lambda a: ":" in a)[0]


def resolve(host: str, port: int) -> list[str]:
    """解析成位址清單（去重、保留順序）。IP 字面值直接回傳。"""
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    fut = _DNS_POOL.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    try:
        infos = fut.result(timeout=DNS_TIMEOUT)
    except _FutTimeout:
        raise PolicyError("DNS_TIMEOUT", {"host": host}) from None
    except OSError as e:
        raise PolicyError("DNS_FAILED", {"host": host, "error": e.__class__.__name__}) from None
    out: list[str] = []
    for fam, _t, _p, _c, sa in infos:
        a = sa[0].split("%", 1)[0]
        if a not in out:
            out.append(a)
    if not out:
        raise PolicyError("DNS_FAILED", {"host": host})
    return out


# --------------------------------------------------------------------------- 網頁解析

class _PageParser(HTMLParser):
    """只收：標題、可見文字、meta refresh、框架網址、表單結構（名稱與型別，**不收值**）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip = 0
        self.text: list[str] = []
        self.refresh: Optional[str] = None
        self.frames: list[str] = []
        self.forms: list[dict] = []
        self.password_inputs = 0

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("script", "style", "noscript", "template"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta" and a.get("http-equiv", "").lower() == "refresh":
            m = re.search(r"url\s*=\s*['\"]?([^'\";]+)", a.get("content", ""), re.I)
            if m and self.refresh is None:
                self.refresh = m.group(1).strip()
        elif tag in ("frame", "iframe") and a.get("src") and len(self.frames) < 10:
            self.frames.append(a["src"])
        elif tag == "form" and len(self.forms) < 10:
            self.forms.append({"action": a.get("action", ""), "method": (a.get("method") or "get").lower(),
                               "inputs": []})
        elif tag in ("input", "select", "textarea"):
            t = (a.get("type") or ("text" if tag == "input" else tag)).lower()
            if t == "password":
                self.password_inputs += 1
            if self.forms and len(self.forms[-1]["inputs"]) < 40:
                self.forms[-1]["inputs"].append({"name": a.get("name", "")[:80], "type": t[:20]})

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            s = data.strip()
            if s:
                self.text.append(s)


def _decode(body: bytes, content_type: str) -> tuple[str, str]:
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    cands = [m.group(1)] if m else []
    mm = re.search(rb"<meta[^>]+charset=['\"]?([\w\-]+)", body[:4096], re.I)
    if mm:
        cands.append(mm.group(1).decode("ascii", "ignore"))
    cands += ["utf-8", "cp932", "big5", "latin-1"]
    for c in cands:
        try:
            return body.decode(c), c
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace"), "utf-8"


#: 寫在 JavaScript 字串裡的 meta refresh（例如 Epson：`document.write("<meta http-equiv='refresh'
#: content='0; URL= ./PRESENTATION/HTML/TOP/INDEX.HTML'>")`）。連線測試不執行 JavaScript，
#: 沒有這條就停在那張空頁，連型號都看不到（2026-10-14 實機：Epson L3150）。
#: **只認轉址這一種寫法**，不去抓頁面上任意的 JavaScript 網址。
_SCRIPT_REFRESH = re.compile(
    r"http-equiv\s*=\s*\\?['\"]?refresh\\?['\"]?\s+content\s*=\s*\\?['\"]?\s*\d+\s*;\s*url\s*=\s*([^'\"\\\s>]+)",
    re.I)
#: 看起來會改變設備狀態的網址一律不跟（就算只是 GET）。
_RISKY_PATH = re.compile(r"logout|logoff|signout|reboot|restart|reset|shutdown|delete|clear|factory", re.I)


def _script_redirects(text: str) -> list[str]:
    out: list[str] = []
    for m in _SCRIPT_REFRESH.finditer(text[:200_000]):
        u = m.group(1).strip()
        if u and u not in out and not _RISKY_PATH.search(u):
            out.append(u)
        if len(out) >= 3:
            break
    return out


def parse_page(body: bytes, content_type: str) -> dict:
    text, charset = _decode(body, content_type)
    p = _PageParser()
    try:
        p.feed(text)
        p.close()
    except Exception:  # noqa: BLE001 — 壞掉的 HTML 也盡量拿到前面那段
        pass
    visible = re.sub(r"\s+", " ", " ".join(p.text)).strip()
    return {
        "charset": charset,
        "title": re.sub(r"\s+", " ", p.title).strip()[:200],
        "text": visible[:SUMMARY_CHARS],
        "text_full_for_detection": visible[:20000],
        "refresh": p.refresh,
        # 只在頁面本身沒有 meta refresh 時才看 JavaScript 寫出來的（真的 meta refresh 優先）
        "script_redirects": [] if p.refresh else _script_redirects(text),
        "frames": p.frames,
        "forms": p.forms,
        "password_inputs": p.password_inputs,
    }


# --------------------------------------------------------------------------- 憑證

def cert_info(der: Optional[bytes]) -> dict:
    if not der:
        return {}
    out: dict[str, Any] = {"sha256": hashlib.sha256(der).hexdigest()}
    try:
        from cryptography import x509
        c = x509.load_der_x509_certificate(der)
        out["subject"] = c.subject.rfc4514_string()
        out["issuer"] = c.issuer.rfc4514_string()
        out["self_signed"] = c.subject == c.issuer
        nb, na = c.not_valid_before_utc, c.not_valid_after_utc
        now = datetime.now(timezone.utc)
        out["not_before"] = nb.isoformat()
        out["not_after"] = na.isoformat()
        out["expired"] = now > na
        out["not_yet_valid"] = now < nb
        try:
            san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            out["san"] = ([f"DNS:{n}" for n in san.get_values_for_type(x509.DNSName)]
                          + [f"IP:{n}" for n in san.get_values_for_type(x509.IPAddress)])[:20]
        except x509.ExtensionNotFound:
            out["san"] = []
        pk = c.public_key()
        out["public_key"] = f"{pk.__class__.__name__.replace('_', '').replace('PublicKey', '')} {getattr(pk, 'key_size', '')}".strip()
    except Exception as e:  # noqa: BLE001
        out["parse_error"] = e.__class__.__name__
    return out


# --------------------------------------------------------------------------- 單一連線

class _Budget:
    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds

    def left(self) -> float:
        return self.deadline - time.monotonic()


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _tcp_error_code(e: BaseException) -> str:
    if isinstance(e, socket.timeout):
        return "TCP_TIMEOUT"
    if isinstance(e, ConnectionRefusedError):
        return "TCP_REFUSED"
    if isinstance(e, OSError) and e.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH,
                                               getattr(errno, "WSAEHOSTUNREACH", -1),
                                               getattr(errno, "WSAENETUNREACH", -1)):
        return "TCP_UNREACHABLE"
    return "TCP_ERROR"


def _connect(ip: str, port: int, timeout: float) -> socket.socket:
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
    except BaseException:
        s.close()
        raise
    return s


def _verified_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _unverified_context() -> ssl.SSLContext:
    """只讀首頁做辨識用（不送任何帳密）。

    服務啟動時 truststore 已把 `ssl.SSLContext` 換成「用作業系統信任庫」的版本；它在
    `CERT_NONE` 時本來就不驗證（Windows / macOS 看 verify_mode，Linux 交給 OpenSSL），
    所以照常建立即可。**不可以改用 truststore 保存的原始類別**：注入之後那個類別的
    `verify_mode` 設定器會繞回 truststore，無限遞迴（實測 RecursionError）。"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _host_header(host: str, scheme: str, port: int) -> str:
    h = f"[{host}]" if ":" in host else host
    default = 443 if scheme == "https" else 80
    return h if port == default else f"{h}:{port}"


def _http_get(sock, host_hdr: str, path: str, budget: _Budget) -> dict:
    """在一條已經建好（含 TLS）的連線上送一個 GET，讀回狀態、標頭、最多 MAX_BODY 的內容。

    **不送 `Connection: close`**（讀完由我們自己關連線）：HP 小型機的網頁伺服器（Virata-EmWeb）
    看到這個標頭，chunked 回應送到 8192 位元組就切斷、不送結尾（2026-10-14 實機，M141w）；
    不帶就完整送出。回應不帶長度也不分塊時照 HTTP/1.1 一定會自己關，不會卡住（還有讀取逾時）。
    真的被切斷時用已經收到的部分（`incomplete`），不當成「不是網頁服務」。
    """
    req = (f"GET {path} HTTP/1.1\r\nHost: {host_hdr}\r\nUser-Agent: {USER_AGENT}\r\n"
           "Accept: text/html,application/xhtml+xml,*/*;q=0.8\r\n"
           "Accept-Language: zh-TW,ja;q=0.8,en;q=0.6\r\n\r\n")
    sock.settimeout(max(1.0, min(READ_TIMEOUT, budget.left())))
    sock.sendall(req.encode("ascii"))
    resp = http.client.HTTPResponse(sock, method="GET")
    resp.begin()
    incomplete = False
    try:
        body = resp.read(MAX_BODY + 1)
    except http.client.IncompleteRead as e:
        body, incomplete = bytes(e.partial or b""), True
    return {"status": resp.status, "reason": resp.reason, "version": resp.version,
            "headers": resp.getheaders(), "body": body[:MAX_BODY], "truncated": len(body) > MAX_BODY,
            "incomplete": incomplete}


def _same_origin(url: str, scheme: str, host: str, port: int) -> bool:
    try:
        u = urlsplit(url)
        up = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return False
    return (u.scheme == scheme and (u.hostname or "").lower().strip("[]") == host.lower()
            and up == port)


def _safe_path(url: str) -> str:
    u = urlsplit(url)
    path = quote(u.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if u.query:
        path += "?" + quote(u.query, safe="=&%:@/!$'()*+,;-._~")
    return path


def _probe_one(host: str, ip: str, scheme: str, port: int, budget: _Budget) -> dict:
    """對一組（協定, 連接埠）做 TCP → TLS → HTTP，回傳這一組的步驟與收集到的網頁。"""
    att: dict[str, Any] = {"scheme": scheme, "port": port, "steps": [], "pages": [],
                           "reachable": False, "tls": None}
    steps = att["steps"]
    host_hdr = _host_header(host, scheme, port)
    base = f"{scheme}://{host_hdr}/"
    queue = ["/"]
    seen: set[str] = set()
    verified: Optional[bool] = None
    sni = None if _is_ip(host) else host
    n = 0
    while queue and n < MAX_REQUESTS:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        if budget.left() <= 1:
            steps.append({"step": "budget", "ok": False, "code": "BUDGET_EXCEEDED"})
            break
        n += 1
        # ---- TCP
        t0 = time.monotonic()
        try:
            sock = _connect(ip, port, min(CONNECT_TIMEOUT, budget.left()))
        except BaseException as e:  # noqa: BLE001
            code = _tcp_error_code(e)
            steps.append({"step": "tcp", "ok": False, "code": code, "ms": _ms(t0),
                          "detail": {"ip": ip, "port": port, "error": e.__class__.__name__,
                                     "errno": getattr(e, "errno", None)}})
            break
        if n == 1:
            steps.append({"step": "tcp", "ok": True, "code": "TCP_OK", "ms": _ms(t0),
                          "detail": {"ip": ip, "port": port}})
        # ---- TLS
        if scheme == "https":
            t0 = time.monotonic()
            raw = sock
            try:
                if verified is False:
                    sock = _unverified_context().wrap_socket(raw, server_hostname=sni)
                else:
                    sock = _verified_context().wrap_socket(raw, server_hostname=sni or host)
                    verified = True
                if att["tls"] is None:
                    att["tls"] = {"verified": True, "version": sock.version(),
                                  "cipher": (sock.cipher() or [None])[0],
                                  "certificate": cert_info(sock.getpeercert(binary_form=True))}
                    steps.append({"step": "tls", "ok": True, "code": "TLS_OK", "ms": _ms(t0),
                                  "detail": att["tls"]})
            except ssl.SSLCertVerificationError as e:
                raw.close()
                verified = False
                reason = {"verify_code": e.verify_code, "verify_message": e.verify_message}
                info: dict[str, Any] = {"verified": False, "reason": reason}
                try:
                    raw2 = _connect(ip, port, min(CONNECT_TIMEOUT, budget.left()))
                    sock = _unverified_context().wrap_socket(raw2, server_hostname=sni)
                    info.update({"version": sock.version(), "cipher": (sock.cipher() or [None])[0],
                                 "certificate": cert_info(sock.getpeercert(binary_form=True))})
                except BaseException as e2:  # noqa: BLE001
                    info["unverified_error"] = e2.__class__.__name__
                    att["tls"] = info
                    steps.append({"step": "tls", "ok": False, "code": "TLS_VERIFY_FAILED",
                                  "ms": _ms(t0), "detail": info})
                    break
                att["tls"] = info
                steps.append({"step": "tls", "ok": False, "code": "TLS_VERIFY_FAILED",
                              "ms": _ms(t0), "detail": info})
            except (ssl.SSLError, OSError) as e:
                raw.close()
                att["tls"] = {"verified": False, "error": e.__class__.__name__,
                              "reason": getattr(e, "reason", None) or str(e)[:200]}
                steps.append({"step": "tls", "ok": False, "code": "TLS_HANDSHAKE_FAILED",
                              "ms": _ms(t0), "detail": att["tls"]})
                break
        # ---- HTTP
        t0 = time.monotonic()
        try:
            r = _http_get(sock, host_hdr, path, budget)
        except socket.timeout:
            steps.append({"step": "http", "ok": False, "code": "HTTP_TIMEOUT", "ms": _ms(t0),
                          "detail": {"path": path}})
            sock.close()
            break
        except (http.client.HTTPException, OSError, ValueError) as e:
            steps.append({"step": "http", "ok": False, "code": "HTTP_BAD_RESPONSE", "ms": _ms(t0),
                          "detail": {"path": path, "error": e.__class__.__name__}})
            sock.close()
            break
        sock.close()
        att["reachable"] = True
        ctype = dict((k.lower(), v) for k, v in r["headers"]).get("content-type", "")
        page = parse_page(r["body"], ctype) if ("html" in ctype.lower() or not ctype) else \
            {"title": "", "text": "", "text_full_for_detection": "", "refresh": None,
             "frames": [], "forms": [], "password_inputs": 0, "charset": ""}
        page.update({"path": path, "status": r["status"],
                     "headers": r["headers"], "content_type": ctype,
                     "bytes": len(r["body"]), "truncated": r["truncated"],
                     "tls_verified": verified if scheme == "https" else None})
        att["pages"].append(page)
        code = "HTTP_OK" if r["status"] < 400 else "HTTP_ERROR_STATUS"
        step = {"step": "http", "ok": r["status"] < 400, "code": code, "ms": _ms(t0),
                "detail": {"path": path, "status": r["status"], "reason": r["reason"],
                           "http_version": "HTTP/1.1" if r["version"] == 11 else "HTTP/1.0",
                           "headers": [list(h) for h in red.redact_headers(r["headers"])],
                           "bytes": len(r["body"]),
                           "title": page["title"], "text": page["text"],
                           "forms": page["forms"], "password_inputs": page["password_inputs"],
                           "frames": page["frames"][:10], "refresh": page["refresh"],
                           "tls_verified": page["tls_verified"]}}
        if r["truncated"]:
            step["note"] = "HTTP_TOO_LARGE"
        elif r.get("incomplete"):
            step["note"] = "HTTP_INCOMPLETE"
        steps.append(step)
        # ---- 下一個要看的頁面：轉址、meta refresh、框架（只限同一台設備）
        nexts: list[str] = []
        loc = dict((k.lower(), v) for k, v in r["headers"]).get("location")
        if 300 <= r["status"] < 400 and loc:
            nexts.append(loc)
        if page.get("refresh"):
            nexts.append(page["refresh"])
        nexts += page.get("frames", [])[:2]
        nexts += page.get("script_redirects", [])[:3]
        for nxt in nexts:
            target = urljoin(base + path.lstrip("/"), nxt)
            if _same_origin(target, scheme, host, port):
                p2 = _safe_path(target)
                if p2 not in seen and p2 not in queue:
                    queue.append(p2)
                    steps.append({"step": "follow", "ok": True, "code": "HTTP_REDIRECT_FOLLOWED",
                                  "detail": {"from": path, "to": p2}})
            else:
                steps.append({"step": "follow", "ok": None, "code": "HTTP_REDIRECT_NOT_FOLLOWED",
                              "detail": {"from": path, "to": target}})
    return att


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- 對外入口

def probe(raw_host: str, port: Any = None, scheme: Any = None) -> dict:
    """連線測試。回傳已遮蔽的完整報告（`report`）與給程式用的摘要。

    輸入或目的地政策不通過 → 丟 `PolicyError`（不連線）。其餘的失敗都寫在報告裡，不丟例外。
    """
    from . import mfp_profiles
    started = datetime.now(timezone.utc)
    budget = _Budget(TOTAL_BUDGET)
    host = parse_host(raw_host)
    targets = parse_port_scheme(port, scheme)
    report: dict[str, Any] = {
        "tool": "mfp-fetch", "kind": "connection-test", "app_version": _app_version(),
        "started_at": started.isoformat(), "target": {"host": host, "tries": [
            {"scheme": s, "port": p} for s, p in targets]},
        "steps": [], "attempts": [],
    }
    t0 = time.monotonic()
    addrs = resolve(host, targets[0][1])
    verdicts = [{"ip": a, "code": check_address(a) or "ADDR_OK"} for a in addrs]
    bad = [v for v in verdicts if v["code"] != "ADDR_OK"]
    report["steps"].append({"step": "dns", "ok": not bad, "code": bad[0]["code"] if bad else "ADDR_OK",
                            "ms": _ms(t0), "detail": {"host": host, "addresses": verdicts}})
    if bad:
        # 任何一個位址不合格就整個拒絕 —— 只挑合格的去連，等於讓 DNS 決定連哪裡
        for v in verdicts:
            v["message"] = MESSAGES.get(v["code"], v["code"])
        raise PolicyError(bad[0]["code"], {"host": host, "addresses": verdicts})
    ip = sorted(addrs, key=lambda a: ":" in a)[0]   # IPv4 優先
    pages: list[dict] = []
    for sc, pt in targets:
        if budget.left() <= 1:
            report["attempts"].append({"scheme": sc, "port": pt, "steps": [
                {"step": "budget", "ok": False, "code": "BUDGET_EXCEEDED"}], "reachable": False})
            continue
        att = _probe_one(host, ip, sc, pt, budget)
        pages += [dict(p, scheme=sc, port=pt) for p in att["pages"]]
        report["attempts"].append({k: v for k, v in att.items() if k != "pages"})
    report["escl"] = _probe_escl(host, ip, report["attempts"], budget)
    detection = mfp_profiles.detect(pages, escl_model=(report["escl"] or {}).get("model") or "")
    report["detection"] = detection
    reachable = [a for a in report["attempts"] if a.get("reachable")]
    report["summary"] = {
        "reachable": bool(reachable),
        "reachable_on": [f"{a['scheme']}:{a['port']}" for a in reachable],
        "tls": next(({"verified": a["tls"].get("verified")} for a in report["attempts"]
                     if a.get("tls")), None),
        "status": detection["status"],
        "scan": bool(report["escl"]),
        "elapsed_ms": _ms(t0),
    }
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    _annotate(report["steps"])
    for a in report["attempts"]:
        _annotate(a.get("steps") or [])
    report = red.redact_obj(report)
    logger.info("mfp 連線測試 %s：可連線=%s 辨識=%s %s %s（%d ms）", host,
                report["summary"]["reachable_on"], detection["status"],
                detection.get("family") or "", detection.get("model") or "", report["summary"]["elapsed_ms"])
    return report


def _probe_escl(host: str, ip: str, attempts: list[dict], budget: "_Budget") -> Optional[dict]:
    """讀 eSCL 能力表（`GET /eSCL/ScannerCapabilities`，只讀）—— 有的話這台可以從本站掃描。

    依連線測試的順序找第一個連得上、有 eSCL 的協定；結果寫成那一組的一個步驟。"""
    from . import mfp_escl
    for att in attempts:
        if not att.get("reachable") or budget.left() <= 2:
            continue
        t0 = time.monotonic()
        try:
            caps = mfp_escl.fetch_capabilities(host, ip, att["scheme"], att["port"],
                                               timeout=min(10.0, budget.left()))
        except mfp_escl.ScanError as e:
            code = e.code if e.code in ("ESCL_NONE", "ESCL_BAD") else "ESCL_NONE"
            att.setdefault("steps", []).append({"step": "escl", "ok": False, "code": code, "ms": _ms(t0),
                                                "detail": {k: v for k, v in e.detail.items() if k != "addresses"}})
            continue
        except PolicyError:
            continue
        summ = mfp_escl.summarize(caps)
        att.setdefault("steps", []).append({"step": "escl", "ok": bool(summ["sources"]), "code": "ESCL_OK" if summ["sources"] else "ESCL_NONE",
                                            "ms": _ms(t0), "detail": {"model": summ["model"],
                                                                      "sources": ", ".join(sorted(summ["sources"]))}})
        if not summ["sources"]:
            continue
        return {"scheme": att["scheme"], "port": att["port"], "version": caps.get("version"),
                "model": summ["model"], "caps": caps, "sources": summ["sources"]}
    return None


def _annotate(steps: list[dict]) -> None:
    """每一步補上固定訊息（畫面用 `tr()` 翻成英日文；報告裡直接看得懂）。"""
    for st in steps:
        if st.get("code"):
            st["message"] = MESSAGES.get(st["code"], st["code"])
        if st.get("note"):
            st["note_message"] = MESSAGES.get(st["note"], st["note"])
        for a in (st.get("detail") or {}).get("addresses") or []:
            a["message"] = MESSAGES.get(a.get("code", ""), a.get("code", ""))


def _app_version() -> str:
    import sys
    m = sys.modules.get("app.main")
    return getattr(m, "VERSION", "") if m else ""
