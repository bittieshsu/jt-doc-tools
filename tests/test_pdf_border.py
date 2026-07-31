"""頁面加框（pdf-border）。

## 由來

使用者要求：「加入一個新工具 幫每頁加框線（主要是針對投影片應用，不過既然有 PDF
或 Office 檔案可以進來 都支援）… 可以設定框線 粗細 顏色 樣式 等等 … 然後要可以
預覽圖 所有頁面」。定位模式選「都做，預設自頁緣內縮」，輸出只要 PDF，進階選項要
圓角、內外雙框、陰影、指定頁面範圍 + 首頁排除。

## 這份要守住的事

1. **框線真的畫進 PDF**（不是只回傳成功）—— 用畫完之後多出來的向量圖元數驗證。
2. **位置正確**：頁緣內縮模式的框要落在「頁面往內縮 margin」的位置；貼齊內容模式
   的框要包住內容而不是整頁。
3. **範圍控制**：首頁排除與指定頁面都要真的跳過，而且**打錯字時不可以變成一頁都
   不畫**（`parse_pages` 回 None 代表全部）。
4. **伺服器端要夾住數值**：線寬給 500pt 會把整頁塗滿，等於毀了使用者的檔案 ——
   前端的 min/max 擋不到 API 呼叫者。
5. **預覽與輸出走同一段程式**：兩邊不一致是這類工具最難查的 bug。
"""
from __future__ import annotations

import fitz
import pytest

from app.tools.pdf_border import border_render as BR


def _make_pdf(pages: int = 3, w: float = 720, h: float = 405) -> fitz.Document:
    """做一份假投影片（16:9），每頁在中央放一小塊內容。"""
    doc = fitz.open()
    for i in range(pages):
        p = doc.new_page(width=w, height=h)
        p.insert_text((w * 0.3, h * 0.5), f"Slide {i + 1}", fontsize=36)
    return doc


def _vector_count(page) -> int:
    return len(page.get_drawings() or [])


# ------------------------------------------------------------ 真的有畫

def test_border_is_actually_drawn():
    doc = _make_pdf(1)
    before = _vector_count(doc[0])
    assert BR.draw_border(doc[0], BR.BorderSpec(), page_no=1, total=1)
    assert _vector_count(doc[0]) > before, "框線沒有真的畫進頁面"
    doc.close()


def test_apply_returns_how_many_pages_were_drawn():
    doc = _make_pdf(4)
    assert BR.apply(doc, BR.BorderSpec()) == 4
    doc.close()


# ------------------------------------------------------------ 位置

def test_page_mode_rect_is_page_inset_by_margin_plus_half_stroke():
    """路徑要往內縮「邊距 + 半個線寬」。

    線寬是**置中於路徑**的：30pt 的線有 15pt 落在路徑外。只縮邊距的話那一半會
    超出頁面被裁掉 —— 使用者把粗細從 10 調到 30，看起來卻幾乎沒變粗（實際回報過）。
    """
    doc = _make_pdf(1)
    page = doc[0]
    w = 6.0
    spec = BR.BorderSpec(mode="page", margin_mm=10, width_pt=w)
    r = BR.target_rect(page, spec)
    from app.core.unit_convert import mm_to_pt
    inset = mm_to_pt(10) + w / 2
    assert r.x0 == pytest.approx(page.rect.x0 + inset, abs=0.5)
    assert r.y0 == pytest.approx(page.rect.y0 + inset, abs=0.5)
    assert r.x1 == pytest.approx(page.rect.x1 - inset, abs=0.5)
    assert r.y1 == pytest.approx(page.rect.y1 - inset, abs=0.5)
    doc.close()


def test_thick_border_stays_fully_inside_the_page():
    """邊距 0 配粗線時，整條線都要看得見（不可以有一半在頁面外）。"""
    doc = _make_pdf(1, w=400, h=300)
    page = doc[0]
    w = 30.0
    assert BR.draw_border(page, BR.BorderSpec(margin_mm=0, width_pt=w),
                          page_no=1, total=1)
    d = page.get_drawings()[0]
    r = d["rect"]
    half = d.get("width", w) / 2
    assert r.x0 - half >= page.rect.x0 - 0.01
    assert r.y0 - half >= page.rect.y0 - 0.01
    assert r.x1 + half <= page.rect.x1 + 0.01
    assert r.y1 + half <= page.rect.y1 + 0.01
    doc.close()


