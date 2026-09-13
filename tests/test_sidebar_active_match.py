"""側欄「使用中」只能標一支 —— 判準是整段路徑，不是前綴。

## 由來（v1.15.36，使用者回報）

開啟「註解平面化」（`/tools/pdf-annotations-flatten/`）時，側欄把
**「註解整理」（`/tools/pdf-annotations/`）也標成使用中**，而且自動捲動
停在錯的那一支（截圖回報）。

原因是比對寫成 `path.startsWith(href)`：`/tools/pdf-annotations-flatten/`
的前綴剛好就是 `/tools/pdf-annotations`。同一組還有「註解清除」。

> 本專案在前綴比對上吃過虧不只一次（`_PUBLIC_EXACT` 被前綴完全遮蔽）。
> **前綴相符不等於同一個東西**，要嘛完全相同、要嘛下一個字元是分隔符。

## 判準

這是瀏覽器端的行為，真正的驗證是用 CDP 開真的頁面數 `.sb-item.active`
（開發時已經這樣驗過三個網址，各只剩一支）。這裡守的是**不可以改回前綴比對**，
外加一條資料面的檢查：把目前的工具 id 帶進同一條規則，不可以有任何一支
會同時標到兩個。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = ROOT / "app" / "web" / "templates" / "base.html"


def _active_block() -> str:
    t = BASE.read_text(encoding="utf-8")
    i = t.find("Highlight active link")
    assert i > 0, "base.html 裡找不到側欄標記使用中的那一段（改寫了？）"
    return t[i:i + 1400]


def test_the_match_is_not_a_bare_prefix():
    """改回 `path.startsWith(href…)` 就會再犯一次。"""
    block = _active_block()
    assert "startsWith(base + '/')" in block, (
        "側欄的比對沒有用「下一個字元是 /」這條 —— 前綴相符不等於同一支工具")
    bare = re.search(r"startsWith\(\s*href\.replace", block)
    assert not bare, (
        "又改回拿 href 直接做前綴比對了：`/tools/pdf-annotations-flatten/` "
        "會把「註解整理」也標成使用中（v1.15.36 使用者回報過）")
    assert "path === base" in block, "少了「完全相同」那一半"


def test_no_tool_would_match_two_sidebar_entries():
    """資料面：把現有的工具 id 套進同一條規則，不可以有一支標到兩個。

    這條會隨著新工具自動生效 —— 哪天有人加了 `/tools/pdf-fill-batch`，
    它跟 `pdf-fill` 就是下一組（規則本身擋得住，這裡是確認沒有例外）。
    """
    from app.tool_registry import discover_tools

    hrefs = [f"/tools/{t.metadata.id}/" for t in discover_tools()]
    assert len(hrefs) > 40, f"只拿到 {len(hrefs)} 支工具，比對基準本身不對"

    def matches(path: str, href: str) -> bool:
        base = href.rstrip("/")
        return path == base or path.startswith(base + "/")

    for path in hrefs:
        hit = [h for h in hrefs if matches(path.rstrip("/"), h)]
        assert len(hit) == 1, f"{path} 會同時標到 {hit}"


def test_a_sub_page_still_lights_up_its_parent():
    """**反向對照**：改成只認「完全相同」的話，子頁面就不會標到父項了
    （`/admin/users/123` 要標在 `/admin/users`）。"""
    def matches(path: str, href: str) -> bool:
        base = href.rstrip("/")
        return path == base or path.startswith(base + "/")

    assert matches("/admin/users/123", "/admin/users")
    assert matches("/tools/pdf-fill/history", "/tools/pdf-fill/")
    assert not matches("/tools/pdf-annotations-flatten", "/tools/pdf-annotations/")
