"""事務機掃描取件：KYOCERA / RICOH 直接取件（SOAP，**實驗功能**，尚未在實機上驗證）。

依據：使用者 2026-10-15 交來的研究（內部文件 `docs-share/mfp-fetch/ricoh-kyocera-direct-protocol-20261015/`），
分析原廠工具元件找到的直接文件服務 —— **不是原廠公開承諾的 API，也沒有實機回應樣本**。所以：

* 方法、欄位、端點照研究文件與請求範本；文件標「實機待驗證」的（KYOCERA 多頁回應、range 單位；
  RICOH v1 / User Code / FTP）照最保守的寫：有上限、不支援的明講，**不猜**。
* 協定紀錄完整留下（方法、狀態碼、型別、大小），實機時拿來對照；**不記帳密、token、票證、下載網址**。

| 路線 | 服務 | 流程 |
|---|---|---|
| `kyocera_soap` / `kyocera_soap_http` | KM-WSDL（HTTPS 9091 / HTTP 9090），SOAP 1.2 | 認證狀態 → 登入（token 放 WS-Security）→ 列文件匣 → 列文件 → 準備下載 → 取資料（Base64）→ 登出 |
| `ricoh_soap` | `/DH/repository`，SOAP 1.1 encoded | 協定版本 → 服務能力（取認證網址、支援的功能類別、登入模式候選）→ 選定**一種**登入方式 → 開工作階段（帳密換票證 → `startSessionByAuthTicket`，或 `startSession`：免登入 / 帳號密碼 / User Code）→ 查詢文件 → 原始表示 → 內容元素 → 下載網址（`getDocContentUrl2` 或 `getDocContentUrl`，GET / POST）→ 結束工作階段 |

RICOH 非票證登入（`startSession`）依 2026-10-19 的分析（內部文件 `docs-share/mfp-fetch/ricoh-startsession-20261019/`）：
第一個結果是狀態（成功是 `OK`），`stringOut` 才是工作階段代號；`stringIn` 的四種組法照分析文件。**登入方式由使用者選或由資訊唯一決定，不輪流試**；帳號空白不等於免登入。

## 安全邊界（跟其他取件同一套）

* 位址：內網、DNS 釘選、不可以是本機（呼叫端先檢查）；TLS 要通過驗證或跟使用者核對過的指紋一致；HTTP 要使用者同意。
* **只送 POST 到固定的服務路徑**（KYOCERA 三個、RICOH `/DH/repository` 加上能力查詢回來的認證路徑）；
  RICOH 的下載網址與認證網址**一定要在同一台設備、同一個協定與連接埠**上，不是就拒絕。不跟隨轉址。
* XML 用函式庫組（不拼字串）、用 defusedxml 剖析（外部實體一律拒絕）；回應大小有上限。
* 不刪除、不改名、不寫入設備上的任何東西；帳密只在這次請求或那件作業的記憶體裡；認證失敗就停，不換組合重試。
"""
from __future__ import annotations

import base64
import http.client
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element, SubElement, register_namespace, tostring

from . import mfp_box, mfp_client as mc, mfp_share as _share
from .mfp_share import Session, ShareError, _sha256, check_path

logger = logging.getLogger("app.mfp_fetch")

#: KYOCERA KM-WSDL 的連接埠（測試換成假伺服器的埠）
KYO_PORTS = {"https": 9091, "http": 9090}
KYO_PATHS = {
    "auth": "/ws/km-wsdl/security/authentication_authorization",
    "box": "/ws/km-wsdl/setting/box_information",
    "data": "/ws/km-wsdl/job/stored_data_operation",
}
RICOH_REPO_PATH = "/DH/repository"

ENV12 = "http://www.w3.org/2003/05/soap-envelope"
ENV11 = "http://schemas.xmlsoap.org/soap/envelope/"
ENC11 = "http://schemas.xmlsoap.org/soap/encoding/"
WSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
XSD = "http://www.w3.org/2001/XMLSchema"
KYO_NS = {
    "auth": "http://www.kyoceramita.com/ws/km-wsdl/security/authentication_authorization",
    "box": "http://www.kyoceramita.com/ws/km-wsdl/setting/box_information",
    "data": "http://www.kyoceramita.com/ws/km-wsdl/job/stored_data_operation",
}
RICOH_REPO_NS = "http://www.ricoh.co.jp/xmlns/soap/rdh/repository"
RICOH_AUTH3_NS = "http://www.ricoh.co.jp/xmlns/soap/rdh/uauthentication-v3"
RICOH_CT = "http://www.ricoh.co.jp/xmlns/schema/rdh/commontypes"
RICOH_RT = "http://www.ricoh.co.jp/xmlns/schema/rdh/repository"

for _p, _u in (("env", ENV12), ("wsa", WSA), ("wsse", WSSE), ("kmauth", KYO_NS["auth"]),
               ("kmboxinfo", KYO_NS["box"]), ("kmsavedata", KYO_NS["data"]), ("soapenv", ENV11),
               ("enc", ENC11), ("xsi", XSI), ("m", RICOH_REPO_NS), ("ua", RICOH_AUTH3_NS)):
    register_namespace(_p, _u)

#: 一次讀多少（研究文件：range 的單位與上限未確認 —— 用小的正整數）
KYO_RANGE = 20
#: 列舉、查詢、輪詢的輪數上限（設備一直不說「結束」也不會無限迴圈）
MAX_ROUNDS = 200
#: 設備還在準備檔案時的等待：1、2、4、8… 秒，總共最多這麼久
BUSY_WAIT_LIMIT = 120.0
#: 一份文件最多幾個部分（RICOH 內容元素、KYOCERA 多次回應）
MAX_PARTS = 500
#: RICOH 一次列出的文件上限（每份要再查一次屬性）
RICOH_MAX_DOCS = 300
#: SOAP 回應的大小上限：控制訊息 / 帶 Base64 檔案的回應
MAX_SOAP = 4 * 1024 * 1024
MAX_SOAP_DATA = 96 * 1024 * 1024
RICOH_TIME_LIMIT = 1800         # 原廠 wrapper 的值（研究文件；單位未確認）
#: RICOH 登入方式（使用者選；`auto` 只在資訊足夠選出唯一一種時才決定，不輪流試）
RICOH_AUTH_MODES = ("auto", "none", "account", "user_code", "user_code_ext")
#: `stringIn` 的 SCHEME 值（Base64 的 `BASIC` / `RGWUCD` / `RGWEXTUCD`）
_RICOH_SCHEME_B64 = {"BASIC": "QkFTSUM=", "RGWUCD": "UkdXVUNE", "RGWEXTUCD": "UkdXRVhUVUNE"}
#: HTTP 的 BASIC 密碼表示（`gwpwes003`）：本地上限 128 UTF-8 bytes（分析文件的實作限制，不是設備上限）
RICOH_PW_MAX = 128
RICOH_URL_HINT = 60

