"""頁面加框 —— 上傳 PDF 或文書檔，替每一頁加上框線。

## 為什麼要收 Office 檔

使用者的原話是「主要是針對投影片應用，不過既然有 PDF 或 Office 檔案可以進來
都支援」。投影片（.pptx / .odp）加外框是最常見的用途，而使用者手上多半是原始的
簡報檔而不是 PDF —— 逼人先去別的工具轉一次 PDF 再回來，等於把工具拆成兩半。

所以這裡接受 PDF 與所有 Office 引擎讀得懂的格式，**內部先轉成 PDF 再加框**，
輸出一律是 PDF（使用者選定：只要 PDF）。

## 預覽用的是同一段畫框程式

預覽縮圖不是前端疊一個 CSS 邊框上去，而是**真的把框畫進 PDF 再渲染成 PNG**
（`border_render.draw_border`，與送出走同一條路）。前端模擬的預覽遲早會跟實際
輸出對不起來，而框線這種東西「差一點」使用者一眼就看得出來。
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

import fitz
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response

from ...config import settings
from ...core import office_convert, pdf_preview, upload_owner as _uo
from ...core.job_manager import job_manager
from ...core.safe_paths import require_uuid_hex
from . import border_render as BR

router = APIRouter()

#: 暫存檔前綴。**要能被 `upload_owner.extract_upload_id` 切出 id** ——
#: 前綴裡不可以再出現底線以外的分隔（pdf-watermark 的 `wm_` 前綴就因為切錯
#: 導致歸屬檢查整個失效，見 v1.11.80）。
_PREFIX = "bd"


def _src_path(upload_id: str) -> Path:
    return settings.temp_dir / f"{_PREFIX}_{upload_id}.pdf"


def _spec_from_form(
    *, mode: str, margin_mm: float, width_pt: float, color: str, style: str,
    radius_mm: float, opacity: float, double: bool, double_gap_mm: float,
    shadow: bool, shadow_color: str, shadow_blur_mm: float,
    shadow_offset_mm: float, shadow_opacity: float,
    pages: str, skip_first: bool, page_count: int,
) -> BR.BorderSpec:
    """把表單欄位收斂成 BorderSpec，順便把數值夾在合理範圍。

    夾範圍是**在伺服器端**做的 —— 前端的 min/max 只是提示，API 呼叫者不受它拘束。
    線寬給到 500pt 之類的值會把整頁塗滿，等於毀了使用者的檔案。
    """
    return BR.BorderSpec(
        mode="content" if mode == "content" else "page",
        margin_mm=max(0.0, min(100.0, float(margin_mm))),
        # 上限 72pt（1 英寸）。再粗就不是「框線」而是「色塊」了；每一頁還會再依
        # 該頁短邊夾一次（見 border_render._effective_width），名片大小的頁面
        # 不會被框吃掉。**上限要讓使用者看得到** —— 前端會把欄位夾回上限值，
        # 不然填 200 卻沒變化，只會以為工具壞了（實際回報過）。
        width_pt=max(0.1, min(72.0, float(width_pt))),
        color=color or "#333333",
        style=style if style in ("solid", "dashed", "dotted") else "solid",
        radius_mm=max(0.0, min(50.0, float(radius_mm))),
        opacity=max(0.05, min(1.0, float(opacity))),
        double=bool(double),
        double_gap_mm=max(0.2, min(30.0, float(double_gap_mm))),
        shadow=bool(shadow),
        shadow_color=shadow_color or "#000000",
        shadow_blur_mm=max(0.0, min(20.0, float(shadow_blur_mm))),
        shadow_offset_mm=max(0.0, min(20.0, float(shadow_offset_mm))),
        shadow_opacity=max(0.0, min(1.0, float(shadow_opacity))),
        pages=BR.parse_pages(pages, page_count),
        skip_first=bool(skip_first),
    )


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "pdf_border.html",
                                      {"request": request})


async def _stash(request: Request, data: bytes, filename: str) -> dict:
    """把上傳（或工作區取回）的檔案轉成 PDF 存起來，回頁數與縮圖清單。"""
    if not data:
        raise HTTPException(400, "空檔案")
    name = Path(filename or "document").name
    is_pdf = name.lower().endswith(".pdf") or data[:4] == b"%PDF"
    if not is_pdf and not office_convert.is_office_file(name):
        raise HTTPException(400, "只支援 PDF 與文書檔（Word / Excel / PowerPoint / ODF）")

    upload_id = uuid.uuid4().hex
    _uo.record(upload_id, request)
    dst = _src_path(upload_id)
    if is_pdf:
        dst.write_bytes(data)
    else:
        raw = settings.temp_dir / f"{_PREFIX}raw_{upload_id}{Path(name).suffix}"
        raw.write_bytes(data)
        try:
            office_convert.convert_to_pdf(raw, dst)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, "文書檔轉換失敗，請確認檔案是否完整")
        finally:
            raw.unlink(missing_ok=True)
        if not dst.exists():
            raise HTTPException(400, "文書檔轉換失敗（沒有產出 PDF）")
    try:
        with fitz.open(str(dst)) as doc:
            n = doc.page_count
    except Exception:  # noqa: BLE001
        dst.unlink(missing_ok=True)
        raise HTTPException(400, "檔案讀取失敗，可能已毀損")
    if not n:
        raise HTTPException(400, "這份文件沒有任何頁面")
    return {
        "upload_id": upload_id, "filename": name, "page_count": n,
        "converted": not is_pdf,
        "pages": [{"page": i + 1,
                   "thumb": f"/tools/pdf-border/thumb/{upload_id}/{i + 1}"}
                  for i in range(n)],
    }


@router.post("/load")
async def load(request: Request, file: UploadFile = File(...)):
    return await _stash(request, await file.read(), file.filename or "")


@router.get("/thumb/{upload_id}/{page}")
async def thumb(upload_id: str, page: int, request: Request,
                large: bool = False):
    """原稿縮圖（還沒加框）。"""
    require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    src = _src_path(upload_id)
    if not src.exists():
        raise HTTPException(404, "檔案不存在（可能已過期）")
    suffix = "_large" if large else ""
    out = settings.temp_dir / f"{_PREFIX}_{upload_id}_t{suffix}_{page}.png"
    if not out.exists():
        pdf_preview.render_page_png(src, out, page - 1,
                                    dpi=160 if large else 72)
    return FileResponse(str(out), media_type="image/png",
                        headers={"Cache-Control": "max-age=300"})


@router.post("/preview")
async def preview(
    request: Request,
    upload_id: str = Form(...),
    page: int = Form(1),
    mode: str = Form("page"),
    margin_mm: float = Form(5.0),
    width_pt: float = Form(1.5),
    color: str = Form("#333333"),
    style: str = Form("solid"),
    radius_mm: float = Form(0.0),
    opacity: float = Form(1.0),
    double: bool = Form(False),
    double_gap_mm: float = Form(1.5),
    shadow: bool = Form(False),
    shadow_color: str = Form("#000000"),
    shadow_blur_mm: float = Form(1.2),
    shadow_offset_mm: float = Form(0.6),
    shadow_opacity: float = Form(0.25),
    pages: str = Form(""),
    skip_first: bool = Form(False),
    large: bool = Form(False),
):
    """把框畫進**這一頁**再渲染成 PNG 回傳。

    刻意不是前端疊 CSS 邊框 —— 圓角、虛線間距、陰影這些在瀏覽器與 PDF 的算法
    不一樣，模擬出來的預覽跟實際輸出對不起來，而框線差一點使用者一眼看得出來。
    """
    require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    src = _src_path(upload_id)
    if not src.exists():
        raise HTTPException(404, "檔案不存在（可能已過期）")
    with fitz.open(str(src)) as doc:
        if page < 1 or page > doc.page_count:
            raise HTTPException(400, "頁碼超出範圍")
        spec = _spec_from_form(
            mode=mode, margin_mm=margin_mm, width_pt=width_pt, color=color,
            style=style, radius_mm=radius_mm, opacity=opacity, double=double,
            double_gap_mm=double_gap_mm, shadow=shadow,
            shadow_color=shadow_color, shadow_blur_mm=shadow_blur_mm,
            shadow_offset_mm=shadow_offset_mm, shadow_opacity=shadow_opacity,
            pages=pages, skip_first=skip_first, page_count=doc.page_count)
        drawn = BR.draw_border(doc[page - 1], spec, page_no=page,
                               total=doc.page_count)
        pix = doc[page - 1].get_pixmap(dpi=160 if large else 72, alpha=False)
        png = pix.tobytes("png")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store",
                             # 讓前端知道這頁有沒有被畫（排除的頁要標示出來）
                             "X-Border-Drawn": "1" if drawn else "0"})


@router.post("/submit")
async def submit(
    request: Request,
    upload_id: str = Form(...),
    mode: str = Form("page"),
    margin_mm: float = Form(5.0),
    width_pt: float = Form(1.5),
    color: str = Form("#333333"),
    style: str = Form("solid"),
    radius_mm: float = Form(0.0),
    opacity: float = Form(1.0),
    double: bool = Form(False),
    double_gap_mm: float = Form(1.5),
    shadow: bool = Form(False),
    shadow_color: str = Form("#000000"),
    shadow_blur_mm: float = Form(1.2),
    shadow_offset_mm: float = Form(0.6),
    shadow_opacity: float = Form(0.25),
    pages: str = Form(""),
    skip_first: bool = Form(False),
    out_name: str = Form(""),
):
    require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    src = _src_path(upload_id)
    if not src.exists():
        raise HTTPException(404, "檔案不存在（可能已過期）")
    with fitz.open(str(src)) as doc:
        page_count = doc.page_count
    spec = _spec_from_form(
        mode=mode, margin_mm=margin_mm, width_pt=width_pt, color=color,
        style=style, radius_mm=radius_mm, opacity=opacity, double=double,
        double_gap_mm=double_gap_mm, shadow=shadow, shadow_color=shadow_color,
        shadow_blur_mm=shadow_blur_mm, shadow_offset_mm=shadow_offset_mm,
        shadow_opacity=shadow_opacity, pages=pages, skip_first=skip_first,
        page_count=page_count)

    stem = Path(out_name or "document").stem or "document"
    out = settings.temp_dir / f"{_PREFIX}out_{upload_id}.pdf"

    def run(job):
        job.message = "加框中…"
        with fitz.open(str(src)) as doc:
            total = doc.page_count
            n = 0
            for i in range(total):
                if job.cancelled:
                    raise RuntimeError("已取消")
                if BR.draw_border(doc[i], spec, page_no=i + 1, total=total):
                    n += 1
                job.progress = (i + 1) / max(1, total) * 0.95
                job.message = f"加框中… {i + 1}/{total}"
            doc.save(str(out), garbage=3, deflate=True)
        job.result_path = out
        job.result_filename = f"{stem}_border.pdf"
        job.progress = 1.0
        job.message = f"完成（{n} / {total} 頁加了框線）"

    job = job_manager.submit("pdf-border", run, request=request,
                             meta={"filename": f"{stem}.pdf",
                                   "count": page_count})
    return {"job_id": job.id}


@router.post("/api/pdf-border", include_in_schema=True)
async def api_pdf_border(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form("page"),
    margin_mm: float = Form(5.0),
    width_pt: float = Form(1.5),
    color: str = Form("#333333"),
    style: str = Form("solid"),
    radius_mm: float = Form(0.0),
    opacity: float = Form(1.0),
    double: bool = Form(False),
    double_gap_mm: float = Form(1.5),
    shadow: bool = Form(False),
    shadow_color: str = Form("#000000"),
    shadow_blur_mm: float = Form(1.2),
    shadow_offset_mm: float = Form(0.6),
    shadow_opacity: float = Form(0.25),
    pages: str = Form(""),
    skip_first: bool = Form(False),
):
    """單次上傳 PDF 或文書檔，替每頁加框後直接回 PDF。

    mode: `page`（自頁緣內縮，預設）/ `content`（貼齊內容）。
    style: `solid` / `dashed` / `dotted`。pages 例：`1,3,5-8`（留空 = 全部）。
    """
    data = await file.read()
    info = await _stash(request, data, file.filename or "")
    upload_id = info["upload_id"]
    src = _src_path(upload_id)
    out = settings.temp_dir / f"{_PREFIX}api_{upload_id}.pdf"
    stem = Path(file.filename or "document").stem or "document"

    import asyncio as _asyncio

    def _do():
        with fitz.open(str(src)) as doc:
            spec = _spec_from_form(
                mode=mode, margin_mm=margin_mm, width_pt=width_pt, color=color,
                style=style, radius_mm=radius_mm, opacity=opacity,
                double=double, double_gap_mm=double_gap_mm, shadow=shadow,
                shadow_color=shadow_color, shadow_blur_mm=shadow_blur_mm,
                shadow_offset_mm=shadow_offset_mm,
                shadow_opacity=shadow_opacity, pages=pages,
                skip_first=skip_first, page_count=doc.page_count)
            BR.apply(doc, spec)
            doc.save(str(out), garbage=3, deflate=True)

    await _asyncio.to_thread(_do)
    return FileResponse(str(out), media_type="application/pdf",
                        filename=f"{stem}_border.pdf")
