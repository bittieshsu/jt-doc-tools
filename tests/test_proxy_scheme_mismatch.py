"""代理宣稱的協定 ≠ 瀏覽器實際的協定（客戶回報，v1.15.26）。

客戶照文件把 IIS 的 `web.config` 寫死 `X-Forwarded-Proto: https`，站台卻只有
http → cookie 全被瀏覽器丟掉 → 遠端使用者一上傳就「CSRF token 遺失或不正確」，
**而在伺服器本機（localhost 是安全來源例外）怎麼測都正常**。

這裡守兩件事：①後端說得出原因 ②文件不再叫人寫死 https。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.core.proxy_scheme import (browser_scheme, forwarded_scheme,
                                   secure_cookie_mismatch)
from tools.repo_paths import public_root

OPS = public_root(Path(__file__).resolve().parents[1]) / "OPS.md"


def _scope(**hdrs):
    return {"headers": [(k.encode(), v.encode()) for k, v in hdrs.items()]}


def test_proxy_claiming_https_while_the_browser_is_on_http_is_reported():
    hint = secure_cookie_mismatch(_scope(**{
        "x-forwarded-proto": "https", "origin": "http://pdf.example.com"}))
    assert hint, "代理說 https、瀏覽器是 http —— 這正是客戶那台的情況，必須認得出來"
    assert "Secure" in hint and "http" in hint
    # 要說得出「本機測不出來」，否則管理員會用 localhost 驗證然後回報「我這裡正常」
    assert "localhost" in hint


def test_referer_is_used_when_there_is_no_origin():
    # 原生表單送出（例如登入）不一定帶 Origin，但會帶 Referer
    assert secure_cookie_mismatch(_scope(**{
        "x-forwarded-proto": "https", "referer": "http://pdf.example.com/login"}))


def test_a_correctly_configured_proxy_is_not_reported():
    assert secure_cookie_mismatch(_scope(**{
        "x-forwarded-proto": "https", "origin": "https://pdf.example.com"})) is None
    assert secure_cookie_mismatch(_scope(origin="http://pdf.example.com")) is None
    assert secure_cookie_mismatch(_scope()) is None


def test_the_reverse_direction_is_not_an_error():
    """代理說 http、瀏覽器其實是 https —— cookie 少了 Secure，功能照常。

    那是硬化建議不是故障，不可以拿故障訊息去嚇人。
    """
    assert secure_cookie_mismatch(_scope(**{
        "x-forwarded-proto": "http", "origin": "https://pdf.example.com"})) is None


def test_scheme_helpers_read_the_left_most_value():
    assert forwarded_scheme(_scope(**{"x-forwarded-proto": "https, http"})) == "https"
    assert browser_scheme(_scope(origin="https://a.example")) == "https"
    assert browser_scheme(_scope()) == ""


def test_the_csrf_rejection_explains_the_cause():
    """403 的內容要帶上原因 —— 只回「token 遺失或不正確」管理員無從查起。"""
    import asyncio

    from app.core import csrf

    sent = []

    async def send(msg):
        sent.append(msg)

    scope = _scope(**{"x-forwarded-proto": "https", "origin": "http://pdf.example.com"})
    asyncio.run(csrf._reject(send, scope))
    body = b"".join(m.get("body", b"") for m in sent).decode("utf-8")
    assert sent[0]["status"] == 403
    assert "CSRF token 遺失或不正確" in body
    assert "X-Forwarded-Proto" in body, "要說出是哪個標頭設錯了"


def test_the_rejection_stays_short_when_the_proxy_is_fine():
    import asyncio

    from app.core import csrf

    sent = []

    async def send(msg):
        sent.append(msg)

    asyncio.run(csrf._reject(send, _scope(origin="https://pdf.example.com")))
    body = b"".join(m.get("body", b"") for m in sent).decode("utf-8")
    assert "X-Forwarded-Proto" not in body


def test_the_iis_example_does_not_hard_code_https():
    """OPS.md 的 IIS 範例就是客戶照抄的那一份 —— 寫死 https 是這次的根因。"""
    text = OPS.read_text(encoding="utf-8")
    iis = text.split("### IIS")[1].split("\n### ")[0]
    sets = re.findall(r'<set\s+name="HTTP_X_FORWARDED_PROTO"\s+value="([^"]*)"', iis)
    assert sets, "IIS 範例要設 HTTP_X_FORWARDED_PROTO"
    for val in sets:
        assert val.lower() != "https", "不可以寫死 https，要依 {HTTPS} 實際帶值"
    assert "{HTTPS}" in iis, "要用 IIS 內建的 {HTTPS} 判斷這個連線實際是什麼協定"


def test_the_shared_requirements_warn_about_the_localhost_exception():
    """共通要求那一節要講出「本機測不出來」，否則沒有人會想到去查代理。"""
    text = OPS.read_text(encoding="utf-8")
    block = text.split("都必須做到這 7 點")[1].split("### nginx")[0]
    assert "localhost" in block
    assert re.search(r"Secure", block)
