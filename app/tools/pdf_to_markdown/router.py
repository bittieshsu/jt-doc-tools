"""Endpoints for the PDF → Markdown tool (powered by pymupdf4llm)."""
from __future__ import annotations

import asyncio
import io
import logging
import re
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from ...config import settings
from ...core import upload_owner as _uo
from ...core.http_utils import content_disposition
from ...core.safe_paths import require_uuid_hex

log = logging.getLogger("app.pdf_to_markdown")

router = APIRouter()

_MAX_PDF_SIZE = 100 * 1024 * 1024  # 100 MB


def _work_dir(uid: str) -> Path:
    require_uuid_hex(uid, "upload_id")
    d = settings.temp_dir / f"pdf2md_{uid}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_stem(filename: str) -> str:
    """Strip path separators + dangerous chars from user-supplied filename."""
    stem = Path(filename).stem if filename else "document"
    # Keep ASCII letters / digits / dash / underscore / CJK chars
    safe = re.sub(r"[^\w一-鿿\-]+", "_", stem)
    safe = safe.strip("_") or "document"
    return safe[:80]


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "pdf_to_markdown.html", {"request": request})


_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(s: str) -> int:
    """Convert a Chinese ordinal string ('一', '十', '十一', '二十三', '一百零五')
    to int. Returns 0 if unrecognized.
    Supports up to 千 (1000s) which covers any legal / government list."""
    if not s:
        return 0
    # Strip leading '零' filler (used in '一百零五' = 100+5 to skip the tens place)
    while s and s[0] == "零":
        s = s[1:]
    if not s:
        return 0
    if s == "十":
        return 10
    if "千" in s:
        parts = s.split("千", 1)
        ks = _CN_DIGIT.get(parts[0], 1) if parts[0] else 1
        return ks * 1000 + _cn_to_int(parts[1])
    if "百" in s:
        parts = s.split("百", 1)
        hs = _CN_DIGIT.get(parts[0], 1) if parts[0] else 1
        return hs * 100 + _cn_to_int(parts[1])
    if "十" in s:
        parts = s.split("十", 1)
        ts = _CN_DIGIT.get(parts[0], 1) if parts[0] else 1
        os_ = _CN_DIGIT.get(parts[1], 0) if parts[1] else 0
        return ts * 10 + os_
    return _CN_DIGIT.get(s, 0)


def _convert_cn_list_markers(md: str) -> str:
    """Convert Chinese ordinal list markers to Markdown numbered list syntax.

    Patterns recognized:
      `一、` `二、` ... `十、` `十一、`        → `1. ` `2. ` ...  (top-level)
      `（一）` `(一)` `（二）` ...            → `   1. ` `   2. ` (nested 3-space indent)
      `１.` `２.`(full-width digits)           → `1.` `2.`

    Skips content inside fenced code blocks (` ``` `) so we don't munge real
    code samples that happen to contain `一、`.
    """
    if not md:
        return md
    lines = md.split("\n")
    out: list[str] = []
    in_code = False
    cn_re = re.compile(r"^([一二三四五六七八九十百千零]+)、\s*(.*)$")
    nested_re = re.compile(r"^[（(]\s*([一二三四五六七八九十百千零]+)\s*[)）]\s*(.*)$")
    fullwidth_re = re.compile(r"^([0-9])\s*[.、]\s*(.*)$".replace("0-9", "０-９"))
    for line in lines:
        if line.lstrip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        # Full-width digit list marker: '１.' '２.' → '1.' '2.'
        fw = fullwidth_re.match(stripped)
        if fw:
            n = ord(fw.group(1)) - 0xFF10
            out.append(" " * indent + f"{n}. {fw.group(2)}")
            continue

        # Top-level Chinese ordinal: 一、二、...
        m = cn_re.match(stripped)
        if m:
            n = _cn_to_int(m.group(1))
            if 1 <= n <= 200:
                out.append(" " * indent + f"{n}. {m.group(2)}")
                continue

        # Nested Chinese ordinal: （一）(一)...
        m = nested_re.match(stripped)
        if m:
            n = _cn_to_int(m.group(1))
            if 1 <= n <= 200:
                # Force 3-space indent so markdown parsers treat as nested
                base = max(indent, 3)
                out.append(" " * base + f"{n}. {m.group(2)}")
                continue

        out.append(line)
    return "\n".join(out)


