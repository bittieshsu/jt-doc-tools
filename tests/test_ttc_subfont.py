"""`.ttc` 要挑對子字型，否則寫進 PDF 的中文是**日文字形**。

## 由來

`.ttc` 是字型集合：一個檔案裡包好幾套字。Linux 常見的 `NotoSansCJK-Regular.ttc`
有 10 套（JP / KR / SC / TC / HK × 一般 + 等寬），而 **index 0 是 JP**。
`font_catalog` 的系統掃描一律硬寫 `idx: 0`，`pdf_text_overlay` 更是完全不經過
catalog、直接把檔案路徑丟給 PyMuPDF（同樣用第 0 套）。

殺傷力在於**完全看不出來**：字都印得出來、不缺字，只是寫法是日文的。實測台灣商務
表單常用的 55 個字裡有 36 個不一樣 —— 公司、電話、地址、銀行、帳戶、簽章、統編、
聯絡…幾乎每個欄位名都中招。最典型的是「海」：日文寫「毎」（一橫），台灣寫「每」
（兩點）。

CLAUDE.md v1.11.40 就記過同一個雷（用印的個資限用章），但當時只修了那一處。

## 技術限制

**PyMuPDF 的公開 API 完全沒有 ttc 索引參數**（`Font()` / `insert_font()` /
`insert_text()` 都沒有），`fontfile` 永遠用第 0 套。要用別套只能用 fontTools 把
子字型抽成位元組，再走 `fontbuffer`。Pillow 則有 `truetype(index=)` 可用。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core import font_catalog as fc

_NOTO = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
_has_noto = _NOTO.exists()
needs_noto = pytest.mark.skipif(not _has_noto, reason="測試機沒有 Noto CJK ttc")


# ------------------------------------------------------------ 索引挑選

@needs_noto
def test_subfont_names_are_readable():
    names = fc._ttc_subfont_names(_NOTO)
    assert len(names) >= 4, f"讀不到子字型清單：{names}"
    assert any("JP" in n for n in names) and any("TC" in n for n in names)


@needs_noto
def test_index_zero_is_japanese():
    """**這就是問題的根源** —— 沒有人挑的話拿到的是第 0 套。"""
    assert "JP" in fc._ttc_subfont_names(_NOTO)[0]


@needs_noto
@pytest.mark.parametrize("cjk,tag", [("traditional", "TC"),
                                     ("simplified", "SC"),
                                     ("japanese", "JP")])
def test_index_matches_the_requested_script(cjk, tag):
    idx = fc._ttc_index_for(str(_NOTO), os.path.getmtime(_NOTO), cjk)
    assert tag in fc._ttc_subfont_names(_NOTO)[idx]


@needs_noto
def test_mono_variants_are_not_picked():
    """等寬那幾套也帶 TC 標記 —— 正文用等寬會很怪。"""
    idx = fc._ttc_index_for(str(_NOTO), os.path.getmtime(_NOTO), "traditional")
    assert "MONO" not in fc._ttc_subfont_names(_NOTO)[idx].upper()


def test_non_ttc_returns_zero(tmp_path):
    f = tmp_path / "x.ttf"
    f.write_bytes(b"not a font")
    assert fc._ttc_index_for(str(f), 0, "traditional") == 0


def test_unknown_script_returns_zero():
    if not _has_noto:
        pytest.skip("no font")
    assert fc._ttc_index_for(str(_NOTO), os.path.getmtime(_NOTO), None) == 0


def test_broken_file_does_not_raise(tmp_path):
    """挑不到就用第 0 套 —— 絕不可以因為挑不到而整個印不出來。"""
    f = tmp_path / "broken.ttc"
    f.write_bytes(b"\x00" * 64)
    assert fc._ttc_index_for(str(f), 0, "traditional") == 0
    assert fc.embeddable_font(f, 3) == (str(f), None)


# ------------------------------------------------------------ 取出可嵌入的位元組

@needs_noto
def test_embeddable_returns_bytes_for_nonzero_index():
    ff, buf = fc.embeddable_font(_NOTO, 3)
    assert ff is None and buf and len(buf) > 100_000


@needs_noto
def test_embeddable_index_zero_stays_a_path():
    """第 0 套不必抽 —— 抽出來要好幾 MB，白佔記憶體。"""
    ff, buf = fc.embeddable_font(_NOTO, 0)
    assert ff == str(_NOTO) and buf is None


@needs_noto
def test_extracted_subfont_is_actually_the_tc_one():
    """抽出來的要真的是繁中那一套 —— 抽錯等於沒修。"""
    import fitz
    idx = fc._ttc_index_for(str(_NOTO), os.path.getmtime(_NOTO), "traditional")
    _ff, buf = fc.embeddable_font(_NOTO, idx)
    d = fitz.open()
    pg = d.new_page()
    pg.insert_font(fontname="F", fontbuffer=buf)
    pg.insert_text((50, 100), "海過郎", fontname="F", fontsize=20)
    names = {f[3] for f in pg.get_fonts()}
    d.close()
    assert any("TC" in n for n in names), f"嵌進去的是 {names}"


# ------------------------------------------------------------ 目錄掃描

@needs_noto
def test_catalog_records_the_right_index():
    """掃描時就要挑好 —— 呼叫端只看 `idx`，不會自己再想一次。"""
    ttcs = [f for f in fc.list_fonts() if str(f.get("path", "")).endswith(".ttc")]
    if not ttcs:
        pytest.skip("目錄裡沒有 ttc")
    assert any(f["idx"] > 0 for f in ttcs), \
        "所有 ttc 的 idx 都是 0 —— 掃描沒有挑子字型"


def test_scan_never_hardcodes_zero_for_system_fonts():
    """靜態守門：系統字型掃描不可以再寫死 `idx: 0`。"""
    import inspect
    src = inspect.getsource(fc)
    i = src.index('"id": f"system:{p}"')
    seg = src[i:i + 500]
    assert '"idx": 0' not in seg, "系統字型掃描又寫死 idx=0 了"


# ------------------------------------------------------------ 實際輸出

@needs_noto
def test_pdf_fill_embeds_the_traditional_subfont(tmp_path):
    """**端到端**：表單填寫寫出去的 PDF，嵌的必須是繁中那一套。

    這是使用者真正拿到的東西 —— 前面幾條都對但這裡錯，功能等於沒修。
    """
    import fitz
    from app.tools.pdf_fill import service
    src, dst = tmp_path / "f.pdf", tmp_path / "o.pdf"
    d = fitz.open()
    pg = d.new_page(width=595, height=842)
    pg.insert_font(fontname="L", fontfile=str(_NOTO))
    pg.insert_text((60, 100), "公司全名：", fontname="L", fontsize=12)
    pg.draw_line(fitz.Point(160, 103), fitz.Point(520, 103))
    d.save(str(src))
    d.close()
    service.fill_pdf(src, dst, {"company_name": "節省海運股份有限公司"})
    names = {f[3] for f in fitz.open(str(dst))[0].get_fonts()}
    assert any("CJK TC" in n for n in names), f"填入的文字用了 {names}"


@needs_noto
def test_pageno_embeds_the_traditional_subfont(tmp_path):
    import sys

    import fitz
    import app.tools.pdf_pageno.router  # noqa: F401 — 讓子模組進 sys.modules
    m = sys.modules["app.tools.pdf_pageno.router"]
    p = tmp_path / "p.pdf"
    d = fitz.open()
    d.new_page(width=595, height=842)
    d.save(str(p))
    d.close()
    doc = fitz.open(str(p))
    m._draw_pageno(doc[0], page_index=0, total=1, position="bc",
                   fmt="第 {n} 頁", start=1, font_size=14, margin_mm=10,
                   color_hex="#000000")
    out = tmp_path / "o.pdf"
    doc.save(str(out))
    names = {f[3] for f in fitz.open(str(out))[0].get_fonts()}
    assert any("CJK TC" in n for n in names), f"頁碼用了 {names}"


def test_watermark_passes_an_index():
    """浮水印走 Pillow，`truetype()` 有 index 參數 —— 每一處都要帶。"""
    import inspect

    from app.tools.pdf_watermark import service as ws
    src = inspect.getsource(ws)
    i = src.index("def _load_font") if "def _load_font" in src else 0
    seg = src[i:]
    calls = seg.count("ImageFont.truetype(")
    with_idx = seg.count("index=_tc_index")
    assert with_idx >= calls - 1, \
        f"{calls} 處載入字型只有 {with_idx} 處帶了子字型索引"


# ------------------------------------------------------------ 相依宣告

def test_fonttools_is_declared_not_just_transitive():
    """fontTools 本來只是 pdf2docx 的傳遞相依 —— 上游哪天換掉，這個功能會
    **無聲**退回日文字形（`except` 會吞掉 ImportError）。必須自己宣告。"""
    root = Path(fc.__file__).resolve().parent.parent.parent
    for name in ("pyproject.toml", "requirements.txt"):
        txt = (root / name).read_text(encoding="utf-8")
        assert "fonttools" in txt.lower(), f"{name} 沒有宣告 fonttools"


# ------------------------------------------------------------ 字型子集化

@needs_noto
def test_subset_is_dramatically_smaller():
    """整支 CJK 字型十幾 MB —— 只嵌用到的字才不會讓一張表單變成 13 MB。

    **門檻是 15 倍不是 50 倍，而且不可以再往上調。** 子集化必須保留原本的
    字形編號（`retain_gids`），否則繪製引擎取不到字形，寫進 PDF 的中文
    **看不見**（v1.14.19 ~ v1.14.21 正式機故障；當時正是因為壓到 8 KB 才
    「看起來很成功」）。保留編號讓檔案比極限壓縮大一些，那是正確性的代價。
    實測 16.4 MB → 843 KB（19.5 倍），門檻取 15 留一點餘裕給不同字集。
    要再往下壓之前，先讓 `tests/test_cjk_font_renders.py` 全綠。
    """
    idx = fc._ttc_index_for(str(_NOTO), os.path.getmtime(_NOTO), "traditional")
    _ff, full = fc.embeddable_font(_NOTO, idx)
    sub = fc.subset_font(_NOTO, idx, "節省股份有限公司")
    assert sub and len(sub) < len(full) / 15, \
        f"沒縮多少：{len(full)} → {len(sub) if sub else 'None'}"


@needs_noto
def test_subset_keeps_every_character_we_asked_for():
    """缺字在畫面上是看不見的方框 —— 使用者不會發現，收件方才會。"""
    import io

    from fontTools.ttLib import TTFont
    text = "節省股份有限公司臺北市中正區忠孝東路一段號統一編號"
    idx = fc._ttc_index_for(str(_NOTO), os.path.getmtime(_NOTO), "traditional")
    sub = fc.subset_font(_NOTO, idx, text)
    cmap = TTFont(io.BytesIO(sub), lazy=True).getBestCmap()
    missing = [c for c in text if ord(c) not in cmap]
    assert not missing, f"子集化後少了：{missing}"


@needs_noto
def test_subset_keeps_digits_and_punctuation_even_if_unused():
    """排版過程可能插入數字 / 標點 —— 沒要求也要留著。"""
    import io

    from fontTools.ttLib import TTFont
    idx = fc._ttc_index_for(str(_NOTO), os.path.getmtime(_NOTO), "traditional")
    sub = fc.subset_font(_NOTO, idx, "節省")     # 完全沒有數字
    cmap = TTFont(io.BytesIO(sub), lazy=True).getBestCmap()
    for ch in "0123456789，。（）":
        assert ord(ch) in cmap, f"少了 {ch}"


@needs_noto
def test_subset_is_cached_by_charset_not_by_string():
    """頁碼每一頁文字都不同（第 1 頁 / 第 2 頁…）但**字元集合一樣**。

    不依集合快取的話每頁都要重跑一次子集化 —— 實測 20 頁要 19 秒。
    """
    idx = fc._ttc_index_for(str(_NOTO), os.path.getmtime(_NOTO), "traditional")
    fc._subset_cached.cache_clear()
    fc.subset_font(_NOTO, idx, "第 1 頁")
    misses_after_first = fc._subset_cached.cache_info().misses
    for n in range(2, 12):
        fc.subset_font(_NOTO, idx, f"第 {n} 頁")
    assert fc._subset_cached.cache_info().misses == misses_after_first, \
        "同一組字元集合又重算了一次"


@needs_noto
def test_coverage_failure_falls_back_to_the_full_font(monkeypatch):
    """**這是最重要的保險**：子集化結果缺字時要整份放棄、用回完整字型。

    檔案大總比印出看不見的方框好。
    """
    monkeypatch.setattr(fc, "_covers", lambda data, text: False)
    fc._subset_cached.cache_clear()
    assert fc.subset_font(_NOTO, 3, "節省") is None
    ff, buf = fc.embeddable_font(_NOTO, 3, text="節省")
    assert buf and len(buf) > 1_000_000, "沒有退回完整字型"


@needs_noto
def test_subset_failure_never_breaks_rendering(monkeypatch):
    """子集化炸掉時要安靜退回，不可以讓整份文件印不出來。"""
    def boom(*a, **k):
        raise RuntimeError("壞掉了")
    monkeypatch.setattr(fc, "_full_font_bytes", boom)
    fc._subset_cached.cache_clear()
    assert fc.subset_font(_NOTO, 3, "節省") is None


def test_empty_text_does_not_subset():
    assert fc.subset_font("/nonexistent.ttf", 0, "") is None


@needs_noto
def test_filled_form_is_small_and_still_readable(tmp_path):
    """**端到端**：一張乾淨表單填完之後不可以變成十幾 MB，而且文字要抽得回來。"""
    import unicodedata

    import fitz
    from app.tools.pdf_fill import service
    src, dst = tmp_path / "f.pdf", tmp_path / "o.pdf"
    d = fitz.open()
    pg = d.new_page(width=595, height=842)
    pg.insert_font(fontname="L", fontfile=str(_NOTO))
    for i, lab in enumerate(["公司全名：", "營業地址："]):
        pg.insert_text((60, 100 + i * 60), lab, fontname="L", fontsize=12)
        pg.draw_line(fitz.Point(160, 103 + i * 60), fitz.Point(520, 103 + i * 60))
    d.save(str(src))
    d.close()
    service.fill_pdf(src, dst, {"company_name": "節省股份有限公司",
                                "address": "臺北市中正區忠孝東路一段 1 號"})
    text = unicodedata.normalize("NFC", fitz.open(str(dst))[0].get_text())
    assert "節省股份有限公司" in text
    assert "忠孝東路" in text
    # 來源表單自己嵌了整支字型（16 MB），所以只驗「我們加上去的那份」不肥：
    # 產出不該比來源大太多。
    assert dst.stat().st_size < src.stat().st_size * 1.2, \
        f"產出 {dst.stat().st_size} vs 來源 {src.stat().st_size}"


def test_editor_registers_the_right_subfont():
    """PDF 編輯器有四處註冊字型 —— 全部都要挑子字型。

    其中一處原本留著一句「# TTC subfont index (0 if not TTC)」的註解卻沒有傳，
    因為 PyMuPDF 根本沒有那個參數；當初發現了但沒解決。
    """
    import inspect
    import sys

    import app.tools.pdf_editor.router  # noqa: F401
    src = inspect.getsource(sys.modules["app.tools.pdf_editor.router"])
    assert src.count("embeddable_font") >= 4, \
        "編輯器還有註冊字型的地方沒有挑子字型"
    assert "fontfile=entry[\"path\"]," not in src, "還有地方直接丟路徑（=第 0 套）"
