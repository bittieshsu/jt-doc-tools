"""`generated-inline.css` 裡不可以出現 JavaScript 運算式。

v1.12.30 把 1,371 個 inline `style="…"` 搬進這個檔案（CSP 移除 unsafe-inline）。
其中**動態算出來的**樣式被原封不動貼了進去，例如：

    [data-s="s3ea515db"]{background:' + avatarBg + ';color:#fff; …}

那是無效的 CSS 宣告 —— 整條 `background` 被瀏覽器丟掉，於是帳號卡片的大頭像
變成**白底白字**，畫面上就是一塊空白（使用者回報「帳號左邊留空是大頭照嗎？」）。
**沒有任何錯誤訊息**，CSS 的錯誤復原是靜悄悄的。

動態樣式的正確做法是**依狀態給 class**（`av-ldap` / `is-ok` …），值寫在 CSS 裡。
"""
from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "static" / "css" / "generated-inline.css"
#: **掃描前一定要去掉註解** —— 本專案已經有好幾次守門測試把「說明裡引用的
#: 反例」當成真的違規（migration 的 FK 順序、fail-open 形狀都踩過）。
_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_JS_EXPR = re.compile(r"'\s*\+|\+\s*'|\$\{")


def test_no_javascript_expressions_in_generated_css():
    body = _COMMENT.sub("", CSS.read_text(encoding="utf-8"))
    bad = [ln.strip() for ln in body.splitlines() if _JS_EXPR.search(ln)]
    assert bad == [], (
        "這些規則把 JS 運算式當成 CSS 值，整條宣告會被丟掉：\n" + "\n".join(bad[:5]))


def test_every_rule_has_balanced_braces_and_a_value():
    """每條宣告都要有值 —— `background:;` 這種空值也是靜悄悄地失效。"""
    body = _COMMENT.sub("", CSS.read_text(encoding="utf-8"))
    assert body.count("{") == body.count("}"), "大括號沒配對"
    empty = re.findall(r"[\w-]+\s*:\s*(?=[;}])", body)
    assert empty == [], f"有空值的宣告：{empty[:5]}"