def test_line_width_is_capped_by_page_size():
    """名片大小的頁面配 72pt 的框會被框吃光 —— 依該頁短邊再夾一次。"""
    doc = _make_pdf(1, w=200, h=120)
    assert BR._effective_width(BR.BorderSpec(width_pt=72), doc[0]) == \
        pytest.approx(120 / 6)
    doc.close()


def test_content_mode_hugs_the_content_not_the_page():
    """貼齊內容的框要明顯小於整頁 —— 不然這個模式等於沒作用。"""
    doc = _make_pdf(1)
    page = doc[0]
    r = BR.target_rect(page, BR.BorderSpec(mode="content", margin_mm=3))
    assert r is not None
    assert r.width < page.rect.width * 0.8, "貼齊內容卻幾乎跟整頁一樣寬"
    assert r.height < page.rect.height * 0.8
    doc.close()


def test_content_mode_never_overflows_the_page():
    """邊距給很大時，框仍要留在頁內 —— 溢出的部分列印會被裁掉，看起來像斷線。"""
    doc = _make_pdf(1)
    page = doc[0]
    r = BR.target_rect(page, BR.BorderSpec(mode="content", margin_mm=80))
    assert r is not None
    assert r.x0 >= page.rect.x0 - 0.01 and r.y0 >= page.rect.y0 - 0.01
    assert r.x1 <= page.rect.x1 + 0.01 and r.y1 <= page.rect.y1 + 0.01
    doc.close()


def test_blank_page_in_content_mode_is_skipped_not_crashed():
    doc = fitz.open()
    doc.new_page(width=720, height=405)          # 完全空白
    assert BR.draw_border(doc[0], BR.BorderSpec(mode="content"),
                          page_no=1, total=1) is False
    doc.close()


def test_margin_larger_than_page_is_skipped():
    doc = _make_pdf(1, w=100, h=100)
    assert BR.draw_border(doc[0], BR.BorderSpec(margin_mm=90),
                          page_no=1, total=1) is False
    doc.close()


def test_rotated_page_border_stays_inside_the_visible_page():
    """旋轉頁要換座標系。

    `page.rect` 是視覺矩形但 `draw_rect` 吃內容座標；直接拿視覺座標去畫，框線會
    跑到頁面外或只畫一半（pdf-pageno 在 issue #21 踩過同一個雷）。
    """
    doc = _make_pdf(1)
    doc[0].set_rotation(90)
    page = doc[0]
    assert BR.draw_border(page, BR.BorderSpec(margin_mm=10), page_no=1, total=1)
    drawn = page.get_drawings()
    assert drawn, "旋轉頁沒有畫出任何東西"
    # 畫出來的圖元（內容座標）要落在未旋轉的 mediabox 內
    mb = page.mediabox
    for d in drawn:
        r = d["rect"]
        assert r.x0 >= mb.x0 - 1 and r.y0 >= mb.y0 - 1
        assert r.x1 <= mb.x1 + 1 and r.y1 <= mb.y1 + 1
    doc.close()


# ------------------------------------------------------------ 套用範圍

def test_skip_first_page():
    doc = _make_pdf(3)
    assert BR.apply(doc, BR.BorderSpec(skip_first=True)) == 2
    assert not doc[0].get_drawings()
    assert doc[1].get_drawings()
    doc.close()


def test_explicit_page_range():
    doc = _make_pdf(5)
    spec = BR.BorderSpec(pages=BR.parse_pages("2,4-5", 5))
    assert BR.apply(doc, spec) == 3
    assert not doc[0].get_drawings() and doc[1].get_drawings()
    assert not doc[2].get_drawings() and doc[3].get_drawings()
    doc.close()


