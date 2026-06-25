"""pdf-pageno 中文頁碼字型回歸。

舊版頁碼一律用內建 helv（無 CJK glyph），選「第 {n} / {N} 頁」這類含中文的
格式時，「第」「頁」會印成缺字「·」（v1.12.12 修：含 CJK 時改用真 CJK 字型，
退而求其次用 PyMuPDF 內建 china-t）。
"""
import importlib
import fitz

R = importlib.import_module("app.tools.pdf_pageno.router")


def test_pageno_font_picks_cjk_for_chinese():
    fn, ff = R._pageno_font("第 4 / 20 頁")
    assert fn != "helv"          # 真 CJK 字型(jtcjk) 或內建 china-t
    assert fn in ("jtcjk", "china-t")


def test_pageno_font_keeps_helv_for_ascii():
    fn, ff = R._pageno_font("4 / 20")
    assert fn == "helv"
    assert ff is None


def test_draw_pageno_chinese_glyphs_present():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    R._draw_pageno(
        page, page_index=3, total=20, position="bc",
        fmt="第 {n} / {N} 頁", start=1, font_size=12,
        margin_mm=10, color_hex="#000000", from_page=1, to_page=20,
    )
    txt = page.get_text()
    # glyph 真的有畫上去（不是缺字）→ 抽得回中文字元
    assert "第" in txt and "頁" in txt
    assert "4" in txt and "20" in txt
    doc.close()


def test_text_width_pt_counts_cjk_full_width():
    # CJK 全形 ≈ 1.0 em、半形 ≈ 0.55 em
    assert R._text_width_pt("AB", 10) == 10 * (0.55 + 0.55)
    assert R._text_width_pt("頁", 10) == 10 * 1.0