MESSAGES: dict[str, str] = {
    "SOAP_UNAVAILABLE": "設備的文件服務沒有回應（KYOCERA 要啟用 9091 / 9090 連接埠的文件服務；RICOH 要有文件伺服器服務）",
    "SOAP_FAULT": "設備回報錯誤（協定紀錄裡有設備的錯誤代碼）",
    "SOAP_DEVICE_ERROR": "設備回報無法完成這個操作（協定紀錄裡有設備的錯誤代碼）",
    "SOAP_BUSY": "設備忙碌中或還在準備檔案，等到上限時間仍沒有完成，請稍後再試",
    "SOAP_ATTACHMENT_UNSUPPORTED": "設備用附件方式傳送檔案，這種方式還沒有實機依據，目前不支援（協定紀錄裡有細節）",
    "SESSION_EXPIRED": "設備的登入已經逾時，請重新列出文件再取件",
    "BOX_PASSWORD_FAILED": "文件匣密碼不對，或這個文件匣需要密碼",
    "DOC_PASSWORD_REQUIRED": "這份文件有設密碼：請在「文件密碼」輸入後再取件",
    "DOC_PASSWORD_FAILED": "文件密碼不對",
    "DOC_NO_CONTENT": "設備沒有回傳這份文件的內容",
    "RICOH_AUTH_UNSUPPORTED": "這台 RICOH 沒有提供可以用的登入方式（協定紀錄裡有設備列出的功能類別）",
    "RICOH_AUTH_MODE_REQUIRED": "請在「登入方式」選這台事務機用的方式（不需登入、帳號密碼或 User Code）；還沒有送出任何登入資料",
    "RICOH_AUTH_MODE_MISMATCH": "選的登入方式跟設備列出的不一樣，沒有送出（協定紀錄裡有設備列出的登入方式）",
    "RICOH_AUTH_INPUT_CONFLICT": "選了「不需登入」但有填帳號、密碼或 User Code：請清掉，或改選對應的登入方式",
    "RICOH_AUTH_PROFILE_UNSUPPORTED": "這台的協定版本不支援選的登入方式，沒有改用其他格式",
    "RICOH_USER_CODE_REQUIRED": "請輸入 User Code（只能是數字，開頭的 0 要保留）",
    "RICOH_PASSWORD_UNSUPPORTED": "這組密碼在 HTTP 登入格式裡無法表示（太長或含特殊字元），沒有送出；可以改用 HTTPS 連線",
    "RICOH_SESSION_REJECTED": "設備沒有接受這次登入（為了避免被鎖，沒有自動重試）：請確認登入方式與資料；一直不通過時請管理員確認設備的認證設定",
    "RICOH_AUTH_URL_UNTRUSTED": "設備回的認證服務網址不在這台事務機上，沒有連過去",
    "RICOH_ORIGINAL_UNAVAILABLE": "這份文件沒有提供原始格式（沒有自動改用其他格式）",
    "RICOH_URL_UNSUPPORTED": "設備給的下載方式目前不支援（例如 FTP，或下載網址不在這台事務機上）",
    "RICOH_FEATURE_UNSUPPORTED": "這台 RICOH 的服務能力沒有列出這項功能（協定紀錄裡有設備列出的功能類別）",
}
_share.MESSAGES.update(MESSAGES)        # ShareError 照代碼找訊息（mfp_share 的說明）


# --------------------------------------------------------------------------- 名稱編碼

def enc_name(raw: str) -> Optional[str]:
    """設備的識別碼 / 文件名稱 → 可以放進路徑的一段（只有英數、`-`、`_`、`~`）。太長回 None。"""
    s = "~" + base64.urlsafe_b64encode(str(raw).encode("utf-8")).decode("ascii").rstrip("=")
    return s if 1 < len(s) <= 255 else None


