"""pdf-pageno 中文頁碼字型回歸。

舊版頁碼一律用內建 helv（無 CJK glyph），選「第 {n} / {N} 頁」這類含中文的
格式時，「第」「頁」會印成缺字「·」（v1.12.12 修：含 CJK 時改用真 CJK 字型，
退而求其次用 PyMuPDF 內建 china-t）。
"""
import importlib
import fitz

R = importlib.import_module("app.tools.pdf_pageno.router")


def test_pageno_font_picks_cjk_for_chinese():
    # v1.14.19 起回三個值：`.ttc` 要挑繁中子字型，而 PyMuPDF 沒有索引參數，
    # 只能把那一套抽成位元組用 fontbuffer 傳（第三個回傳值）。
    fn, ff, buf = R._pageno_font("第 4 / 20 頁")
    assert fn != "helv"          # 真 CJK 字型(jtcjk) 或內建 china-t
    assert fn in ("jtcjk", "china-t")
    # 找得到系統 CJK 字型時，要嘛給檔案路徑、要嘛給抽好的子字型位元組
    if fn == "jtcjk":
        assert ff or buf


def test_pageno_font_keeps_helv_for_ascii():
    fn, ff, buf = R._pageno_font("4 / 20")
    assert fn == "helv"
    assert ff is None and buf is None


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


def test_the_format_written_in_the_api_manual_works():
    """API 手冊的範例是**照抄就要能用**的。

    手冊曾寫 `第 {n} / {total} 頁`，程式只認 `{N}` —— 照著呼叫的人拿到的是
    每一頁都印著字面的「{total}」（2026-09-24 在 Windows 實機上照手冊跑才抓到）。
    現在兩種都認，並且直接拿手冊裡那一行的格式來驗，手冊改了這條跟著改。
    """
    import re
    from tools.repo_paths import public_root
    import pathlib
    md = (public_root(pathlib.Path(__file__).resolve().parent.parent) / "API.md"
          ).read_text(encoding="utf-8")
    fmts = re.findall(r'-F "fmt=([^"]+)"', md)
    assert fmts, "API.md 裡找不到頁碼的 fmt 範例 —— 這條什麼都沒驗到"
    for fmt in fmts + ["{n} / {total}"]:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        R._draw_pageno(
            page, page_index=3, total=20, position="bc",
            fmt=fmt, start=1, font_size=12,
            margin_mm=10, color_hex="#000000", from_page=1, to_page=20,
        )
        txt = page.get_text()
        assert "{" not in txt and "}" not in txt, f"{fmt!r} 印出了沒換掉的樣板：{txt!r}"
        assert "20" in txt, f"{fmt!r} 沒有印出總頁數：{txt!r}"
        doc.close()
