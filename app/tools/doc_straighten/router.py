"""文件拉正 —— 把拍歪、掃歪的文件拉正，裁掉黑邊、去除不勻的底色。

## 這一版只有自動模式

規劃分兩期（CLAUDE.md 的「待辦規劃【第 4 批】」）：第一期先把自動模式端到端
做出來，**拿真實掃描件實測品質**再決定第二期（使用者自己拉四個點）的細節。
合成樣本表現好不代表真實掃描件也好 —— 表單自動填寫就是因為真實語料缺某種
版型才漏掉一整類 bug。

## 為什麼不叫「校正」

這個專案裡「校正 / 校驗」已經是 LLM 逐欄比對的意思（表單填寫、送件檢核），
拿來當影像工具的名字會讓人以為跟 AI 有關 —— 而這支**完全不用 AI、不用 GPU**。

## 兩段式的背景作業

偵測（逐頁算四個角與歪斜角）與套用是兩件事，中間夾著使用者互動（第二期）。
這一期只有自動模式，所以是一個作業跑完；但**進度要逐頁報**，不然使用者
看不出它卡在第幾頁。
"""
from __future__ import annotations

import asyncio as _asyncio
import uuid
from pathlib import Path

import fitz
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from ...config import settings
from ...core import office_convert, pdf_preview, upload_owner as _uo
from ...core.job_manager import job_manager
from ...core.safe_paths import require_uuid_hex
from . import straighten_core as SC

router = APIRouter()

#: 暫存檔前綴。**要能被 `upload_owner.extract_upload_id` 切出 id** ——
#: 前綴裡不可以有底線以外的分隔（`wm_` 那次切錯讓歸屬檢查整個失效，v1.11.80）。
_PREFIX = "ds"

#: 算圖的解析度。200 dpi 是「看得清楚 + 跑得動」的平衡（實測 0.83 秒/頁）；
#: 300 dpi 細節好一點但 1.4 秒/頁、檔案也大一倍。
_DPI_CHOICES = (150, 200, 300)
#: 每個解析度的一句話（給 `option-card` 用）。**下拉選單看不出差別在哪** ——
#: 卡片把取捨直接寫在上面，使用者不必去讀底下那行灰字。
_DPI_NOTES = {150: "快，草稿夠用", 200: "平衡（建議）", 300: "細節好，慢一倍"}


def _src_path(upload_id: str) -> Path:
    return settings.temp_dir / f"{_PREFIX}_{upload_id}.pdf"


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "doc_straighten.html",
                                      {"request": request,
                                       "dpi_choices": _DPI_CHOICES,
                                       "dpi_notes": _DPI_NOTES})


async def _stash(request: Request, data: bytes, filename: str) -> dict:
    if not data:
        raise HTTPException(400, "空檔案")
    name = Path(filename or "document").name
    low = name.lower()
    is_pdf = low.endswith(".pdf") or data[:4] == b"%PDF"
    is_image = low.endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp",
                             ".webp", ".heic", ".heif"))
    if not (is_pdf or is_image or office_convert.is_office_file(name)):
        raise HTTPException(400, "只支援 PDF、圖片（手機拍的也可以）與文書檔")

    upload_id = uuid.uuid4().hex
    _uo.record(upload_id, request)
    dst = _src_path(upload_id)
    if is_pdf:
        dst.write_bytes(data)
    elif is_image:
        # 圖片先包成單頁 PDF，後面的流程就只有一條路
        raw = settings.temp_dir / f"{_PREFIX}raw_{upload_id}{Path(name).suffix}"
        raw.write_bytes(data)
        try:
            # HEIC（iPhone 拍的）要先轉成 PNG —— Pillow 本身不認那個格式，
            # `pillow_heif` 註冊之後才讀得到（`image_utils` 已經處理註冊）。
            from PIL import Image
            from ...core import image_utils as _iu  # noqa: F401 —— 註冊 HEIF
            with Image.open(raw) as img:
                w, h = img.size
            doc = fitz.open()
            # 以 200 dpi 反推頁面點數，讓輸出的紙張大小接近原始拍攝比例
            page = doc.new_page(width=w * 72 / 200, height=h * 72 / 200)
            page.insert_image(page.rect, filename=str(raw))
            doc.save(str(dst))
            doc.close()
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "圖片讀取失敗，可能已毀損或格式不支援")
        finally:
            raw.unlink(missing_ok=True)
    else:
        raw = settings.temp_dir / f"{_PREFIX}raw_{upload_id}{Path(name).suffix}"
        raw.write_bytes(data)
        try:
            office_convert.convert_to_pdf(raw, dst)
        except Exception:  # noqa: BLE001
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
    if n == 0:
        dst.unlink(missing_ok=True)
        raise HTTPException(400, "檔案沒有任何頁面")
    return {"upload_id": upload_id, "pages": n, "name": Path(name).stem}


