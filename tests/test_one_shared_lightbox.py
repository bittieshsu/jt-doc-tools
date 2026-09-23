"""放大檢視（lightbox）只留一份共用實作。

2026-09-14 使用者要求「騎縫章的預覽要可以點下去看大圖」。盤點才發現全站
**已經有 13 份各自實作**（`md2-` / `dd-` / `an-` / `ws-` / `p2i-` / `i2p-` /
`bd-` / `as-` / `af-` / `dt-` / `page-` …），每一份的鍵盤操作、關閉方式、
有沒有翻頁都不一樣。

這正是本專案最常復發的那一類 ——「同一份東西寫在 N 個地方」：
工作區收得下的副檔名、下載鈕標籤、文件數字、Office 相依偵測的式子…
每一次的症狀都是「其中一份改了，另一份沒跟上，而且無聲」。

**這條檢查不強迫回頭改那 13 份**（那是獨立的一輪工作，每一支都要重測），
它只擋住**再長出第 14 份**。既有的列成例外清單，清單本身也要有檢查 ——
不然檔案改名 / 刪掉之後，例外會變成永遠沒人動的死條目。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 共用元件（新的一律用這個）
SHARED_JS = ROOT / "static" / "js" / "lightbox.js"

#: 2026-09-14 盤點當下就存在的 13 份。**只出不進。**
#: 之後要收斂的話是逐支改成 `data-lightbox` ＋ 載入 `lightbox.js`，
#: 每支都要重看預覽（翻頁範圍、圖說、鍵盤操作會變）。
_LEGACY = {
    "app/tools/doc_deident/templates/doc_deident.html",
    "app/tools/image_to_pdf/templates/image_to_pdf.html",
    "app/tools/markdown_to_doc/templates/markdown_to_doc.html",
    "app/tools/pdf_annotations/templates/pdf_annotations.html",
    "app/tools/pdf_annotations_flatten/templates/pdf_annotations_flatten.html",
    "app/tools/pdf_annotations_strip/templates/pdf_annotations_strip.html",
    "app/tools/pdf_border/templates/pdf_border.html",
    "app/tools/pdf_extract_images/templates/pdf_extract_images.html",
    "app/tools/pdf_pageno/templates/pdf_pageno.html",
    "app/tools/pdf_pages/templates/pdf_pages.html",
    "app/tools/pdf_rotate/templates/pdf_rotate.html",
    "app/tools/pdf_to_image/templates/pdf_to_image.html",
    "app/web/templates/my_workspace.html",
}

_OWN_CLASS = re.compile(r"\.([a-z0-9]+-lightbox)\b")


def _owners() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for p in sorted((ROOT / "app").rglob("*.html")):
        hits = set(_OWN_CLASS.findall(p.read_text(encoding="utf-8")))
        hits.discard("jt-lightbox")          # 共用元件本身
        if hits:
            out[p.relative_to(ROOT).as_posix()] = hits
    return out


def test_no_new_private_lightbox():
    """新的放大檢視一律用 `data-lightbox` ＋ `static/js/lightbox.js`。"""
    new = sorted(set(_owners()) - _LEGACY)
    assert new == [], (
        f"這些樣板自己又寫了一份放大檢視：{new}。"
        " 請改用共用元件：容器加 `data-lightbox`，"
        " `{% block scripts %}` 載入 `/static/js/lightbox.js`。")


def test_the_legacy_list_has_not_gone_stale():
    """例外清單自己也要是對的 —— 留著沒必要的豁免比沒有豁免更糟。"""
    owners = _owners()
    gone = sorted(k for k in _LEGACY if not (ROOT / k).exists())
    assert gone == [], f"例外清單指到不存在的檔案：{gone}"
    cleaned = sorted(k for k in _LEGACY if k not in owners)
    assert cleaned == [], (
        f"這幾支已經不再自己寫放大檢視了，請從例外清單刪掉：{cleaned}")


def test_the_shared_component_exists_and_is_wired_somewhere():
    """共用元件要真的存在、而且真的有人用 —— 沒人用的共用元件會慢慢腐爛。"""
    assert SHARED_JS.exists(), "缺 static/js/lightbox.js"
    src = SHARED_JS.read_text(encoding="utf-8")
    assert "data-lightbox" in src and "window.jtLightbox" in src
    users = [p for p in (ROOT / "app").rglob("*.html")
             if "/static/js/lightbox.js" in p.read_text(encoding="utf-8")]
    assert users, "共用元件沒有任何樣板載入它"


def test_the_shared_component_does_not_inject_styles():
    """CSP 的 style-src 沒有 unsafe-inline —— JS 注入的 `<style>` 會被整段擋掉，
    **而且不會有 JS 例外**，只是元件變成沒有樣式的 DOM 攤在頁尾。"""
    src = SHARED_JS.read_text(encoding="utf-8")
    assert "createElement('style')" not in src and 'createElement("style")' not in src
    css = (ROOT / "static" / "css" / "platform.css").read_text(encoding="utf-8")
    for cls in (".jt-lightbox", ".jt-lb-img", ".jt-lb-nav", ".jt-lb-close", ".jt-lb-cap"):
        assert cls in css, f"{cls} 沒有樣式 —— 自己發明的類別名一定要補樣式"
