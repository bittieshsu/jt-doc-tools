"""寫進 PDF 的中文**必須畫得出來**。

## 由來（v1.14.22 正式機故障）

v1.14.19 加了字型子集化（整支 Noto CJK 16 MB 會被嵌進每一份 PDF，
一張填 30 個字的表單變成 13 MB）。子集化本身沒錯，錯在**預設會把 glyph
重新編號** —— 而 Noto CJK 這類是 CID-keyed CFF，MuPDF 是用**原始的
glyph id** 去取字形，編號一對不上就什麼都畫不出來。

這個故障極難察覺，因為壞掉的只有「畫面」：

- 文字層完好 —— 搜尋、複製、`get_text()` 抽取全部正常
- 檔案還變小了，看起來一切都對
- 當時的保險只檢查 `cmap` 有沒有那個字碼；字碼在、字形畫不出來，所以沒攔到
- 我驗收時只確認「13 MB → 48 KB」，**沒有重新算圖看字還在不在**

結果是正式機上**表單填寫 / 用印 / 頁碼 / 浮水印寫進去的中文全部隱形**。

所以這一份的每一項都**算圖數墨水**，不看文字層 —— 字型對不對只有渲染器
說了算。任何「文字明明抽得到」的斷言在這個故障面前都會通過。
"""
from __future__ import annotations

import fitz
import pytest

#: 一頁 A4 在 100 dpi 下，一個 20pt 的中文字大約占幾百個深色像素。
#: 門檻取 300 —— 空白頁是 0～200（抗鋸齒雜訊），畫得出來一定遠高於此。
INK_MIN = 300


def ink(page, dpi: int = 100) -> int:
    """這一頁上有多少深色像素。

    `pix.samples` 是 property，**每次存取都會重建整份緩衝** ——
    一定要先綁成區域變數，放在迴圈條件裡會慢到像當掉（寫這份測試時踩過）。
    """
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    data = pix.samples
    n = len(data)
    return sum(1 for i in range(0, n, 3) if data[i] < 200)


@pytest.fixture(scope="module")
def cjk_font():
    from app.core import font_catalog
    best = font_catalog.best_cjk_path("sans", "traditional")
    if not best:
        pytest.skip("這台機器沒有中文字型")
    return str(best[0]), (best[1] if len(best) > 1 else 0)


# ------------------------------------------------------------ 子集化本身

def test_subset_font_actually_renders(cjk_font):
    """子集化後的字型必須真的畫得出字 —— 這是當初漏掉的那一關。"""
    from app.core import font_catalog
    path, idx = cjk_font
    text = "測試中文字"
    buf = font_catalog.subset_font(path, idx, text)
    assert buf, "子集化回 None（退回完整字型也算安全，但這裡預期成功）"

    doc = fitz.open()
    page = doc.new_page(width=400, height=160)
    page.insert_font(fontname="t", fontbuffer=buf)
    page.insert_text((30, 60), text, fontname="t", fontsize=24)
    assert ink(page) > INK_MIN, (
        "子集化後的字型畫不出中文（文字層仍然正常，所以只有算圖抓得到）")


def test_subset_is_much_smaller_than_the_whole_font(cjk_font):
    """修正不可以把子集化整個放棄 —— 整支嵌進去會讓 PDF 變十幾 MB。"""
    import os

    from app.core import font_catalog
    path, idx = cjk_font
    full = font_catalog._extract_subfont(path, os.path.getmtime(path), idx)
    sub = font_catalog.subset_font(path, idx, "測試中文字")
    assert sub and full
    assert len(sub) < len(full) / 5, (
        f"子集化沒有明顯變小：{len(full)} → {len(sub)}")


def test_coverage_check_rejects_a_font_that_renders_blank():
    """保險要擋得住「畫不出來」，不是只看 cmap 有沒有字碼。"""
    from app.core import font_catalog
    assert font_catalog._renders_ink(b"not a font at all", "測") is False


# ------------------------------------------------------------ 實際產出

def test_overlay_text_writes_visible_chinese(tmp_path):
    """表單填寫 / 用印 / 頁碼 / 浮水印共用這條路 —— 壞掉是全站級的。"""
    from app.core import pdf_text_overlay as PT
    src, dst = tmp_path / "a.pdf", tmp_path / "b.pdf"
    d = fitz.open()
    d.new_page(width=595, height=842)
    d.save(str(src))
    d.close()

    PT.overlay_text(src, dst, [PT.TextPlacement(
        page=0, text="測試中文字", slot=(100, 680, 400, 720),
        base_font_size=20)])

    out = fitz.open(str(dst))
    assert "測試中文字" in out[0].get_text(), "文字層都沒有，那是另一個問題"
    assert ink(out[0]) > INK_MIN, (
        "中文寫進去了但**畫不出來** —— 文字層正常、畫面空白，"
        "正是 v1.14.19 那個故障的樣子")


def test_toc_page_shows_its_titles(tmp_path):
    """目錄頁是拿來看、拿來印的 —— 標題畫不出來這個工具就沒有意義。"""
    from app.tools.pdf_bookmark import bookmark_core as BC
    d = fitz.open()
    for _ in range(5):
        d.new_page(width=595, height=842)
    n = BC.build_toc_page(
        d, [BC.BookmarkItem(title="第一章 緒論", page=3, level=1),
            BC.BookmarkItem(title="第二章 方法", page=4, level=1)],
        BC.TocPageSpec(title="目錄"))
    assert n == 1
    assert ink(d[0]) > INK_MIN, "目錄頁的中文標題沒有畫出來"
