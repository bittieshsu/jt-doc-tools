"""PDF → Image endpoints."""
from __future__ import annotations

import io
import logging
import math
import re
import uuid
import zipfile
from pathlib import Path
from typing import Optional

import fitz
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from ...config import settings
from ...core import office_convert


logger = logging.getLogger(__name__)
router = APIRouter()

#: 輸出格式 → （副檔名, media type）。`jpg` 是 `jpeg` 的別名。
FORMATS: dict[str, tuple[str, str]] = {
    "png": ("png", "image/png"),
    "webp": ("webp", "image/webp"),
    "jpeg": ("jpg", "image/jpeg"),
}
_FORMAT_ALIASES = {"jpg": "jpeg"}
_EXT_MEDIA = {ext: media for ext, media in FORMATS.values()}

#: WebP / JPEG 的品質預設值。實測 16:9 簡報寬 1920：PNG 479 KB、WebP 80 → 122 KB、
#: JPEG 85 → 242 KB（每頁平均）—— WebP 在 80 已經看不出差別。
DEFAULT_QUALITY = 80

#: 指定寬度的範圍（像素）。
WIDTH_MIN, WIDTH_MAX = 16, 10000

#: 單頁最多幾個像素。**這支原本借用預覽用的算圖函式**，最長邊被壓在 1800 像素 ——
#: A4 選 200 / 300 / 400 DPI 產出一模一樣（1265×1790），回應卻寫著使用者選的 DPI。
#: 現在照選的 DPI / 寬度算，只在超過這個上限時才縮（RGB 約 120 MB），
#: 而且**縮了要講出來**（`reduced`），不可以再安靜地給一張比選的小的圖。
MAX_PIXELS = 40_000_000

#: WebP 格式本身的限制：單邊最多 16383 像素。
WEBP_MAX_SIDE = 16383

#: 轉出來的每一頁：`p2i_<upload_id>_p<頁碼>.<副檔名>`。
_PAGE_FILE_RE = re.compile(r"^p2i_([0-9a-f]{32})_p(\d+)\.(png|webp|jpg)$")


def _work_dir() -> Path:
    return settings.temp_dir


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "pdf_to_image.html", {"request": request})


def _normalise_format(fmt: str) -> str:
    f = (fmt or "png").strip().lower()
    f = _FORMAT_ALIASES.get(f, f)
    if f not in FORMATS:
        raise HTTPException(400, f"不支援的圖片格式：{fmt}（可用 png / webp / jpeg）")
    return f


def _parse_width(width: Optional[str]) -> Optional[int]:
    """空白或 0 ＝不指定（照 DPI）。其他值**不可以安靜地夾到範圍裡** ——
    使用者要的是那個寬度，給他別的寬度就是另一張圖。"""
    if width is None or str(width).strip() in ("", "0"):
        return None
    try:
        w = int(str(width).strip())
    except ValueError:
        raise HTTPException(400, f"寬度要是整數像素：{width}")
    if not (WIDTH_MIN <= w <= WIDTH_MAX):
        raise HTTPException(400, f"寬度要在 {WIDTH_MIN}～{WIDTH_MAX} 像素之間：{w}")
    return w


def page_zoom(page_w: float, page_h: float, *, dpi: int, width: Optional[int],
              fmt: str) -> tuple[float, bool]:
    """算這一頁的縮放倍率，回 `(zoom, reduced)`。

    `page_w` / `page_h` 是**轉向後**的頁面尺寸（點）—— `page.rect` 已經含 `/Rotate`，
    所以橫放的頁面指定寬度 1920 就是橫的那一邊 1920。
    `reduced` ＝超過像素上限（或 WebP 的單邊限制）而縮小了。
    """
    page_w = max(page_w, 1.0)
    page_h = max(page_h, 1.0)
    zoom = (width / page_w) if width else (dpi / 72.0)
    w_px, h_px = page_w * zoom, page_h * zoom
    scale = 1.0
    if w_px * h_px > MAX_PIXELS:
        scale = math.sqrt(MAX_PIXELS / (w_px * h_px))
    if fmt == "webp" and max(w_px, h_px) * scale > WEBP_MAX_SIDE:
        scale = min(scale, WEBP_MAX_SIDE / max(w_px, h_px))
    return zoom * scale, scale < 1.0


