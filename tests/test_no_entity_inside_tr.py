"""樣板的 `tr('…')` 裡面不可以寫 HTML 字元參照（`&#10;` / `&nbsp;` …）。

## 由來（v1.15.68，使用者回報）

會議摘要的「直接貼上逐字稿」那個輸入框，提示文字**畫面上直接印出
`&#10;`**，而不是換行。

寫的時候的想法是「`&#10;` 是 HTML 的換行，瀏覽器會解開」——
**在屬性裡確實會**，但那要它真的以 `&#10;` 的形式到達瀏覽器。
`tr()` 的輸出會被 Jinja **自動跳脫**，`&` 變成 `&amp;`，
於是瀏覽器看到的是 `&amp;#10;`，畫出來就是 `&#10;` 這六個字。

**同一份樣板裡其實有寫對的寫法**（書籤、去識別化那幾支）：
`&#10;` 放在 **`tr()` 外面**，那是樣板的原始 HTML，不會被跳脫。
所以判準不是「不可以用字元參照」，是「**不可以寫在 `tr()` 的引數裡**」。

## 為什麼要檢查

三種語言的語系檔裡**都已經跟著抄了那個 `&#10;`**（翻譯的時候原樣保留），
所以這個錯會一路複製到每一種語言，而且**只有把那一頁打開來看才發現得了**
—— 沒有例外、沒有紅字，就只是提示文字裡多了幾個怪字。
"""
from __future__ import annotations

import pathlib
import re

import pytest

import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
from tools.source_text import strip_markup_comments

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: `tr('…')` / `tr("…")` 的引數。逐字元走過太重，這裡只要抓「引號到引號」
#: 就夠 —— 字元參照裡不會有引號，所以配對錯了也不會漏報這一類。
_TR_ARG = re.compile(r"\btr\(\s*(['\"])(.*?)\1", re.S)

#: HTML 字元參照：`&#10;` / `&#x0A;` / `&nbsp;` / `&amp;` …
_ENTITY = re.compile(r"&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]{1,31});")


def _templates() -> list[pathlib.Path]:
    return sorted(p for p in (ROOT / "app").rglob("*.html")
                  if "__pycache__" not in p.parts)


def test_the_scan_actually_reaches_the_templates():
    """「掃 0 個檔」跟「掃過都乾淨」在 pytest 輸出裡長得一模一樣。"""
    files = _templates()
    assert len(files) > 50, f"只掃到 {len(files)} 份樣板，這條檢查等於沒有執行"
    hits = sum(len(_TR_ARG.findall(strip_markup_comments(p.read_text(encoding='utf-8'))))
               for p in files)
    assert hits > 200, f"整個 app/ 只找到 {hits} 個 tr() 引數，判準可能已經失效"


@pytest.mark.parametrize("path", _templates(), ids=lambda p: p.name)
def test_no_html_entity_inside_a_tr_argument(path: pathlib.Path):
    # 註解裡會**引用**那個錯誤寫法當反例（use vs mention，本專案踩過很多次）
    src = strip_markup_comments(path.read_text(encoding="utf-8"))
    bad = []
    for m in _TR_ARG.finditer(src):
        ents = _ENTITY.findall(m.group(2))
        if ents:
            line = src[:m.start()].count("\n") + 1
            bad.append(f"第 {line} 行：{['&' + e + ';' for e in ents]}")
    assert not bad, (
        f"{path.relative_to(ROOT)} 的 tr() 引數裡有 HTML 字元參照：\n"
        + "\n".join(bad)
        + "\n\n`tr()` 的輸出會被自動跳脫，`&` 會變成 `&amp;`，"
          "畫面上就印出 `&#10;` 這種字。\n"
          "**把字元參照放到 tr() 外面**，一行一個 tr()（書籤、去識別化那幾支"
          "本來就是這樣寫的）。\n"
          "不要改成在 Jinja 字串裡寫 \\n —— 那樣執行期是真的換行，但"
          "**抽鍵的掃描器讀的是原始碼**，抓到的是 `\\n` 兩個字元，"
          "語系檔的鍵就跟執行期查的鍵對不起來（實際踩過）。")
