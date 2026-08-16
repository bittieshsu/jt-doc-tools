"""公開文件裡的數字宣稱要跟程式對得上。

## 由來（2026-08-16 使用者要求「README / pages 更新到最新狀況」時查出）

`check_docs_tool_coverage.py` 只驗**工具總數**這一個數字。其他人工維護的
數字沒人守，結果全漂了：

* 「20 個工具走背景作業」—— 實際 25（新工具陸續接上 job_manager，
  數字停在很多版以前）
* 「分 6 大類」—— 實際 5 類（feat-card 也只有 5 張，數字是舊時代的）

這種數字錯了不會有任何錯誤訊息，只有讀的人會覺得「怎麼跟畫面對不上」。
判準一律**從程式實算**，不寫死期望值 —— 寫死的話這份測試自己就是
下一個會漂的數字。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = ROOT / "github" / "README.md"
INDEX = ROOT / "github" / "docs" / "index.html"


def _actual_tool_count() -> int:
    from app.tool_registry import discover_tools
    return len(discover_tools())


def _actual_bg_tool_count() -> int:
    """走作業系統的工具數 —— 掃各工具 router 裡的 job_manager.submit。"""
    n = 0
    for router in (ROOT / "app" / "tools").glob("*/router.py"):
        if "job_manager.submit" in router.read_text(encoding="utf-8",
                                                    errors="replace"):
            n += 1
    return n


def _actual_llm_tool_count() -> int:
    """LLM 加值工具數 —— CLAUDE.md 記的廣義 pattern（多半接 llm_settings，
    不一定 import llm_client）。"""
    pat = re.compile(r"llm_settings|llm_client|LLMClient|llm_classify")
    pkgs = set()
    for py in (ROOT / "app" / "tools").rglob("*.py"):
        if pat.search(py.read_text(encoding="utf-8", errors="replace")):
            pkgs.add(py.relative_to(ROOT / "app" / "tools").parts[0])
    return len(pkgs)


def _actual_category_count() -> int:
    from app.tool_registry import discover_tools
    return len({t.metadata.category for t in discover_tools()})


def _claims(text: str, pattern: str) -> set[int]:
    return {int(m) for m in re.findall(pattern, text)}


@pytest.mark.parametrize("path", [README, INDEX], ids=["README", "index.html"])
def test_total_tool_count_claims(path):
    actual = _actual_tool_count()
    got = _claims(path.read_text(encoding="utf-8"), r"(\d+) 個工具(?:整合|速覽|一站|一覽|，分)")
    assert got == {actual} or not got, (
        f"{path.name} 說有 {got} 個工具，實際 {actual}")


@pytest.mark.parametrize("path", [README, INDEX], ids=["README", "index.html"])
def test_background_tool_count_claims(path):
    actual = _actual_bg_tool_count()
    got = _claims(path.read_text(encoding="utf-8"),
                  r"(\d+) 個工具走(?:作業系統|背景作業)")
    assert got == {actual} or not got, (
        f"{path.name} 說 {got} 個工具走背景作業，實際掃 router 是 {actual} —— "
        "新工具接上 job_manager 之後這個數字要跟著加")


@pytest.mark.parametrize("path", [README, INDEX], ids=["README", "index.html"])
def test_llm_tool_count_claims(path):
    actual = _actual_llm_tool_count()
    text = path.read_text(encoding="utf-8")
    got = _claims(text, r"(\d+) 個工具(?:\*\*)?(?:自動多出|支援 LLM|開啟智慧|如何用 LLM)")
    got |= _claims(text, r"\*\*(\d+) 個工具\*\*自動多出")
    assert got == {actual} or not got, (
        f"{path.name} 說 {got} 個工具有 LLM 加值，實際 {actual}")


def test_category_count_claim():
    actual = _actual_category_count()
    got = _claims(INDEX.read_text(encoding="utf-8"), r"分 (\d+) 大類")
    assert got == {actual} or not got, (
        f"index.html 說分 {got} 大類，實際 metadata.category 有 {actual} 類")