@router.post("/load")
async def load(request: Request, file: UploadFile = File(...)):
    return await _stash(request, await file.read(), file.filename or "")


@router.get("/thumb/{upload_id}/{page}")
async def thumb(upload_id: str, page: int, request: Request):
    require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    src = _src_path(upload_id)
    if not src.exists():
        raise HTTPException(404, "檔案不存在（可能已過期）")
    out = settings.temp_dir / f"{_PREFIX}th_{upload_id}_{page}.png"
    if not out.exists():
        await pdf_preview.render_page_png_async(src, out, page - 1, dpi=70)
    return FileResponse(str(out), media_type="image/png",
                        headers={"Cache-Control": "no-store"})


@router.post("/preview")
async def preview(request: Request, upload_id: str = Form(...),
                  page: int = Form(1), dpi: int = Form(200),
                  binarize: bool = Form(False),
                  detect_quad: bool = Form(True),
                  rotate: int = Form(0),
                  quad: str = Form("")):
    """單頁的「修正後」預覽。

    **預覽跟產出走同一段程式**（`straighten_core.straighten_page`）——
    前端模擬的預覽遲早會跟實際輸出對不起來，而歪斜這種東西「差一點」
    使用者一眼就看得出來。
    """
    require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    src = _src_path(upload_id)
    if not src.exists():
        raise HTTPException(404, "檔案不存在（可能已過期）")

    def _work():
        import cv2
        import numpy as np
        with fitz.open(str(src)) as doc:
            if page < 1 or page > doc.page_count:
                raise HTTPException(404, "頁碼超出範圍")
            pix = doc[page - 1].get_pixmap(dpi=_clamp_dpi(dpi), alpha=False)
            arr = np.frombuffer(pix.samples, np.uint8).reshape(
                pix.height, pix.width, pix.n)
            gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY) if pix.n >= 3 \
                else arr[:, :, 0]
        h, w = gray.shape[:2]
        user_quad = _parse_quad(quad)
        if user_quad is not None:
            # 使用者拉的是**正規化座標**（0~1）—— 換算成這次算圖的像素。
            # 收像素的話，預覽用 200 dpi、輸出用 300 dpi 就整組跑掉了。
            q = np.float32([[x * w, y * h] for x, y in user_quad])
        else:
            # 彩色一起送（見 `_paper_mask`）—— 預覽與輸出要用同一組判準，
            # 不然畫面上看到的四個角跟實際裁出來的不一樣。
            q = SC.find_page_quad(
                gray, arr if pix.n >= 3 else None) if detect_quad else None
        fixed, res = SC.straighten_page(gray, quad=q, do_binarize=binarize,
                                        dpi=_clamp_dpi(dpi), page_no=page,
                                        rotate_deg=_clamp_rotate(rotate))
        png = cv2.imencode(".png", cv2.resize(
            fixed, None, fx=0.45, fy=0.45, interpolation=cv2.INTER_AREA))[1]
        out = settings.temp_dir / f"{_PREFIX}pv_{upload_id}_{page}.png"
        out.write_bytes(png.tobytes())
        # 自動抓到的四角也回給前端**當作拖曳的起點** —— 使用者只要修不滿意的
        # 那幾個角，不必四個重拉。回正規化座標，畫面縮放多少都對得上。
        auto = None
        if q is not None:
            auto = [[round(float(x) / w, 5), round(float(y) / h, 5)] for x, y in q]
        return {"url": f"/tools/doc-straighten/preview-img/{upload_id}/{page}",
                "angle": res.angle, "residual": res.residual,
                "quad_found": res.quad_found, "ms": res.ms,
                "rotate": res.rotate_deg, "quad": auto}

    return await _asyncio.to_thread(_work)


