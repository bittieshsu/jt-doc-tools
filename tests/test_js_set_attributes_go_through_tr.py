"""JS 設定的**顯示屬性**（title / placeholder / aria-label / alt）要走 `tr()`。

這一類是本專案 i18n 的第三個盲區（TEST_PLAN §0.6 列的三個來源之一）：

* 樣板裡的 `title="…"` —— 伺服器渲染時就翻好了，沒問題。
* **JS 執行期設定的** `el.title = '…'` —— 靜態的樣板掃描看不到，
  而且它不是文字節點，逐頁掃描只有在「滑鼠移上去」時才看得到。

2026-09-16 掃下來還有 4 處，全部是**內插的樣板字面值**
（`` el.alt = `第 ${n} 頁` ``）—— 跟對話框那一輪同一個形狀。

## 判準

只看**字面值**：`el.title = '中文'` / `setAttribute('title', '中文')`。
變數（`el.title = someText`）不在範圍內 —— 那要在算出 `someText` 的地方包。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 引號內容的第二個分支要排除反斜線 —— 見 `test_dialog_strings_go_through_tr`
# 裡的說明（分支重疊 ＝ 指數級回溯）。
_ASSIGN = re.compile(
    r"""\.(title|placeholder|ariaLabel|alt)\s*=\s*(['"`])((?:\\.|(?!\2)[^\\])*)\2""")
_SETATTR = re.compile(
    r"""setAttribute\(\s*['"](title|placeholder|aria-label|alt)['"]\s*,"""
    r"""\s*(['"`])((?:\\.|(?!\2)[^\\])*)\2""")
_CJK = re.compile(r"[㐀-鿿]")


def _sources() -> "list[tuple[Path, str]]":
    """(檔案, 只剩程式的 JS)。樣板只取 `<script>` 裡那幾段。"""
    from tools.source_text import blocks, strip_js_comments

    out = []
    files = (sorted((ROOT / "app").rglob("*.html"))
             + sorted((ROOT / "static" / "js").glob("*.js")))
    for f in files:
        raw = f.read_text(encoding="utf-8")
        out.append((f, strip_js_comments(raw) if f.suffix == ".js"
                    else "\n".join(strip_js_comments(b) for b in blocks(raw, "script"))))
    return out


def test_the_scan_actually_finds_attribute_assignments():
    """**先證明掃得到東西。**"""
    n = sum(len(_ASSIGN.findall(s)) + len(_SETATTR.findall(s)) for _f, s in _sources())
    # 門檻是**量出來的**（2026-09-16 實際 10 處）—— 寫一個好看的整數會變成
    # 「永遠成立」或「動不動就紅」。取一半，只要範圍壞掉就會先紅。
    assert n >= 5, f"只找到 {n} 處屬性設定，掃描範圍大概錯了"


#: 屬性賦值的**左邊**。右邊整段交給 `_rhs()` 取 —— 第一版只認「`=` 後面直接
#: 是字串」，於是 `cnt.title = q ? `符合搜尋 ${n} 項` : ''` 這種三元運算
#: 整條漏掉（v1.16.11 發現，側欄搜尋的計數提示在英日介面一直是中文）。
_ASSIGN_LHS = re.compile(r"\.(title|placeholder|ariaLabel|alt)\s*=(?!=)\s*")
_TR_CALL = re.compile(r"""tr\(\s*(['"])(?:\\.|(?!\1)[^\\])*\1""", re.S)


def _rhs(src: str, start: int) -> str:
    """從 `=` 後面取到這個敘述結束（頂層的 `;`、換行、或收尾的括號）。
    要認得字串與樣板字面值 —— 裡面的 `;` 與換行不算結束。"""
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
        elif ch in ";\n" and depth == 0:
            break
        out.append(ch)
        i += 1
    return "".join(out)


def test_no_js_set_attribute_holds_raw_chinese():
    bad: list[str] = []
    for f, src in _sources():
        for m in _SETATTR.finditer(src):
            if _CJK.search(m.group(3)):
                bad.append(f"{f.relative_to(ROOT).as_posix()}: {m.group(0)[:76]}")
        for m in _ASSIGN_LHS.finditer(src):
            rhs = _rhs(src, m.end())
            if _CJK.search(_TR_CALL.sub("", rhs)):
                bad.append(f"{f.relative_to(ROOT).as_posix()}: {m.group(0)}{rhs.strip()[:70]}")
    assert not bad, (
        "這幾處 JS 設定的顯示屬性直接寫了中文（英文 / 日文介面會看到中文，"
        "而且要滑鼠移上去才發現）：\n" + "\n".join(bad))
