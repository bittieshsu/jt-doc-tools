"""事務機掃描取件：診斷資訊的遮蔽規則（**只有這一份**）。

畫面上的診斷步驟、「下載診斷報告」、服務紀錄檔三個地方都經過這裡 —— 規則寫兩份
遲早會不一致，而遮漏一次就是把設備的 Cookie 或密碼交給看報告的人。

遮蔽的東西：

* **標頭**：`Authorization`、`Proxy-Authorization`、`Cookie` 整個值；`Set-Cookie` 保留
  名稱與屬性（`Secure`、`HttpOnly`、`Path` 對除錯有用），只遮值；CSRF / XSRF / 驗證權杖類標頭。
* **網址參數**：名稱像密碼、權杖、工作階段的參數值（`?session=…`、`&passwd=…`）。

遮蔽後寫成「［已遮蔽，長度 N］」：看得出「有帶值、多長」，看不到內容。
"""
from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit, urlunsplit, quote

#: 整個值都遮的標頭（小寫比對）。
_SECRET_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie",
    "x-csrf-token", "x-xsrf-token", "x-auth-token", "x-api-key", "x-session-id",
})

#: 參數名稱裡出現這些字就遮值（比對時忽略大小寫與底線、連字號）。
_SECRET_PARAM_HINTS = (
    "pass", "pwd", "pin", "token", "csrf", "xsrf", "session", "sessid", "sid",
    "auth", "secret", "key", "ticket", "cookie", "credential", "otp",
)


_MASK_PREFIX = "［已遮蔽"


def mask(value: Any) -> str:
    """**重複套用不變**：已經是遮蔽標記的就原樣回傳。

    報告會先在各步驟遮一次（標頭），最後整份再遮一次；沒有這條的話第二次會把
    「［已遮蔽，長度 6］」當成值再遮，變成一串巢狀的標記（實際在畫面上看到過）。"""
    s = str(value or "")
    if s.startswith(_MASK_PREFIX):
        return s
    return f"［已遮蔽，長度 {len(s)}］"


def is_secret_param(name: str) -> bool:
    k = re.sub(r"[_\-\s]", "", (name or "").lower())
    if not k:
        return False
    if k == "sid" or k.endswith("sid"):
        return True
    return any(h in k for h in _SECRET_PARAM_HINTS if h != "sid")


def redact_url(url: str) -> str:
    """網址裡的帳密（`user:pass@`）與敏感參數值遮掉，其餘照原樣。"""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return mask(url)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "［已遮蔽］@" + netloc.rsplit("@", 1)[1]
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        query = "&".join(
            f"{quote(k, safe='')}={mask(v) if is_secret_param(k) else (v if v.startswith(_MASK_PREFIX) else quote(v, safe='/:'))}"
            for k, v in pairs)
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def redact_set_cookie(value: str) -> str:
    """`NAME=VALUE; Path=/; HttpOnly` → `NAME=［已遮蔽，長度 N］; Path=/; HttpOnly`。"""
    head, sep, rest = (value or "").partition(";")
    name, eq, val = head.partition("=")
    if not eq:
        return mask(value)
    return f"{name.strip()}={mask(val.strip())}{sep}{rest}"


def redact_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for k, v in headers:
        lk = (k or "").lower()
        if lk == "set-cookie":
            out.append((k, redact_set_cookie(v)))
        elif lk in _SECRET_HEADERS or is_secret_param(lk.replace("x-", "", 1)) and lk.startswith("x-"):
            out.append((k, mask(v)))
        elif lk in ("location", "content-location", "refresh"):
            out.append((k, _redact_url_in_text(v)))
        else:
            out.append((k, v))
    return out


_URL_IN_TEXT = re.compile(r"""(?i)(https?://[^\s"'<>]+|/[^\s"'<>]*\?[^\s"'<>]+)""")


def _redact_url_in_text(text: str) -> str:
    return _URL_IN_TEXT.sub(lambda m: redact_url(m.group(1)), text or "")


#: 頁面文字裡「名稱=值」或 JSON 形式的敏感值（保險用：取件流程不該把它們放進摘要，
#: 但設備回的頁面內容我們管不到）。
_KV_IN_TEXT = re.compile(
    r"""(?ix)
    (?P<k>["']?[a-z_\-]*(?:pass(?:word|wd)?|pwd|token|csrf|xsrf|session(?:id)?|secret)[a-z_\-]*["']?)
    (?P<sep>\s*[:=]\s*)
    (?!["']?［已遮蔽)
    (?P<v>"[^"]{1,512}"|'[^']{1,512}'|[^\s&;,"'<>]{1,512})
    """)


def redact_text(text: str) -> str:
    """一段任意文字（頁面摘要、錯誤說明）：網址參數與「名稱=值」形式的敏感值都遮掉。"""
    text = _redact_url_in_text(text or "")
    return _KV_IN_TEXT.sub(lambda m: f"{m.group('k')}{m.group('sep')}{mask(m.group('v').strip(chr(34) + chr(39)))}", text)


def redact_obj(obj: Any) -> Any:
    """遞迴處理一份診斷資料（dict / list / str）。鍵名像敏感欄位的整個值遮掉。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            # 只遮**字串值**：`password_inputs: 1` 這種計數是除錯要看的，不是秘密
            if isinstance(k, str) and is_secret_param(k) and isinstance(v, str) and v:
                out[k] = mask(v)
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    if isinstance(obj, tuple):
        return [redact_obj(x) for x in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj
