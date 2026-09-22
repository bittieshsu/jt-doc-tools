"""`tools/check_commit_message.py` 自己要有牙齒。

使用者 2026-09-13 說過、2026-09-21 再確認一次：**公開 repo 的 commit 訊息
不可以有 Claude session 連結**。問題是開發工具那一側**每個 session 都會提示
要加那一行**，措辭還是「取代先前的署名指引」—— 光靠「記得不要加」是守不住的，
所以做成可以裝進 `commit-msg` hook 的檢查。

這支測試守的是**那個檢查本身**：它要擋得住該擋的、也要放行正常的署名。
只驗「會擋 session」的話，一支「什麼都擋」的檢查也會過。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_commit_message", ROOT / "tools" / "check_commit_message.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_it_blocks_every_shape_of_session_link():
    shapes = [
        "v1: 修好某件事\n\nClaude-Session: https://claude.ai/code/session_01AbCdEfGhIjKlMnOpQrStUv",
        "v1: 修好某件事\n\n見 https://claude.ai/code/session_01AbCdEfGhIjKlMnOpQrStUv",
        "v1: 修好某件事\n\nsession_01AbCdEfGhIjKlMnOpQrStUv",
    ]
    for msg in shapes:
        assert _mod.check(msg), f"這個形狀沒被擋下來：{msg!r}"


def test_it_blocks_simplified_chinese():
    assert _mod.check("v1: 修复对应的问题")


def test_it_lets_the_normal_message_through():
    """反向對照 —— 沒有這條的話，一支「什麼都擋」的檢查也會全綠。"""
    ok = ("v1.15.93：新工具「會議摘要」\n\n"
          "累積 34 版。修好匯出的 PDF、擋住內網位址外流。\n\n"
          "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>\n")
    assert not _mod.check(ok), "正常的訊息被誤擋了"


def test_co_authored_by_claude_is_not_a_session_link():
    """署名要留 —— 使用者擋的是 session 連結，不是協作署名。"""
    assert not _mod.check("Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>")


def test_the_word_claude_alone_is_not_enough_to_trip_it():
    """訊息裡提到 Claude 很正常（例如在講某支工具是誰寫的）。"""
    assert not _mod.check("v1: 把 Claude 相關的說明搬到 LLM.md")
