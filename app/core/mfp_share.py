"""事務機掃描取件：檔案分享取件（**實驗功能**，尚未在實機上驗證）。

使用者 2026-10-15：五品牌研究（Codex 交付包，內部文件 `docs-share/mfp-fetch/five-brand-handoff-20261014/`）裡
「1 2 都做 標實驗」—— ① Canon Advanced Box（WebDAV）② SMB 取件。依據都是**原廠手冊公開寫明的路徑**，不是猜的：

| 路線 | 廠牌 | 傳輸 | 位置 | 帳密 |
|---|---|---|---|---|
| `canon_webdav` | Canon（iR-ADV DX 等） | WebDAV，`/share`（共享空間）、`/users`（個人空間） | 設備上的檔案原樣 | Advanced Box 的使用者帳密（認證管理＋TLS 時是 Basic） |
| `canon_smb` | Canon | SMB，分享 `share` / `users` | 同上 | 同上（NTLMv2） |
| `konica_smb` | Konica Minolta（bizhub i 系列） | SMB，公用信箱的分享（名稱是九位信箱編號＋信箱名稱） | 公用信箱的檔案副本 | ID＝信箱編號（照使用者輸入送一次，不自己補零輪試）、密碼＝信箱密碼 |
| `sharp_smb` | SHARP（BP 系列） | SMB，NAS 的 `public` / `shared` / `faxreceive`（照設備實際列出的） | `public` 是「給電腦看的 PDF 副本」 | 設備的使用者帳密 |

v1.16.95 加 KYOCERA / RICOH 的直接取件（SOAP，`mfp_soap`）：依據是分析者從原廠工具整理的方法，
**不是原廠公開的介面**，也沒有實機樣本；一樣標實驗功能。

| `kyocera_soap` / `kyocera_soap_http` | KYOCERA（TASKalfa / ECOSYS） | KM-WSDL，HTTPS 9091 / HTTP 9090 | 機內文件匣（匯出 TIFF） | 設備的使用者帳密；文件匣密碼另填 |
| `ricoh_soap` | RICOH（IM / MP） | `/DH/repository`，連線測試連得上的那組協定與連接埠 | 文件伺服器的文件（原始格式） | 登入方式（自動 / 不需登入 / 帳號密碼 / User Code）；帳號密碼走票證或 `startSession`；文件密碼另填 |

**共享區一律標「共享」**（Canon `share`、Konica 公用信箱、SHARP 的分享）：那是設備本來就讓多人看的地方，不當成個人文件。

## 安全邊界（跟機內信箱取件同一套）

* 位址：內網、DNS 釘選、不可以是本機（`mfp_client`）。WebDAV 只用連線測試連得上的那組協定與連接埠；SMB 只連 445。
* **只讀**：WebDAV 只送 PROPFIND / GET（沿用 `mfp_box._Dav` 的請求層檢查）；SMB 只用列分享、列目錄、讀檔（`_Smb` 不提供其他操作）。
  不刪除、不改名、不寫入設備上的任何東西。
* 路徑由**我們自己**用驗證過的名稱組成（每一段不能是 `.` `..`、不能有斜線、反斜線、控制字元）；設備回的 href / 檔名只收在所選位置底下的。
* 帳密只在這次請求或那件取件作業的記憶體裡；認證失敗就停，不換組合重試。
* WebDAV：HTTPS 憑證要通過驗證或跟使用者核對過的指紋一致；HTTP 要使用者同意。
* SMB：只用 NTLMv2；**伺服器只肯用 SMB1 就在認證之前中止**（不退到 SMB1）；能簽章就簽章。pysmb 沒有 SMB3 加密 ——
  內容在內網不加密傳送，跟 HTTP 一樣要使用者同意一次（`allow_plain`）。
"""
from __future__ import annotations

import hashlib
import logging
import re
import socket
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, unquote, urljoin, urlsplit

from . import mfp_box, mfp_client as mc

logger = logging.getLogger("app.mfp_fetch")

SMB_PORT = 445
SMB_TIMEOUT = 20
MAX_ENTRIES = 1000
MAX_DEPTH = 12
MAX_FILES_PER_JOB = 50
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024

