"""文件翻譯：整份辦公文件翻成另一種語言，**產出同格式、同版面的檔案**。

流程：上傳 → 抽出可翻譯的段落 → 背景作業逐段翻譯 → 寫回原檔 → 轉 PDF 出預覽。

翻譯本身重用「逐句翻譯」那一套（同一個 prompt、同樣的台灣用語對照表、同樣的
領域提示），差別只在**產出**：那邊給對照表，這邊把譯文寫回原檔。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from ...config import settings
from ...core import office_convert, office_text_map as otm, pdf_preview
from ...core import safe_paths as _sp, upload_owner as _uo
from ...core.http_utils import content_disposition
from ...core.job_manager import job_manager
from ...core.llm_settings import llm_settings
# 翻譯的邏輯與「逐句翻譯」共用一份 —— prompt、台灣用語對照表、領域提示、
# 「這段不用翻」的判斷都在那邊，複製一份一定會漂掉。
from ..translate_doc.router import (
    _build_prompt, _detect_language, _is_no_translate, _translate_one,
    _warmup_llm, _LANG_NAMES,
)

router = APIRouter()

#: 預覽幾頁（跟其他工具一致）
PREVIEW_PAGES = 6
#: 單一檔案可翻的段落上限 —— 再多就該拆檔，不然一份作業要跑好幾小時。
MAX_UNITS = 4000


def _src_path(upload_id: str) -> Path:
    return settings.temp_dir / f"dt_{upload_id}_src"


def _out_path(upload_id: str, ext: str) -> Path:
    return settings.temp_dir / f"dt_{upload_id}_out{ext}"


def _meta_path(upload_id: str) -> Path:
    return settings.temp_dir / f"dt_{upload_id}_meta.json"


def _read_meta(upload_id: str) -> dict:
    p = _meta_path(upload_id)
    if not p.exists():
        raise HTTPException(410, "上傳已過期，請重新上傳")
    return json.loads(p.read_text(encoding="utf-8"))


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "doc_translate.html", {
        "request": request,
        "llm_enabled": llm_settings.is_enabled(),
        "langs": _LANG_NAMES,
        "accept": ",".join(otm.SUPPORTED_EXTS),
        "preview_pages": PREVIEW_PAGES,
    })


@router.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    """收檔、抽出可翻譯的段落，回報段落數與預估。"""
    data = await file.read()
    if not data:
        raise HTTPException(400, "檔案是空的")
    name = file.filename or "document"
    ext = Path(name).suffix.lower()
    if not otm.is_supported(name):
        raise HTTPException(
            400,
            "只支援辦公文件（"
            + " / ".join(e.lstrip(".") for e in otm.SUPPORTED_EXTS)
            + "）。PDF 請用「逐句翻譯」——"
            "PDF 裡沒有段落，文字是定位好的碎片，換成長度不同的譯文版面一定跑掉。")

    upload_id = uuid.uuid4().hex
    _uo.record(upload_id, request)
    src = _src_path(upload_id)
    src.write_bytes(data)

    work_ext = otm.LEGACY_TO_MODERN.get(ext, ext)
    if work_ext != ext:
        # 舊的二進位格式改不了 XML —— 先轉成新格式，翻完再轉回去。
        work = settings.temp_dir / f"dt_{upload_id}_work{work_ext}"
        try:
            await asyncio.to_thread(
                office_convert.convert_with_filter, src, work,
                work_ext.lstrip("."), _filter_for(work_ext))
        except Exception:
            raise HTTPException(400, f"這份 {ext} 讀不進來（檔案可能毀損或不是真的 {ext}）")
        work_data = work.read_bytes()
    else:
        work_data = data

    try:
        units, _state = await asyncio.to_thread(otm.extract_units, work_data, work_ext)
    except Exception:
        raise HTTPException(400, "檔案讀不進來（可能毀損，或不是真正的辦公文件）")
    if not units:
        raise HTTPException(400, "這份文件裡找不到可以翻譯的文字")
    if len(units) > MAX_UNITS:
        raise HTTPException(
            400, f"段落太多（{len(units)}，上限 {MAX_UNITS}）—— 請先拆成幾份再翻。")

    _meta_path(upload_id).write_text(json.dumps({
        "filename": name, "ext": ext, "work_ext": work_ext,
        "units": len(units),
        "chars": sum(len(u.text) for u in units),
    }, ensure_ascii=False), encoding="utf-8")
    return {
        "upload_id": upload_id,
        "filename": name,
        "units": len(units),
        "chars": sum(len(u.text) for u in units),
        "sample": [u.text[:60] for u in units[:5]],
    }


def _filter_for(ext: str) -> str:
    return {
        ".docx": "MS Word 2007 XML",
        ".xlsx": "Calc MS Excel 2007 XML",
        ".pptx": "Impress MS PowerPoint 2007 XML",
        ".doc": "MS Word 97",
        ".xls": "MS Excel 97",
        ".ppt": "MS PowerPoint 97",
    }[ext]


@router.post("/start")
async def start(request: Request):
    if not llm_settings.is_enabled():
        raise HTTPException(503, "LLM 服務未啟用 —— 請先到「LLM 設定」啟用")
    body = await request.json()
    upload_id = str(body.get("upload_id") or "").strip()
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    meta = _read_meta(upload_id)
    source_lang = str(body.get("source_lang") or "auto")
    target_lang = str(body.get("target_lang") or "zh-TW")
    domain = str(body.get("domain") or "")[:200]

    def run(job) -> None:
        _run_job(job, upload_id, meta, source_lang, target_lang, domain)

    job = job_manager.submit(
        "doc-translate", run,
        meta={"filename": meta["filename"], "total": meta["units"],
              "target_lang": target_lang},
        request=request,
    )
    job.meta["view_url"] = f"/tools/doc-translate/?job={job.id}"
    return {"job_id": job.id, "total": meta["units"]}


def _run_job(job, upload_id: str, meta: dict, source_lang: str,
             target_lang: str, domain: str) -> None:
    from concurrent.futures import ThreadPoolExecutor

    client = llm_settings.make_client()
    if client is None:
        raise RuntimeError("LLM 服務未啟用")
    model = llm_settings.get_model_for("doc-translate")
    conf = llm_settings.get()
    concurrency = max(1, min(16, int(conf.get("translate_concurrency", 4))))

    ext, work_ext = meta["ext"], meta["work_ext"]
    src = _src_path(upload_id)
    work = (settings.temp_dir / f"dt_{upload_id}_work{work_ext}"
            if work_ext != ext else src)
    units, state = otm.extract_units(work.read_bytes(), work_ext)
    total = len(units)
    if source_lang == "auto":
        source_lang = _detect_language("\n".join(u.text for u in units[:50]))

    job.message = f"準備中…（共 {total} 段）"
    _warmup_llm(client, model)

    out: dict[int, str] = {}
    done = 0
    lock = threading.Lock()

    def one(i: int) -> None:
        nonlocal done
        text = units[i].text
        if _is_no_translate(text):
            translated = text
        else:
            try:
                translated = _translate_one(client, model, text,
                                            source_lang, target_lang, domain)
            except Exception:
                # 單段失敗就保留原文 —— 一段翻不出來不該讓整份作業掛掉，
                # 使用者拿到的檔案裡那一段還是看得懂的原文。
                translated = text
        with lock:
            out[i] = translated
            done += 1
            job.progress = 0.05 + 0.75 * (done / max(1, total))
            job.message = f"翻譯中… {done}/{total} 段"
        if job.cancelled:
            raise RuntimeError("已取消")

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(one, range(total)))

    job.message = "寫回檔案…"
    job.progress = 0.82
    new_bytes = otm.rebuild(state, out, units)
    result = _out_path(upload_id, work_ext)
    result.write_bytes(new_bytes)

    if work_ext != ext:
        # 來源是舊格式 → 轉回去，使用者拿到的副檔名跟他上傳的一樣
        final = _out_path(upload_id, ext)
        office_convert.convert_with_filter(result, final, ext.lstrip("."),
                                           _filter_for(ext))
        result = final

    job.message = "產生預覽…"
    job.progress = 0.9
    pages = _make_preview(upload_id, result)

    stem = Path(meta["filename"]).stem
    job.meta.update({
        "download_url": f"/tools/doc-translate/download/{upload_id}",
        "download_name": f"{stem}_{target_lang}{ext}",
        "preview_pages": pages,
        "translated": sum(1 for i, v in out.items() if v != units[i].text),
        "total": total,
    })
    job.message = f"完成（{total} 段）"
    job.progress = 1.0


def _make_preview(upload_id: str, result: Path) -> int:
    """把產出的檔案轉成 PDF，前幾頁存成 PNG。預覽失敗不影響下載。"""
    try:
        pdf = settings.temp_dir / f"dt_{upload_id}_preview.pdf"
        office_convert.convert_to_pdf(result, pdf)
        import fitz
        with fitz.open(pdf) as doc:
            n = min(PREVIEW_PAGES, doc.page_count)
        for i in range(n):
            png = settings.temp_dir / f"dt_{upload_id}_p{i + 1}.png"
            pdf_preview.render_page_png(pdf, png, page_index=i, dpi=90)
        return n
    except Exception:
        return 0


@router.get("/preview/{upload_id}/{page}")
async def preview(upload_id: str, page: int, request: Request):
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    if not (1 <= page <= PREVIEW_PAGES):
        raise HTTPException(400, "頁碼超出範圍")
    png = settings.temp_dir / f"dt_{upload_id}_p{page}.png"
    if not png.exists():
        raise HTTPException(404, "預覽不存在")
    return FileResponse(png, media_type="image/png")


@router.get("/download/{upload_id}")
async def download(upload_id: str, request: Request):
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    meta = _read_meta(upload_id)
    out = _out_path(upload_id, meta["ext"])
    if not out.exists():
        raise HTTPException(404, "還沒有產出（作業可能還沒跑完）")
    stem = Path(meta["filename"]).stem
    name = f"{stem}_translated{meta['ext']}"
    return FileResponse(out, filename=name,
                        headers={"Content-Disposition": content_disposition(name)})


@router.post("/api/doc-translate", include_in_schema=True)
async def api_doc_translate(request: Request, file: UploadFile = File(...),
                            target_lang: str = Form("zh-TW"),
                            source_lang: str = Form("auto"),
                            domain: str = Form("")):
    """對外 API：上傳辦公文件，直接回翻譯好的同格式檔案（同步，會等）。"""
    if not llm_settings.is_enabled():
        raise HTTPException(503, "LLM 服務未啟用")
    up = await upload(request, file)
    upload_id = up["upload_id"]
    meta = _read_meta(upload_id)

    class _J:            # 同步路徑沒有 job 物件，給一個最小替身
        progress = 0.0
        message = ""
        cancelled = False
        meta: dict = {}

    j = _J()
    j.meta = {}
    await asyncio.to_thread(_run_job, j, upload_id, meta,
                            source_lang, target_lang, domain)
    out = _out_path(upload_id, meta["ext"])
    stem = Path(meta["filename"]).stem
    name = f"{stem}_translated{meta['ext']}"
    return FileResponse(out, filename=name,
                        headers={"Content-Disposition": content_disposition(name)})