def _convert_pdf_to_markdown(
    src: Path,
    *,
    page_chunks: bool,
    page_separator: bool,
    include_images: bool,
    image_format: str,
    image_dir: Optional[Path] = None,
) -> tuple[str, list[Path]]:
    """Run pymupdf4llm on src PDF. Returns (markdown_text, image_paths).

    pymupdf4llm writes images to `image_path` when `write_images=True`. We
    collect those into a list so the caller can package them into a ZIP.
    """
    import pymupdf4llm  # type: ignore
    kwargs: dict = {
        "page_chunks": False,    # always join — caller can split if needed
        "show_progress": False,
    }
    if include_images and image_dir is not None:
        kwargs["write_images"] = True
        kwargs["image_path"] = str(image_dir)
        kwargs["image_format"] = image_format

    md = pymupdf4llm.to_markdown(str(src), **kwargs)
    if not isinstance(md, str):
        # When page_chunks=True it returns list[dict] — but we forced False above
        md = str(md)

    # Optional page separator. pymupdf4llm 0.3.x already inserts page markers
    # in some configurations; for consistency we don't add extra.
    if not page_separator:
        # Remove '-----' page break lines if any
        md = re.sub(r"^-----\s*$", "", md, flags=re.MULTILINE)

    # Post-process: convert Chinese ordinal list markers to standard Markdown
    # numbered lists so any markdown viewer (incl. our preview) renders them
    # as real ordered lists with proper indent / numbering.
    md = _convert_cn_list_markers(md)

    images: list[Path] = []
    if include_images and image_dir is not None and image_dir.exists():
        for p in sorted(image_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                images.append(p)
    return md, images


def _build_zip(stem: str, md_path: Path, images: list[Path]) -> Path:
    """Pack the .md file + images into a ZIP for download."""
    zip_path = md_path.parent / f"{stem}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(md_path, arcname=md_path.name)
        for img in images:
            zf.write(img, arcname=f"images/{img.name}")
    return zip_path


@router.post("/convert")
async def convert(
    request: Request,
    file: UploadFile = File(...),
    include_images: bool = Form(False),
    page_separator: bool = Form(True),
    image_format: str = Form("png"),
):
    """Convert PDF to Markdown. Returns JSON with markdown text + download URL.

    The original PDF + generated artifacts are kept under a temp working dir
    keyed by upload_id; clean-up handled by the temp janitor."""
    fname = file.filename or "document.pdf"
    if not fname.lower().endswith(".pdf"):
        raise HTTPException(400, "只支援 PDF")
    data = await file.read()
    if not data:
        raise HTTPException(400, "空檔案")
    if len(data) > _MAX_PDF_SIZE:
        raise HTTPException(400, f"PDF 超過上限 {_MAX_PDF_SIZE // (1024*1024)} MB")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "不是 PDF 檔")
    if image_format not in ("png", "jpg", "webp"):
        image_format = "png"

    uid = uuid.uuid4().hex
    _uo.record(uid, request)
    wdir = _work_dir(uid)
    src = wdir / "src.pdf"
    src.write_bytes(data)
    stem = _safe_stem(fname)

    def _do():
        image_dir = wdir / "images" if include_images else None
        if image_dir:
            image_dir.mkdir(exist_ok=True)
        try:
            md, imgs = _convert_pdf_to_markdown(
                src,
                page_chunks=False,
                page_separator=page_separator,
                include_images=include_images,
                image_format=image_format,
                image_dir=image_dir,
            )
        except Exception as e:
            log.exception("pdf2md conversion failed")
            raise HTTPException(500, f"轉換失敗: {e.__class__.__name__}") from e
        md_path = wdir / f"{stem}.md"
        md_path.write_text(md, encoding="utf-8")
        if include_images and imgs:
            zip_path = _build_zip(stem, md_path, imgs)
            return md, md_path, zip_path, len(imgs)
        return md, md_path, None, 0

    md_text, md_path, zip_path, n_imgs = await asyncio.to_thread(_do)

    return {
        "ok": True,
        "upload_id": uid,
        "stem": stem,
        "char_count": len(md_text),
        "line_count": md_text.count("\n") + 1,
        "image_count": n_imgs,
        "markdown": md_text,
        "download_url": f"/tools/pdf-to-markdown/download/{uid}/md",
        "download_zip_url": (
            f"/tools/pdf-to-markdown/download/{uid}/zip" if zip_path else None
        ),
    }


