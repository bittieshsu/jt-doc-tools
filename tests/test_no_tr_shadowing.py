"""`tr` 是表格列最自然的變數名，也是前端翻譯函式的名字 —— 撞名會讓整段 JS 當場死掉。

## 由來

2026-09-06 使用者在正式機回報「乘車證明整理這工具是不是壞了，我拉檔案進去都出錯」，
畫面上是 `上傳錯誤：tr is not a function`。**整支工具在任何語言下都不能用。**

根因是 i18n 那幾輪把顯示字串包成 `tr(...)` 時，包進了這種區塊：

    rows.forEach(e => {
      const tr = document.createElement('tr');   // ← tr 在這裡是 DOM 元素
      inp.placeholder = tr('科目');               // ← 於是這行變成「呼叫一個元素」
    });

一次掃出 **16 處**，橫跨三支工具（乘車證明 / 書籤與目錄 / 字數統計）與九個管理頁。
最惡劣的一種是 `const tr = { 'host required': tr('Host 為必填') }` ——
**在自己的初始式裡呼叫自己**（暫時性死區），記錄轉發只要儲存失敗就炸。

## 為什麼既有的守門抓不到

`test_template_js_syntax.py` 用 `node --check` 驗語法 —— 這種遮蔽**語法完全合法**，
只有執行到那一行才炸。而 i18n 的掃描器只看「有沒有包 `tr()`」，包了就算數，
不看包進去的地方 `tr` 是不是別的東西。**兩道既有防線都是綠的。**
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
DECL = re.compile(r"\b(?:const|let|var)\s+tr\b|\bfor\s*\(\s*(?:const|let|var)\s+tr\b")
CALL = re.compile(r"(?<![\w.$])tr\s*\(")


def _enclosing_block(text: str, at: int) -> str | None:
    """`at` 這個宣告所在的大括號區塊。"""
    depth, start, i = 0, None, at
    while i > 0:
        if text[i] == "}":
            depth += 1
        elif text[i] == "{":
            if depth == 0:
                start = i
                break
            depth -= 1
        i -= 1
    if start is None:
        return None
    depth, j = 0, start
    while j < len(text):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[start:j]
        j += 1
    return text[start:]


def _sources() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for root in (ROOT / "app", ROOT / "static" / "js"):
        for p in sorted(root.rglob("*")):
            if p.suffix in {".html", ".js"} and "__pycache__" not in str(p):
                out.append(p)
    return out


def test_tr_is_never_shadowed_where_it_is_called():
    """同一個範圍內既宣告 `tr` 變數又呼叫 `tr(...)` = 執行到就炸。"""
    bad: list[str] = []
    for p in _sources():
        text = p.read_text(encoding="utf-8")
        for m in DECL.finditer(text):
            blk = _enclosing_block(text, m.start())
            if blk and CALL.search(blk):
                line = text.count("\n", 0, m.start()) + 1
                bad.append(f"{p.relative_to(ROOT).as_posix()}:{line}")
    assert not bad, (
        "這些地方宣告了名為 `tr` 的變數，而同一個範圍內又呼叫 tr(...) —— "
        "執行到那一行會丟 `tr is not a function`：\n  " + "\n  ".join(bad)
        + "\n把區域變數改名（例如 rowEl / errMap），不要動全域的翻譯函式。")


def test_the_scan_actually_reads_something():
    """**這條守的是上面那條自己。**

    掃不到檔案的話 `assert not bad` 永遠成立 —— 「一個檔都沒掃」跟「掃過都乾淨」
    在 pytest 輸出裡長得一模一樣。這個專案已經因為這件事被騙過好幾次。
    """
    files = _sources()
    assert len(files) > 100, f"只收到 {len(files)} 個檔案，掃描範圍不對"
    assert any(CALL.search(p.read_text(encoding="utf-8")) for p in files), \
        "一個 tr(...) 呼叫都沒看到，掃描器八成壞了"