@router.get("/preview-img/{upload_id}/{page}")
async def preview_img(upload_id: str, page: int, request: Request):
    require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    out = settings.temp_dir / f"{_PREFIX}pv_{upload_id}_{page}.png"
    if not out.exists():
        raise HTTPException(404, "預覽不存在（請重新產生）")
    return FileResponse(str(out), media_type="image/png",
                        headers={"Cache-Control": "no-store"})



def _clamp_rotate(deg) -> int:
    """只收 0 / 90 / 180 / 270。**其餘一律當 0**，不要丟例外 ——
    這是畫面上按鈕送來的值，送壞了不該讓整個預覽失敗。"""
    try:
        d = int(deg) % 360
    except (TypeError, ValueError):
        return 0
    return d if d in (0, 90, 180, 270) else 0


def _parse_quad(raw: str):
    """解析前端送來的四個角：`"x1,y1,x2,y2,x3,y3,x4,y4"`，**正規化 0~1**。

    格式不對就回 `None`（退回自動偵測）—— 這是使用者拖出來的東西，
    不該因為少一個數字就整個失敗。範圍稍微超出一點（拖到圖外）放行並夾住，
    `quad_is_sane()` 還會再擋一次自交 / 過小。
    """
    if not raw:
        return None
    try:
        nums = [float(x) for x in raw.replace(" ", "").split(",") if x != ""]
    except ValueError:
        return None
    if len(nums) != 8:
        return None
    pts = [(min(1.0, max(0.0, nums[i])), min(1.0, max(0.0, nums[i + 1])))
           for i in range(0, 8, 2)]
    return pts

def _clamp_dpi(dpi: int) -> int:
    """夾在允許的範圍內 —— 前端的下拉只是提示，API 呼叫者不受它拘束。

    給到 1200 dpi 會讓一頁算圖吃掉幾百 MB 記憶體。
    """
    try:
        d = int(dpi)
    except Exception:  # noqa: BLE001
        return 200
    return min(_DPI_CHOICES[-1], max(_DPI_CHOICES[0], d))


def _run_job(src: Path, out: Path, *, dpi: int, binarize: bool,
             detect_quad: bool, stem: str, overrides: dict | None = None):
    def run(job):
        job.message = "拉正中…"

        def progress(done: int, total: int):
            job.progress = (done / max(1, total)) * 0.97
            job.message = f"拉正中… {done}/{total} 頁"

        results = SC.straighten_pdf(src, out, dpi=dpi, do_binarize=binarize,
                                    detect_quad=detect_quad,
                                    overrides=overrides,
                                    progress=progress,
                                    cancelled=lambda: job.cancelled)
        job.result_path = out
        job.result_filename = f"{stem}_straightened.pdf"
        done = [r for r in results if not r.skipped]
        skipped = len(results) - len(done)
        worst = max((abs(r.residual) for r in done), default=0.0)
        # **摘要放 meta** —— `Job.to_public()` 只送 meta，放 job.result 畫面看不到
        job.meta["pages"] = len(results)
        job.meta["worst_residual"] = round(worst, 2)
        job.meta["quad_pages"] = sum(1 for r in results if r.quad_found)
        job.meta["kept_pages"] = skipped
        # 使用者自己動過的頁數 —— 完成訊息要講出來，不然他不確定有沒有吃到
        job.meta["manual_pages"] = len(overrides or {})
        job.progress = 1.0
        # **原樣保留幾頁一定要講出來** —— 使用者丟一份原生 PDF 進來，
        # 看到「完成」卻什麼都沒變的話會以為工具壞了。
        msg = f"完成（{len(results)} 頁"
        if done:
            msg += f"，處理 {len(done)} 頁、殘留歪斜最大 {worst:.2f}°"
        if skipped:
            msg += f"；{skipped} 頁本來就是正的且有文字層，原樣保留（文字不會變成圖片）"
        if overrides:
            msg += f"；{len(overrides)} 頁套用了你自己調的轉向 / 四個角"
        job.message = msg + "）"
    return run



