"""三元運算的**每一個分支**都要各自包 `tr()`。

## 由來

包 JS 字串時我們**刻意不包整個三元運算**（`tr(cond ? \'A\' : \'B\')`）——
包起來的話查表的鍵是執行期才算出來的，永遠查不到，而且無聲。正確寫法是
**逐個分支包**：`cond ? tr(\'A\') : tr(\'B\')`。

問題是自動包字串的工具遇到三元運算就整條跳過，於是**只有其中一半被包過**
的情況從來沒有人管。2026-09-16 實際掃出 **13 處**，每一處在英文 / 日文介面
下都會顯示中文：

* 儲存結果只有失敗那半翻了（`r.ok ? \'✓ 已儲存\' : (\'✗ \' + tr(...))`）
* 右鍵選單的標題只有「插入」那半翻了，「覆寫」那半沒有
* 字數統計超過一小時的那條路（`3時20分`）整串是中文

**這一類逐頁掃抓不到** —— 那些字只在按下去、切換、或資料超過某個值之後才
出現（TEST_PLAN §0.6 記的那個盲區）。

## 判準

「同一行裡**既有 `tr(`、又有沒被 `tr(` 包住的中文字面值**」。只看有沒有
`tr(` 的話，整行都沒包的反而不會紅 —— 但那一類由既有的
`tools/i18n_wrap_js_display.py` 管，這裡專門補三元運算那個縫。

**限制**：只看單行。跨行寫的三元運算掃不到 —— 所以下面有一條反向對照，
證明這支掃描器真的抓得到東西。
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.source_text import strip_js_comments  # noqa: E402

CJK = re.compile("[\u3400-\u9fff]")

#: 只認「這個值一定是拿去顯示的」位置。三元運算也可能在算 class 名稱或
#: API 參數，那些包了反而會出事（翻掉之後拿去比較，畫面正常但邏輯壞掉）。
DISPLAY = re.compile(
    r"(?:\.(?:textContent|innerHTML|title|placeholder|ariaLabel)\s*=|"
    r"\b(?:alert|showToast|showConfirm|friendlyServerError)\s*\()")

#: 字串的開頭前面（去掉空白後）長這樣就算「已經包過」。
WRAPPED = re.compile(r"\btr\(\s*$")


def _string_literals(line: str, base: int = 0):
    """逐字元走過一行，回出每個字串字面值 `(開頭位置, 內容)`。

    **不可以用正規式配對引號** —— 第一版用 `'[^']*'`，遇到
    ``tr('中文').replace('{0}', x) + (f ? `，中文 ${f}` : '')`` 會把兩個
    不相干的引號配成一對，把中間的樣板字面整段吞進去。這正是本專案在
    「用正規式剖析原始碼」上踩過的同一個坑。

    **樣板字面要拆開看**：中文常常就寫在反引號裡面，但 `${…}` 裡面是
    **程式碼**不是文字 —— 整段當成一個字串的話，
    ``` `<span>${x ? a : tr('空')}</span>` ``` 會因為裡面有個「空」被判成
    沒包（實測誤報過）。所以文字段落逐段回報，`${…}` 遞迴進去再走一次。
    """
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c in "'\"":
            j = i + 1
            while j < n:
                if line[j] == "\\":
                    j += 2
                    continue
                if line[j] == c:
                    break
                j += 1
            yield base + i, line[i + 1:j]
            i = j + 1
            continue
        if c == "`":
            j = i + 1
            chunk = j
            while j < n:
                if line[j] == "\\":
                    j += 2
                    continue
                if line[j] == "`":
                    break
                if line[j] == "$" and j + 1 < n and line[j + 1] == "{":
                    yield base + chunk - 1, line[chunk:j]
                    k, depth = j + 2, 1
                    while k < n and depth:
                        depth += {"{": 1, "}": -1}.get(line[k], 0)
                        k += 1
                    yield from _string_literals(line[j + 2:k - 1], base + j + 2)
                    j = chunk = k
                    continue
                j += 1
            yield base + chunk - 1, line[chunk:j]
            i = j + 1
            continue
        i += 1


def _js_blocks(path: pathlib.Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    if path.suffix != ".html":
        return [src]
    # `re.I`：`<SCRIPT>` 也是合法的標籤（CodeQL 以「過濾 HTML 的正規式漏掉大寫」報成 High）
    return re.findall(r"<script\b[^>]*>(.*?)</script\b[^>]*>", src, re.S | re.I)


def _bare_cjk_branches(js: str) -> list[str]:
    """回傳「有包過 `tr(`、卻還留著沒包的中文字面值」的那幾行。"""
    out: list[str] = []
    for line in strip_js_comments(js).splitlines():
        if "?" not in line or "tr(" not in line or not DISPLAY.search(line):
            continue
        for pos, s in _string_literals(line):
            if CJK.search(s) and not WRAPPED.search(line[:pos]):
                out.append(line.strip())
                break
    return out


def _files() -> list[pathlib.Path]:
    return (sorted(REPO.glob("app/**/*.html"))
            + sorted(REPO.glob("static/js/*.js")))


def test_the_scan_actually_reads_something():
    """**「掃 0 個檔」跟「掃過都乾淨」在 pytest 輸出裡長得一模一樣。**"""
    files = _files()
    assert len(files) > 100, f"只收到 {len(files)} 個檔，掃描範圍壞了"
    assert sum(len(_js_blocks(f)) for f in files) > 100


def test_the_scan_catches_a_known_bad_line():
    """反向對照：這支掃描器真的抓得到那個形狀嗎？

    沒有這一條的話，判準寫壞（例如正規式打錯）會讓整份掃描永遠是綠的。
    """
    bad = "x.textContent = ok ? \'已儲存\' : tr(\'失敗\');"
    good = "x.textContent = ok ? tr(\'已儲存\') : tr(\'失敗\');"
    assert _bare_cjk_branches(bad), "掃描器抓不到已知的壞寫法"
    assert not _bare_cjk_branches(good), "掃描器把正確寫法判成壞的"


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_every_ternary_branch_is_wrapped(path: pathlib.Path):
    bad: list[str] = []
    for js in _js_blocks(path):
        bad += _bare_cjk_branches(js)
    assert not bad, (
        f"{path.relative_to(REPO)}：三元運算只包了一半，另一半在英文 / 日文"
        f"介面下會顯示中文。逐個分支包 `tr()`：\n  " + "\n  ".join(bad[:5]))
