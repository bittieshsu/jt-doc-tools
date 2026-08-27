"""兩個「看不到 JS 例外、只有畫面怪怪的」樣板雷的守門。

兩者的共同點是**沒有任何錯誤訊息**：伺服器回 200、console 乾淨、
自動化的「頁面有沒有 JS 錯誤」檢查全綠，只有真的用眼睛看的人發現不對。

1. `{% block scripts %}` 裡不可以放看得見的標記。
   base.html 的 scripts 區塊在 `<main>` **外面**（`</body>` 前），
   放進去的卡片不受內容欄約束 → 攤成整個視窗寬、左邊壓到側欄底下。
   v1.14.60 把「可上傳的檔案大小」搬到頁面最下面時就是這樣搬錯的。

2. 行內 JS 裡不可以出現 script 結束標籤的字面寫法（**連註解裡都不行**）。
   HTML 剖析器不管它在不在 JS 註解或字串裡，看到就關掉 script，
   後面的程式碼全部變成畫面上的純文字。issue #15 記過一次，
   v1.14.61 又在「解釋這條規則」的註解裡踩到第二次。

   偵測方式不是找那個字面寫法（找不到 —— 對剖析器來說它就是合法的結尾），
   而是反過來看**兩個 script 區塊之間漏出了 JS**。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = sorted(REPO.glob("app/**/templates/**/*.html"))
IDS = [str(p.relative_to(REPO)) for p in TEMPLATES]

_SCRIPTS_BLOCK = re.compile(r"\{%-?\s*block\s+scripts\s*-?%\}(.*?)\{%-?\s*endblock", re.S)
_SCRIPT_OPEN = re.compile(r"<script\b[^>]*>", re.I)
# 只認一眼就知道是程式碼的東西，避免把中文說明誤判成 JS
_LOOKS_LIKE_JS = re.compile(r"(^|\s)(const |let |var |function |document\.|window\.|=>)")


@pytest.mark.parametrize("path", TEMPLATES, ids=IDS)
def test_scripts_block_holds_no_visible_markup(path: Path):
    src = path.read_text(encoding="utf-8")
    for m in _SCRIPTS_BLOCK.finditer(src):
        body = m.group(1)
        body = re.sub(r"<script\b.*?</script\s*>", "", body, flags=re.S | re.I)
        body = re.sub(r"<style\b.*?</style\s*>", "", body, flags=re.S | re.I)
        body = re.sub(r"\{#.*?#\}", "", body, flags=re.S)
        tags = sorted(set(re.findall(r"<([a-zA-Z][a-zA-Z0-9-]*)", body)))
        assert not tags, (
            f"{path.name}：`block scripts` 裡有看得見的標記 {tags}。"
            " 那個區塊在 <main> 外面，放進去的東西會攤成整個視窗寬 —— 搬到 block content。"
        )


@pytest.mark.parametrize("path", TEMPLATES, ids=IDS)
def test_no_js_leaks_outside_script_tags(path: Path):
    src = path.read_text(encoding="utf-8")
    pos = 0
    while True:
        opened = _SCRIPT_OPEN.search(src, pos)
        if not opened:
            return
        end = src.find("</script>", opened.end())
        if end == -1:
            return
        nxt = _SCRIPT_OPEN.search(src, end)
        gap = src[end + len("</script>"): nxt.start() if nxt else len(src)]
        assert not _LOOKS_LIKE_JS.search(gap), (
            f"{path.name}:{src[:end].count(chr(10)) + 1} 之後有 JS 漏到 script 區塊外面。"
            " 多半是行內 JS 裡（含註解）寫了 script 結束標籤的字面寫法，"
            " 剖析器在那裡就把 script 關掉了 —— 後面的程式碼會變成畫面上的純文字。"
        )
        pos = end + len("</script>")
