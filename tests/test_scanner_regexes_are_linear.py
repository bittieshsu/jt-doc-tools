"""守門的正規式不可以有**重疊的分支** —— 那是指數級回溯。

抓「引號裡的內容」時最自然的寫法是

    (['"])(?:\\.|(?!\1).)*\1

而 `\\.` 與 `(?!\1).` **重疊**：`\a` 既可以配成一次 `\\.`，也可以配成兩次
`(?!\1).`。配不上的時候引擎會把每一種切法都試一遍。實測（`re`，`\a` × N）：

| N | 重疊的寫法 | 改成 `[^\\]` |
|---:|---:|---:|
| 18 | 128 ms | 0.01 ms |
| 20 | 325 ms | 0.01 ms |
| 22 | 1,056 ms | 0.01 ms |
| 24 | **5,393 ms** | 0.01 ms |

掃的是我們自己的原始碼，攻擊者碰不到 —— **但守門卡住跟守門壞掉一樣難查**
（我曾經在測試裡寫出無窮迴圈，症狀是三個 pytest 各吃滿一顆 CPU）。
而且這個形狀會被複製貼上：三支掃描器裡就已經有三份。

判準只認**這個形狀本身**（`\\.` 之後接一個吃得下反斜線的分支），
不做時間量測 —— 計時的測試在忙碌的機器上會變成假警報。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: `\\.` ... `(?!\N).`（第二個分支是 `.` 或 `[^…]` 但沒排除反斜線）
_AMBIGUOUS = re.compile(r"\\\\\.\|\(\?!\\[0-9]\)(?:\.|\[\^(?![^\]]*\\\\)[^\]]*\])")


def _scanned() -> list[pathlib.Path]:
    out = [p for p in (ROOT / "tests").glob("*.py")]
    out += [p for p in (ROOT / "tools").rglob("*.py")]
    return sorted(out)


def test_the_scan_actually_reaches_the_scanners():
    """**先證明掃得到東西** —— 「掃 0 個檔」跟「掃過都乾淨」在 pytest 的
    輸出裡長得一模一樣（本專案第 N 次）。"""
    files = _scanned()
    assert len(files) >= 200, f"只收到 {len(files)} 個檔案，判準壞了"
    hits = [p for p in files if "(?!\\1)" in p.read_text(encoding="utf-8")
            or "(?!\\2)" in p.read_text(encoding="utf-8")]
    assert len(hits) >= 2, (
        f"只找到 {len(hits)} 支用這種引號式子的掃描器 —— "
        "它們改寫或搬走的話這條要先紅，不要變成空迴圈")


@pytest.mark.parametrize("path", [p for p in _scanned()],
                         ids=lambda p: p.name)
def test_no_overlapping_alternatives_in_quoted_string_regexes(path: pathlib.Path):
    src = path.read_text(encoding="utf-8")
    if path.name == pathlib.Path(__file__).name:
        pytest.skip("這一份在講那個寫法本身（use vs mention）")
    bad = _AMBIGUOUS.findall(src)
    assert not bad, (
        f"{path.relative_to(ROOT)} 用了重疊的分支：`\\\\.` 後面那個分支也吃得下"
        f"反斜線，配不上時就是指數級回溯。把它改成 `[^\\\\]`。"
    )