def dec_name(seg: str) -> str:
    if not isinstance(seg, str) or not re.fullmatch(r"~[A-Za-z0-9_-]{1,254}", seg):
        raise ShareError("SHARE_PATH_INVALID")
    try:
        return base64.urlsafe_b64decode(seg[1:] + "=" * (-len(seg[1:]) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise ShareError("SHARE_PATH_INVALID") from None


# --------------------------------------------------------------------------- HTTP（沿用 _Dav 的連線、TLS、同意）

_PATH_OK = re.compile(r"^/[\x21-\x7e]{0,2047}$")


class _SoapHttp(mfp_box._Dav):
    """一台設備的 SOAP 連線。請求路徑只能是允許清單裡的；POST 自己送，GET 沿用 `_Dav.request`。"""

    def __init__(self, host: str, ip: str, scheme: str, port: int, *, paths, trust_sha256: Optional[str],
                 allow_http: bool, trace: list):
        super().__init__(host, ip, scheme, port, trust_sha256=trust_sha256, allow_http=allow_http, trace=trace)
        self._paths: set[str] = set(paths)

    def allow(self, path: str) -> None:
        if not _PATH_OK.match(path or ""):
            raise ShareError("RICOH_URL_UNSUPPORTED", {"reason": "path"})
        self._paths.add(path)

    def _target_ok(self, path: str) -> bool:
        return path in self._paths

    def post(self, path: str, body: bytes, content_type: str, *, label: str, extra: Optional[dict] = None,
             to_file: Optional[Path] = None, max_bytes: int = MAX_SOAP,
             timeout: float = mfp_box.READ_TIMEOUT) -> dict:
        if not self._target_ok(path):
            raise ValueError("unsafe request target")
        t0 = time.monotonic()
        entry: dict[str, Any] = {"method": "POST", "path": path.split("?", 1)[0][:120], "call": label}
        sock = None
        try:
            sock = self._open(timeout)
            sock.settimeout(timeout)
            head = (f"POST {path} HTTP/1.1\r\nHost: {self.host_hdr}\r\nUser-Agent: {mfp_box.USER_AGENT}\r\n"
                    f"Accept: */*\r\nAccept-Encoding: identity\r\nContent-Type: {content_type}\r\n"
                    f"Content-Length: {len(body)}\r\n")
            for k, v in (extra or {}).items():
                head += f"{k}: {v}\r\n"
            sock.sendall((head + "\r\n").encode("utf-8") + body)
            resp = http.client.HTTPResponse(sock, method="POST")
            resp.begin()
            headers = {k.lower(): v for k, v in resp.getheaders()}
            out: dict[str, Any] = {"status": resp.status, "headers": headers}
            entry.update(status=resp.status, content_type=headers.get("content-type", "")[:80])
            if headers.get("location"):
                entry["location"] = mfp_box._location_path(headers["location"])
            enc = headers.get("content-encoding", "identity").strip().lower()
            if enc not in ("", "identity"):
                raise mfp_box.BoxError("DOC_ENCODING_UNSUPPORTED", {"encoding": enc[:20]})
            if to_file is not None and resp.status == 200 and not _is_xml(headers):
                n = 0
                with open(to_file, "wb") as f:
                    while True:
                        try:
                            chunk = resp.read(256 * 1024)
                        except http.client.IncompleteRead:
                            raise mfp_box.BoxError("DOC_INCOMPLETE") from None
                        if not chunk:
                            break
                        n += len(chunk)
                        if n > max_bytes:
                            raise mfp_box.BoxError("DOC_TOO_LARGE", {"limit_mb": max_bytes // 1024 // 1024})
                        f.write(chunk)
                clen = headers.get("content-length")
                if clen and clen.isdigit() and int(clen) != n:
                    raise mfp_box.BoxError("DOC_INCOMPLETE", {"expected": int(clen), "got": n})
                out["bytes"] = entry["bytes"] = n
                return out
            cap = max_bytes if to_file is None else MAX_SOAP
            try:
                data = resp.read(cap + 1)
            except http.client.IncompleteRead as e:
                data = bytes(e.partial or b"")
            if len(data) > cap:
                raise mfp_box.BoxError("DOC_TOO_LARGE" if cap > MAX_SOAP else "DAV_BAD_RESPONSE",
                                       {"reason": "response too large", "limit_mb": cap // 1024 // 1024})
            out["body"] = data
            entry["bytes"] = len(data)
            return out
        except mfp_box.BoxError as e:
            entry.setdefault("error", e.code)
            raise
        except (OSError, http.client.HTTPException) as e:
            code = "DAV_TIMEOUT" if isinstance(e, TimeoutError) else "DAV_CONNECTION_FAILED"
            entry["error"] = code
            raise mfp_box.BoxError(code, {"error": e.__class__.__name__, "call": label}) from None
        finally:
            if sock is not None:
                sock.close()
            entry["ms"] = int((time.monotonic() - t0) * 1000)
            if len(self.trace) < mfp_box.MAX_TRACE:
                self.trace.append(entry)


def _is_xml(headers: dict) -> bool:
    ct = headers.get("content-type", "").lower()
    return "xml" in ct or "multipart/" in ct


# --------------------------------------------------------------------------- XML 剖析

def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _parse(data: bytes, label: str):
    from defusedxml.ElementTree import fromstring
    from defusedxml.common import DefusedXmlException
    try:
        return fromstring(data)
    except (DefusedXmlException, ValueError, SyntaxError):
        raise mfp_box.BoxError("DAV_BAD_RESPONSE", {"reason": "not xml", "call": label}) from None


def _body(root) -> Any:
    for el in root:
        if _local(el.tag) == "Body":
            return el
    raise mfp_box.BoxError("DAV_BAD_RESPONSE", {"reason": "no soap body"})


def _fault(body) -> Optional[dict]:
    """SOAP 1.1 / 1.2 的 Fault → {code, text}（文字截短；不含任何我們送出去的值）。"""
    for el in body:
        if _local(el.tag) != "Fault":
            continue
        code, text = "", ""
        for d in el.iter():
            n = _local(d.tag)
            if n in ("faultcode", "Value") and not code:
                code = (d.text or "").strip()
            elif n in ("faultstring", "Text") and not text:
                text = (d.text or "").strip()
        detail = " ".join((d.text or "").strip() for d in el.iter() if (d.text or "").strip())
        return {"code": code[:80], "text": text[:200], "all": detail[:600]}
    return None


def _first(el, name: str):
    for d in el.iter():
        if _local(d.tag) == name:
            return d
    return None


def _text(el, name: str) -> str:
    d = _first(el, name)
    return (d.text or "").strip() if d is not None else ""


# --------------------------------------------------------------------------- 日期 / 數字

def _epoch(v: Any) -> Optional[float]:
    """設備的日期 → epoch。格式沒有實機樣本：ISO 8601、`YYYYMMDDhhmmss`、或拆成 year / month… 的結構都試；不認得回 None。"""
    if isinstance(v, dict):
        try:
            return datetime(int(v.get("year")), int(v.get("month")), int(v.get("day")),
                            int(v.get("hour") or 0), int(v.get("minute") or 0), int(v.get("second") or 0)).timestamp()
        except (TypeError, ValueError):
            return None
    s = str(v or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d{14}", s):
        try:
            return datetime.strptime(s, "%Y%m%d%H%M%S").timestamp()
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _num(v: Any) -> Optional[int]:
    s = str(v or "").strip()
    return int(s) if s.isdigit() else None


def _write_part(dest: Path, data: bytes, max_bytes: int) -> int:
    if len(data) > max_bytes:
        raise mfp_box.BoxError("DOC_TOO_LARGE", {"limit_mb": max_bytes // 1024 // 1024})
    dest.write_bytes(data)
    return len(data)


# =========================================================================== KYOCERA

#: 設備回的 result → 我們的錯誤代碼（研究文件第 7 節；不在表裡的非成功值一律當錯誤，不當成功）
_KYO_ERRORS = {
    "INVALID_PASSWORD_OR_USERID_ERROR": "AUTH_FAILED",
    "INVALID_USERID_ERROR": "AUTH_FAILED",
    "OUT_OF_LOGIN_SESSION_ERROR": "SESSION_EXPIRED",
    "INVALID_TOKEN_ERROR": "SESSION_EXPIRED",
    "ACCESS_ERROR": "SHARE_FORBIDDEN",
    "BOX_NOT_FOUND_ERROR": "SHARE_NOT_FOUND",
    "DOCUMENT_NOT_FOUND_ERROR": "SHARE_FILE_GONE",
    "FILE_NOT_FOUND_ERROR": "SHARE_FILE_GONE",
    "INVALID_OPERATION_ID_ERROR": "SESSION_EXPIRED",
}
_KYO_OK = {"", "SUCCESS", "ALL_GET_COMPLETE"}
_KYO_AUTH_METHODS = {"LOCAL_AUTHENTICATION", "NETWORK_AUTHENTICATION"}


def kyocera_target(scheme: str) -> int:
    return KYO_PORTS["https" if scheme == "https" else "http"]


class KyoceraSession(Session):
    """KM-WSDL。建立時就登入（設備要求的話）；`close()` 登出並釋放列舉。"""
    transport = "soap"

    def __init__(self, route: str, host: str, ip: str, scheme: str, *, user: str, password: str,
                 box_password: str, trust_sha256: Optional[str], allow_http: bool, trace: list):
        self.route = route
        port = kyocera_target(scheme)
        self.http = _SoapHttp(host, ip, scheme, port, paths=KYO_PATHS.values(), trust_sha256=trust_sha256,
                              allow_http=allow_http, trace=trace)
        self._box_password = box_password or ""
        self._token: Optional[str] = None
        self._encode = ""
        self._logged_in = False
        self._login(user, password)

    def __repr__(self) -> str:
        return f"<KyoceraSession {self.route}>"

    def via(self) -> dict:
        return {"protocol": "SOAP", "scheme": self.http.scheme, "port": self.http.port}

    def tls(self) -> Optional[dict]:
        return dict(self.http.tls) if self.http.tls else None

    # ---- 呼叫
    def _envelope(self, service: str, method: str, fields: list, token: bool = True) -> bytes:
        ns = KYO_NS[service]
        env = Element(f"{{{ENV12}}}Envelope")
        hdr = SubElement(env, f"{{{ENV12}}}Header")
        SubElement(hdr, f"{{{WSA}}}Action").text = f"{ns}/{method}"
        if token and self._token:
            sec = SubElement(hdr, f"{{{WSSE}}}Security")
            ut = SubElement(sec, f"{{{WSSE}}}UsernameToken")
            SubElement(ut, f"{{{WSSE}}}Username").text = self._token
        req = SubElement(SubElement(env, f"{{{ENV12}}}Body"), f"{{{ns}}}{method}Request")
        _fill(req, ns, fields)
        return tostring(env, encoding="utf-8", xml_declaration=True)

    def _call(self, service: str, method: str, fields: list, *, token: bool = True, max_bytes: int = MAX_SOAP):
        body = self._envelope(service, method, fields, token)
        r = self.http.post(KYO_PATHS[service], body, "application/soap+xml; charset=utf-8", label=method,
                           max_bytes=max_bytes)
        ct = r["headers"].get("content-type", "").lower()
        if r["status"] in (404, 405, 501):
            raise ShareError("SOAP_UNAVAILABLE", {"status": r["status"], "call": method})
        if "multipart/" in ct:
            raise ShareError("SOAP_ATTACHMENT_UNSUPPORTED", {"content_type": ct[:80], "call": method})
        if r["status"] in (301, 302, 307, 308):
            raise ShareError("DAV_REDIRECT_REFUSED", {"call": method})
        root = _parse(r.get("body") or b"", method)
        b = _body(root)
        f = _fault(b)
        if f:
            if self.http.trace:
                self.http.trace[-1]["fault"] = f["code"] or f["text"][:80]
            raise ShareError("SOAP_FAULT", {"call": method, "fault": f["code"], "reason": f["text"]})
        if r["status"] != 200:
            raise ShareError("DAV_UNEXPECTED_STATUS", {"status": r["status"], "call": method})
        resp = next(iter(b), None)
        if resp is None:
            raise mfp_box.BoxError("DAV_BAD_RESPONSE", {"reason": "empty body", "call": method})
        result = _text(resp, "result")
        if self.http.trace:
            self.http.trace[-1]["result"] = result[:60]
        return resp, result

    def _check(self, result: str, method: str, *, box: bool = False) -> None:
        if result in _KYO_OK:
            return
        if result == "INVALID_PASSWORD_ERROR":
            raise ShareError("BOX_PASSWORD_FAILED" if box else "AUTH_FAILED", {"call": method})
        code = _KYO_ERRORS.get(result, "SOAP_DEVICE_ERROR")
        if code == "SESSION_EXPIRED" and not self._logged_in:
            code = "AUTH_REQUIRED_USER"         # 沒有登入就被要 token：這台要帳號
        raise ShareError(code, {"call": method, "result": result[:60]})

    # ---- 登入 / 登出
    def _login(self, user: str, password: str) -> None:
        resp, result = self._call("auth", "get_authentication_status", [], token=False)
        method = _text(resp, "authentication_method")
        if self.http.trace:
            self.http.trace[-1]["authentication_method"] = method[:40]
        if not user:
            if method in _KYO_AUTH_METHODS:
                raise ShareError("AUTH_REQUIRED_USER", {"authentication_method": method})
            return          # 不知道要不要登入：先不送帳密，設備真的要求會回 token 錯誤 → AUTH_REQUIRED_USER
        am = method if method in _KYO_AUTH_METHODS else "LOCAL_AUTHENTICATION"
        resp, result = self._call("auth", "login_system", [("userid", user), ("password", password),
                                                           ("authentication_method", am)], token=False)
        self._check(result, "login_system")
        tok = _first(resp, "usertoken")
        token = _text(tok, "token") if tok is not None else ""
        if not token:
            raise ShareError("AUTH_FAILED", {"call": "login_system", "reason": "no token"})
        self._token, self._encode = token, _text(tok, "encode")
        self._logged_in = True

    def close(self) -> None:
        if not self._logged_in:
            return
        try:
            self._call("auth", "logout_system", [("usertoken", [("encode", self._encode), ("token", self._token)])])
        except mfp_box.BoxError:
            pass
        finally:
            self._logged_in, self._token = False, None

    # ---- 列舉（建立 → 逐批讀 → 一定釋放）
    def _enumerate(self, create: str, create_fields: list, get: str, item: str, destroy: str,
                   *, box: bool = False) -> list:
        resp, result = self._call("box", create, create_fields)
        self._check(result, create, box=box)
        enum = _text(resp, "enumeration")
        if not enum:
            raise mfp_box.BoxError("DAV_BAD_RESPONSE", {"reason": "no enumeration", "call": create})
        out: list = []
        try:
            for _ in range(MAX_ROUNDS):
                resp, result = self._call("box", get, [("enumeration", enum)])
                self._check(result, get, box=box)
                got = [el for el in resp if _local(el.tag) == item]
                out.extend(got)
                if len(out) > mfp_box.MAX_TRACE * 100:
                    raise ShareError("SHARE_TOO_MANY", {"limit": len(out)})
                if result == "ALL_GET_COMPLETE" or not got:
                    break
        finally:
            try:
                self._call("box", destroy, [("enumeration", enum)])
            except mfp_box.BoxError:
                pass
        return out

    def roots(self) -> list[dict]:
        infos = self._enumerate("create_box_enumeration", [("type", "USER"), ("range", KYO_RANGE)],
                                "get_box_information", "information", "destroy_box_enumeration")
        roots = []
        for inf in infos:
            num = _text(inf, "number")
            if not re.fullmatch(r"\d{1,9}", num):
                continue
            name = _text(inf, "name")[:80]
            owner = _text(inf, "owner")
            roots.append({"id": num, "label": f"{num} {name}".strip(), "shared": not owner,
                          "documents": _num(_text(inf, "document_count"))})
        return sorted(roots, key=lambda r: int(r["id"]))

    def _box(self, root: str) -> str:
        if not re.fullmatch(r"\d{1,9}", root or ""):
            raise ShareError("SHARE_ROOT_INVALID")
        return root

    def list(self, root: str, path: list[str]) -> list[dict]:
        box = self._box(root)
        if check_path(path):
            raise ShareError("SHARE_PATH_INVALID")      # 文件匣底下沒有資料夾
        docs = self._enumerate(
            "create_document_enumeration",
            [("identification_information", [("type", "USER"), ("number", box), ("password", self._box_password)]),
             ("range", KYO_RANGE)],
            "get_document_information", "document_information", "destroy_document_enumeration", box=True)
        out = []
        for d in docs:
            name = _text(d, "name")
            seg = enc_name(name) if name else None
            if not seg:
                continue
            out.append({"name": seg, "label": name[:160], "dir": False, "size": _num(_text(d, "data_size")),
                        "modified": _epoch(_text(d, "final_modify_date") or _text(d, "register_date")),
                        "pages": _num(_text(d, "page_count")), "type": _text(d, "form")[:40]})
        return sorted(out, key=lambda x: x["label"].lower())

    def display_name(self, root: str, path: list[str]) -> str:
        return dec_name(path[-1]) if path else ""

    def download(self, root: str, path: list[str], dest: Path, max_bytes: int) -> dict:
        box = self._box(root)
        path = check_path(path)
        if len(path) != 1:
            raise ShareError("SHARE_PATH_INVALID")
        name = dec_name(path[0])
        # 設備匯出 TIFF（全彩、不分頁）—— 研究文件：沒有「原始表示」可選，TIFF 不等於機內原件；PDF 由本站轉
        resp, result = self._call("data", "prepare_downloading_data", [
            ("identification_information", [("type", "USER"), ("indicator", [("box_number", box)]),
                                             ("password", self._box_password)]),
            ("file_name", name), ("type", "TIFF"), ("color_selection", "FULL_COLOR"), ("file_separation", "OFF")])
        self._check(result, "prepare_downloading_data", box=True)
        op = _text(resp, "operation_id")
        if not op:
            raise mfp_box.BoxError("DAV_BAD_RESPONSE", {"reason": "no operation_id"})
        parts: list[Path] = []
        total, waited, delay = 0, 0.0, 1.0
        for _ in range(MAX_ROUNDS):
            resp, result = self._call("data", "start_downloading_data", [("operation_id", op)],
                                      max_bytes=min(MAX_SOAP_DATA, max(MAX_SOAP, max_bytes * 4 // 3 + MAX_SOAP)))
            data_el = _first(resp, "binary_data")
            if result == "PREPARING_NOW" and (data_el is None or not (data_el.text or "").strip()):
                if waited >= BUSY_WAIT_LIMIT:
                    raise ShareError("SOAP_BUSY", {"waited_s": int(waited)})
                time.sleep(delay)
                waited += delay
                delay = min(delay * 2, 8.0)
                continue
            self._check(result, "start_downloading_data", box=True)
            if data_el is not None and (data_el.text or "").strip():
                try:
                    raw = base64.b64decode("".join((data_el.text or "").split()), validate=True)
                except ValueError:
                    raise mfp_box.BoxError("DAV_BAD_RESPONSE", {"reason": "bad base64"}) from None
                declared = _num(_text(resp, "data_size"))
                if declared is not None and declared != len(raw):
                    raise mfp_box.BoxError("DOC_INCOMPLETE", {"expected": declared, "got": len(raw)})
                if len(parts) >= MAX_PARTS:
                    raise ShareError("SHARE_TOO_MANY", {"limit": MAX_PARTS})
                p = dest if not parts else dest.parent / f"part{len(parts):03d}.bin"
                total += _write_part(p, raw, max_bytes - total)
                parts.append(p)
                if self.http.trace:
                    self.http.trace[-1]["part"] = {"bytes": len(raw), "file_type": _text(resp, "file_type")[:20]}
            if result == "ALL_GET_COMPLETE" or (result in ("", "SUCCESS") and data_el is None):
                break
            # 研究文件：多頁 / 分頁時同一個 operation_id 要不要重複呼叫還沒實機確認 —— 收到資料就再問一次，
            # 直到設備說 ALL_GET_COMPLETE 或不再給資料；每一次的內容各自存成一個部分，不串接
        if not parts:
            raise ShareError("DOC_NO_CONTENT")
        if dest not in parts:
            parts[0].replace(dest)
            parts[0] = dest
        out = {"bytes": total, "sha256": _sha256(dest)}
        if len(parts) > 1:
            out["parts"] = parts
        return out


def _fill(parent, ns: str, fields: list) -> None:
    for name, value in fields:
        el = SubElement(parent, f"{{{ns}}}{name}")
        if isinstance(value, list):
            _fill(el, ns, value)
        else:
            el.text = str(value)


# =========================================================================== RICOH

#: 已確認的下載方式（研究文件第 7 節）；FTP 沒有樣本，不做
_RICOH_SCHEMES = {"https": ["https+post", "https+get"], "http": ["http+post", "http+get"]}
_RICOH_ERRORS = {
    "COMMON_BAD_SESSION_ID": "SESSION_EXPIRED",
    "REPOSITORY_NEED_PASSWORD": "DOC_PASSWORD_REQUIRED",
    "REPOSITORY_ACCESS_DENIED": "SHARE_FORBIDDEN",
    "REPOSITORY_BAD_OBJECT_ID": "SHARE_FILE_GONE",
    "REPOSITORY_OBJECT_BUSY": "SOAP_BUSY",
    "COMMON_SYSTEM_BUSY": "SOAP_BUSY",
    "REPOSITORY_URL_SCHEME_NOT_FOUND": "RICOH_URL_UNSUPPORTED",
}


def _r(name: str, xtype: str, value: Any) -> tuple:
    return (name, xtype, value)


def _b64(v: bytes) -> str:
    return base64.b64encode(v).decode("ascii")


_GW_PREFIX = (b"abcdefghijklmnopqrstuvwxy" b"ABCDEFGHIJKLMNOPQRSTUVWXYZ" b"0123456789")


def ricoh_gwpwes003(password: str, prefix: Optional[bytes] = None) -> bytes:
    """HTTP 的 BASIC 密碼表示（`PES:Encoding=gwpwes003`，分析文件 2.4）。這是協定相容性表示，不是保護。

    15 個字元的隨機前綴（`a-y A-Z 0-9`）＋ `z` ＋ 密碼的 UTF-8，每個 byte 右旋 2 位再 XOR 0x72。
    結果含 0 時原廠會被 C 字串截斷 → 不送（不截短、不改密碼、不換格式重試）。`prefix` 只給測試向量用。
    """
    import secrets
    raw = password.encode("utf-8")
    if "\0" in password or len(raw) > RICOH_PW_MAX:
        raise ShareError("RICOH_PASSWORD_UNSUPPORTED", {"reason": "length or NUL"})
    if prefix is None:
        prefix = bytes(secrets.choice(_GW_PREFIX) for _ in range(15))
    if len(prefix) != 15 or any(b not in _GW_PREFIX for b in prefix):
        raise ValueError("invalid prefix")
    out = bytes(((((b >> 2) | (b << 6)) & 0xFF) ^ 0x72) for b in prefix + b"z" + raw)
    if 0 in out:
        raise ShareError("RICOH_PASSWORD_UNSUPPORTED", {"reason": "unrepresentable character"})
    return out


def ricoh_string_in(mode: str, *, protocol: int, transport: str, user: str = "", password: str = "",
                    user_code: str = "", _test_prefix: Optional[bytes] = None) -> str:
    """`startSession` 的 `stringIn`（分析文件第 2 節）。模式要已經選定；這裡不猜、不重試。"""
    if mode == "none":
        return ""
    if mode == "BASIC":
        if protocol < 200 or not user or "\0" in user:
            raise ShareError("RICOH_AUTH_PROFILE_UNSUPPORTED", {"mode": mode})
        uid = _b64(user.encode("utf-8"))
        if transport == "https":
            if "\0" in password:
                raise ShareError("RICOH_PASSWORD_UNSUPPORTED", {"reason": "NUL"})
            pwd, pes = _b64(password.encode("utf-8")), ""
        else:
            pwd, pes = _b64(ricoh_gwpwes003(password, _test_prefix)), "gwpwes003"
        return f"SCHEME={_RICOH_SCHEME_B64['BASIC']};UID:UserName={uid};PWD:Password={pwd};PES:Encoding={pes};"
    if mode in ("RGWUCD", "RGWEXTUCD"):
        if not (user_code.isascii() and user_code.isdigit()):
            raise ShareError("RICOH_USER_CODE_REQUIRED")
        code = _b64(user_code.encode("ascii"))
        if protocol < 200:
            if mode == "RGWEXTUCD":
                raise ShareError("RICOH_AUTH_PROFILE_UNSUPPORTED", {"mode": mode})
            return f"SCHEME={_RICOH_SCHEME_B64['BASIC']};UID=;PWD={code};"
        return f"SCHEME={_RICOH_SCHEME_B64[mode]};UCD:UserCode={code};"
    raise ShareError("RICOH_AUTH_MODE_REQUIRED", {"reason": "unknown mode"})


class RicohSession(Session):
    """`/DH/repository`。建立時：服務能力 → 帳密換票證 → 開工作階段；`close()` 結束工作階段。"""
    transport = "soap"
    ROOT = "documents"

    def __init__(self, route: str, host: str, ip: str, scheme: str, port: int, *, user: str, password: str,
                 doc_password: str, trust_sha256: Optional[str], allow_http: bool, trace: list,
                 auth_mode: str = "auto", user_code: str = ""):
        if auth_mode not in RICOH_AUTH_MODES:
            raise ShareError("RICOH_AUTH_MODE_REQUIRED", {"reason": "unknown mode"})
        self.route = route
        self.http = _SoapHttp(host, ip, scheme, port, paths=[RICOH_REPO_PATH], trust_sha256=trust_sha256,
                              allow_http=allow_http, trace=trace)
        self._doc_password = doc_password or ""
        self._sid = ""
        self._names: dict[str, str] = {}
        # 原廠元件的初始流程先查協定版本（研究文件第 2 節）。沒有這個方法的機型照樣往下走；整個服務不在就停
        try:
            ver, _o = self._repo("getProtocolVersion", [])
            self.protocol_version = str(ver or "").strip()[:20]
        except ShareError as e:
            if e.code == "SOAP_UNAVAILABLE":
                raise
            self.protocol_version = ""
        caps, cap_out = self._repo("getServiceCapability", [_r("sessionId", "string", "")])
        props = _props(caps)
        classes = cap_out.get("supportedClasses")
        classes = [str(c).strip() for c in classes] if isinstance(classes, list) else []
        #: 設備列出的功能類別；沒列（空的）就不限制 —— 研究文件：按實際回傳決定功能
        self.classes = [c for c in classes if c][:60]
        self.caps = {k: str(v)[:120] for k, v in props.items() if k in (
            "machineType", "moduleVersion", "repositoryType", "uauthenticationProviderName",
            "uauthenticationDomainName", "isUserAuthenticatableByTicket", "maxResultRowBlockSizeForDocIDList",
            "startSessionSchemeCandidates")}
        if self.protocol_version:
            self.caps["protocolVersion"] = self.protocol_version
        if self.http.trace:
            self.http.trace[-1]["capability"] = {k: v for k, v in self.caps.items()}
            self.http.trace[-1]["classes"] = list(self.classes)
        cand = str(props.get("startSessionSchemeCandidates") or "")
        #: 設備宣告的非票證登入模式；屬性不存在＝不知道（None），跟「空的」分開
        self.candidates: Optional[list[str]] = (
            [t for t in re.findall(r"[A-Z0-9_]+", cand.upper()) if t in _RICOH_SCHEME_B64]
            if "startSessionSchemeCandidates" in props else None)
        try:
            self.protocol_int: Optional[int] = int(self.protocol_version) if self.protocol_version else None
        except ValueError:
            self.protocol_int = None
        if self.http.trace:
            self.http.trace[-1]["login_candidates"] = self.candidates
        mode = self._choose_mode(auth_mode, user=user, password=password, user_code=user_code, props=props)
        self.auth_mode = mode
        if self.http.trace:
            self.http.trace[-1]["login_mode"] = mode
        if mode == "ticket":
            self._login_ticket(props, user, password)
        else:
            self._login_start_session(mode, user=user, password=password, user_code=user_code)

    # ---- 登入
    def _ticket_usable(self, props: dict) -> bool:
        return (self.supports("basic.startSessionByAuthTicket") and bool(props.get("uauthenticationUrl"))
                and str(props.get("isUserAuthenticatableByTicket", "true")).lower() != "false")

    def _choose_mode(self, want: str, *, user: str, password: str, user_code: str, props: dict) -> str:
        """選定**一種**登入方式（送任何登入資料之前）。回 `ticket` / `none` / `BASIC` / `RGWUCD` / `RGWEXTUCD`。"""
        has_ticket = self.supports("basic.startSessionByAuthTicket")
        has_start = "basic.startSession" in self.classes
        if not has_ticket and not has_start:
            raise ShareError("RICOH_AUTH_UNSUPPORTED", {
                "login": [c for c in self.classes if "startsession" in c.lower()] or ["-"],
                "capability": dict(self.caps)})
        account = bool(user)
        code = bool(user_code)
        if want == "auto":
            if account and code:
                raise ShareError("RICOH_AUTH_MODE_REQUIRED", {"reason": "account and user code both given"})
            if account:
                want = "account"
            elif code:
                subs = [m for m in ("RGWUCD", "RGWEXTUCD") if self.candidates and m in self.candidates]
                if len(subs) != 1:
                    raise ShareError("RICOH_AUTH_MODE_REQUIRED", {"reason": "user code type unknown",
                                                                  "candidates": self.candidates})
                want = "user_code" if subs[0] == "RGWUCD" else "user_code_ext"
            elif has_start:
                # 帳號、User Code 都空白：用「不需登入」試一次 —— 不送任何帳密，不會害帳號被鎖。
                # 分析文件 5.2 第 8 條提醒「帳號空白不等於免登入」：所以設備拒絕時不換別的方式重試，
                # 請使用者填帳號或 User Code；列出 0 份時畫面也會提醒可能要登入（客戶 2026-10-20：「自動判斷不夠自動」）
                self.auto_fallback_none = True
                want = "none"
            else:
                raise ShareError("AUTH_REQUIRED_USER", {"reason": "ricoh requires an account"})
        if want == "none":
            if account or password or code:
                raise ShareError("RICOH_AUTH_INPUT_CONFLICT")
            if not has_start:
                raise ShareError("RICOH_AUTH_MODE_MISMATCH", {"mode": "none", "classes": "no basic.startSession"})
            return "none"
        if want == "account":
            if code:
                raise ShareError("RICOH_AUTH_INPUT_CONFLICT", {"reason": "user code given"})
            if not account:
                raise ShareError("AUTH_REQUIRED_USER", {"reason": "ricoh requires an account"})
            if self._ticket_usable(props):
                return "ticket"
            if not has_start:
                raise ShareError("RICOH_AUTH_UNSUPPORTED", {"capability": dict(self.caps)})
            if self.candidates is not None and "BASIC" not in self.candidates:
                raise ShareError("RICOH_AUTH_MODE_MISMATCH", {"mode": "BASIC", "candidates": self.candidates})
            if self.protocol_int is None or self.protocol_int < 200:
                raise ShareError("RICOH_AUTH_PROFILE_UNSUPPORTED", {"mode": "BASIC",
                                                                    "protocol": self.protocol_version or "-"})
            return "BASIC"
        # User Code
        sub = "RGWUCD" if want == "user_code" else "RGWEXTUCD"
        if account or password:
            raise ShareError("RICOH_AUTH_INPUT_CONFLICT", {"reason": "account given"})
        if not (user_code.isascii() and user_code.isdigit()):
            raise ShareError("RICOH_USER_CODE_REQUIRED")
        if not has_start:
            raise ShareError("RICOH_AUTH_MODE_MISMATCH", {"mode": sub, "classes": "no basic.startSession"})
        if self.candidates is not None and sub not in self.candidates:
            raise ShareError("RICOH_AUTH_MODE_MISMATCH", {"mode": sub, "candidates": self.candidates})
        if self.protocol_int is None or (sub == "RGWEXTUCD" and self.protocol_int < 200):
            raise ShareError("RICOH_AUTH_PROFILE_UNSUPPORTED", {"mode": sub, "protocol": self.protocol_version or "-"})
        return sub

    def _login_ticket(self, props: dict, user: str, password: str) -> None:
        auth_url = str(props.get("uauthenticationUrl") or "")
        auth_path = self._own_path(auth_url, "RICOH_AUTH_URL_UNTRUSTED")
        self.http.allow(auth_path)
        name, dom = user, str(props.get("uauthenticationDomainName") or "")
        if "\\" in user and not dom:
            dom, _, name = user.partition("\\")
        ticket, _o = self._soap(auth_path, RICOH_AUTH3_NS, "authenticateByPassword", [
            _r("providerName", "string", str(props.get("uauthenticationProviderName") or "")),
            _r("domainName", "string", dom), _r("authName", "string", name), _r("password", "string", password),
            _r("duration", "int", RICOH_TIME_LIMIT), _r("options", "props", [])], auth=True)
        ticket = "".join(str(ticket or "").split())
        if not ticket:
            raise ShareError("AUTH_FAILED", {"call": "authenticateByPassword", "reason": "no ticket"})
        sid, _o = self._repo("startSessionByAuthTicket", [
            _r("authTicket", "base64Binary", ticket), _r("authenticationUrl", "string", auth_url),
            _r("timeLimit", "unsignedInt", RICOH_TIME_LIMIT)], auth=True)
        self._sid = str(sid or "").strip()
        if not self._sid:
            raise mfp_box.BoxError("DAV_BAD_RESPONSE", {"reason": "no session id"})

    def _login_start_session(self, mode: str, *, user: str, password: str, user_code: str) -> None:
        string_in = ricoh_string_in(mode, protocol=self.protocol_int or 0, transport=self.http.scheme,
                                    user=user, password=password, user_code=user_code)
        try:
            status, out = self._repo("startSession", [_r("stringIn", "string", string_in),
                                                      _r("timeLimit", "unsignedInt", RICOH_TIME_LIMIT)], auth=True)
        except ShareError as e:
            if e.code == "AUTH_FAILED":
                if mode == "none" and getattr(self, "auto_fallback_none", False):
                    raise ShareError("AUTH_REQUIRED_USER", {"reason": "device requires login", "mode": "none"}) from None
                raise ShareError("RICOH_SESSION_REJECTED", {"mode": mode, **(e.detail or {})}) from None
            raise
        finally:
            string_in = ""          # noqa: F841 —— 登入字串含認證資料，用完就丟
        status = str(status or "").strip()
        sid = out.get("stringOut")
        sid = sid.strip() if isinstance(sid, str) else ""
        if status != "OK":
            # 非 OK 也是失敗（不一定是 Fault）；狀態只記看起來像代碼的
            if mode == "none" and getattr(self, "auto_fallback_none", False):
                raise ShareError("AUTH_REQUIRED_USER", {"reason": "device requires login", "mode": "none",
                                 "status": status if re.fullmatch(r"[A-Z0-9_]{1,40}", status) else "-"})
            raise ShareError("RICOH_SESSION_REJECTED", {
                "mode": mode, "status": status if re.fullmatch(r"[A-Z0-9_]{1,40}", status) else "-"})
        if not sid or len(sid) > 512:
            raise mfp_box.BoxError("DAV_BAD_RESPONSE", {"reason": "no session id", "call": "startSession"})
        self._sid = sid

    def __repr__(self) -> str:
        return f"<RicohSession {self.route}>"

    def supports(self, cls: str) -> bool:
        return not self.classes or cls in self.classes

    def _need(self, cls: str) -> None:
        if not self.supports(cls):
            raise ShareError("RICOH_FEATURE_UNSUPPORTED", {"feature": cls, "classes": self.classes})

    def via(self) -> dict:
        return {"protocol": "SOAP", "scheme": self.http.scheme, "port": self.http.port}

    def tls(self) -> Optional[dict]:
        return dict(self.http.tls) if self.http.tls else None

    def _own_path(self, url: str, code: str) -> str:
        """設備回的網址 → 路徑。一定要是同一台設備、同一個協定與連接埠（不然不連）。"""
        try:
            u = urlsplit(str(url).strip())
            port = u.port or (443 if u.scheme == "https" else 80)
        except ValueError:
            raise ShareError(code, {"reason": "bad url"}) from None
        host = (u.hostname or "").lower()
        d = self.http
        if (u.scheme != d.scheme or port != d.port or host not in (d.host.lower(), d.ip)
                or u.username or u.password or u.fragment):
            raise ShareError(code, {"host_matches": host in (d.host.lower(), d.ip),
                                    "scheme": u.scheme[:10], "port": port})
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        if not _PATH_OK.match(path):
            raise ShareError(code, {"reason": "path"})
        return path

    # ---- SOAP 1.1 encoded
    def _envelope(self, ns: str, method: str, params: list) -> bytes:
        env = Element(f"{{{ENV11}}}Envelope")
        # 型別值裡用到的前綴（xsi:type="xsd:string"、enc:arrayType="ct:property[0]"）要手動宣告 ——
        # ElementTree 只替標籤與屬性名稱裡的命名空間宣告；xsi、enc 用在屬性名稱，它會自己宣告（前綴已註冊）
        for p, u in (("xsd", XSD), ("ct", RICOH_CT), ("rt", RICOH_RT)):
            env.set(f"xmlns:{p}", u)
        m = SubElement(SubElement(env, f"{{{ENV11}}}Body"), f"{{{ns}}}{method}")
        m.set(f"{{{ENV11}}}encodingStyle", ENC11)
        for name, xtype, value in params:
            _rparam(m, name, xtype, value)
        return tostring(env, encoding="utf-8", xml_declaration=True)

    def _soap(self, path: str, ns: str, method: str, params: list, *, auth: bool = False,
              to_file: Optional[Path] = None) -> tuple[Any, dict]:
        body = self._envelope(ns, method, params)
        r = self.http.post(path, body, "text/xml; charset=utf-8", label=method,
                           extra={"SOAPAction": f'"{ns}#{method}"'})
        if r["status"] in (404, 405, 501):
            raise ShareError("SOAP_UNAVAILABLE", {"status": r["status"], "call": method})
        if r["status"] in (301, 302, 307, 308):
            raise ShareError("DAV_REDIRECT_REFUSED", {"call": method})
        root = _parse(r.get("body") or b"", method)
        b = _body(root)
        f = _fault(b)
        if f:
            known = next((k for k in _RICOH_ERRORS if k in f["all"]), None)
            if self.http.trace:
                self.http.trace[-1]["fault"] = known or f["code"] or f["text"][:80]
            if auth:
                raise ShareError("AUTH_FAILED", {"call": method, "fault": known or f["code"]})
            raise ShareError(_RICOH_ERRORS[known] if known else "SOAP_FAULT",
                             {"call": method, "fault": known or f["code"], "reason": f["text"]})
        if r["status"] != 200:
            raise ShareError("DAV_UNEXPECTED_STATUS", {"status": r["status"], "call": method})
        return _rresult(b)

    def _repo(self, method: str, params: list, **kw) -> tuple[Any, dict]:
        return self._soap(RICOH_REPO_PATH, RICOH_REPO_NS, method, params, **kw)

    def close(self) -> None:
        if not self._sid:
            return
        try:
            self._repo("terminateSession", [_r("sessionId", "string", self._sid)])
        except mfp_box.BoxError:
            pass
        finally:
            self._sid = ""

    # ---- 列文件
    def roots(self) -> list[dict]:
        return [{"id": self.ROOT, "label": self.ROOT, "shared": True}]

    def _block(self) -> int:
        n = _num(self.caps.get("maxResultRowBlockSizeForDocIDList"))
        return max(1, min(n or 50, 50))

    def list(self, root: str, path: list[str]) -> list[dict]:
        if root != self.ROOT:
            raise ShareError("SHARE_ROOT_INVALID")
        if check_path(path):
            raise ShareError("SHARE_PATH_INVALID")
        self._need("query.advancedSearch")
        ids = self._search(deep=False)
        if not ids:
            # 文件伺服器的文件可能放在資料夾裡（客戶實機：「共用資料夾」）。原廠的一般查詢是最上層、不往下找；
            # 一份都沒有時再「連子資料夾一起找」一次。只是讀取（登入已經完成，跟帳密無關）；設備不接受就當空的
            try:
                ids = self._search(deep=True)
                if self.http.trace:
                    self.http.trace[-1]["deep_search"] = len(ids)
            except ShareError as e:
                if e.code in ("SESSION_EXPIRED", "SHARE_TOO_MANY"):
                    raise
                if self.http.trace:
                    self.http.trace[-1]["deep_search"] = "refused"
        entries = []
        for i in ids:
            seg = enc_name(i)
            if not seg:
                continue
            props, _o = self._repo("getProps", [_r("sessionId", "string", self._sid), _r("objectId", "string", i)])
            p = _props(props)
            other = _props(p.get("otherProps"))
            if not entries and self.http.trace:
                # 協定紀錄記「設備回了哪些屬性名稱」（只記名稱、不記內容）：實機的日期與大小
                # 不一定叫 modifiedDate / contentLength（客戶 IM C3510 顯示「—」），對照用
                self.http.trace[-1]["prop_names"] = sorted(str(k)[:40] for k in p)[:40]
                self.http.trace[-1]["other_prop_names"] = sorted(str(k)[:40] for k in other)[:40]
            title = str(p.get("title") or i)[:160]
            self._names[seg] = title
            size = next((_num(v) for v in (p.get("contentLength"), other.get("nf:contentLength"), p.get("size"),
                                           other.get("nf:size")) if _num(v) is not None), None)
            when = next((_epoch(v) for v in (p.get("modifiedDate"), p.get("createdDate"), other.get("nf:modifiedDate"),
                                             other.get("nf:createdDate"), other.get("nf:storedDate"))
                         if _epoch(v) is not None), None)
            entries.append({"name": seg, "label": title, "dir": False, "size": size,
                            "modified": when,
                            "type": str(other.get("nf:application") or "")[:20],
                            "locked": str(other.get("nf:passwordProtected", "")).lower() == "true"})
        return sorted(entries, key=lambda x: (-(x["modified"] or 0), x["label"].lower()))

    def _search(self, *, deep: bool) -> list[str]:
        ids: list[str] = []
        offset = 0
        for _ in range(MAX_ROUNDS):
            status, out = self._repo("executeAdvancedSearch", [
                _r("sessionId", "string", self._sid), _r("rowOffset", "unsignedInt", offset),
                _r("resultRowBlockSize", "unsignedInt", self._block()), _r("resultSetId", "string", ""),
                _r("fromFolderId", "string", ""), _r("deepSearch", "boolean", deep),
                _r("select", "strings", ["identifier"]), _r("from", "string", "document"),
                _r("whereAnd", "ops", []), _r("whereOr", "ops", []), _r("orderBy", "order", [])])
            rows = out.get("rowList")
            rows = rows if isinstance(rows, list) else ([] if rows in (None, "") else [rows])
            for i in _row_ids(rows):
                if i not in ids:
                    ids.append(i)
            if len(ids) > RICOH_MAX_DOCS:
                raise ShareError("SHARE_TOO_MANY", {"limit": RICOH_MAX_DOCS})
            offset += len(rows)
            if str(status or "").strip() == "QUERY_AT_END" or not rows:
                break
        return ids

    def display_name(self, root: str, path: list[str]) -> str:
        if not path:
            return ""
        if path[-1] in self._names:
            return self._names[path[-1]]
        try:
            props, _o = self._repo("getProps", [_r("sessionId", "string", self._sid),
                                                _r("objectId", "string", dec_name(path[-1]))])
            return str(_props(props).get("title") or dec_name(path[-1]))[:160]
        except mfp_box.BoxError:
            return dec_name(path[-1])

    # ---- 下載
    def download(self, root: str, path: list[str], dest: Path, max_bytes: int) -> dict:
        if root != self.ROOT:
            raise ShareError("SHARE_ROOT_INVALID")
        path = check_path(path)
        if len(path) != 1:
            raise ShareError("SHARE_PATH_INVALID")
        doc_id = dec_name(path[0])
        self._need("rendition")
        if self._doc_password:
            self._need("objectPassword")
            ok, _o = self._repo("checkObjectPassword", [_r("sessionId", "string", self._sid),
                                                        _r("objectId", "string", doc_id),
                                                        _r("password", "string", self._doc_password)])
            if str(ok).strip().lower() not in ("true", "1"):
                raise ShareError("DOC_PASSWORD_FAILED")
        try:
            rend, _o = self._repo("getDocRendition", [
                _r("sessionId", "string", self._sid), _r("docId", "string", doc_id),
                _r("renditionRequest", "rendition", [("nf:renditionType", "original", 0, False)])])
        except ShareError as e:
            if e.code in ("DOC_PASSWORD_REQUIRED", "SHARE_FORBIDDEN", "SHARE_FILE_GONE", "SESSION_EXPIRED", "SOAP_BUSY"):
                raise
            raise ShareError("RICOH_ORIGINAL_UNAVAILABLE", e.detail) from None
        rend_id = str(_props(rend).get("identifier") or "").strip()
        if not rend_id:
            raise ShareError("RICOH_ORIGINAL_UNAVAILABLE", {"reason": "no rendition id"})
        elements: list[str] = []
        start = 0
        for _ in range(MAX_ROUNDS if self.supports("contentElement") else 0):
            status, out = self._repo("getDocElementList", [
                _r("sessionId", "string", self._sid), _r("docId", "string", rend_id),
                _r("startIndex", "unsignedInt", start), _r("maximumResultItems", "unsignedInt", 100)])
            lists = out.get("propLists")
            lists = lists if isinstance(lists, list) else ([] if lists in (None, "") else [lists])
            got = [str(_props(x).get("identifier") or "").strip() for x in lists]
            got = [g for g in got if g]
            elements.extend(got)
            if len(elements) > MAX_PARTS:
                raise ShareError("SHARE_TOO_MANY", {"limit": MAX_PARTS})
            start += len(lists)
            if str(status or "").strip() != "TRUNCATED" or not lists:
                break
        if not elements:
            elements = [rend_id]        # 沒有列出內容元素：表示本身就是要下載的內容
        parts: list[Path] = []
        total = 0
        for n, eid in enumerate(elements):
            if "basic.getDocContentUrl2" in self.classes:
                # 分析文件 6.2：有這項能力就用 URL2；validDurationHint 照原廠的 0、只要 POST、options 空陣列
                url, out = self._repo("getDocContentUrl2", [
                    _r("sessionId", "string", self._sid), _r("docId", "string", eid),
                    _r("validDurationHint", "unsignedInt", 0),
                    _r("schemePreference", "strings", [self.http.scheme + "+post"]),
                    _r("options", "props", [])])
            else:
                url, out = self._repo("getDocContentUrl", [
                    _r("sessionId", "string", self._sid), _r("docId", "string", eid),
                    _r("validDurationHint", "unsignedInt", RICOH_URL_HINT),
                    _r("schemePreference", "strings", _RICOH_SCHEMES[self.http.scheme])])
            scheme = str(out.get("scheme") or "").strip().lower()
            if scheme not in _RICOH_SCHEMES[self.http.scheme]:
                raise ShareError("RICOH_URL_UNSUPPORTED", {"scheme": scheme[:20]})
            upath = self._own_path(str(url or ""), "RICOH_URL_UNSUPPORTED")
            self.http.allow(upath)
            p = dest if n == 0 else dest.parent / f"part{n:03d}.bin"
            left = max_bytes - total
            if scheme.endswith("+get"):
                r = self.http.request("GET", upath, to_file=p, max_bytes=left)
            else:
                body_b64 = _props(out.get("parameter")).get("body")
                try:
                    body = base64.b64decode("".join(str(body_b64 or "").split()), validate=True)
                except ValueError:
                    body = b""
                if not body:
                    raise mfp_box.BoxError("DAV_BAD_RESPONSE", {"reason": "post body missing"})
                r = self.http.post(upath, body, "text/xml;charset=utf-8", label="download", to_file=p,
                                   max_bytes=left)
            self.http._paths.discard(upath)          # 下載網址用完就丟
            if r["status"] != 200 or "bytes" not in r:
                p.unlink(missing_ok=True)
                raise ShareError("SHARE_FILE_GONE" if r["status"] == 404 else "DAV_UNEXPECTED_STATUS",
                                 {"status": r["status"], "content_type": r["headers"].get("content-type", "")[:60]})
            total += r["bytes"]
            parts.append(p)
        res = {"bytes": total, "sha256": _sha256(dest)}
        if len(parts) > 1:
            res["parts"] = parts
        return res


# ---- SOAP 1.1 encoded：參數與結果

def _typed(el, xtype: str) -> None:
    el.set(f"{{{XSI}}}type", xtype)


def _rparam(parent, name: str, xtype: str, value: Any) -> None:
    el = SubElement(parent, name)
    if xtype == "strings":
        el.set(f"{{{XSI}}}type", "enc:Array")
        el.set(f"{{{ENC11}}}arrayType", f"xsd:string[{len(value)}]")
        for v in value:
            it = SubElement(el, "item")
            _typed(it, "xsd:string")
            it.text = str(v)
    elif xtype in ("props", "ops", "order"):
        t = {"props": "ct:property", "ops": "rt:querySimpleOperator", "order": "rt:queryOrderBy"}[xtype]
        el.set(f"{{{XSI}}}type", "enc:Array")
        el.set(f"{{{ENC11}}}arrayType", f"{t}[{len(value)}]")
        for k, v in value:          # 目前只送空陣列；保留 property 的寫法
            it = SubElement(el, "item")
            _typed(it, t)
            n = SubElement(it, "propName"); _typed(n, "xsd:string"); n.text = str(k)
            vv = SubElement(it, "propVal"); _typed(vv, "xsd:string"); vv.text = str(v)
    elif xtype == "rendition":
        el.set(f"{{{XSI}}}type", "enc:Array")
        el.set(f"{{{ENC11}}}arrayType", f"rt:renditionPreference[{len(value)}]")
        for nm, val, pref, optional in value:
            it = SubElement(el, "item")
            _typed(it, "rt:renditionPreference")
            for fname, ftype, fval in (("name", "xsd:string", nm), ("value", "xsd:string", val),
                                       ("preference", "xsd:unsignedInt", pref),
                                       ("optional", "xsd:boolean", "true" if optional else "false")):
                f = SubElement(it, fname)
                _typed(f, ftype)
                f.text = str(fval)
    else:
        _typed(el, f"xsd:{xtype}")
        if isinstance(value, bool):
            el.text = "true" if value else "false"
        elif value not in (None, ""):
            el.text = str(value)


def _rdecode(el, ids: dict, depth: int = 0) -> Any:
    if depth > 12:
        return None
    href = el.get("href") or ""
    if href.startswith("#"):
        target = ids.get(href[1:])
        if target is None or target is el:
            return None
        el = target
    kids = list(el)
    if not kids:
        return (el.text or "").strip()
    if el.get(f"{{{ENC11}}}arrayType") is not None or all(_local(k.tag) == "item" for k in kids):
        return [_rdecode(k, ids, depth + 1) for k in kids]
    return {_local(k.tag): _rdecode(k, ids, depth + 1) for k in kids}


def _rresult(body) -> tuple[Any, dict]:
    """方法回應 → （第一個結果, 其餘具名 out 參數）。multiRef 用 id / href 解參照。"""
    ids = {el.get("id"): el for el in body.iter() if el.get("id")}
    resp = next((el for el in body if not el.get("id")), None)
    if resp is None:
        raise mfp_box.BoxError("DAV_BAD_RESPONSE", {"reason": "empty body"})
    kids = list(resp)
    if not kids:
        return None, {}
    first = _rdecode(kids[0], ids)
    return first, {_local(k.tag): _rdecode(k, ids) for k in kids[1:]}


def _props(v: Any) -> dict:
    """property 陣列（[{propName, propVal}…]）→ dict。已經是 dict 的照原樣（沒有 propName 的那種）。"""
    if isinstance(v, dict):
        if "propName" in v:
            return {str(v.get("propName")): v.get("propVal")}
        return v
    out: dict = {}
    if isinstance(v, list):
        for it in v:
            if isinstance(it, dict) and "propName" in it:
                out[str(it.get("propName"))] = it.get("propVal")
            elif isinstance(it, dict):
                out.update(it)
    return out


def _row_ids(rows: list) -> list[str]:
    out = []
    for row in rows:
        if isinstance(row, str):
            v = row
        else:
            v = _props(row).get("identifier")
        v = str(v or "").strip()
        if v:
            out.append(v)
    return out
