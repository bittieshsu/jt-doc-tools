"""Tests for the watermark service — focused on CJK font fallback."""
from __future__ import annotations

import pytest
from PIL import ImageFont

from app.tools.pdf_watermark.service import (
    _has_cjk, _font_covers_cjk, _load_font,
)


def _no_cjk_font() -> bool:
    """這台機器上有沒有中文字型。

    **缺字型要跳過不是失敗** —— 這幾條驗的是「中文真的畫得出來」，
    沒有字型時它們必然紅，但那反映的是機器不是程式
    （2026-09-06 CI 的核心 job 沒裝字型，一次紅了 10 條）。
    """
    from app.core import font_catalog
    return not font_catalog.best_cjk_path("sans", "traditional")


_needs_cjk = pytest.mark.skipif(_no_cjk_font(), reason="這台機器沒有中文字型")


def test_has_cjk_detects_chinese():
    assert _has_cjk("已蓋章")
    assert _has_cjk("混合 mixed 中英")
    assert _has_cjk("カタカナ")
    assert _has_cjk("한글")


def test_has_cjk_false_for_ascii():
    assert not _has_cjk("Hello World 123")
    assert not _has_cjk("")


@_needs_cjk
def test_load_font_picks_cjk_fallback_for_cjk_text():
    """If text has CJK and font_path is empty, the loaded font must
    actually cover CJK (regression for: 浮水印中文 → 顯示方框 on Windows)."""
    font = _load_font("", 32, text="已蓋章")
    if isinstance(font, ImageFont.FreeTypeFont):
        assert _font_covers_cjk(font), \
            "_load_font returned a non-CJK font for CJK text"


def _find_dejavu():
    """找一份 DejaVuSans —— 這裡只是要**一個沒有中文字的字型**當樣本。

    原本只看 Pillow 套件內附的 `PIL/fonts/DejaVuSans.ttf`，但
    **Pillow 12.3 起不再內附字型**（連 `fonts` 目錄都沒有），於是這條守門
    在新一點的環境上一律跳過 —— 而它守的是「中文文字拿到沒有中文字的字型
    會變成方框」，那正是 v1.11.x 在 Windows 上踩過的真 bug。
    **跳過不等於通過**，所以改成系統路徑也找一輪。

    macOS / Windows 預設沒有 DejaVu，找不到就照舊跳過（那是誠實的跳過）。
    """
    import PIL
    from pathlib import Path
    candidates = [
        Path(PIL.__file__).parent / "fonts" / "DejaVuSans.ttf",   # Pillow < 12.3
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),  # Debian / Ubuntu
        Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),           # Fedora / RHEL
        Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),              # Arch
        Path("/opt/homebrew/share/fonts/DejaVuSans.ttf"),         # macOS (brew)
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


@_needs_cjk
def test_load_font_skips_non_cjk_user_font_when_text_has_cjk():
    """Caller passes an explicit non-CJK font (DejaVuSans). For ASCII text
    we keep that choice; for CJK text we should fall back to a CJK face."""
    dejavu = _find_dejavu()
    if dejavu is None:
        pytest.skip("這台機器上找不到 DejaVuSans（Pillow 內建與系統路徑都沒有）")
    f_ascii = _load_font(str(dejavu), 24, text="hello")
    assert isinstance(f_ascii, ImageFont.FreeTypeFont)
    f_cjk = _load_font(str(dejavu), 24, text="中文")
    if isinstance(f_cjk, ImageFont.FreeTypeFont):
        assert _font_covers_cjk(f_cjk) or f_cjk.path != str(dejavu), \
            "CJK text got DejaVuSans (no CJK glyphs) — would render as tofu"


def test_preview_watermarked_per_page(tmp_path):
    """Multi-page watermark preview: the `page` form field selects which page is
    rendered, and out-of-range is clamped (GitHub #28 follow-up — page switching)."""
    import io, json
    import fitz
    from fastapi.testclient import TestClient
    import app.main as app_main

    doc = fitz.open()
    for i in range(3):
        doc.new_page(width=595, height=842).insert_text((72, 72), f"PAGE {i+1}")
    buf = io.BytesIO(); doc.save(buf); doc.close()
    c = TestClient(app_main.app)
    params = json.dumps({"mode": "tile", "opacity": 0.3, "text": "WM", "rotation_deg": 30})

    def req(page):
        r = c.post("/tools/pdf-watermark/preview-watermarked",
                   files={"file": ("a.pdf", buf.getvalue(), "application/pdf")},
                   data={"params": params, "page": str(page)})
        assert r.status_code == 200, r.text
        return r.json()

    assert req(0)["page"] == 0 and req(0)["page_count"] == 3
    assert req(2)["page"] == 2          # last page selectable
    assert req(5)["page"] == 2          # out-of-range clamped to last
    assert req(-1)["page"] == 0         # negative clamped to first