#: `target`：用哪組協定與連接埠 —— `reachable`＝連線測試連得上的那幾組裡挑；`fixed`＝路線自己決定（KYOCERA 9091 / 9090）；
#: 沒有＝SMB 445。
ROUTES: dict[str, dict] = {
    "canon_webdav": {"vendor": "Canon", "transport": "webdav", "auth": "user", "roots": ("share", "users"),
                     "target": "reachable"},
    "canon_smb": {"vendor": "Canon", "transport": "smb", "auth": "user", "roots": ("share", "users")},
    "konica_smb": {"vendor": "Konica Minolta", "transport": "smb", "auth": "box", "roots": None},
    "sharp_smb": {"vendor": "Sharp", "transport": "smb", "auth": "user", "roots": None},
    "kyocera_soap": {"vendor": "Kyocera", "transport": "soap", "auth": "user", "roots": None, "target": "fixed",
                     "scheme": "https"},
    "kyocera_soap_http": {"vendor": "Kyocera", "transport": "soap", "auth": "user", "roots": None,
                          "target": "fixed", "scheme": "http"},
    "ricoh_soap": {"vendor": "Ricoh", "transport": "soap", "auth": "user", "roots": None, "target": "reachable"},
}
#: 每個廠牌能用的路線（第一個是預設）。
VENDOR_ROUTES: dict[str, tuple[str, ...]] = {
    "Canon": ("canon_webdav", "canon_smb"),
    "Konica Minolta": ("konica_smb",),
    "Sharp": ("sharp_smb",),
    "Kyocera": ("kyocera_soap", "kyocera_soap_http"),
    "Ricoh": ("ricoh_soap",),
}
#: 位置是不是共享的（畫面上要標「共享」）。
SHARED_ROOTS = {"share": True, "users": False, "public": True, "shared": True, "faxreceive": True}

MESSAGES: dict[str, str] = {
    "SHARE_ROUTE_INVALID": "這台事務機沒有這種取件方式",
    "SHARE_ROOT_INVALID": "位置不正確",
    "SHARE_PATH_INVALID": "資料夾或檔案名稱不正確",
    "SHARE_NOT_FOUND": "設備上找不到這個位置（可能沒有啟用，或名稱不同）",
    "SHARE_FORBIDDEN": "設備拒絕存取這個位置（這個帳號沒有權限）",
    "SHARE_TOO_MANY": "這個資料夾的項目超過一次能列出的上限",
    "SHARE_NO_FILES_SELECTED": "請選擇要取件的檔案",
    "SHARE_TOO_MANY_FILES": "一次最多取 50 個檔案",
    "SHARE_FILE_TOO_LARGE": "檔案超過大小上限",
    "SHARE_FILE_GONE": "設備上已經沒有這個檔案（可能已被移走或刪除）",
    "SHARE_LOGIN_PAGE": "設備回的是登入頁或錯誤頁，不是檔案（登入可能已經失效）",
    "SMB_PLAIN_NEEDS_CONSENT": "SMB 連線不加密：文件會以未加密方式在內網傳送，要先同意才會連線",
    "SMB_CONNECT_FAILED": "連不上事務機的 SMB 服務（445 連接埠沒有開，或設備沒有啟用 SMB 伺服器）",
    "SMB1_REFUSED": "設備只肯用 SMB1（不安全的舊版協定），已停止；請管理員在設備上啟用 SMB2 以上",
    "SMB_AUTH_FAILED": "SMB 認證失敗：帳號或密碼不對（為了避免帳號被鎖，沒有自動重試）",
    "SMB_ERROR": "SMB 操作失敗（可能是權限不足、設備要求加密，或設備回了預期之外的錯誤）",
    "SMB_UNAVAILABLE": "伺服器沒有安裝 SMB 用戶端套件（pysmb）",
    "BOX_NUMBER_REQUIRED": "請輸入信箱編號",
    "BOX_SHARE_NOT_FOUND": "設備上找不到這個信箱的分享資料夾（信箱編號不對，或這個信箱沒有啟用 SMB 分享）",
}
#: 這些錯誤代表連線本身有問題，整件取件就停。
FATAL = mfp_box.FATAL | {"SMB_PLAIN_NEEDS_CONSENT", "SMB_CONNECT_FAILED", "SMB1_REFUSED", "SMB_AUTH_FAILED",
                         "SMB_UNAVAILABLE", "SHARE_NOT_FOUND", "SHARE_FORBIDDEN",
                         # KYOCERA / RICOH（`mfp_soap`）
                         "SOAP_UNAVAILABLE", "SESSION_EXPIRED", "BOX_PASSWORD_FAILED", "RICOH_AUTH_UNSUPPORTED",
                         "RICOH_AUTH_URL_UNTRUSTED", "RICOH_FEATURE_UNSUPPORTED"}