def render_page(page: "fitz.Page", out: Path, *, dpi: int, width: Optional[int],
                fmt: str, quality: int) -> dict:
    """把一頁算成圖片寫到 `out`，回這一頁的資訊。"""
    zoom, reduced = page_zoom(page.rect.width, page.rect.height,
                              dpi=dpi, width=width, fmt=fmt)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = None
    if width and not reduced and pix.width != width and abs(pix.width - width) <= 2:
        # 換算時的捨入讓寬度差一兩個像素 —— 使用者指定的是確切的寬度，縮回去。
        # **只收捨入誤差**：實測 1170 種頁面尺寸 × 寬度 × 格式一次都沒觸發；
        # 差得更多就是算錯了，縮回去等於把錯藏起來（變異驗證時它把「寬度整個
        # 沒生效」蓋成只紅兩條）。
        from PIL import Image
        samples = pix.samples
        img = Image.frombytes("RGB", (pix.width, pix.height), samples)
        h = max(1, round(pix.height * width / pix.width))
        logger.info("pdf-to-image：第 %d 頁算出 %d px，縮成指定的 %d px",
                    page.number + 1, pix.width, width)
        img = img.resize((width, h), Image.LANCZOS)
    if fmt == "png" and img is None:
        pix.save(str(out))
        w_px, h_px = pix.width, pix.height
    else:
        from PIL import Image
        if img is None:
            samples = pix.samples            # 這是 property，每次存取都重建一份
            img = Image.frombytes("RGB", (pix.width, pix.height), samples)
        if fmt == "webp":
            img.save(str(out), "WEBP", quality=quality, method=4)
        elif fmt == "jpeg":
            img.save(str(out), "JPEG", quality=quality, optimize=True, progressive=True)
        else:
            img.save(str(out), "PNG")
        w_px, h_px = img.width, img.height
    return {
        "width_px": w_px,
        "height_px": h_px,
        "dpi": round(zoom * 72.0),
        "reduced": reduced,
        "size_bytes": out.stat().st_size,
    }


@router.post("/convert")
async def convert(
    request: Request,
    file: UploadFile = File(...),
    dpi: int = Form(200),
    format: str = Form("png"),
    width: Optional[str] = Form(None),
    quality: int = Form(DEFAULT_QUALITY),
):
    """Convert PDF / Office doc to per-page images.

    `format`：`png`（預設）/ `webp` / `jpeg`（`jpg` 也收）。
    `width`：指定每一頁的寬度（像素，16～10000）；有給就不看 `dpi`，
    每一頁縮放成同一個寬度、高度依頁面比例。
    `dpi`：沒給寬度時的解析度，夾在 [72, 600]。
    `quality`：WebP / JPEG 的品質 1～100，預設 80；PNG 無損，不看這個值。

    單頁超過 4000 萬像素（或 WebP 單邊超過 16383）的會縮小，那一頁的
    `reduced` 是 true —— 不會安靜地給一張比要求小的圖。
    """
    try:
        dpi = int(dpi)
    except (TypeError, ValueError):
        dpi = 200
    dpi = max(72, min(600, dpi))
    fmt = _normalise_format(format)
    want_width = _parse_width(width)
    try:
        quality = int(quality)
    except (TypeError, ValueError):
        quality = DEFAULT_QUALITY
    quality = max(1, min(100, quality))
    ext_out = FORMATS[fmt][0]
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    orig_name = file.filename or "document"
    # Surface filename to the audit middleware (logged on response).
    request.state.upload_filename = orig_name
    ext = Path(orig_name).suffix.lower()
    is_pdf = ext == ".pdf"
    is_office = office_convert.is_office_file(orig_name)
    if not (is_pdf or is_office):
        raise HTTPException(
            400,
            f"不支援的檔案格式：{ext or '未知'}；支援 PDF 與 Office 檔（.docx/.xlsx/.pptx/.odt/.ods/.odp/.doc/.xls/.ppt/.rtf/.txt/.csv）",
        )

    upload_id = uuid.uuid4().hex
    from ...core import upload_owner as _uo
    _uo.record(upload_id, request)
    work = _work_dir()
    work.mkdir(parents=True, exist_ok=True)

    # Heavy lifting (soffice convert + PyMuPDF page render loop) is sync and
    # blocks the asyncio event loop if run inline — same trap as v1.1.29
    # fixed in pdf-extract-text. Push to thread pool so the rest of the
    # site stays responsive while a big file converts.
    import asyncio as _asyncio

    def _do_convert():
        if is_pdf:
            src_p = work / f"p2i_{upload_id}_in.pdf"
            src_p.write_bytes(data)
        else:
            office_src = work / f"p2i_{upload_id}_in{ext}"
            office_src.write_bytes(data)
            src_p = work / f"p2i_{upload_id}_in.pdf"
            try:
                office_convert.convert_to_pdf(office_src, src_p, timeout=120.0)
            except RuntimeError:
                raise HTTPException(
                    500,
                    "找不到 Office 轉檔引擎（OxOffice / LibreOffice）。請到「轉檔引擎設定」確認安裝路徑。",
                )
            except Exception as e:
                raise HTTPException(500, f"轉檔失敗：{e}")
            if not src_p.exists():
                raise HTTPException(500, "轉檔未產生 PDF。")
        try:
            (work / f"p2i_{upload_id}_name.txt").write_text(orig_name, encoding="utf-8")
        except Exception:
            pass
        pages_local = []
        with fitz.open(str(src_p)) as doc:
            for i in range(doc.page_count):
                out = work / f"p2i_{upload_id}_p{i+1}.{ext_out}"
                info = render_page(doc[i], out, dpi=dpi, width=want_width,
                                   fmt=fmt, quality=quality)
                pages_local.append({
                    "index": i,
                    **info,
                    "preview_url": f"/tools/pdf-to-image/preview/{out.name}",
                })
        return pages_local

    pages_info = await _asyncio.to_thread(_do_convert)
    total_bytes = sum(p["size_bytes"] for p in pages_info)

    return {
        "upload_id": upload_id,
        "filename": file.filename,
        "page_count": len(pages_info),
        "format": fmt,
        "quality": quality if fmt != "png" else None,
        # 指定寬度時每一頁的 DPI 不同（看各頁尺寸），頂層就不給一個假的數字
        "size_mode": "width" if want_width else "dpi",
        "width": want_width,
        "dpi": None if want_width else dpi,
        "reduced_pages": [p["index"] + 1 for p in pages_info if p["reduced"]],
        "total_bytes": total_bytes,
        "pages": pages_info,
    }


