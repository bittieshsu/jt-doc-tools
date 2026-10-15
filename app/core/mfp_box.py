"""事務機掃描取件：FUJIFILM 機內信箱（WebDAV）—— **推測實作，尚未在實機上驗證**。

使用者 2026-10-14：手上暫時沒有 FUJIFILM 實機，「用推測方式撰寫」；取件預設存成 PDF，也可以存原始格式。
依據是原廠取件工具（Network Scanner Utility 3，2.0.60）的靜態分析（內部文件 `docs-share/mfp-fetch/
codex-handoff-20261014/`）：它用 SOAP 管理介面加 WebDAV 取信箱文件。**這裡只用 WebDAV**（SOAP 的完整呼叫
格式沒有查出來，不猜）。

## 推測的部分（實機驗證時逐條核對；協定紀錄會記下每一步，方便對照）

1. 信箱路徑 `/dav/box-<編號>`（原廠程式裡的字串 `dav/box-%u`：編號不補零）。設備回的 href 若補了零
   （`box-001`），之後就照設備的寫法。
2. 列文件：`PROPFIND`、`Depth: 1`、`allprop`。子項目的 href 是 `<信箱>/<文件編號>`（資料夾）或
   `<信箱>/<文件編號>.<副檔名>`（整份檔案）。屬性名稱照原廠程式裡看得到的（`Pages`、`Compression`、
   `deleteAfterGet`…），命名空間限 `DAV:` 與 FUJIFILM / FUJI XEROX 的六個。
3. 認證：先不帶帳密送一次，看 `WWW-Authenticate` 的 realm：
   * `box-…` → 帳號 `box`、密碼是信箱密碼（原廠程式的一個分支）
   * `user-…`、`admin…` → 設備的使用者帳號與密碼
   * 其他 → 不猜，回「不支援的認證方式」，realm 記在協定紀錄裡
   支援 Digest 與 Basic。**帳密只送一次**：失敗就停，不換組合重試（避免帳號被鎖）。
4. 取件：文件本身是檔案（`5.tif`）就整份 GET；只有資料夾（`5/`）就再 PROPFIND 一層，取
   `<數字>-<數字>.tif|jpg`（略過 `thumbnail/`），依兩個數字排序 —— 這兩個數字的意思**還沒有實機確認**，
   每個檔案的原始名稱都留在取件紀錄裡。

## 安全邊界

* **只送 `PROPFIND` 與 `GET`**（`ALLOWED_METHODS`，請求層檢查）—— 不刪除、不修改設備上的任何東西。
  設備或信箱設定成「取件後刪除」時，刪除是設備自己做的：清單上看得到這個設定就先講，要使用者確認才取。
  取完再列一次清單，記下原件還在不在。
* 位址與連接埠沿用 `mfp_client` 的檢查與 DNS 釘選。設備回的 href 只收同一台、同一個信箱底下、
  **格式固定**的路徑（文件 `\\d+` 或 `\\d+.副檔名`、頁 `\\d+-\\d+.tif|jpg`）；送出的請求路徑由這些數字重新組成，
  設備回的字串不會原樣進到請求裡。不跟隨轉址（信箱路徑補斜線的那一次除外）。
* **帳密只在記憶體裡**（這次請求、或這件取件作業），不寫進資料庫、紀錄檔、協定紀錄。
* HTTPS 的憑證要通過驗證，或跟使用者核對過的指紋（SHA-256）一致，才會開始存取信箱；指紋不符就停（不降級）。
  只有 HTTP 的設備要使用者明確同意（帳密與文件會以未加密方式在內網傳送）。
* XML 用 defusedxml（外部實體、DTD 展開一律拒絕）；回應大小、文件數、頁數都有上限。
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import logging
import re
import secrets
import ssl
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urljoin, urlsplit

from . import mfp_client as mc
from . import mfp_output

logger = logging.getLogger("app.mfp_fetch")

USER_AGENT = "jt-doc-tools (mfp-fetch mailbox)"
BOX_MIN, BOX_MAX = 1, 999
#: 一次 PROPFIND 回應最多讀多少。
MAX_XML = 4 * 1024 * 1024
#: 一個信箱最多列幾份文件；一份文件最多幾個頁檔。
MAX_DOCS = 500
MAX_RESOURCES = 1000
#: 一次取件最多幾份文件、每份多大、全部多大。
MAX_DOCS_PER_JOB = 20
MAX_DOC_BYTES = 200 * 1024 * 1024
MAX_TOTAL_BYTES = 400 * 1024 * 1024
READ_TIMEOUT = 60.0
#: 一份文件最多花多久（含逐頁下載）。
DOC_TIMEOUT = 15 * 60.0
#: 協定紀錄最多幾筆。
MAX_TRACE = 80

NS_DAV = "DAV:"
#: 原廠程式裡看得到的命名空間（新品牌 FUJIFILM、舊品牌 FUJI XEROX）。只收這些 + `DAV:` 的屬性。
VENDOR_NS = frozenset({
    "http://www.fujifilm.com/fb/2021/04/ssm/storageService",
    "http://www.fujifilm.com/fb/2021/04/boxAccess",
    "http://www.fujifilm.com/fb/2021/03/ssm/management/mailbox",
    "http://www.fujixerox.co.jp/2003/12/ssm/storageService",
    "http://www.fujixerox.co.jp/2005/06/boxAccess",
    "http://www.fujixerox.co.jp/2003/12/ssm/management/mailbox",
})
_KNOWN_NS = VENDOR_NS | {NS_DAV}
ALLOWED_METHODS = frozenset({"PROPFIND", "GET"})
_PROPFIND_BODY = (b'<?xml version="1.0" encoding="utf-8"?>'
                  b'<D:propfind xmlns:D="DAV:"><D:allprop/></D:propfind>')

MESSAGES: dict[str, str] = {
    "BOX_INVALID": "信箱編號要是 1 到 999 的數字",
    "BOX_NOT_FOUND": "設備回應找不到這個信箱（編號不存在，或設備的 WebDAV 沒有啟用）",
    "BOX_FORBIDDEN": "設備拒絕存取這個信箱（這個帳號沒有權限）",
    "WEBDAV_UNAVAILABLE": "設備不接受 WebDAV 指令（WebDAV 沒有啟用，或這個連接埠不是 WebDAV）",
    "AUTH_REQUIRED_BOX": "這個信箱有設密碼，請輸入信箱密碼",
    "AUTH_REQUIRED_USER": "設備要求登入，請輸入設備的使用者帳號與密碼",
    "AUTH_FAILED": "認證失敗：帳號或密碼不對（為了避免帳號被鎖，沒有自動重試）",
    "AUTH_MODE_UNSUPPORTED": "設備要求的認證方式目前不支援（協定紀錄裡有設備回的認證資訊）",
    "TLS_UNTRUSTED": "設備的憑證沒有通過驗證（事務機多半是自簽憑證）。請核對下面的指紋，確認是這台設備再信任",
    "TLS_IDENTITY_FAILED": "設備的憑證跟你信任的不一樣，已停止，沒有存取信箱。設備換過憑證的話，請重新核對指紋",
    "TLS_FAILED": "跟設備建立 TLS 加密連線失敗",
    "PLAIN_HTTP_NEEDS_CONSENT": "這台事務機用 HTTP 連線：帳密與文件會以未加密方式在內網傳送，要先同意才會連線",
    "DAV_CONNECTION_FAILED": "連不上事務機",
    "DAV_TIMEOUT": "等事務機回應逾時",
    "DAV_BAD_RESPONSE": "設備的回應格式不符預期（協定紀錄裡有細節）",
    "DAV_REDIRECT_REFUSED": "設備要求轉址到信箱以外的位置，沒有跟隨",
    "DAV_UNEXPECTED_STATUS": "設備回了預期之外的狀態碼",
    "DAV_TOO_MANY_DOCS": "信箱裡的文件超過一次能列出的上限",
    "DOC_NOT_FOUND": "信箱裡已經沒有這份文件（可能已被取走或刪除）",
    "DOC_NO_RESOURCE": "找不到這份文件可以下載的檔案",
    "DOC_TOO_LARGE": "文件超過大小上限",
    "DOC_TIMEOUT": "下載這份文件超過時間上限",
    "DOC_BAD_FILE": "設備回的不是 TIFF、JPEG 或 PDF（可能是登入頁或錯誤訊息）",
    "DOC_ENCODING_UNSUPPORTED": "設備用了不支援的傳輸壓縮方式",
    "DOC_INCOMPLETE": "檔案沒有完整傳完",
    "DOC_INVALID": "取回的檔案打不開（可能毀損，或用了不支援的壓縮方式）",
    "DELETE_AFTER_GET_UNCONFIRMED": "這個信箱設定成「取件後刪除」：取件後設備可能會刪掉原件，請先確認",
    "NO_DOCUMENTS_SELECTED": "請選擇要取件的文件",
    "TOO_MANY_DOCUMENTS": "一次最多取 20 份文件",
    "CANCELLED": "已取消",
}

#: 遇到這些錯誤，整件取件就停（後面的文件也一定會遇到同樣的問題）。
FATAL = frozenset({"AUTH_FAILED", "AUTH_MODE_UNSUPPORTED", "AUTH_REQUIRED_BOX", "AUTH_REQUIRED_USER",
                   "TLS_UNTRUSTED", "TLS_IDENTITY_FAILED", "TLS_FAILED", "PLAIN_HTTP_NEEDS_CONSENT",
                   "DAV_CONNECTION_FAILED", "CANCELLED", "BOX_NOT_FOUND", "BOX_FORBIDDEN",
                   "WEBDAV_UNAVAILABLE"})


class BoxError(Exception):
    def __init__(self, code: str, detail: Optional[dict] = None):
        super().__init__(code)
        self.code = code
        self.message = MESSAGES.get(code, code)
        self.detail = detail or {}


class Creds:
    """使用者這次輸入的帳密。**只在記憶體裡**；`repr` 不印內容（避免不小心寫進紀錄檔）。"""

    __slots__ = ("box_password", "user", "password")

    def __init__(self, box_password: str = "", user: str = "", password: str = ""):
        self.box_password = str(box_password or "")[:128]
        self.user = str(user or "")[:128]
        self.password = str(password or "")[:128]

    def __repr__(self) -> str:
        return "<Creds>"


def parse_box(v: Any) -> int:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise BoxError("BOX_INVALID") from None
    if not BOX_MIN <= n <= BOX_MAX:
        raise BoxError("BOX_INVALID")
    return n


# --------------------------------------------------------------------------- 認證

_TOKEN = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_RE_SEP = re.compile(r"[\s,]*")
_RE_PARAM = re.compile(rf"({_TOKEN})\s*=\s*(\"(?:[^\"\\]|\\.)*\"|{_TOKEN})")
_RE_SCHEME = re.compile(rf"({_TOKEN})(?=[\s,]|$)")


def parse_challenges(values: list[str]) -> list[dict]:
    """`WWW-Authenticate`（可能有好幾個標頭、一個標頭裡好幾個方案）→ `[{"scheme", "params"}]`。"""
    out: list[dict] = []
    for raw in values[:8]:
        s = (raw or "")[:4096]
        pos, cur = 0, None
        while pos < len(s):
            pos = _RE_SEP.match(s, pos).end()
            if pos >= len(s):
                break
            m = _RE_PARAM.match(s, pos)
            if m and cur is not None:
                val = m.group(2)
                if val.startswith('"'):
                    val = re.sub(r"\\(.)", r"\1", val[1:-1])
                cur["params"][m.group(1).lower()] = val[:512]
                pos = m.end()
                continue
            m = _RE_SCHEME.match(s, pos)
            if not m:
                break
            cur = {"scheme": m.group(1).lower(), "params": {}}
            out.append(cur)
            pos = m.end()
    return out[:8]


def realm_kind(realm: str) -> str:
    r = (realm or "").strip().lower()
    if r == "box" or r.startswith("box-"):
        return "box"
    if r.startswith("user-") or r.startswith("admin"):
        return "user"
    return "unknown"


_DIGEST_ALGS = {"md5": "md5", "md5-sess": "md5", "sha-256": "sha256", "sha-256-sess": "sha256"}


def _pick(challenges: list[dict]) -> Optional[dict]:
    """Digest（支援的演算法與 qop）優先，其次 Basic；都沒有回 None。"""
    for c in challenges:
        if c["scheme"] == "digest":
            alg = c["params"].get("algorithm", "MD5").lower()
            qops = [q.strip().lower() for q in c["params"].get("qop", "").split(",") if q.strip()]
            if alg in _DIGEST_ALGS and (not qops or "auth" in qops) and c["params"].get("nonce"):
                return c
    for c in challenges:
        if c["scheme"] == "basic":
            return c
    return None


class _Auth:
    """選定的認證方式與帳密（記憶體裡）。`header()` 每次呼叫算一個新的 Authorization 值。"""

    def __init__(self, challenge: dict, username: str, password: str):
        self.scheme = challenge["scheme"]
        self.params = dict(challenge["params"])
        self.username, self.password = username, password
        self.nc = 0

    def __repr__(self) -> str:
        return f"<_Auth {self.scheme}>"

    def summary(self) -> dict:
        return {"scheme": self.scheme, "realm": self.params.get("realm", "")[:80],
                "realm_kind": realm_kind(self.params.get("realm", "")),
                "algorithm": self.params.get("algorithm") if self.scheme == "digest" else None,
                "non_ascii": not (self.username + self.password).isascii()}

    def header(self, method: str, uri: str) -> str:
        if self.scheme == "basic":
            cs = self.params.get("charset", "").lower()
            raw = f"{self.username}:{self.password}"
            enc = "utf-8" if cs == "utf-8" or not raw.isascii() else "ascii"
            return "Basic " + base64.b64encode(raw.encode(enc)).decode("ascii")
        p = self.params
        alg = p.get("algorithm", "MD5")
        hname = _DIGEST_ALGS[alg.lower()]

        def h(s: str) -> str:
            return hashlib.new(hname, s.encode("utf-8"), usedforsecurity=False).hexdigest()

        realm, nonce = p.get("realm", ""), p.get("nonce", "")
        self.nc += 1
        nc = f"{self.nc:08x}"
        cnonce = secrets.token_hex(8)
        ha1 = h(f"{self.username}:{realm}:{self.password}")
        if alg.lower().endswith("-sess"):
            ha1 = h(f"{ha1}:{nonce}:{cnonce}")
        ha2 = h(f"{method}:{uri}")
        qops = [q.strip().lower() for q in p.get("qop", "").split(",") if q.strip()]

        def q(v: str) -> str:
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'

        parts = []
        if self.username.isascii():
            parts.append(f"username={q(self.username)}")
        else:
            from urllib.parse import quote
            parts.append("username*=UTF-8''" + quote(self.username, safe=""))
        parts += [f"realm={q(realm)}", f"nonce={q(nonce)}", f"uri={q(uri)}", f"algorithm={alg}"]
        if "auth" in qops:
            resp = h(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
            parts += [f"response={q(resp)}", "qop=auth", f"nc={nc}", f"cnonce={q(cnonce)}"]
        else:
            parts.append(f"response={q(h(f'{ha1}:{nonce}:{ha2}'))}")
        if p.get("opaque"):
            parts.append(f"opaque={q(p['opaque'])}")
        return "Digest " + ", ".join(parts)


def choose_auth(challenges: list[dict], creds: Creds) -> _Auth:
    """依設備的要求決定送哪一組帳密。**不猜**：realm 認不得、方案不支援就停。"""
    c = _pick(challenges)
    if c is None:
        raise BoxError("AUTH_MODE_UNSUPPORTED", {"challenges": _challenge_summary(challenges)})
    kind = realm_kind(c["params"].get("realm", ""))
    if kind == "box":
        if not creds.box_password:
            raise BoxError("AUTH_REQUIRED_BOX", {"realm": c["params"].get("realm", "")[:80]})
        return _Auth(c, "box", creds.box_password)
    if kind == "user":
        if not creds.user or not creds.password:
            raise BoxError("AUTH_REQUIRED_USER", {"realm": c["params"].get("realm", "")[:80]})
        return _Auth(c, creds.user, creds.password)
    raise BoxError("AUTH_MODE_UNSUPPORTED", {"challenges": _challenge_summary(challenges)})


def _challenge_summary(challenges: list[dict]) -> list[dict]:
    """協定紀錄用：方案、realm、演算法、qop（nonce 這類一次性的值不記）。"""
    return [{"scheme": c["scheme"], "realm": c["params"].get("realm", "")[:80],
             "algorithm": c["params"].get("algorithm"), "qop": c["params"].get("qop")} for c in challenges]


# --------------------------------------------------------------------------- 連線

class _Dav:
    """一台設備的 WebDAV 連線（位址已檢查、已解析）。每個請求開一條新連線，讀完自己關。"""

    def __init__(self, host: str, ip: str, scheme: str, port: int, *, trust_sha256: Optional[str],
                 allow_http: bool, trace: list):
        self.host, self.ip, self.scheme, self.port = host, ip, scheme, int(port)
        self.host_hdr = mc._host_header(host, scheme, self.port)
        self.sni = None if mc._is_ip(host) else host
        self.trust = (trust_sha256 or "").replace(":", "").strip().lower() or None
        self.allow_http = bool(allow_http)
        self.trace = trace
        self.tls: Optional[dict] = None
        self.auth: Optional[_Auth] = None

    # ---- 連線前的條件：TLS 身分、HTTP 同意
    def _open(self, timeout: float):
        if self.scheme != "https" and not self.allow_http:
            raise BoxError("PLAIN_HTTP_NEEDS_CONSENT")
        try:
            sock = mc._connect(self.ip, self.port, min(mc.CONNECT_TIMEOUT, timeout))
        except OSError as e:
            raise BoxError("DAV_CONNECTION_FAILED", {"error": e.__class__.__name__}) from None
        if self.scheme != "https":
            return sock
        if self.tls is None or self.tls.get("verified"):
            try:
                s = mc._verified_context().wrap_socket(sock, server_hostname=self.sni or self.host)
                self.tls = {"verified": True}
                return s
            except ssl.SSLCertVerificationError:
                sock.close()
            except (ssl.SSLError, OSError) as e:
                sock.close()
                raise BoxError("TLS_FAILED", {"error": e.__class__.__name__}) from None
            try:
                sock = mc._connect(self.ip, self.port, min(mc.CONNECT_TIMEOUT, timeout))
            except OSError as e:
                raise BoxError("DAV_CONNECTION_FAILED", {"error": e.__class__.__name__}) from None
        try:
            s = mc._unverified_context().wrap_socket(sock, server_hostname=self.sni)
        except (ssl.SSLError, OSError) as e:
            sock.close()
            raise BoxError("TLS_FAILED", {"error": e.__class__.__name__}) from None
        der = s.getpeercert(binary_form=True)
        fp = hashlib.sha256(der or b"").hexdigest()
        if self.trust and fp == self.trust:
            self.tls = {"verified": False, "pinned": True, "sha256": fp}
            return s
        s.close()
        info = mc.cert_info(der)
        if self.trust:
            raise BoxError("TLS_IDENTITY_FAILED", {"certificate": info, "trusted_sha256": self.trust})
        raise BoxError("TLS_UNTRUSTED", {"certificate": info})

    def _target_ok(self, path: str) -> bool:
        """送出去的請求路徑只能是固定的幾種形狀（子類別換成自己的，例如 Canon Advanced Box）。"""
        return bool(_SAFE_TARGET.match(path))

    def request(self, method: str, path: str, *, depth: Optional[str] = None, to_file: Optional[Path] = None,
                max_bytes: int = MAX_XML, timeout: float = READ_TIMEOUT, auth: bool = True) -> dict:
        """送一個 PROPFIND / GET。`to_file` 有給就把 200 的內容串流寫進檔案（超過 `max_bytes` 丟 `DOC_TOO_LARGE`）。"""
        if method not in ALLOWED_METHODS:
            raise ValueError(f"method not allowed: {method}")
        if not self._target_ok(path):
            raise ValueError("unsafe request target")
        t0 = time.monotonic()
        # 協定紀錄不記查詢字串：設備給的下載網址可能帶一次性的權杖（RICOH getDocContentUrl）
        entry: dict[str, Any] = {"method": method, "path": path.split("?", 1)[0]}
        if depth is not None:
            entry["depth"] = depth
        sock = None
        try:
            sock = self._open(timeout)
            sock.settimeout(timeout)
            head = (f"{method} {path} HTTP/1.1\r\nHost: {self.host_hdr}\r\nUser-Agent: {USER_AGENT}\r\n"
                    "Accept: */*\r\nAccept-Encoding: identity\r\n")
            body = b""
            if method == "PROPFIND":
                body = _PROPFIND_BODY
                head += (f"Depth: {depth or '1'}\r\nContent-Type: text/xml; charset=\"utf-8\"\r\n"
                         f"Content-Length: {len(body)}\r\n")
            if auth and self.auth is not None:
                head += f"Authorization: {self.auth.header(method, path)}\r\n"
                entry["auth"] = self.auth.scheme
            sock.sendall((head + "\r\n").encode("utf-8") + body)
            resp = http.client.HTTPResponse(sock, method=method)
            resp.begin()
            hdr_list = resp.getheaders()
            headers = {k.lower(): v for k, v in hdr_list}
            out: dict[str, Any] = {"status": resp.status, "headers": headers,
                                   "challenges": parse_challenges(
                                       [v for k, v in hdr_list if k.lower() == "www-authenticate"])}
            entry.update(status=resp.status, content_type=headers.get("content-type", "")[:80])
            if out["challenges"]:
                entry["challenges"] = _challenge_summary(out["challenges"])
            if headers.get("location"):
                entry["location"] = _location_path(headers["location"])
            enc = headers.get("content-encoding", "identity").strip().lower()
            if to_file is not None and resp.status == 200:
                if enc not in ("", "identity"):
                    raise BoxError("DOC_ENCODING_UNSUPPORTED", {"encoding": enc[:20]})
                n = 0
                with open(to_file, "wb") as f:
                    while True:
                        try:
                            chunk = resp.read(256 * 1024)
                        except http.client.IncompleteRead:
                            raise BoxError("DOC_INCOMPLETE") from None
                        if not chunk:
                            break
                        n += len(chunk)
                        if n > max_bytes:
                            raise BoxError("DOC_TOO_LARGE", {"limit_mb": max_bytes // 1024 // 1024})
                        f.write(chunk)
                clen = headers.get("content-length")
                if clen and clen.isdigit() and int(clen) != n:
                    raise BoxError("DOC_INCOMPLETE", {"expected": int(clen), "got": n})
                out["bytes"] = entry["bytes"] = n
                return out
            cap = max_bytes if to_file is None else 64 * 1024
            try:
                data = resp.read(cap + 1)
            except http.client.IncompleteRead as e:
                data = bytes(e.partial or b"")
            if len(data) > cap:
                if to_file is None and resp.status == 207:
                    raise BoxError("DAV_BAD_RESPONSE", {"reason": "too large", "limit_kb": cap // 1024})
                data = data[:cap]
            out["body"] = data
            entry["bytes"] = len(data)
            return out
        except BoxError as e:
            entry.setdefault("error", e.code)
            raise
        except (OSError, http.client.HTTPException) as e:
            code = "DAV_TIMEOUT" if isinstance(e, TimeoutError) else "DAV_CONNECTION_FAILED"
            entry["error"] = code
            raise BoxError(code, {"error": e.__class__.__name__, "path": path.split("?", 1)[0]}) from None
        finally:
            if sock is not None:
                sock.close()
            entry["ms"] = int((time.monotonic() - t0) * 1000)
            if len(self.trace) < MAX_TRACE:
                self.trace.append(entry)


#: 送出去的請求路徑：只會是我們自己用數字組出來的這幾種。
_SAFE_TARGET = re.compile(
    r"^/dav/box-\d{1,4}/?(?:\d{1,9}(?:\.[A-Za-z]{3,4})?/?(?:\d{1,6}-\d{1,6}\.(?i:tiff?|jpe?g))?)?$")


def _location_path(loc: str) -> str:
    try:
        return urlsplit(loc.strip()).path[:200]
    except ValueError:
        return "?"


# --------------------------------------------------------------------------- href 與 PROPFIND 回應

_DOC_SEG = re.compile(r"^(\d{1,9})(?:\.([A-Za-z]{3,4}))?$")
_PAGE_SEG = re.compile(r"^(\d{1,6})-(\d{1,6})\.(tif|tiff|jpg|jpeg)$", re.I)
_DOC_EXTS = {"tif": "tif", "tiff": "tif", "jpg": "jpg", "jpeg": "jpg", "pdf": "pdf"}


class _Box:
    """一個信箱的路徑：設備回的寫法若補了零（`box-001`），之後照設備的寫法。"""

    def __init__(self, number: int):
        self.number = number
        self.seg = f"box-{number}"
        self.slash = False                    # 設備要不要信箱路徑後面的斜線（轉址時照設備的）
        self._re = re.compile(rf"^/dav/(box-0*{number})(?:/(.*))?$")

    @property
    def path(self) -> str:
        return f"/dav/{self.seg}"

    @property
    def base(self) -> str:
        """列信箱時用的路徑。"""
        return self.path + ("/" if self.slash else "")

    def rel(self, href: str, dev: _Dav, request_path: str) -> Optional[list[str]]:
        """設備回的 href → 信箱底下的路徑片段（`[]` 是信箱本身）；不在這個信箱底下、或寫法可疑 → None。"""
        h = (href or "").strip()
        if not h or len(h) > 1024 or "\\" in h:
            return None
        try:
            if any(unquote(x) in (".", "..") for x in urlsplit(h).path.split("/")):
                return None                   # `..` 合併網址時會被吃掉、看起來像信箱裡的文件 —— 合併之前就擋
            u = urlsplit(urljoin(f"{dev.scheme}://{dev.host_hdr}{request_path}", h))
        except ValueError:
            return None
        if u.username or u.password or u.query or u.fragment:
            return None
        if (u.scheme or dev.scheme) != dev.scheme:
            return None
        host = (u.hostname or "").lower()
        if host and host not in (dev.host.lower(), dev.ip):
            return None
        try:
            port = u.port or (443 if dev.scheme == "https" else 80)
        except ValueError:
            return None
        if port != dev.port:
            return None
        path = unquote(u.path)
        if "%" in path or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in path):
            return None                       # 解碼一次之後還有 %：二次編碼，不收
        m = self._re.match(path)
        if not m:
            return None
        parts = [p for p in (m.group(2) or "").split("/") if p]
        if any(p in (".", "..") for p in parts):
            return None
        if m.group(1) != self.seg:
            self.seg = m.group(1)             # 設備的寫法（例如補零）
        return parts


def parse_multistatus(data: bytes) -> tuple[list[dict], list[str]]:
    """PROPFIND 的 207 回應 → `[{"href", "props", "collection"}]`，以及看到的屬性名稱（協定紀錄用）。"""
    from defusedxml.ElementTree import fromstring
    from defusedxml.common import DefusedXmlException
    try:
        root = fromstring(data or b"")
    except (DefusedXmlException, SyntaxError, ValueError):
        raise BoxError("DAV_BAD_RESPONSE", {"reason": "xml"}) from None
    if root.tag != "{DAV:}multistatus":
        raise BoxError("DAV_BAD_RESPONSE", {"reason": "not multistatus", "root": str(root.tag)[:80]})
    out: list[dict] = []
    seen: set[str] = set()
    for resp in root.findall("{DAV:}response"):
        if len(out) > MAX_DOCS + MAX_RESOURCES:
            raise BoxError("DAV_TOO_MANY_DOCS", {"limit": MAX_DOCS})
        href = (resp.findtext("{DAV:}href") or "").strip()
        props: dict[str, str] = {}
        collection = False
        for ps in resp.findall("{DAV:}propstat"):
            status = ps.findtext("{DAV:}status") or ""
            ok = bool(re.search(r"\s200\s", " " + status + " "))
            prop = ps.find("{DAV:}prop")
            if prop is None:
                continue
            for child in list(prop)[:200]:
                tag = str(child.tag)
                if len(seen) < 120:
                    seen.add(tag)
                if not ok or not tag.startswith("{"):
                    continue
                ns, _, local = tag[1:].partition("}")
                if ns not in _KNOWN_NS:
                    continue
                if ns == NS_DAV and local == "resourcetype":
                    collection = child.find("{DAV:}collection") is not None
                    continue
                if local in props and ns != NS_DAV:
                    continue                  # 同名時 DAV: 的優先
                props[local] = (child.text or "").strip()[:200]
        out.append({"href": href, "props": props, "collection": collection})
    return out, sorted(seen)


def _int(v: Optional[str]) -> Optional[int]:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def epoch(v: Optional[str]) -> Optional[float]:
    """WebDAV 的時間（`getlastmodified` 是 HTTP 日期、`creationdate` 是 ISO 8601）→ epoch 秒；看不懂回 None。

    畫面由瀏覽器依使用者那邊的時區顯示 —— 伺服器的時區可能不一樣（正式機原本就是 UTC）。"""
    s = (v or "").strip()
    if not s:
        return None
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts = dt.timestamp()
    return ts if 0 < ts < 4102444800 else None      # 1970～2100 年以外的當成看不懂


def _truthy(v: Optional[str]) -> Optional[bool]:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("true", "1", "on", "yes", "enable", "enabled"):
        return True
    if s in ("false", "0", "off", "no", "disable", "disabled", ""):
        return False
    return None


def _documents(entries: list[dict], box: _Box, dev: _Dav, req_path: str) -> tuple[dict, dict, int]:
    """PROPFIND Depth 1 的回應 → （文件 {id: 文件}、信箱本身的屬性、認不得的項目數）。"""
    docs: dict[str, dict] = {}
    box_props: dict[str, str] = {}
    ignored = 0
    for e in entries:
        parts = box.rel(e["href"], dev, req_path)
        if parts is None:
            ignored += 1
            continue
        if not parts:
            box_props = e["props"]
            continue
        m = _DOC_SEG.match(parts[0]) if len(parts) == 1 else None
        if not m:
            ignored += 1
            continue
        doc_id, ext = m.group(1), m.group(2) or ""
        if ext and ext.lower() not in _DOC_EXTS:
            ignored += 1
            continue
        d = docs.setdefault(doc_id, {"id": doc_id, "file": None, "folder": None, "props": {}})
        if e["collection"] or not ext and e["props"].get("getcontenttype", "") == "":
            d["folder"] = doc_id
        else:
            d["file"] = f"{doc_id}.{ext}" if ext else doc_id
        for k, v in e["props"].items():
            d["props"].setdefault(k, v)
        if len(docs) > MAX_DOCS:
            raise BoxError("DAV_TOO_MANY_DOCS", {"limit": MAX_DOCS})
    return docs, box_props, ignored


def _public_doc(d: dict) -> dict:
    p = d["props"]
    fmt = ""
    if d["file"] and "." in d["file"]:
        fmt = d["file"].rsplit(".", 1)[1].upper()
    ctype = p.get("getcontenttype", "")
    return {
        "id": d["id"],
        "name": (p.get("displayname") or "")[:120],
        "pages": _int(p.get("Pages") or p.get("pages")),
        "size": _int(p.get("getcontentlength")),
        "modified": epoch(p.get("getlastmodified") or p.get("creationdate")),
        "format": fmt or (ctype.split("/")[-1].upper()[:12] if ctype else ""),
        "compression": (p.get("Compression") or "")[:30],
        "color": (p.get("ColorMode") or "")[:30],
        "resolution": (p.get("Resolution") or "")[:30],
        "how": "whole" if d["file"] else ("pages" if d["folder"] else "unknown"),
        "delete_after_get": _truthy(p.get("deleteAfterGet")),
    }


# --------------------------------------------------------------------------- 對外：開信箱、列文件、取件

class Session:
    """開好的信箱（已通過認證）。帳密只在 `dav.auth` 裡，作業結束就丟掉。"""

    def __init__(self, dav: _Dav, box: _Box):
        self.dav, self.box = dav, box
        self.docs: dict[str, dict] = {}
        self.box_props: dict[str, str] = {}
        self.qnames: list[str] = []
        self.ignored = 0

    def via(self) -> dict:
        return {"protocol": "WebDAV", "scheme": self.dav.scheme, "port": self.dav.port}

    def tls(self) -> Optional[dict]:
        return dict(self.dav.tls) if self.dav.tls else None

    def list(self) -> None:
        r = self.dav.request("PROPFIND", self.box.base, depth="1")
        if r["status"] != 207:
            raise _status_error(r)
        entries, self.qnames = parse_multistatus(r.get("body") or b"")
        self.docs, self.box_props, self.ignored = _documents(entries, self.box, self.dav, self.box.base)

    def public(self) -> dict:
        docs = sorted(self.docs.values(), key=lambda d: int(d["id"]))
        return {"box": self.box.number, "documents": [_public_doc(d) for d in docs],
                "delete_after_get": _truthy(self.box_props.get("deleteAfterGet")),
                "box_name": (self.box_props.get("displayname") or "")[:80],
                "ignored": self.ignored}


def _status_error(r: dict) -> BoxError:
    st = r["status"]
    if st == 401:
        return BoxError("AUTH_FAILED", {"status": st})
    if st == 403:
        return BoxError("BOX_FORBIDDEN", {"status": st})
    if st == 404:
        return BoxError("BOX_NOT_FOUND", {"status": st})
    if st in (405, 501):
        return BoxError("WEBDAV_UNAVAILABLE", {"status": st})
    return BoxError("DAV_UNEXPECTED_STATUS", {"status": st})


def open_box(host: str, ip: str, scheme: str, port: int, *, box: int, creds: Creds,
             trust_sha256: Optional[str] = None, allow_http: bool = False,
             trace: Optional[list] = None) -> Session:
    """開信箱並列出文件。失敗丟 `BoxError`（`detail` 裡有原因；協定紀錄在傳進來的 `trace`）。

    1. 不帶帳密 PROPFIND（Depth 1）`/dav/box-<編號>`（照原廠程式的寫法，不加斜線）。207 → 不用認證，完成。
    2. 301/302/307/308 轉到**同一個信箱**（補斜線、補零）→ 跟一次。其他轉址不跟。
    3. 401 → 依 realm 決定送哪一組帳密，**只送一次**；再 401 就是認證失敗（Digest 的 `stale=true`
       是設備換了 nonce，不是密碼錯，可以用新的 nonce 再送一次 —— 這不算換帳密重試）。
    """
    _check_target(ip, scheme, port)
    trace = trace if trace is not None else []
    dav = _Dav(host, ip, scheme, port, trust_sha256=trust_sha256, allow_http=allow_http, trace=trace)
    b = _Box(box)
    sess = Session(dav, b)
    r = dav.request("PROPFIND", b.base, depth="1", auth=False)
    if r["status"] in _REDIRECTS:
        loc = r["headers"].get("location", "")
        if b.rel(loc, dav, b.base) != []:
            raise BoxError("DAV_REDIRECT_REFUSED", {"to": _location_path(loc)})
        b.slash = _location_path(loc).endswith("/")
        r = dav.request("PROPFIND", b.base, depth="1", auth=False)
        if r["status"] in _REDIRECTS:
            raise BoxError("DAV_UNEXPECTED_STATUS", {"status": r["status"], "reason": "redirect loop"})
    path = b.base
    if r["status"] == 401:
        dav.auth = choose_auth(r["challenges"], creds)
        r = dav.request("PROPFIND", path, depth="1")
        if r["status"] == 401 and dav.auth.scheme == "digest":
            fresh = [c for c in r["challenges"] if c["scheme"] == "digest"
                     and c["params"].get("stale", "").lower() == "true"]
            if fresh:
                dav.auth = _Auth(fresh[0], dav.auth.username, dav.auth.password)
                r = dav.request("PROPFIND", path, depth="1")
    if r["status"] != 207:
        raise _status_error(r)
    entries, sess.qnames = parse_multistatus(r.get("body") or b"")
    sess.docs, sess.box_props, sess.ignored = _documents(entries, b, dav, path)
    return sess


_REDIRECTS = (301, 302, 307, 308)


def _check_target(ip: str, scheme: str, port: int) -> None:
    pairs = mc.parse_port_scheme(port, scheme)
    if pairs != [(scheme, int(port))]:
        raise mc.PolicyError("PORT_NOT_ALLOWED", {"input": port})
    code = mc.check_address(ip)
    if code:
        raise mc.PolicyError(code, {"ip": ip})


def _resources(sess: Session, doc: dict) -> list[dict]:
    """這份文件要下載的檔案（依序）：整份檔案一個；否則資料夾底下的頁檔。"""
    if doc["file"]:
        return [{"seg": doc["file"], "path": f"{sess.box.path}/{doc['file']}", "name": doc["file"]}]
    if not doc["folder"]:
        raise BoxError("DOC_NO_RESOURCE")
    folder = f"{sess.box.path}/{doc['folder']}/"
    r = sess.dav.request("PROPFIND", folder, depth="1")
    if r["status"] in _REDIRECTS:
        raise BoxError("DAV_REDIRECT_REFUSED", {"to": _location_path(r["headers"].get("location", ""))})
    if r["status"] == 404:
        raise BoxError("DOC_NOT_FOUND")
    if r["status"] != 207:
        raise _status_error(r)
    entries, _q = parse_multistatus(r.get("body") or b"")
    pages: dict[tuple[int, int], dict] = {}
    for e in entries:
        parts = sess.box.rel(e["href"], sess.dav, folder)
        if not parts or len(parts) != 2 or parts[0] != doc["folder"]:
            continue                      # 資料夾本身、縮圖（thumbnail/…）、其他
        m = _PAGE_SEG.match(parts[1])
        if not m or e["collection"]:
            continue
        key = (int(m.group(1)), int(m.group(2)))
        ext = "tif" if m.group(3).lower().startswith("tif") else "jpg"
        # 同一個位置同時有 TIFF 與 JPEG：取 TIFF（比較接近原稿）
        if key in pages and pages[key]["ext"] == "tif":
            continue
        pages[key] = {"ext": ext, "name": parts[1]}
        if len(pages) > MAX_RESOURCES:
            raise BoxError("DOC_TOO_LARGE", {"reason": "too many pages"})
    if not pages:
        raise BoxError("DOC_NO_RESOURCE")
    return [{"seg": p["name"], "path": f"{folder}{p['name']}", "name": p["name"]}
            for _k, p in sorted(pages.items())]


def download(sess: Session, doc_id: str, workdir: Path, *, budget_bytes: int,
             is_cancelled: Callable[[], bool], on_progress: Callable[[dict], None]) -> list[dict]:
    """下載一份文件的所有檔案到 workdir，回傳 `[{"path", "kind", "bytes", "sha256", "source"}]`（依序）。"""
    doc = sess.docs.get(str(doc_id))
    if not doc:
        raise BoxError("DOC_NOT_FOUND")
    workdir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + DOC_TIMEOUT
    files: list[dict] = []
    try:
        for i, res in enumerate(_resources(sess, doc), 1):
            if is_cancelled():
                raise BoxError("CANCELLED")
            if time.monotonic() > deadline:
                raise BoxError("DOC_TIMEOUT")
            left = min(MAX_DOC_BYTES - sum(f["bytes"] for f in files), budget_bytes)
            if left <= 0:
                raise BoxError("DOC_TOO_LARGE", {"limit_mb": MAX_DOC_BYTES // 1024 // 1024})
            part = workdir / f"part{i:04d}.bin"
            r = sess.dav.request("GET", res["path"], to_file=part, max_bytes=left)
            if r["status"] != 200:
                part.unlink(missing_ok=True)
                if r["status"] == 404:
                    raise BoxError("DOC_NOT_FOUND")
                raise _status_error(r)
            kind = mfp_output.sniff(part)
            if kind is None:
                part.unlink(missing_ok=True)
                raise BoxError("DOC_BAD_FILE", {"content_type": r["headers"].get("content-type", "")[:60]})
            final = part.with_suffix(mfp_output.EXT[kind])
            part.rename(final)
            try:
                info = mfp_output.inspect(final, kind)
            except mfp_output.OutputError:
                raise BoxError("DOC_INVALID", {"kind": kind, "file": res["name"]}) from None
            files.append({"path": final, "kind": kind, "bytes": r.get("bytes", 0), "pages": info["pages"],
                          "dpi": info["dpi"], "sha256": _sha256(final), "source": res["name"]})
            on_progress({"files": len(files)})
    except BaseException:
        for f in files:
            Path(f["path"]).unlink(missing_ok=True)
        for p in workdir.glob("part*.bin"):
            p.unlink(missing_ok=True)
        raise
    return files


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def doc_resolution(doc: dict) -> int:
    """文件屬性裡的解析度（`200`、`200x200`、`200dpi`）→ dpi；看不懂回 0。"""
    m = re.match(r"\s*(\d{2,4})", str(doc["props"].get("Resolution") or ""))
    n = int(m.group(1)) if m else 0
    return n if 50 <= n <= 2400 else 0
