"""每一支工具都要有搜尋關鍵字（中文 + 英文）。

## 由來

首頁與側欄的搜尋靠 `app/main.py:_TOOL_ALIASES`。這張表是**人工維護**的，工具卻一直
在加 —— 盤點時發現「送件前檢核」與「乘車證明整理」兩支從加進來就沒有關鍵字，
**完全搜不到**。工具本身好好的、選單裡也在，只有搜尋找不到，所以不會有人回報。

同一類問題在這個專案發生過很多次（README / 介紹站的工具清單、扳手標記、
LLM 工具計數）。凡是人工維護的清單，就要有一支測試比對真實來源。
"""
from __future__ import annotations

import re

import pytest

from app.main import _TOOL_ALIASES
from app.tool_registry import discover_tools


def _registered() -> dict[str, str]:
    return {t.metadata.id: t.metadata.name for t in discover_tools()}


def test_every_registered_tool_has_keywords():
    missing = sorted(set(_registered()) - set(_TOOL_ALIASES))
    assert not missing, (
        f"這些工具沒有搜尋關鍵字，使用者搜不到：{missing}\n"
        "在 app/main.py 的 _TOOL_ALIASES 補上（中文 + 英文都要）。")


def test_no_keywords_for_tools_that_do_not_exist():
    """反向也要看 —— 改名之後留著舊 id 會讓搜尋指向不存在的工具。

    停用中的工具（`enabled=False`）保留條目是可以的：程式碼還在，之後可能開回來。
    """
    import pathlib

    from app import tool_registry
    disabled = set()
    root = pathlib.Path(tool_registry.__file__).resolve().parent / "tools"
    for init in root.glob("*/__init__.py"):
        txt = init.read_text(encoding="utf-8")
        m = re.search(r'id\s*=\s*["\']([\w-]+)["\']', txt)
        if m and re.search(r"enabled\s*=\s*False", txt):
            disabled.add(m.group(1))
    stale = sorted(set(_TOOL_ALIASES) - set(_registered()) - disabled)
    assert not stale, f"_TOOL_ALIASES 有不存在的工具 id：{stale}"


@pytest.mark.parametrize("tool_id", sorted(_TOOL_ALIASES))
def test_keywords_have_both_chinese_and_english(tool_id):
    """中英文都要有 —— 使用者兩種都會打。"""
    kw = _TOOL_ALIASES[tool_id]
    assert re.search(r"[一-鿿]", kw), f"{tool_id} 沒有中文關鍵字"
    assert re.search(r"[A-Za-z]{3,}", kw), f"{tool_id} 沒有英文關鍵字"
    assert len(kw) > 20, f"{tool_id} 的關鍵字太少（{len(kw)} 字）"
