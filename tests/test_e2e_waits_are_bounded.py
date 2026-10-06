"""瀏覽器測試等回覆時一定要有上限。

完整測試跑到會議摘要那支時整套停了將近一小時（2026-10-05）：送給瀏覽器的
`Runtime.evaluate` 一直沒有回來（頁面跳出對話框就會這樣），而 `send()` 用的是
不帶逾時的 `recv()` —— `_wait()` 自己的上限根本輪不到。單獨跑那條只要 9 秒，
所以這種卡住看起來像「機器很忙」，最後是被外層的 `timeout` 殺掉、連一行結果都沒有。

判準：`tests/` 裡對 websocket 呼叫 `recv()` 一定要帶 `timeout`。逾時的話那一條紅，
而且堆疊會指到卡住的那一步，不會把後面幾千條一起拖住。
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def _unbounded_recv_calls(src: str) -> list[int]:
    out = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "recv" and not node.args
                and not any(k.arg == "timeout" for k in node.keywords)):
            out.append(node.lineno)
    return out


def test_browser_tests_never_wait_for_a_reply_forever():
    bad = []
    for p in sorted(TESTS.glob("test_*.py")):
        for line in _unbounded_recv_calls(p.read_text(encoding="utf-8")):
            bad.append(f"{p.name}:{line}")
    assert not bad, "recv() 沒有 timeout（瀏覽器卡住時整套測試會停在這裡）：\n" + "\n".join(bad)


def test_the_check_reaches_the_browser_tests():
    """先證明掃得到東西：一個 recv 都沒找到的話，上面那條永遠是綠的。"""
    n = sum(p.read_text(encoding="utf-8").count(".recv(") for p in TESTS.glob("test_*.py"))
    assert n >= 10, f"只找到 {n} 處 recv —— 瀏覽器測試的寫法變了？"


def test_the_rule_catches_the_shape_it_is_for():
    assert _unbounded_recv_calls("m = ws.recv()\n") == [1]
    assert _unbounded_recv_calls("m = ws.recv(timeout=5)\n") == []
