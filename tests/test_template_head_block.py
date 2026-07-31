"""工具模板的 `<style>` 一定要放在 base.html 真的有的區塊裡。

## 由來

新工具「頁面加框」寫成 `{% block extra_head %}` —— base.html 裡沒有這個區塊，
Jinja **不會報錯**，整段樣式就被安靜丟掉了。畫面上的症狀就是「樣式沒套」：
版面全垮但沒有任何錯誤訊息、console 也是乾淨的，只能靠肉眼看出來。

（同一輪使用者才回報過 SSO 頁「樣式沒套」，那是另一個原因；這種靠眼睛發現的
問題出現兩次就該有測試擋。）

## 這份檢查什麼

模板裡出現的每個 `{% block X %}`，X 必須是 base.html（或它自己 extends 的父模板）
定義過的區塊名。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "app" / "web" / "templates" / "base.html"

#: 掃描的模板：工具頁 + 管理頁
TEMPLATES = sorted(
    list((ROOT / "app" / "tools").rglob("templates/*.html"))
    + list((ROOT / "app" / "admin" / "templates").glob("*.html"))
    + list((ROOT / "app" / "web" / "templates").glob("*.html"))
)

_BLOCK_RE = re.compile(r"{%-?\s*block\s+([a-zA-Z_][a-zA-Z0-9_]*)")


def _blocks(path: Path) -> set[str]:
    return set(_BLOCK_RE.findall(path.read_text(encoding="utf-8")))


def test_base_defines_the_blocks_we_expect():
    """先確認掃描本身有效 —— base.html 抓不到區塊的話下面全部會假性通過。"""
    b = _blocks(BASE)
    assert {"title", "head", "content", "scripts"} <= b, b


@pytest.mark.parametrize("tpl", TEMPLATES, ids=lambda p: p.name)
def test_template_only_uses_blocks_that_exist(tpl: Path):
    src = tpl.read_text(encoding="utf-8")
    if "{% extends" not in src:
        pytest.skip("不是繼承 base 的頁面")
    known = _blocks(BASE)
    used = _blocks(tpl)
    # 模板自己也可以定義新區塊給它的子模板用；這裡只看「有 extends 的葉子頁」，
    # 未知區塊名就是打錯字。
    unknown = used - known
    assert not unknown, (
        f"{tpl.name} 用了 base.html 沒有的區塊：{sorted(unknown)} —— "
        "Jinja 不會報錯，那段內容會被安靜丟掉（症狀是「樣式沒套」）")
