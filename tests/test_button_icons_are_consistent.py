"""按鈕圖示的兩條檢查。

使用者 2026-09-13 回報「這按鈕有兩個少 icon」—— 掃描修正的四顆旋轉鈕裡，
前兩顆有圖示、後兩顆沒有。**元素都在、沒有 JS 例外、也沒有殘留中文**，
既有的檢查一條都抓不到，只有截圖看得出來。

這裡釘兩條判準：

① **同一排（中間只隔空白的相鄰 `<button>`）要嘛都有圖示、要嘛都沒有。**
   「同一排」一定要用位置判斷 —— 我第一版用 `<button.*?</button>` 配出
   「連續數顆」，非貪婪比對會跨過中間的 `</div>`，把根本不相鄰的按鈕算成一組
   （實測誤報 39 組，逐個看完才發現分組本身是錯的）。

② **有圖示的按鈕，JS 不可以用 `textContent` 覆寫整顆按鈕。**
   那會把 `<svg>` 一起洗掉：文件翻譯的「停止翻譯」在顯示的那一刻就被
   `$('btnStop').textContent = ...` 洗成純文字，所以那顆按鈕的圖示
   **從來沒有出現過**。文字要放自己的 `<span>`，JS 只改那個 span。

兩條都**只認畫面上真的是一排動作鈕的情況**：下拉選單項目、分頁箭頭、
分頁籤（`menu-item` / `dropdown` / `tab` / `pager` / `chip`）與純符號、
純副檔名標籤（`.xlsx`、`Word (.docx)`、`‹`）排除 —— 那些本來就不配圖示，
算進來只會讓這份清單變成雜訊而沒有人看（用詞檢查那次的教訓）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_BTN = re.compile(r"<button\b([^>]*)>(.*?)</button>", re.S)
_BTN_ID = re.compile(r'<button\b[^>]*\bid="([A-Za-z0-9_-]+)"[^>]*>(.*?)</button>', re.S)
_HAS_ICON = re.compile(r"icon\(|<svg")
_CJK_WORD = re.compile(r"[一-鿿]")
# 選單項目 / 分頁 / 分頁籤 —— 不是一排動作鈕
_NOT_A_ROW = re.compile(r"menu-item|dropdown|\btab\b|data-tab|pager|chip")


def _templates() -> list[Path]:
    return sorted(ROOT.glob("app/**/templates/**/*.html"))


def _label(inner: str) -> str:
    s = re.sub(r"{{\s*tr\('([^']*)'\)\s*}}", r"\1", inner)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"{{.*?}}|{%.*?%}|{#.*?#}", "", s, flags=re.S)
    return re.sub(r"\s+", " ", s).strip()


def _is_action_label(label: str) -> bool:
    """含中文字才算動作鈕；純符號與副檔名（.xlsx / Word (.docx)）不是。"""
    if not _CJK_WORD.search(label):
        return False
    return not re.match(r"^[.\w]*\s*\(?\.\w+\)?$", label)


def _rows(text: str):
    """相鄰的按鈕（中間只有空白）算同一排。"""
    btns = [(m.start(), m.end(), m.group(1), m.group(2)) for m in _BTN.finditer(text)]
    cur: list = []
    for b in btns:
        if cur and text[cur[-1][1] : b[0]].strip() == "":
            cur.append(b)
        else:
            if len(cur) > 1:
                yield cur
            cur = [b]
    if len(cur) > 1:
        yield cur


def test_buttons_in_one_row_agree_on_icons() -> None:
    bad: list[str] = []
    checked = 0
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        for row in _rows(text):
            keep = [b for b in row if not _NOT_A_ROW.search(b[2])]
            if len(keep) < 2:
                continue
            labels = [_label(b[3]) for b in keep]
            if not all(_is_action_label(l) for l in labels):
                continue
            checked += 1
            has = [bool(_HAS_ICON.search(b[3])) for b in keep]
            if any(has) and not all(has):
                line = text[: keep[0][0]].count("\n") + 1
                shown = " | ".join(
                    ("有" if h else "缺") + "圖示：" + l for h, l in zip(has, labels)
                )
                bad.append(f"{path.relative_to(ROOT)}:{line}  {shown}")
    # 掃 0 排跟「掃過都乾淨」在 pytest 輸出裡長得一樣 —— 釘住它真的有看到東西。
    assert checked >= 40, f"只檢查到 {checked} 排按鈕，判準可能失效了"
    assert not bad, "同一排按鈕的圖示不一致（畫面上看得出來，其他檢查抓不到）：\n" + "\n".join(bad)


def test_iconed_buttons_are_not_overwritten_by_textcontent() -> None:
    bad: list[str] = []
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        iconed = {
            m.group(1)
            for m in _BTN_ID.finditer(text)
            if _HAS_ICON.search(m.group(2))
        }
        for bid in sorted(iconed):
            pat = (
                r"(?:\$\(\s*['\"]%s['\"]\s*\)|getElementById\(\s*['\"]%s['\"]\s*\))"
                r"\s*\.textContent\s*=" % (re.escape(bid), re.escape(bid))
            )
            for m in re.finditer(pat, text):
                line = text[: m.start()].count("\n") + 1
                bad.append(
                    f"{path.relative_to(ROOT)}:{line}  #{bid} 有圖示卻被 textContent 覆寫"
                )
    assert not bad, (
        "有圖示的按鈕被 JS 用 textContent 洗掉（圖示會無聲消失）：\n"
        + "\n".join(bad)
        + "\n文字請放自己的 <span>，JS 只改那個 span。"
    )
