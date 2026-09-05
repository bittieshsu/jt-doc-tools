"""介面語言切換端點 `/ui-locale` 的安全性（開放重導）。"""


def test_ui_locale_next_rejects_backslash_open_redirect():
    """`/\\evil.com` —— 瀏覽器會把反斜線正規化成斜線，變成協定相對網址。

    原本 `/ui-locale` 自己寫了一份「開頭是 / 且不是 //」的判斷，這一種就漏掉了
    （CodeQL #169 也指著同一行）。改成共用登入那份 `safe_next`。
    """
    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        for bad in ("/\\evil.com", "//evil.com", "https://evil.com",
                    "/%5cevil.com", "/\r\nSet-Cookie: x=1"):
            r = c.post("/ui-locale", data={"locale": "en", "next": bad},
                       follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"] == "/", f"{bad!r} 沒被擋掉"
        r = c.post("/ui-locale", data={"locale": "en", "next": "/admin/sso"},
                   follow_redirects=False)
        assert r.headers["location"] == "/admin/sso", "站內網址不可以被誤擋"
