"""每一個 cookie 的 `secure` 旗標都要走同一支判斷。

`auth_routes.is_https_request()` 的說明早就寫著「**所有 cookie 的 `secure`
都要走這裡**」—— 但**沒有檢查**，於是 `/ui-locale` 用的是
`request.url.scheme == "https"`。本專案關掉了 uvicorn 的 `proxy_headers`
（見 memory `client_ip_via_helper_not_client_host`），所以反向代理後面
`request.url.scheme` **永遠是 http** → https 站台上那個 cookie 沒有 Secure。

2026-09-14 ZAP 才第一次抓到：加了日文之後語言切換變成一組連結，爬蟲
因此第一次 POST 到 `/ui-locale`。**在那之前那條路徑從來沒被掃到過** ——
「掃出 0 條」跟「沒問題」是兩件事。

判準走 **AST**：找 `set_cookie(...)` 的 `secure=` 關鍵字，值必須是呼叫
`is_https_request(...)`，或是一個**由它算出來的區域變數**。
字串比對會被註解與說明騙過去（本專案踩過很多次）。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"

#: 這個名字的區域變數必須是 `is_https_request(...)` 的結果（下面會驗）。
_ALLOWED_NAMES = {"is_https"}


def _is_cookie_call(node: ast.AST) -> bool:
    """`resp.set_cookie(...)` 或 `resp.delete_cookie(...)`。"""
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("set_cookie", "delete_cookie"))


def _is_helper_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "is_https_request")


def _files() -> list[Path]:
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_scan_actually_reaches_some_set_cookie_calls():
    """**先證明掃得到東西** —— 掃 0 個呼叫跟全部合格在 pytest 輸出裡一樣。"""
    found = 0
    for p in _files():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if _is_cookie_call(node):
                found += 1
    # **刪 cookie 也要帶旗標**（`Max-Age=0` 不會沿用建立時的 flags），
    # 所以 `delete_cookie` 一起收。
    assert found >= 6, f"只掃到 {found} 個 set/delete_cookie —— 掃描器大概壞了"


def test_every_cookie_secure_flag_uses_the_shared_helper():
    bad: list[str] = []
    for p in _files():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not _is_cookie_call(node):
                continue
            for kw in node.keywords:
                if kw.arg != "secure":
                    continue
                ok = (_is_helper_call(kw.value)
                      or (isinstance(kw.value, ast.Name)
                          and kw.value.id in _ALLOWED_NAMES))
                if not ok:
                    bad.append(f"{p.relative_to(ROOT).as_posix()}:{node.lineno} "
                               f"secure={ast.unparse(kw.value)}")
    assert not bad, (
        "cookie 的 secure 沒有走 is_https_request()：\n" + "\n".join(bad)
        + "\n反向代理後面 request.url.scheme 永遠是 http —— "
          "https 站台上那個 cookie 會少掉 Secure。")


def test_the_allowed_local_names_really_come_from_the_helper():
    """豁免的區域變數名必須真的是 `is_https_request(...)` 算出來的。

    只驗前半段的話，有人把 `is_https = False` 寫死也會全綠 —— 而那正是
    「豁免清單自己變成洞」的樣子。
    """
    assigned: dict[str, list[str]] = {n: [] for n in _ALLOWED_NAMES}
    for p in _files():
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src)
        # 只看**真的拿那個名字當 cookie 旗標**的檔案 —— `csrf.py` 是
        # pure-ASGI 中介層（沒有 `Request` 物件，自己寫死頭），它裡面同名的
        # 區域變數跟這條規則無關，掃進來只會製造假警報。
        used = {kw.value.id
                for n2 in ast.walk(tree) if _is_cookie_call(n2)
                for kw in n2.keywords
                if kw.arg == "secure" and isinstance(kw.value, ast.Name)}
        if not used:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in used:
                    assigned[tgt.id].append(
                        f"{p.relative_to(ROOT).as_posix()}:{node.lineno} "
                        f"= {ast.unparse(node.value)}")
    for name, places in assigned.items():
        assert places, f"豁免的變數名 `{name}` 在 app/ 裡根本沒有人指派 —— 清單過期了"
        for place in places:
            assert "is_https_request(" in place, (
                f"`{name}` 不是由 is_https_request() 算出來的：{place}")
