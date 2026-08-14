"""刪除 cookie 的回應也要帶安全旗標。

## 由來（外部資安掃描 2026-08-13）

掃描器在 `/logout` 抓到：

    Set-Cookie: jtdt_session=""; expires=...; Max-Age=0; Path=/; SameSite=...

**沒有 `HttpOnly`、沒有 `Secure`**。建立 cookie 時明明都設了 —— 問題在於
`Max-Age=0` / `expires` 這種「刪除用」的 Set-Cookie **不會自動沿用建立當時的
旗標**，得再寫一次。少了它們，那筆刪除回應本身就是一個可被 JS 讀取、
可經明文 HTTP 送出的 cookie 標頭。

## 這裡也守著一個容易改壞的地方

**HTTP 連線時不可以加 `Secure`** —— 加了瀏覽器會忽略那筆刪除指令，
使用者按了登出卻沒真的登出，比原本的問題更嚴重。所以旗標要看連線協定，
不能無條件寫死 `secure=True`。
"""
from __future__ import annotations

import re

import pytest


def _set_cookies(resp) -> list[str]:
    raw = resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") \
        else [resp.headers.get("set-cookie", "")]
    return [c for c in raw if c]


def _session_cookie(resp) -> str:
    for c in _set_cookies(resp):
        if c.split("=", 1)[0].strip() == "jtdt_session":
            return c
    return ""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("JTDT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JTDT_CSRF_DISABLE", "1")
    from fastapi.testclient import TestClient

    import app.main as main
    return TestClient(main.app)


def test_logout_delete_cookie_is_httponly(client):
    """刪除回應少了 HttpOnly，那筆 Set-Cookie 就是 JS 讀得到的。"""
    r = client.get("/logout", follow_redirects=False)
    c = _session_cookie(r)
    if not c:
        pytest.skip("這個部署沒有送出 session cookie 刪除標頭")
    assert re.search(r"\bHttpOnly\b", c, re.I), f"刪除 cookie 少了 HttpOnly：{c}"


def test_logout_delete_cookie_has_path_and_samesite(client):
    """Path 要與建立時一致，否則瀏覽器會留著原本那個 cookie。"""
    r = client.get("/logout", follow_redirects=False)
    c = _session_cookie(r)
    if not c:
        pytest.skip("這個部署沒有送出 session cookie 刪除標頭")
    assert "Path=/" in c, f"刪除 cookie 的 Path 不是 /：{c}"
    assert re.search(r"SameSite=", c, re.I), f"刪除 cookie 少了 SameSite：{c}"


def test_logout_adds_secure_behind_https_proxy(client):
    """反向代理是 HTTPS 時，刪除回應要帶 Secure。"""
    r = client.get("/logout", headers={"X-Forwarded-Proto": "https"},
                   follow_redirects=False)
    c = _session_cookie(r)
    if not c:
        pytest.skip("這個部署沒有送出 session cookie 刪除標頭")
    assert re.search(r"\bSecure\b", c), f"HTTPS 下刪除 cookie 少了 Secure：{c}"


def test_logout_omits_secure_on_plain_http(client):
    """**HTTP 下不可以加 Secure。**

    加了瀏覽器會忽略這筆刪除指令 —— 使用者按了登出卻沒真的登出，
    比原本少一個旗標更嚴重。
    """
    r = client.get("/logout", follow_redirects=False)
    c = _session_cookie(r)
    if not c:
        pytest.skip("這個部署沒有送出 session cookie 刪除標頭")
    assert not re.search(r"\bSecure\b", c), (
        f"純 HTTP 卻加了 Secure，這筆刪除會被瀏覽器忽略：{c}")


def test_no_delete_cookie_without_flags_in_source():
    """靜態把關：新寫的 `delete_cookie` 不可以漏掉旗標。

    這是最容易復發的地方 —— 加一個新的登出路徑時很自然就只寫
    `delete_cookie(name, path="/")`，而那正是這次被掃出來的形狀。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    bad = []
    for f in root.rglob("*.py"):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "delete_cookie"):
                continue
            kw = {k.arg for k in node.keywords}
            if "httponly" not in kw:
                bad.append(f"{f.relative_to(root.parent)}:{node.lineno}")
    assert not bad, (
        "這些 delete_cookie 沒有帶 httponly（刪除回應不會沿用建立時的旗標）：\n"
        + "\n".join(bad))


# ---------------------------------------------- 建立時的旗標（不只刪除）

def test_set_cookie_calls_all_pass_secure():
    """靜態把關：`set_cookie` 一律要傳 `secure`。

    外部掃描抓到 2FA 待驗證 cookie 建立時沒有 `secure` —— 它裝的是待驗證
    權杖，少了這個旗標會經明文送出，比刪除時漏旗標嚴重。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    bad = []
    for f in root.rglob("*.py"):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "set_cookie"):
                continue
            if "secure" not in {k.arg for k in node.keywords}:
                bad.append(f"{f.relative_to(root.parent)}:{node.lineno}")
    assert not bad, (
        "這些 set_cookie 沒有傳 secure（HTTPS 下會經明文送出）：\n" + "\n".join(bad))


def test_https_detection_handles_proxy_chain():
    """多層代理時 `X-Forwarded-Proto` 會是逗號串，要取**最外層**那一段。

    只比對整個字串的話，`https, http` 會被判成不是 HTTPS，
    對外明明是 HTTPS 卻不加 `Secure`。
    """
    from unittest.mock import Mock

    from app.web.auth_routes import is_https_request

    def _req(scheme: str, fwd: str | None):
        r = Mock()
        r.url.scheme = scheme
        r.headers = {"X-Forwarded-Proto": fwd} if fwd else {}
        r.headers = type("H", (), {"get": lambda self, k, d="": (
            {"X-Forwarded-Proto": fwd} if fwd else {}).get(k, d)})()
        return r

    assert is_https_request(_req("https", None)) is True
    assert is_https_request(_req("http", "https")) is True
    assert is_https_request(_req("http", "https, http")) is True   # 多層代理
    assert is_https_request(_req("http", "http")) is False
    assert is_https_request(_req("http", None)) is False
