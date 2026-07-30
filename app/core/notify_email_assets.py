"""通知信裡要內嵌的圖片（站台 logo、工具圖示）。

## 為什麼要自己產圖

信件裡放圖只有三種做法，兩種不能用：

* **外部網址** —— 讀信軟體預設不載入外部圖片（防追蹤像素），使用者會看到破圖或
  一條「顯示圖片」的提示。而且伺服器不一定有對外可達的網址。
* **`data:` URI** —— Gmail 與多數讀信軟體直接濾掉。
* **內嵌附件（`cid:`）** —— 圖片跟著信一起送，不需要對外連線。**這是唯一可靠的**。

所以這裡負責把圖片準備成 PNG 位元組，由 `notify_channels.send_email` 掛成
`multipart/related` 的附件。

## 工具圖示怎麼來

圖示的定義只有一份 —— `components/icons.html` 那個 Jinja macro。這裡**不另外抄一份
SVG path**（抄了就會跟畫面上的圖示長得不一樣），而是呼叫同一個 macro 取得 SVG，
再用 PyMuPDF 轉成 PNG。PyMuPDF 本來就是核心相依，不必為了寄信多裝一個繪圖套件。

底色用與側欄相同的配色索引，讓信裡的圖示和使用者在網站上看到的是同一個。

## 產出會快取

每個工具的圖示是固定的，但每寄一封信就重畫一次很浪費（一次約 10ms，量大時會
累積）。用記憶體快取即可 —— 圖示不會在執行期間改變。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("app.notify_assets")

#: 與 `platform.css` 的 `.tile-color-N` 同一組配色。
#:
#: 網站上的色塊是 135° 漸層（左上淺 → 右下深）。信件不支援漸層，所以取一個
#: 單色。**取淺色端**：小色塊的視覺重心在左上，取深色端會變得又暗又濁
#: （13 號的深端 #854d0e render 出來像一塊爛泥），與畫面上看到的對不起來。
_TILE_COLORS = [
    "#f472b6", "#38bdf8", "#34d399", "#fbbf24", "#fb7185", "#fb923c",
    "#2dd4bf", "#facc15", "#a3e635", "#fda4af", "#22d3ee", "#84cc16",
    "#f97316", "#eab308",
]

#: 圖示 PNG 的邊長（信裡顯示 40px；用兩倍解析度才不會在高解析螢幕上糊掉）
_ICON_PX = 80
#: logo 的高度上限（同樣是兩倍）
_LOGO_MAX_PX = 96

_cache: dict[str, bytes] = {}
_lock = threading.Lock()


def _render_svg_to_png(svg: str, width: int, height: int) -> bytes:
    """SVG → PNG。用 PyMuPDF（已是核心相依），不額外引繪圖套件。"""
    import fitz
    doc = fitz.open(stream=svg.encode("utf-8"), filetype="svg")
    try:
        page = doc[0]
        zoom_x = width / max(1.0, page.rect.width)
        zoom_y = height / max(1.0, page.rect.height)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom_x, zoom_y), alpha=True)
        return pix.tobytes("png")
    finally:
        doc.close()


def _icon_svg(name: str, color: str) -> str:
    """圓角色塊 + 白色圖示。圖示的 path 取自**同一個 macro**，不另抄一份。"""
    from app.main import templates
    macro = templates.env.get_template("components/icons.html").module.icon
    inner = str(macro(name, 24))
    # macro 回的是完整 <svg>；取出裡面的內容，重新包一層有底色的畫布
    body = inner.split(">", 1)[1].rsplit("</svg>", 1)[0] if ">" in inner else ""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" '
        'viewBox="0 0 48 48">'
        f'<rect x="0" y="0" width="48" height="48" rx="11" fill="{color}"/>'
        '<g transform="translate(12,12)" fill="none" stroke="#ffffff" '
        'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'
        f'{body}</g></svg>'
    )


def tool_icon_png(tool_id: str) -> Optional[bytes]:
    """某個工具的圖示 PNG（含底色）。取不到就回 None —— 信照樣寄，只是沒有圖。"""
    key = f"icon:{tool_id}"
    with _lock:
        if key in _cache:
            return _cache[key]
    try:
        from app.main import _tpl_tool_tiles
        tile = next((t for t in _tpl_tool_tiles() if t["id"] == tool_id), None)
        if not tile:
            return None
        color = _TILE_COLORS[int(tile.get("color") or 0) % len(_TILE_COLORS)]
        png = _render_svg_to_png(_icon_svg(tile.get("icon") or "tool", color),
                                 _ICON_PX, _ICON_PX)
    except Exception as e:  # noqa: BLE001 — 沒有圖示不該讓通知寄不出去
        logger.info("工具圖示產生失敗（%s）：%s", tool_id, e.__class__.__name__)
        return None
    with _lock:
        _cache[key] = png
    return png


def site_logo_png() -> Optional[bytes]:
    """站台 logo 的 PNG。管理員上傳過就用那張，否則用內建的。"""
    key = "logo"
    with _lock:
        if key in _cache:
            return _cache[key]
    try:
        from . import branding
        src: Optional[Path] = branding.get_custom_logo_path()
        if not src or not src.is_file():
            # 內建 logo：信件底色是白的，用深色版才看得見
            src = (Path(__file__).resolve().parent.parent.parent
                   / "static" / "images" / "logo-on-light.png")
        if not src.is_file():
            return None
        data = src.read_bytes()
        # 縮到信裡要用的大小 —— 原圖可能是幾百 KB 的高解析度圖，
        # 每封信都夾一份會讓信箱很快變大。
        try:
            import io

            from PIL import Image
            with Image.open(io.BytesIO(data)) as im:
                im = im.convert("RGBA")
                if im.height > _LOGO_MAX_PX:
                    ratio = _LOGO_MAX_PX / im.height
                    im = im.resize((max(1, int(im.width * ratio)), _LOGO_MAX_PX),
                                   Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format="PNG", optimize=True)
                data = buf.getvalue()
        except Exception:  # noqa: BLE001 — 縮不了就用原圖
            pass
    except Exception as e:  # noqa: BLE001
        logger.info("站台 logo 讀取失敗：%s", e.__class__.__name__)
        return None
    with _lock:
        _cache[key] = data
    return data


def invalidate() -> None:
    """管理員換了 logo 之後呼叫。"""
    with _lock:
        _cache.pop("logo", None)