def _parse_overrides(raw: str, page_count: int) -> dict:
    """解析逐頁覆寫：`{"3": {"rotate": 90, "quad": [[x,y] × 4]}, ...}`。

    **壞掉的項目個別丟掉，不要整批失敗** —— 這是使用者在畫面上調了半天的
    東西，其中一頁的資料有問題不該讓整份工作送不出去。頁碼是 1-based
    （畫面上看到的那個數字），超出範圍的直接忽略。
    """
    if not raw:
        return {}
    import json
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[int, dict] = {}
    for k, v in data.items():
        try:
            page = int(k)
        except (TypeError, ValueError):
            continue
        if not (1 <= page <= page_count) or not isinstance(v, dict):
            continue
        item: dict = {}
        rot = _clamp_rotate(v.get("rotate", 0))
        if rot:
            item["rotate"] = rot
        q = v.get("quad")
        if isinstance(q, list) and len(q) == 4 and all(
                isinstance(pt, (list, tuple)) and len(pt) == 2 for pt in q):
            try:
                item["quad"] = [[min(1.0, max(0.0, float(x))),
                                 min(1.0, max(0.0, float(y)))] for x, y in q]
            except (TypeError, ValueError):
                pass
        if item:
            out[page] = item
    return out

@router.post("/submit")
async def submit(request: Request, upload_id: str = Form(...),
                 dpi: int = Form(200), binarize: bool = Form(False),
                 detect_quad: bool = Form(True), out_name: str = Form(""),
                 overrides: str = Form("")):
    require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    src = _src_path(upload_id)
    if not src.exists():
        raise HTTPException(404, "檔案不存在（可能已過期）")
    with fitz.open(str(src)) as doc:
        page_count = doc.page_count
    stem = Path(out_name or "document").stem or "document"
    out = settings.temp_dir / f"{_PREFIX}out_{upload_id}.pdf"
    ov = _parse_overrides(overrides, page_count)
    job = job_manager.submit(
        "doc-straighten",
        _run_job(src, out, dpi=_clamp_dpi(dpi), binarize=binarize,
                 detect_quad=detect_quad, stem=stem, overrides=ov),
        request=request,
        meta={"filename": f"{stem}.pdf", "count": page_count})
    return {"job_id": job.id}


@router.post("/api/doc-straighten", include_in_schema=True)
async def api_doc_straighten(request: Request, file: UploadFile = File(...),
                             dpi: int = Form(200), binarize: bool = Form(False),
                             detect_quad: bool = Form(True)):
    """一次呼叫：上傳 → 拉正 → 直接回 PDF（同步，適合小檔）。"""
    info = await _stash(request, await file.read(), file.filename or "")
    upload_id = info["upload_id"]
    src = _src_path(upload_id)
    out = settings.temp_dir / f"{_PREFIX}out_{upload_id}.pdf"

    def _work():
        results = SC.straighten_pdf(src, out, dpi=_clamp_dpi(dpi),
                                    do_binarize=binarize,
                                    detect_quad=detect_quad)
        worst = max((abs(r.residual) for r in results), default=0.0)
        return results, worst

    results, worst = await _asyncio.to_thread(_work)
    # `FileResponse(filename=...)` 自己會處理 RFC 5987（中文檔名）——
    # `content_disposition()` 回的是**字串**，只在手動組標頭時才用得上。
    return FileResponse(
        str(out), media_type="application/pdf",
        filename=f"{info['name']}_straightened.pdf",
        headers={"X-Straighten-Pages": str(len(results)),
                 "X-Straighten-Worst-Residual": f"{worst:.2f}"})
