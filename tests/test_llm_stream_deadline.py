"""串流回應要有**整次生成的上限**，不是只有每個 chunk。

由來（2026-09-08，客戶的年報翻到一半永遠卡住）：文件裡有一段表格的填空欄位

    For the transition period from<16 個不斷行空白> to

送給 gemma4:26b 之後模型停不下來。而 `stream=True` 時 httpx 的 `timeout`
是**每個 chunk 的讀取逾時** —— 只要一直有 token 進來，逾時永遠不觸發。
於是整份文件的翻譯卡在那一段，畫面顯示「翻譯中… N/M」不動、**沒有任何錯誤
訊息、也不會失敗**，使用者只能一直等。

設定裡那個欄位的說明寫的是「單次 HTTP 呼叫上限」—— 它承諾了一個實際上
不存在的保證。
"""
from __future__ import annotations

import time

import pytest

from app.core import llm_client


def test_the_deadline_fires_when_generation_never_ends():
    t0 = time.monotonic() - 5.0
    with pytest.raises(llm_client.StreamDeadline):
        llm_client._check_deadline(t0, 1.0)


def test_it_does_not_fire_early():
    llm_client._check_deadline(time.monotonic(), 60.0)      # 不可以丟例外


def test_no_limit_means_no_deadline():
    """設定成 0 / None 時不強制（留一條路給刻意要跑很久的部署）。"""
    llm_client._check_deadline(time.monotonic() - 9999, 0)


def test_the_message_says_it_is_the_model_not_the_network():
    """「逾時」講不清楚是誰的問題。

    連線失敗與「模型停不下來」的處理方式完全不同 —— 前者查網路，
    後者要看那一段文字。
    """
    with pytest.raises(llm_client.StreamDeadline, match="停不下來"):
        llm_client._check_deadline(time.monotonic() - 5, 1.0)


def test_every_streaming_loop_checks_the_deadline():
    """**兩處串流迴圈都要檢查** —— 只補一處等於沒補。

    用 AST 找 `for line in r.iter_lines():` 這種迴圈，確認每一個的
    第一件事都是呼叫 `_check_deadline`（註解裡提到不算數）。
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(llm_client))
    loops = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        it = node.iter
        if (isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute)
                and it.func.attr == "iter_lines"):
            loops.append(node)
    assert len(loops) >= 2, f"只找到 {len(loops)} 個串流迴圈，掃描壞了"
    for loop in loops:
        calls = {c.func.id for c in ast.walk(loop)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert "_check_deadline" in calls, \
            f"第 {loop.lineno} 行的串流迴圈沒有檢查整次生成的上限"
