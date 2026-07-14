"""Regression: the handwriting date stamp must render crisp, not blurry.

Bug (user report, v1.12.70): the inserted date came out heavily blurred in the
final PDF. Root cause — the raster resolution of the date PNG was bound directly
to the *visual* font size (px). A small visual size (or a date box dragged large
on the page) meant a tiny raster stretched up to the physical placement size, so
it pixelated / blurred.

Fix — the ``/render-date`` endpoint now supersamples: it renders the PNG at a
high absolute pixel density (>= 240px tall) regardless of the visual font size,
while the *suggested mm* size stays computed from the visual font size. So the
visual size is unchanged but the raster has enough pixels to stay sharp when
placed / dragged.

These tests pin that contract:
  • a small visual font size still yields a high-resolution PNG (supersampled)
  • the suggested mm size still tracks the visual font size (visual size intact)
  • effective DPI at the suggested placement size is comfortably print-quality
"""
from __future__ import annotations


def _render(client, **body):
    payload = {"text": "2026-07-08", "font_style": "lxgw", "texture": "medium"}
    payload.update(body)
    r = client.post("/tools/pdf-stamp/render-date", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _eff_dpi(height_px: int, height_mm: float) -> float:
    """DPI when a raster `height_px` tall is placed at `height_mm` physical."""
    return height_px / (height_mm / 25.4)


def test_small_visual_font_still_high_resolution(client):
    """A tiny visual font size must NOT produce a tiny raster (would blur)."""
    j = _render(client, font_size_px=16)
    # Supersample floor is 240px tall render -> real content height well above the
    # old ~23px raster that caused the blur.
    assert j["height_px"] >= 180, f"raster too small, will blur: {j['height_px']}px"
    # Visual size is preserved: suggested mm tracks the *visual* font size (16px),
    # not the (much larger) render resolution.
    assert j["suggested_height_mm"] <= 6.0, j["suggested_height_mm"]


def test_effective_dpi_is_print_quality_at_suggested_size(client):
    """At the suggested placement size, effective DPI must be print-grade."""
    for visual in (16, 96, 200):
        j = _render(client, font_size_px=visual)
        dpi = _eff_dpi(j["height_px"], j["suggested_height_mm"])
        assert dpi >= 220, f"visual={visual}: only {dpi:.0f} DPI (blurry)"


def test_visual_size_scales_with_font_size(client):
    """Larger visual font size -> larger suggested mm (visual sizing intact)."""
    small = _render(client, font_size_px=24)["suggested_height_mm"]
    large = _render(client, font_size_px=200)["suggested_height_mm"]
    assert large > small


def test_render_png_dimensions_capped(client):
    """Supersample must stay bounded so PNGs don't blow past the extras limit."""
    j = _render(client, font_size_px=200)
    # render_px capped at 500 -> height stays modest even for the biggest visual.
    assert j["height_px"] <= 600, j["height_px"]
