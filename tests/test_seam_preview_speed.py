"""騎縫章預覽：只蓋要看的那一頁。

使用者回報「這頁跑好久」「預覽一直沒出來」。正式機日誌：每個預覽請求
**90～104 秒**，而且前端同時發 20 個。

根因不是 DPI（預覽只有 78 dpi）—— 是**每看一頁預覽就把章蓋滿整份 PDF**，
然後只取其中一頁算圖。52 頁的文件 × 20 個並行請求 = 1040 頁份的合成工作。

修法：版位仍照整份文件算（**切片編號取決於該頁在「組」裡的位置，只算一頁
會算錯**），但影像合成只做需要的那一頁。

**這裡最重要的判準是正確性，不是速度**：預覽必須跟最終產出長得一模一樣，
否則使用者看到的是 A、印出來的是 B。
"""
from __future__ import annotations

import io

import fitz
import pytest
from PIL import Image

from app.tools.pdf_seam_stamp import seam_core as SC


@pytest.fixture(scope="module")
def doc_bytes():
    d = fitz.open()
    for i in range(12):
        d.new_page(width=595, height=842).insert_text(
            (72, 100), f"page {i + 1}", fontsize=14)
    raw = d.tobytes()
    d.close()
    return raw


@pytest.fixture(scope="module")
def stamp_png():
    im = Image.new("RGBA", (600, 200), (200, 20, 20, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _spec():
    return SC.SeamSpec(mode="side", group=3, edge="right", size_mm=40,
                       offset_mm=3, pos_mm=0, angle_deg=0, opacity=1.0)


def _render(raw, png, page_no, only):
    doc = fitz.open(stream=raw, filetype="pdf")
    SC.apply_seam(doc, png, _spec(), only_pages=only)
    pix = doc[page_no].get_pixmap(dpi=72, alpha=False)
    data = pix.tobytes("png")
    doc.close()
    return data


@pytest.mark.parametrize("page_no", [0, 1, 2, 5, 11])
def test_preview_matches_the_real_output(doc_bytes, stamp_png, page_no):
    """**只蓋一頁的結果，必須跟蓋滿整份之後那一頁一模一樣。**

    這是整個最佳化的前提。差一點點都不行 —— 使用者看到的是預覽，
    收件方看到的是產出。
    """
    full = _render(doc_bytes, stamp_png, page_no, None)
    one = _render(doc_bytes, stamp_png, page_no, {page_no})
    assert one == full, f"第 {page_no + 1} 頁的預覽跟實際產出不一樣"


def test_only_pages_really_skips_the_others(doc_bytes, stamp_png):
    """沒被指定的頁不可以被蓋到 —— 否則等於沒省到工。"""
    doc = fitz.open(stream=doc_bytes, filetype="pdf")
    SC.apply_seam(doc, stamp_png, _spec(), only_pages={0})
    counts = [len(doc[i].get_images()) for i in range(doc.page_count)]
    doc.close()
    assert counts[0] > 0, "指定的那一頁沒有被蓋章"
    assert sum(counts[1:]) == 0, f"其他頁也被蓋了：{counts}"


def test_full_apply_is_unchanged(doc_bytes, stamp_png):
    """不傳 only_pages 時行為完全不變 —— 送出走的是這條路。"""
    doc = fitz.open(stream=doc_bytes, filetype="pdf")
    SC.apply_seam(doc, stamp_png, _spec())
    counts = [len(doc[i].get_images()) for i in range(doc.page_count)]
    doc.close()
    assert all(c > 0 for c in counts), f"有頁面沒被蓋到：{counts}"


def test_preview_is_much_cheaper(doc_bytes, stamp_png):
    """省下來的工作量要跟頁數成正比 —— 用「蓋了幾張圖」量，不用計時。

    計時在小樣本上會被雜訊淹沒，而且在不同機器上不穩定；
    「蓋了幾張圖」是確定的數字。
    """
    def stamped_images(only):
        doc = fitz.open(stream=doc_bytes, filetype="pdf")
        SC.apply_seam(doc, stamp_png, _spec(), only_pages=only)
        n = sum(len(doc[i].get_images()) for i in range(doc.page_count))
        doc.close()
        return n

    full = stamped_images(None)
    one = stamped_images({4})
    assert one * 5 <= full, f"只蓋一頁還做了 {one} 張圖（整份是 {full}）"


def test_the_page_hint_tells_users_they_need_not_wait():
    """介面要講「不必等預覽跑完」—— 使用者以為要等它全部好才能送出。"""
    from pathlib import Path
    html = Path("app/tools/pdf_seam_stamp/templates/pdf_seam_stamp.html"
                ).read_text(encoding="utf-8")
    assert "不必等" in html or "不用等" in html, "沒有告訴使用者可以直接送出"
