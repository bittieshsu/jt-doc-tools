#!/usr/bin/env python3
"""由 `static/images/logo-doc.svg` 產生 `static/images/logo-doc.png`（內建 Logo「文件圖示」）。

PNG 是 favicon、登入頁、通知信用的（信件不能用 SVG）；改了 SVG 的顏色或圖示就重跑這支。

PyMuPDF（核心相依）畫不出 SVG 的漸層（整塊變黑），所以分兩段：
底色的圓角方塊與斜向漸層用 Pillow 畫（顏色從 SVG 的 `<stop>` 讀），
白色的文件圖示拿 SVG 拿掉底色之後交給 PyMuPDF 轉，再疊上去。

    python tools/build_logo_doc.py
"""
from __future__ import annotations

import io
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "static" / "images" / "logo-doc.svg"
DST = ROOT / "static" / "images" / "logo-doc.png"
SIZE = 512


def _hex(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def render(size: int = SIZE) -> bytes:
    import fitz
    from PIL import Image, ImageDraw

    svg = SRC.read_text(encoding="utf-8")
    c0, c1 = (_hex(c) for c in re.findall(r'stop-color="(#[0-9a-fA-F]{6})"', svg)[:2])
    radius = float(re.search(r'<rect [^>]*rx="([\d.]+)"', svg).group(1))
    box = float(re.search(r'viewBox="0 0 ([\d.]+) ', svg).group(1))

    # 斜向（左上 → 右下）漸層
    grad = Image.new("RGBA", (size, size))
    px = grad.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1))
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(c0, c1)) + (255,)
    # 圓角遮罩（放大四倍畫再縮回來，邊緣才平滑）
    big = size * 4
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=radius / box * big, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask.resize((size, size), Image.LANCZOS))

    # 白色圖示：拿掉底色那一塊之後交給 PyMuPDF
    icon_svg = re.sub(r"<rect [^>]*/>", "", re.sub(r"<defs>.*?</defs>", "", svg, flags=re.S))
    doc = fitz.open(stream=icon_svg.encode("utf-8"), filetype="svg")
    try:
        zoom = size / max(1.0, doc[0].rect.width)
        icon = Image.open(io.BytesIO(doc[0].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=True)
                                     .tobytes("png"))).convert("RGBA")
    finally:
        doc.close()
    out.alpha_composite(icon.resize((size, size)))
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()


if __name__ == "__main__":
    DST.write_bytes(render())
    print(f"{DST.relative_to(ROOT)}：{SIZE}×{SIZE}")