@pytest.mark.parametrize("text,expect", [
    ("", None),
    ("   ", None),
    ("abc", None),          # 打錯字 → 全部，不是一頁都不畫
    ("99", None),           # 全部超出範圍 → 全部
    ("1,3,5-8", {1, 3, 5, 6, 7, 8}),
    ("3-1", {1, 2, 3}),     # 順序寫反也要能用
    ("1，2", {1, 2}),        # 全形逗號（中文輸入法常打出來的）
])
def test_parse_pages(text, expect):
    assert BR.parse_pages(text, 10) == expect


def test_typo_in_range_does_not_silently_skip_everything():
    """打錯字時最糟的結果是「什麼都沒發生」—— 使用者會以為工具壞了。"""
    doc = _make_pdf(3)
    spec = BR.BorderSpec(pages=BR.parse_pages("第一頁", 3))
    assert BR.apply(doc, spec) == 3
    doc.close()


# ------------------------------------------------------------ 樣式選項

@pytest.mark.parametrize("spec", [
    BR.BorderSpec(style="dashed"),
    BR.BorderSpec(style="dotted", width_pt=3),
    BR.BorderSpec(radius_mm=8),
    BR.BorderSpec(double=True, double_gap_mm=2),
    BR.BorderSpec(shadow=True),
    BR.BorderSpec(opacity=0.4),
], ids=["虛線", "點線", "圓角", "雙線", "陰影", "半透明"])
def test_every_style_option_renders(spec):
    doc = _make_pdf(1)
    assert BR.draw_border(doc[0], spec, page_no=1, total=1)
    assert doc[0].get_drawings()
    doc.close()


def test_double_border_draws_two_rects():
    doc = _make_pdf(1)
    BR.draw_border(doc[0], BR.BorderSpec(), page_no=1, total=1)
    single = len(doc[0].get_drawings())
    doc.close()
    doc = _make_pdf(1)
    BR.draw_border(doc[0], BR.BorderSpec(double=True), page_no=1, total=1)
    assert len(doc[0].get_drawings()) > single, "雙框只畫了一條線"
    doc.close()


def test_radius_is_converted_to_a_fraction_not_used_raw():
    """PyMuPDF 的 radius 是「佔短邊的比例」，不是絕對長度。

    直接把 mm 丟進去會被當成極大的比例（>0.5 會丟例外或畫成膠囊形）。
    """
    doc = _make_pdf(1)
    r = BR.target_rect(doc[0], BR.BorderSpec(radius_mm=8))
    frac = BR._radius_frac(BR.BorderSpec(radius_mm=8), r)
    assert 0 < frac <= 0.5
    # 半徑大於短邊時要被夾在 0.5，不可以丟例外
    big = BR._radius_frac(BR.BorderSpec(radius_mm=500), r)
    assert big == 0.5
    doc.close()


def test_hex_colour_parsing():
    assert BR.hex_to_rgb01("#ffffff") == (1.0, 1.0, 1.0)
    assert BR.hex_to_rgb01("000") == (0.0, 0.0, 0.0)
    assert BR.hex_to_rgb01("亂寫") == (0.0, 0.0, 0.0)   # 壞值不可以炸


# ------------------------------------------------------------ 伺服器端夾範圍

def test_server_clamps_values():
    """前端的 min/max 只是提示，API 呼叫者不受它拘束。

    線寬給 500pt 會把整頁塗滿 —— 等於毀了使用者的檔案。
    """
    from app.tools.pdf_border.router import _spec_from_form
    s = _spec_from_form(
        mode="亂寫", margin_mm=99999, width_pt=500, color="", style="亂寫",
        radius_mm=-5, opacity=99, double=True, double_gap_mm=0,
        shadow=True, shadow_color="", shadow_blur_mm=999,
        shadow_offset_mm=-1, shadow_opacity=5,
        pages="", skip_first=False, page_count=3)
    assert s.mode == "page"          # 不認得的模式退回預設
    assert s.style == "solid"
    assert 0 <= s.margin_mm <= 100
    assert 0.1 <= s.width_pt <= 72
    assert s.radius_mm >= 0
    assert 0 < s.opacity <= 1.0
    assert s.double_gap_mm >= 0.2
    assert 0 <= s.shadow_blur_mm <= 20
    assert 0 <= s.shadow_opacity <= 1.0


