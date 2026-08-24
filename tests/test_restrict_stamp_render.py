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


# ---------------------------------------------------------------------------
# 「加上『僅供 … 使用，他用無效』字樣」開關（v1.14.48）
#
# 用途文字本身已經把話說完時（「本影本僅供投標使用」），再套一次固定句就是
# 贅字。預設仍然加（舊行為不變），取消勾選才只印用途本身。
# ---------------------------------------------------------------------------
import io as _io

import pytest as _pytest
from PIL import Image as _Image

from app.tools.pdf_stamp import restrict_render as _rr


def _ink_ratio(png: bytes) -> float:
    """有多少比例的像素有墨水 —— 用來確認「少畫了東西」而不只是尺寸變了。"""
    im = _Image.open(_io.BytesIO(png)).convert("RGBA")
    px = im.getdata()
    inked = sum(1 for p in px if p[3] > 8)
    return inked / max(1, len(px))


@_pytest.mark.parametrize("fn,kwargs", [
    (_rr.render_rectangle_stamp, {}),
    (_rr.render_vertical_stamp, {}),
    (_rr.render_diagonal_stamp, {}),
])
def test_show_phrase_off_makes_a_smaller_stamp(fn, kwargs):
    """關掉固定句 → 章面一定變小（少了「僅供」與「使用，他用無效」兩段）。

    只比對「有沒有回傳成功」是不夠的 —— 那種測試在完全沒少畫東西時也會綠。
    這裡比的是**實際尺寸**：直式少一欄（寬變窄）、橫式少兩行（高變矮）。
    """
    on_png, on_w, on_h = fn(purpose="投標", show_phrase=True, **kwargs)
    off_png, off_w, off_h = fn(purpose="投標", show_phrase=False, **kwargs)
    assert (off_w * off_h) < (on_w * on_h), "關掉固定句之後章面沒有變小"
    assert _ink_ratio(off_png) > 0, "關掉之後整張是空的（用途本身也沒畫）"


def test_show_phrase_defaults_to_on():
    """預設值必須維持舊行為 —— 既有使用者與對外 API 呼叫不帶這個參數。"""
    a = _rr.render_rectangle_stamp(purpose="投標")
    b = _rr.render_rectangle_stamp(purpose="投標", show_phrase=True)
    assert (a[1], a[2]) == (b[1], b[2])


def test_show_phrase_off_still_keeps_footer_and_border():
    """關掉固定句不影響其他欄位：日期 / 申請人 / 份數與邊框照常。"""
    with_foot = _rr.render_rectangle_stamp(
        purpose="投標", date_str="2026/08/24", applicant="王小明",
        copy_label="第 1 份 / 共 3 份", show_phrase=False)
    bare = _rr.render_rectangle_stamp(purpose="投標", show_phrase=False)
    assert with_foot[2] > bare[2], "footer 沒有被畫出來（高度沒增加）"


def test_endpoint_accepts_show_phrase(client):
    """端點要收得下這個欄位，且**不帶時預設為加**（相容既有呼叫端）。"""
    base = {"purpose": "投標", "style": "rectangle"}
    r_on = client.post("/tools/pdf-stamp/render-restrict-stamp", json=base)
    r_off = client.post("/tools/pdf-stamp/render-restrict-stamp",
                        json={**base, "show_phrase": False})
    assert r_on.status_code == 200 and r_off.status_code == 200
    on, off = r_on.json(), r_off.json()
    assert (off["width_px"] * off["height_px"]) < (on["width_px"] * on["height_px"])
