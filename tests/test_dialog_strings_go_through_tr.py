"""對話框的訊息要走 `tr()`（v1.15.51）。

TEST_PLAN §0.6 的第三格寫著「對話框只能靠人工逐頁操作」—— 但**靜態掃描看
得到所有分支**，包括那些只有出錯時才會走到的。2026-09-16 掃下來一次抓到
**15 處**沒包的，形狀分三種：

* 內插的樣板字面值 —— `` showAlert(`已清除 ${n} 筆鎖定`) ``
* 純字串忘了包 —— `showAlert('兩次輸入不一致')`
* **三元運算只包了一半** —— `` cond ? '已啟用…' : tr('已關閉…') ``（最陰險：
  另一半是對的，看起來像已經處理過了）

## 判準

取出**第一個參數**那一段，把裡面的 `tr('…')` 全部挖掉，**剩下的不可以有中文**。

**不可以只驗「開頭是不是 `tr(`」** —— 那會把
`cond ? tr('A') : tr('B')`、`err.message || tr('C')` 這種正確的寫法判成違規，
而誤報一多這份檢查就會被當雜訊忽略（用詞守門那次的教訓）。

**只看第一個參數** —— 後面的選項物件（`{title, okText}`）另有守門。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_CALL = re.compile(r"\b(showConfirm|showToast|showAlert|showModal)\s*\(\s*")
_CJK = re.compile(r"[㐀-鿿]")

#: `tr('…')` / `tr("…")` —— 挖掉這些之後剩下的中文才算違規。
# **第二個分支要排除反斜線**（`[^\\]` 不是 `.`）—— 不然 `\x` 既可以配成
# 一次 `\\.` 也可以配成兩次 `.`，配不上時就是指數級回溯（CodeQL 報的 ReDoS）。
# 掃的是我們自己的原始碼、攻擊者碰不到，但守門卡住跟守門壞掉一樣難查。
_TR_CALL = re.compile(r"""tr\(\s*(['"])(?:\\.|(?!\1)[^\\])*\1""", re.S)


def _first_arg(src: str, start: int) -> str:
    """從 `(` 後面取到**第一個頂層逗號**（或收尾的 `)`）為止。

    要認得字串與樣板字面值裡的逗號 —— 不然
    `showToast(tr('a, b'), 'ok')` 會被切在字串中間。
    """
    depth, i, out, quote = 0, start, [], ""
    while i < len(src) and i - start < 400:
        ch = src[i]
        if quote:
            if ch == "\\":
                out.append(src[i:i + 2]); i += 2; continue
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        out.append(ch)
        i += 1
    return "".join(out)


def _files() -> list[Path]:
    return (sorted((ROOT / "app").rglob("*.html"))
            + sorted((ROOT / "static" / "js").glob("*.js")))


def _sources() -> "list[tuple[Path, str]]":
    """(檔案, 只剩程式的 JS)。

    **一定要去掉註解** —— 說明裡會引用「錯誤的寫法」當反例，掃進去就是誤報
    （本專案在這件事上踩過很多次；這一條自己也踩到，我寫的註解裡有
    「日期 / 個資限用章」當例子）。

    樣板只取 `<script>` 裡面那幾段：把整份 HTML 丟給 `strip_js_comments`
    會把內文當程式看。
    """
    from tools.source_text import blocks, strip_js_comments

    out = []
    for f in _files():
        raw = f.read_text(encoding="utf-8")
        if f.suffix == ".js":
            out.append((f, strip_js_comments(raw)))
        else:
            out.append((f, "\n".join(strip_js_comments(b) for b in blocks(raw, "script"))))
    return out


def test_the_scan_actually_finds_dialog_calls():
    """**先證明掃得到東西** —— 這幾支 helper 改名時這條會先紅。"""
    n = sum(len(_CALL.findall(src)) for _f, src in _sources())
    assert n >= 60, f"只找到 {n} 處對話框呼叫，掃描範圍大概錯了"


def test_every_dialog_message_goes_through_tr():
    from tools.source_text import blocks, strip_js_comments

    bad: list[str] = []
    for f, src in _sources():
        for m in _CALL.finditer(src):
            arg = _first_arg(src, m.end())
            left = _TR_CALL.sub("", arg)
            if _CJK.search(left):
                bad.append(f"{f.relative_to(ROOT).as_posix()}: {arg.strip()[:80]}")
    assert not bad, (
        "這幾處對話框的訊息沒有走 tr()（英文 / 日文介面會看到中文）：\n"
        + "\n".join(bad))
