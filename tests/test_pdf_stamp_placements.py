"""pdf-stamp「每頁獨立位置」placements 模式測試（issue #38 / Phase B）。

重點守住兩件事：
1. 新模式真的能「每頁不同位置、同頁多個」；
2. **舊模式零回歸** —— 沒送 placements_json 時輸出與新增此功能前完全一致。
"""
from __future__ import annotations

import json
from io import BytesIO

import fitz
import pytest
from fastapi.testclient import TestClient

import app.main as app_main
# 注意：`from app.tools.pdf_stamp import router` 拿到的是 APIRouter 物件不是模組，
# 取模組層常數要用 import_module。
import importlib
_stamp_mod = importlib.import_module("app.tools.pdf_stamp.router")


def _pdf(n: int = 3) -> bytes:
    doc = fitz.open()
    for i in range(n):
        pg = doc.new_page(width=595, height=842)
        pg.insert_text((72, 100), f"Page {i + 1}", fontsize=20)
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _seal_png() -> bytes:
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (200, 200), (255, 255, 255, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((5, 5, 195, 195), outline=(200, 0, 0, 255), width=8)
    buf = BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _ink(page: fitz.Page) -> int:
    """該頁「非白像素」數 — 用來判斷有沒有被蓋章（比 get_images 可靠，
    get_images 會回報共用的 xref 資源造成假陽性）。"""
    pm = page.get_pixmap(dpi=60)
    px = pm.samples
    return sum(1 for j in range(0, len(px), pm.n) if px[j] < 240)


def _c() -> TestClient:
    return TestClient(app_main.app)


# ---------- 新模式：每頁獨立 / 同頁多個 ----------

def test_placements_per_page_positions():
    """第 1 頁 2 個章、第 2 頁不蓋、第 3 頁 1 個章。"""
    src = _pdf(3)
    base = fitz.open(stream=src, filetype="pdf")
    base_ink = [_ink(base[i]) for i in range(3)]
    base.close()

    plc = [
        {"page": 0, "x_mm": 40, "y_mm": 250, "width_mm": 20, "height_mm": 20},
        {"page": 0, "x_mm": 150, "y_mm": 100, "width_mm": 20, "height_mm": 20},
        {"page": 2, "x_mm": 100, "y_mm": 150, "width_mm": 25, "height_mm": 25},
    ]
    r = _c().post(
        "/tools/pdf-stamp/api/pdf-stamp",
        files={"file": ("d.pdf", src, "application/pdf"),
               "stamp_image": ("s.png", _seal_png(), "image/png")},
        data={"placements_json": json.dumps(plc)},
    )
    assert r.status_code == 200, r.text
    out = fitz.open(stream=r.content, filetype="pdf")
    ink = [_ink(out[i]) for i in range(3)]
    out.close()
    # 第 1 / 3 頁墨量明顯增加；第 2 頁與原檔相當（沒被蓋）
    assert ink[0] > base_ink[0] * 2, f"第1頁應有 2 個章: {ink[0]} vs {base_ink[0]}"
    assert abs(ink[1] - base_ink[1]) < base_ink[1] * 0.2, "第2頁不該被蓋章"
    assert ink[2] > base_ink[2] * 1.5, "第3頁應有 1 個章"


def test_placements_out_of_range_page_skipped():
    """placement 指到超過頁數的頁 → 跳過，不報錯（多檔頁數不同的保護）。"""
    src = _pdf(2)
    plc = [{"page": 0, "x_mm": 50, "y_mm": 200, "width_mm": 20, "height_mm": 20},
           {"page": 99, "x_mm": 50, "y_mm": 50, "width_mm": 20, "height_mm": 20}]
    r = _c().post(
        "/tools/pdf-stamp/api/pdf-stamp",
        files={"file": ("d.pdf", src, "application/pdf"),
               "stamp_image": ("s.png", _seal_png(), "image/png")},
        data={"placements_json": json.dumps(plc)},
    )
    assert r.status_code == 200, r.text
    out = fitz.open(stream=r.content, filetype="pdf")
    assert out.page_count == 2
    out.close()


# ---------- 舊模式零回歸 ----------

def test_legacy_path_unchanged_without_placements():
    """不帶 placements_json → 走舊路徑，每頁同位置（行為與新功能前一致）。"""
    src = _pdf(3)
    base = fitz.open(stream=src, filetype="pdf")
    base_ink = [_ink(base[i]) for i in range(3)]
    base.close()
    r = _c().post(
        "/tools/pdf-stamp/api/pdf-stamp",
        files={"file": ("d.pdf", src, "application/pdf"),
               "stamp_image": ("s.png", _seal_png(), "image/png")},
        data={"x_mm": "150", "y_mm": "240", "width_mm": "20",
              "height_mm": "20", "page_mode": "all"},
    )
    assert r.status_code == 200, r.text
    out = fitz.open(stream=r.content, filetype="pdf")
    ink = [_ink(out[i]) for i in range(3)]
    out.close()
    for i in range(3):
        assert ink[i] > base_ink[i] * 1.5, f"舊模式第{i+1}頁應被蓋章"


def test_legacy_page_mode_first_still_works():
    src = _pdf(3)
    base = fitz.open(stream=src, filetype="pdf")
    base_ink = [_ink(base[i]) for i in range(3)]
    base.close()
    r = _c().post(
        "/tools/pdf-stamp/api/pdf-stamp",
        files={"file": ("d.pdf", src, "application/pdf"),
               "stamp_image": ("s.png", _seal_png(), "image/png")},
        data={"page_mode": "first", "x_mm": "100", "y_mm": "200"},
    )
    assert r.status_code == 200
    out = fitz.open(stream=r.content, filetype="pdf")
    ink = [_ink(out[i]) for i in range(3)]
    out.close()
    assert ink[0] > base_ink[0] * 1.5, "第1頁應被蓋"
    assert abs(ink[1] - base_ink[1]) < base_ink[1] * 0.2, "第2頁不該被蓋"


# ---------- 驗證 / 安全 ----------

@pytest.mark.parametrize("bad,msg", [
    ("not json", "JSON"),
    ('{"page":0}', "陣列"),
    ('[{"page":-1}]', "負"),
    ('[{"page":0,"kind":"evil"}]', "kind"),
    ('["oops"]', "物件"),
])
def test_placements_validation_rejects_bad_input(bad, msg):
    r = _c().post(
        "/tools/pdf-stamp/api/pdf-stamp",
        files={"file": ("d.pdf", _pdf(1), "application/pdf"),
               "stamp_image": ("s.png", _seal_png(), "image/png")},
        data={"placements_json": bad},
    )
    assert r.status_code == 400, f"應拒絕 {bad!r}"


def test_placements_count_limit(tmp_path):
    """超過上限的 placements 直接擋掉（防資源耗盡）。"""
    plc = [{"page": 0, "x_mm": 10, "y_mm": 10} for _ in range(_stamp_mod._MAX_PLACEMENTS + 1)]
    r = _c().post(
        "/tools/pdf-stamp/api/pdf-stamp",
        files={"file": ("d.pdf", _pdf(1), "application/pdf"),
               "stamp_image": ("s.png", _seal_png(), "image/png")},
        data={"placements_json": json.dumps(plc)},
    )
    assert r.status_code == 400


def test_placements_rejects_non_png_b64():
    """png_b64 不是 PNG（例如偽裝的可執行檔）→ 拒絕。"""
    import base64
    fake = base64.b64encode(b"MZ\x90\x00 not a png").decode()
    plc = [{"page": 0, "kind": "date", "png_b64": fake,
            "x_mm": 10, "y_mm": 10, "width_mm": 20, "height_mm": 10}]
    r = _c().post(
        "/tools/pdf-stamp/api/pdf-stamp",
        files={"file": ("d.pdf", _pdf(1), "application/pdf"),
               "stamp_image": ("s.png", _seal_png(), "image/png")},
        data={"placements_json": json.dumps(plc)},
    )
    assert r.status_code == 400


def test_placements_unknown_asset_id_rejected():
    plc = [{"page": 0, "asset_id": "no-such-asset-id",
            "x_mm": 10, "y_mm": 10, "width_mm": 20, "height_mm": 20}]
    r = _c().post(
        "/tools/pdf-stamp/api/pdf-stamp",
        files={"file": ("d.pdf", _pdf(1), "application/pdf"),
               "stamp_image": ("s.png", _seal_png(), "image/png")},
        data={"placements_json": json.dumps(plc)},
    )
    assert r.status_code == 400


def test_placements_date_and_restrict_kinds_are_stamped():
    """頁面個別用印可放「日期」「個資限用章」（各自帶 png_b64），且都真的畫進 PDF。"""
    import base64
    import fitz

    date_png = _seal_png()
    restrict_png = _seal_png()
    plc = [
        {"page": 0, "kind": "date", "png_b64": base64.b64encode(date_png).decode(),
         "x_mm": 20, "y_mm": 20, "width_mm": 30, "height_mm": 12},
        {"page": 1, "kind": "restrict",
         "png_b64": base64.b64encode(restrict_png).decode(),
         "x_mm": 60, "y_mm": 120, "width_mm": 40, "height_mm": 20},
    ]
    r = _c().post(
        "/tools/pdf-stamp/api/pdf-stamp",
        files={"file": ("d.pdf", _pdf(2), "application/pdf"),
               "stamp_image": ("s.png", _seal_png(), "image/png")},
        data={"placements_json": json.dumps(plc)},
    )
    assert r.status_code == 200, r.text
    d = fitz.open(stream=r.content, filetype="pdf")
    try:
        assert len(d[0].get_images(full=True)) == 1, "第 1 頁應有日期圖"
        assert len(d[1].get_images(full=True)) == 1, "第 2 頁應有個資限用章圖"
    finally:
        d.close()
