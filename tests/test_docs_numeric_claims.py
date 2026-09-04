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
    text = path.read_text(encoding="utf-8")
    got = _claims(text, r"(\d+) 個工具(?:整合|速覽|一站|一覽|，分)")
    # 「這有 45 工具」—— hero 標題沒有「個」字，第一版正則因此漏掉它，
    # 使用者看著首頁大標題問「為何是 45」才發現（2026-08-16）。
    got |= _claims(text, r"有 (\d+) 工具")
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

def test_llm_card_count_matches_the_tool_count():
    """介紹站的 LLM 卡片數要等於實際有 LLM 加值的工具數。

    **只驗標題那個數字是不夠的**：v1.14.67 加了「文件翻譯」時，數字從 11 改成
    12 了，但**卡片沒補** —— 頁面寫著「12 個工具」底下卻只列 11 張，使用者
    找不到新工具（使用者回報：「沒有提到新的文件翻譯啊」）。
    """
    import re as _re
    from app.core.llm_settings import llm_settings as _L

    html = (ROOT / "github" / "docs" / "index.html").read_text(encoding="utf-8")
    cards = _re.findall(r'<article class="llm-card[^"]*">(.*?)</article>', html, _re.S)
    titles = [_re.search(r"<h3>(.*?)</h3>", c, _re.S).group(1).strip()
              for c in cards if _re.search(r"<h3>", c)]
    # `pdf-ocr-vision` 是 pdf-ocr 的另一種模式，不是獨立工具
    tools = {t["id"].replace("-vision", "") for t in _L.KNOWN_LLM_TOOLS}
    assert len(titles) == len(tools), (
        f"介紹站有 {len(titles)} 張 LLM 卡片，實際有 LLM 的工具是 {len(tools)} 個。"
        f"\n卡片：{titles}"
    )


# ---------------------------------------------------------------------------
# README 的 pytest 徽章
#
# 2026-09-04 使用者截圖回報時看到的：徽章寫著「470 passed」，實際是 5,9xx。
# 那個數字從很多版以前就沒人動過 —— 又是「同一個數字寫在兩個地方」的老病。
#
# 判準刻意做成**單邊**的：徽章不可以低於 tests/ 裡 `def test_` 的個數。
# 參數化只會把實際跑的項目變**更多**，所以「徽章 < 函式定義數」一定是漂掉了，
# 不可能誤報。反過來抓精確值要真的 collect 一次（慢，而且在跑測試的行程裡
# 再 collect 一次會去回收別人的 tmp_path 基底目錄）—— 那條路不划算。
# ---------------------------------------------------------------------------

_TEST_DEF_RE = re.compile(r"^\s*(?:async\s+)?def test_", re.M)
_BADGE_RE = re.compile(r"pytest-([\d,]+)%20passed")


def _defined_test_functions() -> int:
    return sum(
        len(_TEST_DEF_RE.findall(p.read_text(encoding="utf-8")))
        for p in (ROOT / "tests").glob("*.py")
    )


def test_readme_pytest_badge_is_not_stale():
    m = _BADGE_RE.search(README.read_text(encoding="utf-8"))
    assert m, "README 找不到 pytest 徽章"
    claimed = int(m.group(1).replace(",", ""))
    defined = _defined_test_functions()
    assert claimed >= defined, (
        f"README 的 pytest 徽章寫 {claimed}，但 tests/ 裡光是 def test_ 就有 "
        f"{defined} 個（參數化只會更多）—— 徽章已經漂掉，請更新成最近一次"
        f"完整跑出來的數字。"
    )
