"""自家的介面圖片不可以大到離譜（v1.14.61）。

起因：兩張預設商標圖都是 **8571 像素見方**、合計 1.6 MB，而它們實際顯示的
最大尺寸是 240 像素；深色版那張更只顯示在 38 像素見方的側欄裡，**每一頁都會
下載一次**。這種問題不會有任何症狀 —— 畫面完全正確，只是每個人每次開頁都
多付一次流量；是全站版面溢出掃描（樣式套用前圖片用原始尺寸撐開頁面）順手
抓到的。

`static/vendor/` 是外部套件（PDF.js 等），不歸我們管，排除。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "static"
MAX_BYTES = 200 * 1024
SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}


def _own_images() -> list[Path]:
    if not STATIC.is_dir():
        return []
    return sorted(
        p for p in STATIC.rglob("*")
        if p.is_file() and p.suffix.lower() in SUFFIXES
        and "vendor" not in p.relative_to(STATIC).parts
    )


IMAGES = _own_images()


def test_there_are_images_to_check():
    # 免得比對邏輯壞掉時整支測試安靜地什麼都沒驗
    assert IMAGES, "static/ 裡一張自家圖片都沒掃到，過濾條件壞了"


@pytest.mark.parametrize("img", IMAGES, ids=[str(p.relative_to(REPO)) for p in IMAGES])
def test_ui_image_is_not_oversized(img: Path):
    size = img.stat().st_size
    assert size <= MAX_BYTES, (
        f"{img.relative_to(REPO)} 有 {size / 1024:.0f} KB —— 介面圖片請先縮到實際顯示尺寸"
        f"（單檔上限 {MAX_BYTES // 1024} KB）。這種檔案不會有任何症狀，只是每個人每次開頁都多付一次流量。"
    )
