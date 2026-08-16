"""每一支模板都要**渲染得起來**，而且註解裡不可以寫出樣板標籤的字面寫法。

## 由來

v1.14.31 我在 `admin_permissions.html` 的 JS 註解裡寫了一句說明，裡面照抄了
本檔上方的條件式字面寫法當作指路 —— **Jinja 不管它在不在 JS 註解裡**，
看到就當成真的標籤，於是多出一個沒關的條件，`/admin/permissions` 整頁變成
**500**。

更糟的是我當時的檢查是「這一頁有沒有 JS 例外」—— 頁面根本沒渲染出來，
沒有 JS 就沒有例外，**那個檢查是空的、還回報通過**。是後來把畫面截圖下來
用眼睛看才發現的。

這跟本專案記過的「`<script>` 區塊裡不可以寫字面 `</script>`」是同一類雷：
**註解不是安全區**，剖析器不看你的意圖。

## 這份測試擋什麼

1. 每一支模板都要能被 Jinja 剖析（語法層）。
2. 註解裡不可以出現樣板標籤的字面寫法。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_DIRS = ["app/web/templates", "app/admin/templates"]
_DIRS += [str(p.relative_to(ROOT)) for p in (ROOT / "app/tools").glob("*/templates")]
TEMPLATES = sorted(p for d in _DIRS for p in (ROOT / d).rglob("*.html"))


@pytest.mark.parametrize("path", TEMPLATES,
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_template_parses(path):
    """Jinja 剖析得過 —— 剖析不過就是整頁 500。"""
    import jinja2

    env = jinja2.Environment(autoescape=True)
    try:
        env.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except jinja2.TemplateSyntaxError as exc:
        pytest.fail(f"{path.relative_to(ROOT)}:{exc.lineno} 剖析失敗：{exc.message}")


#: JS / HTML 註解裡出現這些就是踩雷 —— Jinja 照樣會把它當標籤。
_TAG_IN_COMMENT = re.compile(r"\{%-?\s*(if|for|block|else|elif|end\w+|macro|set|with)\b")


def _comment_lines(src: str):
    """逐行找 JS 行註解與 HTML 註解裡的內容。

    只做行層級的近似 —— 目的是抓「說明文字裡照抄了標籤」，不是完整剖析。
    """
    in_html_comment = False
    for i, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if in_html_comment:
            yield i, line
            if "-->" in line:
                in_html_comment = False
            continue
        if s.startswith("<!--"):
            # Jinja 自己的註解 {# ... #} 不算，HTML 註解才會被 Jinja 剖析
            if "-->" not in line:
                in_html_comment = True
            yield i, line
            continue
        if s.startswith("//") or s.startswith("*") or s.startswith("/*"):
            yield i, line


@pytest.mark.parametrize("path", TEMPLATES,
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_comments_do_not_contain_literal_template_tags(path):
    """註解裡不可以寫出 `{% ... %}` 的字面寫法。

    想在說明裡指涉某個條件區塊，用文字描述（「本檔上方那個條件區塊」），
    不要照抄語法。
    """
    src = path.read_text(encoding="utf-8")
    bad = [f"{path.relative_to(ROOT)}:{i}  {line.strip()[:70]}"
           for i, line in _comment_lines(src) if _TAG_IN_COMMENT.search(line)]
    assert not bad, (
        "註解裡有樣板標籤的字面寫法 —— Jinja 不管它在不在註解裡，"
        "看到就當成真的標籤，整頁會變成 500：\n" + "\n".join(bad))
