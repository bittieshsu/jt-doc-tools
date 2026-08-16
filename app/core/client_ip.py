"""Canonical end-user IP resolution for AUDIT / HISTORY / DISPLAY.

Single source of truth so every place that records "who did this from where"
agrees. Honours ``X-Forwarded-For`` (left-most hop = original client) set by a
trusted reverse proxy, falling back to the transport peer.

Why this module exists: uvicorn is started with ``proxy_headers=False`` (so the
proxy-SSO trust decision can rely on the *real* transport peer, not a spoofable
header — see ``proxy_sso``). That means ``request.client.host`` is the nginx
peer (``127.0.0.1``) under a reverse proxy, NOT the workstation. Audit/history
must therefore read XFF themselves. Regression history: v1.12.61 turned off
proxy_headers and every site still using ``request.client.host`` started logging
``127.0.0.1`` (fixed in v1.12.65 by routing them through here).

SECURITY: never use this for a trust/authorisation decision. When the app is
reachable without going through the proxy, a client can forge X-Forwarded-For.
For trust decisions use the raw ``request.client.host`` (``proxy_sso._client_ip``).
Operators must configure the reverse proxy to strip inbound XFF and set its own.
"""
from __future__ import annotations


def real_client_ip(request) -> str:
    """Best-effort end-user IP for audit / history / display (max 64 chars)."""
    try:
        xff = request.headers.get("X-Forwarded-For", "") or ""
    except Exception:
        xff = ""
    if xff:
        # left-most is the original client (per convention)
        return xff.split(",", 1)[0].strip()[:64]
    try:
        return (request.client.host if getattr(request, "client", None) else "")[:64]
    except Exception:
        return ""


def forwarded_scheme(request) -> str:
    """對外的協定（`https` / `http`）。

    **`X-Forwarded-Proto` 可能是逗號串**：多層反向代理時每一層各自附加，
    變成 `https, http`。取**第一段**才是最外層面對使用者的那一段。

    這個判斷原本散在四個地方各寫一份，v1.14.29 只修了 cookie 那一份，
    另外三個沒跟著改（v1.14.31 對抗式驗證抓到）。後果不只是少個旗標：

    * `app/main.py` 的 HSTS：`XFP='https, http'` 時**整個 HSTS 標頭不發**，
      而同一份回應的 session cookie 卻正確帶了 `Secure` —— 同一個請求對
      「這是不是 HTTPS」給出兩個互相矛盾的答案。
    * `sso_routes._public_base()`：產出 `'https, http://doc.example.com'`
      這種壞掉的字串，直接成為 OIDC 的 `redirect_uri` 與 SAML 的 ACS URL
      → 與 IdP 註冊值比對失敗，**登入全掛**。

    前後空白也要吃掉（`' https '` 原本一樣失效）。
    """
    try:
        fwd = request.headers.get("X-Forwarded-Proto", "") or ""
    except Exception:  # noqa: BLE001
        fwd = ""
    first = fwd.split(",")[0].strip().lower()
    if first in ("http", "https"):
        return first
    try:
        return (request.url.scheme or "http").lower()
    except Exception:  # noqa: BLE001
        return "http"


def forwarded_host(request) -> str:
    """對外的主機名。`X-Forwarded-Host` 同樣可能是逗號串。"""
    try:
        h = request.headers.get("X-Forwarded-Host", "") or ""
    except Exception:  # noqa: BLE001
        h = ""
    first = h.split(",")[0].strip()
    if first:
        return first
    try:
        return request.url.netloc
    except Exception:  # noqa: BLE001
        return ""
