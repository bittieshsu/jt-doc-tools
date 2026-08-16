"""`X-Forwarded-Proto` 的解析要全站一致。

## 由來

外部掃描抓到 2FA cookie 少 `Secure`，v1.14.29 因此加了 `is_https_request()`，
並在說明裡寫「**所有 cookie 的 `secure` 都要走這裡**」。

範圍寫錯了 —— **逗號串是「協定判斷」的通病，不是「cookie」的通病**。
v1.14.31 的對抗式驗證發現同一輪只搬了一半的呼叫點，另外三處原封不動：

* `app/main.py` 的 HSTS：`XFP='https, http'` 時**整個 HSTS 標頭不發**，
  而同一份回應的 session cookie 卻正確帶了 `Secure` —— 同一個請求對
  「這是不是 HTTPS」給出兩個互相矛盾的答案。
* `sso_routes._public_base()`：產出 `'https, http://doc.example.com'`，
  直接成為 OIDC 的 `redirect_uri` 與 SAML 的 ACS URL → 與 IdP 註冊值比對
  失敗，**登入全掛**。
* `auth_routes._sso_logout_redirect()`：同一個形狀。

多層反向代理（本專案的 doc.jason.tools 就是 nginx → app）每一層各自附加一段，
`https, http` 是正常現象不是攻擊。
"""
from __future__ import annotations

import pytest


class _FakeRequest:
    """只帶 headers 與 url —— 這幾個函式用得到的就這些。"""

    def __init__(self, xfp: str = "", xfh: str = "", scheme: str = "http",
                 netloc: str = "doc.example.com"):
        h = {}
        if xfp:
            h["X-Forwarded-Proto"] = xfp
        if xfh:
            h["X-Forwarded-Host"] = xfh
        self.headers = h
        self.url = type("U", (), {"scheme": scheme, "netloc": netloc})()


#: (X-Forwarded-Proto, 期望的協定)
SCHEME_CASES = [
    ("https", "https"),
    ("https, http", "https"),        # 多層代理：最外層是 https
    ("https,http", "https"),         # 沒有空格
    (" https ", "https"),            # 前後空白
    ("HTTPS", "https"),              # 大小寫
    ("http, https", "http"),         # 最外層是 http —— 內層的 https 不算
    ("http", "http"),
    ("", "http"),                    # 沒有標頭時退回 request.url.scheme
    ("garbage", "http"),             # 認不得的值不可以當成 https
]


@pytest.mark.parametrize("xfp,want", SCHEME_CASES)
def test_forwarded_scheme(xfp, want):
    from app.core.client_ip import forwarded_scheme

    assert forwarded_scheme(_FakeRequest(xfp=xfp)) == want


@pytest.mark.parametrize("xfp,want", SCHEME_CASES)
def test_is_https_request_agrees(xfp, want):
    """cookie 那條路徑跟共用 helper 不可以有第二套答案。"""
    from app.web.auth_routes import is_https_request

    assert is_https_request(_FakeRequest(xfp=xfp)) is (want == "https")


@pytest.mark.parametrize("xfh,want", [
    ("a.example.com", "a.example.com"),
    ("a.example.com, b.example.com", "a.example.com"),
    (" a.example.com ", "a.example.com"),
    ("", "doc.example.com"),
])
def test_forwarded_host(xfh, want):
    from app.core.client_ip import forwarded_host

    assert forwarded_host(_FakeRequest(xfh=xfh)) == want


def test_public_base_never_contains_a_comma():
    """SSO 的對外網址不可以長出 `'https, http://...'` 這種東西。

    這個字串會直接成為 OIDC 的 `redirect_uri` 與 SAML 的 ACS URL。
    """
    from app.web.sso_routes import _public_base

    for xfp in ("https", "https, http", "https,http", " https "):
        got = _public_base(_FakeRequest(xfp=xfp))
        assert "," not in got, f"XFP={xfp!r} 產出壞掉的網址：{got!r}"
        assert got.startswith("https://"), got


def test_hsts_present_behind_multi_layer_proxy():
    """多層代理時 HSTS 不可以消失。

    同一份回應裡 cookie 帶了 `Secure` 卻沒有 HSTS，是自相矛盾的狀態。
    """
    import os

    os.environ.setdefault("JTDT_CSRF_DISABLE", "1")
    from fastapi.testclient import TestClient

    from app.main import app
    c = TestClient(app)

    for xfp in ("https", "https, http", "https,http", " https "):
        r = c.get("/healthz", headers={"X-Forwarded-Proto": xfp})
        assert "Strict-Transport-Security" in r.headers, f"XFP={xfp!r} 沒有 HSTS"

    # 對外是 http 時不可以發 HSTS（那會把使用者鎖在 https 上）
    r = c.get("/healthz", headers={"X-Forwarded-Proto": "http, https"})
    assert "Strict-Transport-Security" not in r.headers