@router.get("/pdf/{upload_id}")
async def get_pdf(request: Request, upload_id: str):
    """Serve the original uploaded PDF for in-browser preview (PDF.js)."""
    require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    wdir = _work_dir(upload_id)
    src = wdir / "src.pdf"
    if not src.exists():
        raise HTTPException(404, "PDF 不存在或已過期")
    return FileResponse(
        str(src),
        media_type="application/pdf",
        headers={"Content-Disposition": "inline; filename=src.pdf"},
    )


@router.get("/download/{upload_id}/{kind}")
async def download(request: Request, upload_id: str, kind: str):
    require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    if kind not in ("md", "zip"):
        raise HTTPException(400, "kind must be 'md' or 'zip'")
    wdir = _work_dir(upload_id)
    if not wdir.exists():
        raise HTTPException(404, "工作目錄不存在或已過期")
    suffix = ".md" if kind == "md" else ".zip"
    candidates = [p for p in wdir.iterdir() if p.is_file() and p.suffix == suffix]
    if not candidates:
        raise HTTPException(404, "輸出檔不存在")
    out = candidates[0]
    # Starlette's FileResponse(filename=) handles CJK + RFC 5987 properly;
    # passing content_disposition() string into headers= would be silently
    # ignored and browser defaults to a generic ".txt" name.
    return FileResponse(
        str(out),
        media_type="text/markdown; charset=utf-8" if kind == "md" else "application/zip",
        filename=out.name,
    )


# ---------------- public API (single-shot, no UI session) ----------------

@router.post("/api/pdf-to-markdown", include_in_schema=True)
async def api_pdf_to_markdown(
    request: Request,
    file: UploadFile = File(...),
    include_images: bool = Form(False),
    page_separator: bool = Form(True),
    image_format: str = Form("png"),
):
    """Programmatic endpoint. Returns the Markdown body as text/markdown when
    include_images=False; ZIP when include_images=True."""
    fname = file.filename or "document.pdf"
    if not fname.lower().endswith(".pdf"):
        raise HTTPException(400, "只支援 PDF")
    data = await file.read()
    if not data or not data.startswith(b"%PDF"):
        raise HTTPException(400, "不是有效的 PDF")
    if len(data) > _MAX_PDF_SIZE:
        raise HTTPException(400, f"PDF 超過上限 {_MAX_PDF_SIZE // (1024*1024)} MB")
    if image_format not in ("png", "jpg", "webp"):
        image_format = "png"

    uid = uuid.uuid4().hex
    wdir = _work_dir(uid)
    src = wdir / "src.pdf"
    src.write_bytes(data)
    stem = _safe_stem(fname)

    def _do():
        image_dir = wdir / "images" if include_images else None
        if image_dir:
            image_dir.mkdir(exist_ok=True)
        md, imgs = _convert_pdf_to_markdown(
            src,
            page_chunks=False,
            page_separator=page_separator,
            include_images=include_images,
            image_format=image_format,
            image_dir=image_dir,
        )
        return md, imgs

    md_text, imgs = await asyncio.to_thread(_do)

    if include_images and imgs:
        # Pack in-memory ZIP
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{stem}.md", md_text)
            for img in imgs:
                zf.write(img, arcname=f"images/{img.name}")
        buf.seek(0)
        from fastapi.responses import Response
        return Response(
            buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": content_disposition(f"{stem}.zip")},
        )

    # Plain markdown response
    return PlainTextResponse(
        md_text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": content_disposition(f"{stem}.md")},
    )
