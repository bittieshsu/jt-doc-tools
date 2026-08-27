"""自動存入工作區的每一個失敗原因，畫面上都要有對應的說法。

2026-08-27 使用者回報：作業清單上有些顯示「到工作區取用」、有些顯示
「結果已逾期清除」，看不出差別在哪。查正式機資料庫發現最近 35 筆裡有
**14 筆的原因是 `still_watching`** —— 而前端的對照表只翻譯了四個原因碼，
這個掉進通用訊息「未自動存入工作區」。

後果：使用者不知道自己需要按「存至工作區」，結果檔在保留期限（預設 24 小時）
到期後就沒了，而畫面從頭到尾沒告訴他要做什麼。

**同一份清單寫在兩個地方一定會漂** —— 原因碼在 Python 產生、說法在 JS 裡寫，
這支測試就是那道橋。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _produced_reason_codes() -> set[str]:
    src = (ROOT / "app" / "core" / "job_autosave.py").read_text(encoding="utf-8")
    return set(re.findall(r'_reason\(\s*"([a-z_]+)"', src))


def _translated_reason_codes() -> set[str]:
    html = (ROOT / "app" / "web" / "templates" / "my_jobs.html").read_text(encoding="utf-8")
    # 取 `const why = { ... }[w.reason]` 那段裡的鍵
    m = re.search(r"const why = \{(.*?)\}\[w\.reason\]", html, re.S)
    assert m, "找不到原因對照表 —— 這支測試的前提變了，請重新確認"
    return set(re.findall(r"^\s*([a-z_]+)\s*:", m.group(1), re.M))


def test_every_reason_code_has_a_message():
    produced = _produced_reason_codes()
    translated = _translated_reason_codes()
    # `workspace_disabled` 刻意不進對照表：那個情境由另一段 UI 處理
    # （顯示保留期限），不是「失敗」。
    expected = produced - {"workspace_disabled"}
    missing = sorted(expected - translated)
    assert not missing, (
        f"這些原因碼在畫面上沒有對應的說法：{missing}\n"
        "  會掉進通用訊息「未自動存入工作區」，使用者不知道自己該做什麼。")


def test_no_stale_translations():
    """對照表裡不該有程式已經不再產生的原因碼 —— 那是清單漂掉的另一半。"""
    stale = sorted(_translated_reason_codes() - _produced_reason_codes())
    assert not stale, f"這些原因碼程式已經不會產生了，對照表卻還留著：{stale}"


def test_unknown_reason_is_shown_not_swallowed():
    """新增原因碼但忘了翻譯時，畫面要**把代碼顯示出來**。

    退路寫成固定字串的話，新的原因碼會無聲地變成一句沒有資訊的話 ——
    這次就是這樣拖到使用者回報才發現。
    """
    html = (ROOT / "app" / "web" / "templates" / "my_jobs.html").read_text(encoding="utf-8")
    m = re.search(r"\}\[w\.reason\]\s*\|\|\s*([^;]+);", html)
    assert m, "找不到對照表的退路"
    fallback = m.group(1)
    assert "w.reason" in fallback, (
        "未知原因碼的退路沒有把代碼顯示出來 —— 下次新增原因碼又會無聲吞掉")


def test_still_watching_tells_the_user_what_to_do():
    """這個原因最常見（正式機 35 筆裡佔 14 筆），訊息一定要可行動。"""
    html = (ROOT / "app" / "web" / "templates" / "my_jobs.html").read_text(encoding="utf-8")
    m = re.search(r"still_watching:\s*(.+?)(?=\n\s+[a-z_]+:|\n\s*\})", html, re.S)
    assert m, "still_watching 沒有對應的說法"
    text = m.group(1)
    assert "存至工作區" in text, "沒告訴使用者該按哪個按鈕"
    assert "保留期" in text or "清除" in text, "沒說明不處理的後果"