class ShareError(mfp_box.BoxError):
    def __init__(self, code: str, detail: Optional[dict] = None):
        super().__init__(code, detail)
        self.message = MESSAGES.get(code) or mfp_box.MESSAGES.get(code, code)


def message(code: str) -> str:
    return MESSAGES.get(code) or mfp_box.MESSAGES.get(code) or code


# --------------------------------------------------------------------------- 名稱與路徑

_BAD_CHARS = re.compile(r'[\\/\x00-\x1f\x7f]')


def valid_segment(seg: Any) -> bool:
    s = seg if isinstance(seg, str) else ""
    return 0 < len(s) <= 255 and s not in (".", "..") and not _BAD_CHARS.search(s) and s.strip() == s


def check_path(segs: Any) -> list[str]:
    """使用者送來的路徑（字串陣列）→ 驗證過的路徑。不收字串（避免各平台分隔字元的歧義）。"""
    if not isinstance(segs, list) or len(segs) > MAX_DEPTH or not all(valid_segment(s) for s in segs):
        raise ShareError("SHARE_PATH_INVALID")
    return list(segs)


def split_account(raw: str) -> tuple[str, str]:
    """`網域\\帳號` → （帳號, 網域）；`帳號@網域`（UPN）照原樣當帳號送（NTLM 收得下）。"""
    s = (raw or "").strip()
    if "\\" in s:
        dom, _, user = s.partition("\\")
        return user, dom
    return s, ""


# --------------------------------------------------------------------------- 共同介面

class Session:
    route: str
    transport: str

    def roots(self) -> list[dict]:
        raise NotImplementedError

    def list(self, root: str, path: list[str]) -> list[dict]:
        raise NotImplementedError

    def download(self, root: str, path: list[str], dest: Path, max_bytes: int) -> dict:
        raise NotImplementedError

    def via(self) -> dict:
        raise NotImplementedError

    def tls(self) -> Optional[dict]:
        return None

    def close(self) -> None:
        pass


def _root_entry(rid: str, label: str = "") -> dict:
    return {"id": rid, "label": label or rid, "shared": SHARED_ROOTS.get(rid.lower(), True)}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- WebDAV（Canon Advanced Box）

#: 送出去的請求路徑：`/share` 或 `/users` 底下、每一段都是我們自己用 quote() 編過的。
_CANON_TARGET = re.compile(r"^/(?:share|users)(?:/[A-Za-z0-9._~%!$&'()*+,;=:@-]+)*/?$")


class _CanonDav(mfp_box._Dav):
    """只換掉允許的請求路徑形狀；方法限制（只有 PROPFIND / GET）、TLS、認證都沿用 `mfp_box._Dav`。"""

    def _target_ok(self, path: str) -> bool:
        up = path.upper()
        return (bool(_CANON_TARGET.match(path)) and "%2F" not in up and "%5C" not in up and "%00" not in up
                and not any(seg in (".", "..") for seg in path.split("/")))


def _dav_path(root: str, path: list[str], directory: bool) -> str:
    p = "/" + root + "".join("/" + quote(seg, safe="") for seg in path)
    return p + "/" if directory else p