@router.get("/preview/{filename}")
async def preview(filename: str, request: Request):
    from app.core.safe_paths import safe_join, is_safe_name
    from ...core import upload_owner
    if not (filename.startswith("p2i_") and is_safe_name(filename)):
        raise HTTPException(400, "invalid filename")
    path = safe_join(_work_dir(), filename)
    # fail-closed：認不出 upload_id 就不給（見 upload_owner.require_by_filename）。
    upload_owner.require_by_filename(filename, request)
    if not path.exists():
        raise HTTPException(404, "not found")
    return FileResponse(str(path), media_type=_EXT_MEDIA.get(path.suffix.lstrip(".").lower(), "application/octet-stream"))


@router.get("/download/{upload_id}")
async def download(upload_id: str, request: Request):
    from app.core.safe_paths import require_uuid_hex
    from ...core import upload_owner
    require_uuid_hex(upload_id, "upload_id")
    upload_owner.require(upload_id, request)
    work = _work_dir()
    # Recover original filename
    orig = "document.pdf"
    name_file = work / f"p2i_{upload_id}_name.txt"
    try:
        if name_file.exists():
            orig = name_file.read_text(encoding="utf-8").strip() or orig
    except Exception:
        pass
    base = orig.rsplit(".", 1)[0]

    # Find all rendered pages. NOTE: sort by the NUMERIC page index parsed
    # from the filename — a plain string sort gives _p1, _p10, _p11 … _p2 …
    # which (combined with re-numbering) scrambled the ZIP filenames for any
    # PDF with ≥10 pages (page 10 got renamed _p2.png, etc.).
    #
    # 正規式是**固定的**，編號比對放在外面 —— 不把網址上的值組進正規式
    # （CodeQL #198；`upload_id` 雖然上面驗過是 32 碼十六進位，組進去仍是壞習慣）。
    pages: list[tuple[int, Path]] = []
    for p in work.glob(f"p2i_{upload_id}_p*"):
        m = _PAGE_FILE_RE.match(p.name)
        if m and m.group(1) == upload_id:
            pages.append((int(m.group(2)), p))
    pages.sort(key=lambda x: x[0])
    if not pages:
        raise HTTPException(404, "沒有產生的圖片，請重新上傳")

    if len(pages) == 1:
        # Single page → direct image download
        path = pages[0][1]
        ext = path.suffix.lstrip(".")
        return FileResponse(
            str(path), media_type=_EXT_MEDIA.get(ext, "application/octet-stream"),
            filename=f"{base}.{ext}",
        )

    # Multi-page → ZIP bundle. Use the page's OWN number from its filename for
    # the arcname (do NOT re-enumerate) so it always matches the PDF page.
    zip_path = work / f"p2i_{upload_id}.zip"
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as z:
        for num, p in pages:
            z.write(p, arcname=f"{base}_p{num}{p.suffix}")
    return FileResponse(
        str(zip_path), media_type="application/zip",
        filename=f"{base}.zip",
    )