# ------------------------------------------------------------ 端點

def _client():
    from fastapi.testclient import TestClient
    import app.main as app_main
    return TestClient(app_main.app)


def _pdf_bytes(pages: int = 2) -> bytes:
    doc = _make_pdf(pages)
    data = doc.tobytes()
    doc.close()
    return data


def test_load_then_preview_then_submit():
    c = _client()
    r = c.post("/tools/pdf-border/load",
               files={"file": ("slides.pdf", _pdf_bytes(3), "application/pdf")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["page_count"] == 3
    assert len(d["pages"]) == 3
    uid = d["upload_id"]

    r = c.post("/tools/pdf-border/preview",
               data={"upload_id": uid, "page": "1", "width_pt": "2"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers.get("X-Border-Drawn") == "1"
    assert len(r.content) > 500

    r = c.post("/tools/pdf-border/submit",
               data={"upload_id": uid, "out_name": "slides.pdf"})
    assert r.status_code == 200, r.text
    assert "job_id" in r.json()


def test_preview_reports_skipped_pages():
    """被排除的頁要讓前端知道，才標得出「不加框」。"""
    c = _client()
    uid = c.post("/tools/pdf-border/load",
                 files={"file": ("s.pdf", _pdf_bytes(2), "application/pdf")}
                 ).json()["upload_id"]
    r = c.post("/tools/pdf-border/preview",
               data={"upload_id": uid, "page": "1", "skip_first": "true"})
    assert r.status_code == 200 and r.headers.get("X-Border-Drawn") == "0"
    r = c.post("/tools/pdf-border/preview",
               data={"upload_id": uid, "page": "2", "skip_first": "true"})
    assert r.headers.get("X-Border-Drawn") == "1"


@pytest.mark.parametrize("name", ["a.zip", "a.mp4", "a.exe", "a.png"])
def test_rejects_unsupported_upload(name):
    """收不了的格式要當場擋掉。

    注意 **`.txt` / `.csv` / `.rtf` 是收的** —— Office 引擎讀得懂，轉成 PDF 加框
    是合理的用法。這裡測的是真正處理不了的類型（壓縮檔、影片、執行檔、圖片）。
    """
    c = _client()
    r = c.post("/tools/pdf-border/load",
               files={"file": (name, b"not a document", "application/octet-stream")})
    assert r.status_code == 400


def test_empty_upload_rejected():
    c = _client()
    r = c.post("/tools/pdf-border/load",
               files={"file": ("a.pdf", b"", "application/pdf")})
    assert r.status_code == 400


def test_bad_upload_id_rejected():
    c = _client()
    r = c.post("/tools/pdf-border/preview",
               data={"upload_id": "../../etc/passwd", "page": "1"})
    assert r.status_code in (400, 404)


def test_page_out_of_range_rejected():
    c = _client()
    uid = c.post("/tools/pdf-border/load",
                 files={"file": ("s.pdf", _pdf_bytes(2), "application/pdf")}
                 ).json()["upload_id"]
    assert c.post("/tools/pdf-border/preview",
                  data={"upload_id": uid, "page": "99"}).status_code == 400


def test_public_api_returns_pdf():
    c = _client()
    r = c.post("/tools/pdf-border/api/pdf-border",
               files={"file": ("s.pdf", _pdf_bytes(2), "application/pdf")},
               data={"width_pt": "2", "color": "#ff0000"})
    assert r.status_code == 200, r.text
    assert r.content[:4] == b"%PDF"
    with fitz.open(stream=r.content, filetype="pdf") as doc:
        assert doc.page_count == 2
        assert doc[0].get_drawings(), "API 產出的 PDF 沒有框線"


def test_tool_is_registered_and_granted():
    """新工具要進工具清單，也要進內建角色 —— 少了角色的話一般使用者看不到它。"""
    from app.tool_registry import discover_tools
    from app.core.roles import SEED_ROLES
    ids = {t.metadata.id for t in discover_tools()}
    assert "pdf-border" in ids
    granted = {t for r in SEED_ROLES for t in (r.get("tools") or [])}
    assert "pdf-border" in granted, "pdf-border 沒有被任何內建角色授權"