class WebDavSession(Session):
    transport = "webdav"

    def __init__(self, route: str, host: str, ip: str, scheme: str, port: int, *, user: str, password: str,
                 trust_sha256: Optional[str], allow_http: bool, trace: list):
        self.route = route
        self.dav = _CanonDav(host, ip, scheme, port, trust_sha256=trust_sha256, allow_http=allow_http, trace=trace)
        self._user, self._password = user, password

    def __repr__(self) -> str:
        return f"<WebDavSession {self.route}>"

    def via(self) -> dict:
        return {"protocol": "WebDAV", "scheme": self.dav.scheme, "port": self.dav.port}

    def tls(self) -> Optional[dict]:
        return dict(self.dav.tls) if self.dav.tls else None

    def roots(self) -> list[dict]:
        return [_root_entry(r) for r in ROUTES[self.route]["roots"]]

    def _propfind(self, target: str) -> dict:
        r = self.dav.request("PROPFIND", target, depth="1", auth=self.dav.auth is not None)
        if r["status"] == 401 and self.dav.auth is None:
            c = mfp_box._pick(r["challenges"])
            if c is None:
                raise ShareError("AUTH_MODE_UNSUPPORTED", {"challenges": mfp_box._challenge_summary(r["challenges"])})
            if not self._user:
                raise ShareError("AUTH_REQUIRED_USER", {"realm": c["params"].get("realm", "")[:80]})
            self.dav.auth = mfp_box._Auth(c, self._user, self._password)
            r = self.dav.request("PROPFIND", target, depth="1")
            if r["status"] == 401 and self.dav.auth.scheme == "digest":
                fresh = [x for x in r["challenges"] if x["scheme"] == "digest"
                         and x["params"].get("stale", "").lower() == "true"]
                if fresh:
                    self.dav.auth = mfp_box._Auth(fresh[0], self._user, self._password)
                    r = self.dav.request("PROPFIND", target, depth="1")
        if r["status"] in (301, 302, 307, 308):
            raise ShareError("DAV_REDIRECT_REFUSED", {"to": mfp_box._location_path(r["headers"].get("location", ""))})
        if r["status"] == 401:
            raise ShareError("AUTH_FAILED", {"status": 401})
        if r["status"] == 403:
            raise ShareError("SHARE_FORBIDDEN", {"status": 403})
        if r["status"] == 404:
            raise ShareError("SHARE_NOT_FOUND", {"status": 404})
        if r["status"] in (405, 501):
            raise ShareError("WEBDAV_UNAVAILABLE", {"status": r["status"]})
        if r["status"] != 207:
            raise ShareError("DAV_UNEXPECTED_STATUS", {"status": r["status"]})
        return r

    def list(self, root: str, path: list[str]) -> list[dict]:
        if root not in ROUTES[self.route]["roots"]:
            raise ShareError("SHARE_ROOT_INVALID")
        path = check_path(path)
        target = _dav_path(root, path, True)
        r = self._propfind(target)
        entries, _q = mfp_box.parse_multistatus(r.get("body") or b"")
        out: list[dict] = []
        base = ["", root] + path
        for e in entries:
            segs = self._href_segments(e["href"], target)
            if segs is None or segs[:len(base)] != base or len(segs) != len(base) + 1:
                continue                    # 自己這一層、其他位置、更深層、可疑的 href
            name = segs[-1]
            if not valid_segment(name):
                continue
            p = e["props"]
            out.append({"name": name, "dir": bool(e["collection"]),
                        "size": mfp_box._int(p.get("getcontentlength")),
                        "modified": mfp_box.epoch(p.get("getlastmodified") or p.get("creationdate")),
                        "type": (p.get("getcontenttype") or "")[:60]})
            if len(out) > MAX_ENTRIES:
                raise ShareError("SHARE_TOO_MANY", {"limit": MAX_ENTRIES})
        return sorted(out, key=lambda x: (not x["dir"], x["name"].lower()))

    def _href_segments(self, href: str, request_path: str) -> Optional[list[str]]:
        h = (href or "").strip()
        if not h or len(h) > 2048 or "\\" in h:
            return None
        try:
            raw_parts = urlsplit(h).path.split("/")
        except ValueError:
            return None
        if any(unquote(x) in (".", "..") for x in raw_parts):
            return None                     # `..` 在合併網址時會被吃掉、變成「這一層的檔案」—— 合併之前就擋
        d = self.dav
        try:
            u = urlsplit(urljoin(f"{d.scheme}://{d.host_hdr}{request_path}", h))
            port = u.port or (443 if d.scheme == "https" else 80)
        except ValueError:
            return None
        if u.username or u.password or u.query or u.fragment or (u.scheme or d.scheme) != d.scheme:
            return None
        host = (u.hostname or "").lower()
        if (host and host not in (d.host.lower(), d.ip)) or port != d.port:
            return None
        raw = u.path.rstrip("/").split("/")
        segs = [unquote(s) for s in raw]
        if any(unquote(s) != s for s in segs):
            return None                     # 解碼一次之後還能再解：二次編碼，不收
        return segs

    def download(self, root: str, path: list[str], dest: Path, max_bytes: int) -> dict:
        path = check_path(path)
        if not path:
            raise ShareError("SHARE_PATH_INVALID")
        if self.dav.auth is None:
            self.list(root, path[:-1])      # 先讓認證就位（第一次就是下載時）
        r = self.dav.request("GET", _dav_path(root, path, False), to_file=dest, max_bytes=max_bytes)
        if r["status"] == 404:
            raise ShareError("SHARE_FILE_GONE")
        if r["status"] == 401:
            raise ShareError("AUTH_FAILED", {"status": 401})
        if r["status"] == 403:
            raise ShareError("SHARE_FORBIDDEN", {"status": 403})
        if r["status"] != 200:
            raise ShareError("DAV_UNEXPECTED_STATUS", {"status": r["status"]})
        ctype = r["headers"].get("content-type", "").lower()
        if "text/html" in ctype and not path[-1].lower().endswith((".htm", ".html")):
            dest.unlink(missing_ok=True)
            raise ShareError("SHARE_LOGIN_PAGE", {"content_type": ctype[:60]})
        return {"bytes": r.get("bytes", 0), "sha256": _sha256(dest), "content_type": ctype[:60]}


