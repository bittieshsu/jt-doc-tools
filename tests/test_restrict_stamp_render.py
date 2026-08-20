"""個資限用章的渲染 —— 橫式 / 直式 / 對角線。

## 由來

直式是 2026-08-20 使用者要求加的：公文與證件影本常用中文直書，
橫式章壓在直書文件上視覺方向打架。這個工具之前完全沒有測試 ——
連橫式一起收進來。

## 直式的三個查核點（都是實作時真的踩過的）

1. **標點要轉直書呈現形**（U+FE10 區）：橫式「，」直排會浮在格子
   中間偏左，看起來像放錯位置
2. **半形句點要轉間隔號**：`.` 直排小到近乎看不見，實測 115.08.20
   渲染成「115 08 20」——日期的分段直接消失
3. **對齊**：「僅供」起於天頭、主文置中、「使用，他用無效」收於
   地腳 —— 三欄全置中的話「僅供」浮在半空，像排版錯誤
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from app.tools.pdf_stamp import restrict_render as rr


def _ink_ratio(png: bytes) -> float:
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    px = list(img.getdata())
    ink = sum(1 for r, g, b, a in px if a > 40)
    return ink / max(1, len(px))


# ---------------------------------------------------------------- 橫式（既有）

def test_rectangle_renders_ink():
    png, w, h = rr.render_rectangle_stamp("銀行開戶", date_str="2026/08/20",
                                          applicant="王小明")
    assert w > h, "橫式應為寬扁"
    assert _ink_ratio(png) > 0.02, "紙上要有墨水（不能只驗檔案有產出）"


def test_diagonal_renders_ink():
    png, w, h = rr.render_diagonal_stamp("銀行開戶")
    assert _ink_ratio(png) > 0.01


# ---------------------------------------------------------------------- 直式

def test_vertical_renders_ink():
    png, w, h = rr.render_vertical_stamp("銀行開戶", date_str="115.08.20",
                                         applicant="王小明",
                                         copy_label="第 1 份，共 2 份")
    assert _ink_ratio(png) > 0.02


def test_vertical_without_footer_is_narrow():
    """只有主文三欄（無 footer）時，直式應為窄高。"""
    png, w, h = rr.render_vertical_stamp("銀行開戶")
    assert h > w, f"直式（無 footer）應窄高，實際 {w}x{h}"


def test_vertical_punct_mapping():
    """直書標點：橫式「，"」轉呈現形、半形句點轉間隔號。"""
    out = rr._vertical_text("使用，他用無效。115.08.20")
    assert "，" not in out and "︐" in out
    assert "。" not in out and "︒" in out
    assert "." not in out and "·" in out


def test_vertical_border_styles_differ():
    """double / single / none 三種邊框要真的不一樣。"""
    outs = {b: rr.render_vertical_stamp("測試", border_style=b)[0]
            for b in ("double", "single", "none")}
    assert len({v for v in outs.values()}) == 3, "三種邊框產出相同 —— 參數沒生效"


def test_vertical_alignment_top_and_bottom():
    """「僅供」起於天頭、「使用，他用無效」收於地腳。

    驗法：整張圖最上緣一小條與最下緣一小條都要有墨水（置中版型的
    上下兩端只有邊框，內文區是空的 —— 拿掉邊框就驗得出差別）。
    """
    png, w, h = rr.render_vertical_stamp("很長的用途文字測試直式排版",
                                         border_style="none")
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    px = img.load()

    def band_has_ink(y0, y1):
        return any(px[x, y][3] > 40
                   for y in range(y0, y1) for x in range(0, w, 3))

    top_band = band_has_ink(0, max(1, h // 8))
    bottom_band = band_has_ink(h - max(1, h // 8), h)
    assert top_band, "天頭沒有內容 ——「僅供」沒有靠上"
    assert bottom_band, "地腳沒有內容 ——「使用，他用無效」沒有靠下"


# ------------------------------------------------------------------- 端點

@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app
    return TestClient(app)


@pytest.mark.parametrize("style", ["rectangle", "vertical", "diagonal"])
def test_endpoint_accepts_all_styles(client, style):
    r = client.post("/tools/pdf-stamp/render-restrict-stamp",
                    json={"purpose": "銀行開戶", "style": style,
                          "date_str": "115.08.20"})
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert d["png_b64"] and d["width_px"] > 0 and d["height_px"] > 0


def test_endpoint_unknown_style_falls_back(client):
    r = client.post("/tools/pdf-stamp/render-restrict-stamp",
                    json={"purpose": "測試", "style": "no-such"})
    assert r.status_code == 200
