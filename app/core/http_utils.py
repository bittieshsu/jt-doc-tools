"""HTTP helpers — small, dependency-free utilities for FastAPI/Starlette.

Currently just :func:`content_disposition` for handling CJK filenames in
attachment headers.
"""
from __future__ import annotations

import re
from urllib.parse import quote


def content_disposition(filename: str, disposition: str = "attachment") -> str:
    """Build a `Content-Disposition` header value safe for any filename.

    HTTP headers are encoded as latin-1 by Starlette, so a raw CJK filename
    in ``filename="..."`` raises UnicodeEncodeError. RFC 5987 lets us add
    ``filename*=UTF-8''<percent-encoded>`` which modern browsers honour.

    We emit BOTH parameters: an ASCII-safe ``filename=`` for ancient clients
    plus the percent-encoded ``filename*=`` for the rest.

    Starlette's :class:`FileResponse(filename=...)` already does this dance
    internally — only use this helper when manually constructing headers
    for :class:`Response` / :class:`StreamingResponse`.
    """
    ascii_safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename).strip("_") or "download"
    encoded = quote(filename, safe="")
    return f'{disposition}; filename="{ascii_safe}"; filename*=UTF-8\'\'{encoded}'


def browser_origin(headers) -> str:
    """使用者瀏覽器所在的網址（`https://doc.example.com` 這種只到主機與連接埠的形式）。

    給通知信組「我的作業」的連結用：伺服器自己不知道對外網址（可能經反向代理、
    也可能是內網 IP），但**送出作業的那個請求**，瀏覽器會在 `Origin` 標頭寫上它
    當時用的網址 —— 那正是這位使用者連得回來的網址。沒有 `Origin` 時看 `Referer`。

    只收 http / https、有主機名、不帶帳密；路徑一律丟掉。拿不到就回空字串（呼叫端
    就不放連結）。這個值只拿來組寄給**送出者本人**的連結，不拿來做任何判斷。
    """
    from urllib.parse import urlsplit
    for name in ("origin", "referer"):
        raw = (headers.get(name) or "").strip()
        if not raw or raw == "null" or len(raw) > 2000:
            continue
        try:
            u = urlsplit(raw)
            port = u.port            # 連接埠不是數字時會丟 ValueError
        except ValueError:
            continue
        if u.scheme not in ("http", "https") or not u.hostname or "@" in u.netloc:
            continue
        if not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", u.netloc):
            continue
        host = u.hostname if ":" not in u.hostname else f"[{u.hostname}]"
        return f"{u.scheme}://{host}" + (f":{port}" if port else "")
    return ""