# --------------------------------------------------------------------------- SMB（pysmb）

def _smb_classes():
    try:
        from smb.SMBConnection import SMBConnection
        from smb.base import NotConnectedError, NotReadyError, SharedDevice
        from smb.smb_structs import OperationFailure, ProtocolError
    except ImportError:
        raise ShareError("SMB_UNAVAILABLE") from None
    return SMBConnection, SharedDevice, (OperationFailure, NotConnectedError, NotReadyError, ProtocolError)


class _SMB1Refused(Exception):
    pass


def _strict_connection_class():
    """pysmb 的 SMBConnection，**伺服器選了 SMB1 就在認證之前中止**（不讓帳密走 SMB1 的流程）。"""
    SMBConnection, _sd, _errs = _smb_classes()

    class StrictSMB(SMBConnection):
        def _handleNegotiateResponse_SMB1(self, message):     # noqa: N802 —— pysmb 的方法名稱
            raise _SMB1Refused()

    return StrictSMB


class _CappedWriter:
    """寫進檔案、算大小；超過上限就丟例外（pysmb 會把它傳出 retrieveFile）。"""

    def __init__(self, f, limit: int):
        self.f, self.limit, self.n = f, limit, 0

    def write(self, data: bytes) -> int:
        self.n += len(data)
        if self.n > self.limit:
            raise ShareError("SHARE_FILE_TOO_LARGE", {"limit_mb": self.limit // 1024 // 1024})
        return self.f.write(data)


class SmbSession(Session):
    transport = "smb"

    def __init__(self, route: str, host: str, ip: str, *, user: str, password: str, allow_plain: bool,
                 trace: list, connection_class=None):
        self.route, self.host, self.ip = route, host, ip
        self.trace = trace
        if not allow_plain:
            raise ShareError("SMB_PLAIN_NEEDS_CONSENT")
        code = mc.check_address(ip)
        if code:
            raise mc.PolicyError(code, {"ip": ip})
        _c, self._SharedDevice, self._errors = _smb_classes()
        klass = connection_class or _strict_connection_class()
        username, domain = split_account(user)
        remote = (host if not mc._is_ip(host) else "SMBSERVER")[:15]
        self.conn = klass(username, password, "JTDT", remote, domain=domain, use_ntlm_v2=True,
                          sign_options=1, is_direct_tcp=True)   # 1 = SIGN_WHEN_SUPPORTED：設備支援就簽章
        t0 = time.monotonic()
        entry: dict[str, Any] = {"method": "SMB connect", "path": f"{ip}:{SMB_PORT}", "auth": "ntlmv2" if user else "anonymous"}
        try:
            ok = self.conn.connect(ip, SMB_PORT, timeout=SMB_TIMEOUT)
        except _SMB1Refused:
            entry["error"] = "SMB1_REFUSED"
            self._close_quietly()
            raise ShareError("SMB1_REFUSED") from None
        except (OSError, socket.timeout) as e:
            entry["error"] = "SMB_CONNECT_FAILED"
            raise ShareError("SMB_CONNECT_FAILED", {"error": e.__class__.__name__}) from None
        except self._errors as e:
            entry["error"] = "SMB_ERROR"
            self._close_quietly()
            raise ShareError("SMB_ERROR", {"error": e.__class__.__name__}) from None
        finally:
            entry["ms"] = int((time.monotonic() - t0) * 1000)
            self._log(entry)
        if not ok:
            self._close_quietly()
            raise ShareError("SMB_AUTH_FAILED")
        if not getattr(self.conn, "isUsingSMB2", False):
            self._close_quietly()
            raise ShareError("SMB1_REFUSED")
        entry["status"] = "ok"
        entry["dialect"] = "SMB2"

    def __repr__(self) -> str:
        return f"<SmbSession {self.route}>"

    def _log(self, entry: dict) -> None:
        if len(self.trace) < mfp_box.MAX_TRACE:
            self.trace.append(entry)

    def _close_quietly(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        self._close_quietly()

    def via(self) -> dict:
        return {"protocol": "SMB", "scheme": "smb", "port": SMB_PORT}

    def _call(self, what: str, fn: Callable, *a, **kw):
        t0 = time.monotonic()
        entry: dict[str, Any] = {"method": f"SMB {what}", "path": str(a[0] if a else "")[:120]}
        try:
            out = fn(*a, **kw)
            entry["status"] = "ok"
            return out
        except ShareError as e:
            entry["error"] = e.code
            raise
        except self._errors as e:
            text = str(e)
            entry["error"] = "SMB_ERROR"
            if "0xc0000022" in text.lower() or "ACCESS_DENIED" in text.upper():
                raise ShareError("SHARE_FORBIDDEN") from None
            if "0xc0000034" in text.lower() or "0xc000003a" in text.lower() or "0xc00000cc" in text.lower() \
                    or "NOT_FOUND" in text.upper() or "BAD_NETWORK_NAME" in text.upper():
                raise ShareError("SHARE_NOT_FOUND") from None
            raise ShareError("SMB_ERROR", {"error": e.__class__.__name__}) from None
        except (OSError, socket.timeout) as e:
            entry["error"] = "SMB_CONNECT_FAILED"
            raise ShareError("SMB_CONNECT_FAILED", {"error": e.__class__.__name__}) from None
        finally:
            entry["ms"] = int((time.monotonic() - t0) * 1000)
            self._log(entry)

    def shares(self) -> list[str]:
        out = []
        for d in self._call("listShares", self.conn.listShares, timeout=SMB_TIMEOUT):
            if d.type == self._SharedDevice.DISK_TREE and not d.isSpecial and valid_segment(d.name):
                out.append(d.name)
        return out[:200]

    def roots(self) -> list[dict]:
        fixed = ROUTES[self.route]["roots"]
        names = self.shares()
        if fixed:
            return [_root_entry(n) for n in fixed if n in names]
        return [_root_entry(n) for n in names]

    def list(self, root: str, path: list[str]) -> list[dict]:
        if not valid_segment(root):
            raise ShareError("SHARE_ROOT_INVALID")
        path = check_path(path)
        rows = self._call("listPath", self.conn.listPath, root, "/" + "/".join(path), timeout=SMB_TIMEOUT)
        out = []
        for f in rows:
            if f.filename in (".", "..") or not valid_segment(f.filename):
                continue
            out.append({"name": f.filename, "dir": bool(f.isDirectory),
                        "size": None if f.isDirectory else int(f.file_size or 0),
                        "modified": float(f.last_write_time) if f.last_write_time else None, "type": ""})
            if len(out) > MAX_ENTRIES:
                raise ShareError("SHARE_TOO_MANY", {"limit": MAX_ENTRIES})
        return sorted(out, key=lambda x: (not x["dir"], x["name"].lower()))

    def download(self, root: str, path: list[str], dest: Path, max_bytes: int) -> dict:
        path = check_path(path)
        if not path or not valid_segment(root):
            raise ShareError("SHARE_PATH_INVALID")
        with open(dest, "wb") as f:
            w = _CappedWriter(f, max_bytes)
            try:
                self._call("retrieveFile", self.conn.retrieveFile, root, "/" + "/".join(path), w,
                           timeout=SMB_TIMEOUT)
            except ShareError as e:
                if e.code == "SHARE_NOT_FOUND":
                    raise ShareError("SHARE_FILE_GONE") from None
                raise
        return {"bytes": w.n, "sha256": _sha256(dest), "content_type": ""}


# --------------------------------------------------------------------------- 對外入口

def routes_for(vendor: Optional[str]) -> tuple[str, ...]:
    return VENDOR_ROUTES.get(vendor or "", ())


def needs_reachable_target(route: str) -> bool:
    """這條路線要不要用連線測試連得上的協定與連接埠（WebDAV、RICOH）。"""
    return ROUTES.get(route, {}).get("target") == "reachable"


def protocol(route: str) -> str:
    return {"webdav": "WebDAV", "smb": "SMB", "soap": "SOAP"}[ROUTES[route]["transport"]]


def display_name(sess: Session, root: str, path: list[str]) -> str:
    """畫面與作業紀錄上的檔名：SOAP 的路徑段是編碼過的識別碼，要換回設備上的名稱。"""
    fn = getattr(sess, "display_name", None)
    if fn is not None:
        try:
            return str(fn(root, path) or path[-1])
        except mfp_box.BoxError:
            return path[-1]
    return path[-1]


def open_session(route: str, *, host: str, ip: str, scheme: Optional[str], port: Optional[int], user: str,
                 password: str, box: Optional[str], box_password: str, trust_sha256: Optional[str],
                 allow_http: bool, allow_plain: bool, trace: list, auth_mode: str = "auto",
                 user_code: str = "", folder_password: str = "") -> Session:
    """開一個取件連線（認證只送一次）。路線與廠牌是否相符由呼叫端（路由）先檢查。"""
    if route not in ROUTES:
        raise ShareError("SHARE_ROUTE_INVALID")
    spec = ROUTES[route]
    if spec["transport"] == "soap":
        from . import mfp_soap
        if spec.get("target") == "fixed":
            code = mc.check_address(ip)
            if code:
                raise mc.PolicyError(code, {"ip": ip})
            return mfp_soap.KyoceraSession(route, host, ip, spec["scheme"], user=user, password=password,
                                           box_password=box_password, trust_sha256=trust_sha256,
                                           allow_http=allow_http, trace=trace)
        if not scheme or not port:
            raise ShareError("BOX_NO_TARGET")
        mfp_box._check_target(ip, scheme, int(port))
        return mfp_soap.RicohSession(route, host, ip, scheme, int(port), user=user, password=password,
                                     doc_password=box_password, trust_sha256=trust_sha256,
                                     allow_http=allow_http, trace=trace, auth_mode=auth_mode or "auto",
                                     user_code=user_code, folder_password=folder_password)
    if spec["transport"] == "webdav":
        if not scheme or not port:
            raise ShareError("BOX_NO_TARGET")
        mfp_box._check_target(ip, scheme, int(port))
        return WebDavSession(route, host, ip, scheme, int(port), user=user, password=password,
                             trust_sha256=trust_sha256, allow_http=allow_http, trace=trace)
    if spec["auth"] == "box":
        num = (box or "").strip()
        if not re.fullmatch(r"\d{1,9}", num):
            raise ShareError("BOX_NUMBER_REQUIRED")
        sess = SmbSession(route, host, ip, user=num, password=box_password, allow_plain=allow_plain, trace=trace)
        sess.box = num          # type: ignore[attr-defined]
        return sess
    return SmbSession(route, host, ip, user=user, password=password, allow_plain=allow_plain, trace=trace)


def konica_box_share(sess: SmbSession, box: str) -> str:
    """Konica 公用信箱的分享名稱：設備列出的分享裡，以**九位信箱編號**開頭的那一個（讀設備列出的，不自己拼）。"""
    want = f"{int(box):09d}"
    hits = [n for n in sess.shares() if n.startswith(want)]
    if not hits:
        raise ShareError("BOX_SHARE_NOT_FOUND")
    return sorted(hits)[0]


# KYOCERA / RICOH：`mfp_soap` 匯入本模組，所以放在最後；它被匯入時會把自己的訊息登記進 MESSAGES
# （先匯入哪一個都一樣，測試釘住）
from . import mfp_soap as _soap  # noqa: E402,F401
