"""介紹站的工具分類要跟程式裡的一致。

## 由來（v1.14.26）

騎縫章在程式裡是「填單用印」（跟用印與簽名同一類，權限也綁在一起），
我在介紹站卻把它放進「檔案編輯」。使用者到填單用印那一欄找不到它，
直接回報「新工具沒加進去」—— 其實有加，只是放錯欄。

既有的 `tools/check_docs_tool_coverage.py` 只驗「有沒有被列到」，
**不驗放在哪一欄**，所以放錯它一路綠燈。分類錯的後果跟沒列到一樣：
使用者在他認為該有的地方找不到，就當作沒有這個功能。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
INDEX = ROOT / "github" / "docs" / "index.html"

#: 介紹站的欄位標題對到程式裡的分類。名稱不同的在這裡對照。
#: 介紹站可以合併呈現（例如把兩個分類放同一欄），但**不可以放到不相干的欄**。
COLUMN_TO_CATEGORY = {
    "填單用印": {"填單用印"},
    "檔案編輯": {"檔案編輯"},
    "內容處理": {"內容處理"},
    "格式轉換": {"格式轉換"},
    "資安處理": {"資安處理"},
    "商務工具": {"商務工具", "內容處理"},
}


def _site_placement() -> dict[str, str]:
    """介紹站上每個工具名稱 → 它所在的欄位標題。"""
    html = INDEX.read_text(encoding="utf-8")
    heads = [(m.start(), m.group(1).strip())
             for m in re.finditer(r"<h3[^>]*>([^<]+)</h3>", html)]
    out: dict[str, str] = {}
    for m in re.finditer(r'<span class="fn">([^<]+)', html):
        name = m.group(1).strip()
        column = None
        for pos, title in heads:
            if pos < m.start():
                column = title
        if column:
            out[name] = column
    return out


@pytest.fixture(scope="module")
def tools_meta():
    from app.tool_registry import discover_tools
    return {t.metadata.name: t.metadata.category for t in discover_tools()}


def test_intro_site_puts_tools_in_the_right_column(tools_meta):
    """放錯欄跟沒列到一樣 —— 使用者在他認為該有的地方找不到。"""
    placement = _site_placement()
    assert placement, "介紹站解析不到任何工具，這條檢查本身壞了"
    wrong = []
    for name, column in placement.items():
        cat = tools_meta.get(name)
        if cat is None:
            continue                    # 不是工具（或名稱不同）—— 由涵蓋檢查負責
        allowed = COLUMN_TO_CATEGORY.get(column)
        if allowed is None:
            continue                    # 介紹站自己的區塊，不對應分類
        if cat not in allowed:
            wrong.append(f"「{name}」程式裡屬「{cat}」，"
                         f"介紹站卻放在「{column}」欄")
    assert not wrong, "介紹站的工具分類與程式不一致：\n" + "\n".join(wrong)


def test_stamping_tools_stay_together(tools_meta):
    """用印類的工具要放在一起 —— 使用者是照用途找的。"""
    placement = _site_placement()
    stamps = [n for n, c in tools_meta.items() if c == "填單用印"]
    assert stamps, "找不到任何填單用印的工具"
    misplaced = [n for n in stamps
                 if n in placement and placement[n] != "填單用印"]
    assert not misplaced, f"這些用印工具沒放在「填單用印」欄：{misplaced}"
