"""乘車證明整理工具路由 — 上傳解析 + buffer 管理 + 匯出。"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime
from typing import List

logger = logging.getLogger(__name__)

import fitz
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)

from ...core.http_utils import content_disposition
from . import buffer, exporter, parser, settings as user_settings

router = APIRouter()

_MAX_FILE_BYTES = 20 * 1024 * 1024   # 單檔 20MB
_MAX_FILES = 200                     # 一次最多 200 檔
_ENTRY_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def _request_user(request: Request):
    return getattr(request.state, "user", None)


def _extract_text(pdf_bytes: bytes) -> str:
    """抽 PDF 全文字。乘車證明是官方 PDF、文字層正常，直接串接各頁。"""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "transit_proof.html", {"request": request})


def _parse_uploads(files: List[UploadFile],
                   datas: List[bytes]) -> tuple[list, list, list]:
    """回 (parsed_entries, errors, blobs)。errors: [{file, error}]。

    `blobs` 與 `parsed` **逐項對齊**（第 i 筆解析結果來自 blobs[i] 那份 PDF），
    讓上層可以把原件跟 entry 一起存下來。用索引對齊而不是回去比對檔名 ——
    同一次上傳可能有同名檔案，靠檔名配對會配錯（本專案在「用索引對譯文」
    上吃過虧，這裡剛好相反：**在同一個迴圈裡同時 append 才不會錯位**）。
    """
    parsed, errors, blobs = [], [], []
    for f, data in zip(files, datas):
        fname = (f.filename or "upload.pdf")
        if not fname.lower().endswith(".pdf"):
            errors.append({"file": fname, "error": "只支援 PDF"})
            continue
        if not data:
            errors.append({"file": fname, "error": "空檔"})
            continue
        try:
            text = _extract_text(data)
        except Exception as e:  # noqa: BLE001
            errors.append({"file": fname, "error": f"讀取 PDF 失敗（{type(e).__name__}）"})
            continue
        try:
            entry = parser.parse_text(text)
        except parser.ParseError:
            errors.append({"file": fname,
                           "error": "無法辨識為乘車證明（格式不符或版面不支援）"})
            continue
        except Exception as e:  # noqa: BLE001
            errors.append({"file": fname, "error": f"解析失敗（{type(e).__name__}）"})
            continue
        entry["source_file"] = fname
        entry["note"] = ""
        parsed.append(entry)
        blobs.append(data)          # 同一個迴圈裡 append，保證對齊
    return parsed, errors, blobs


async def _read_datas(files: List[UploadFile]) -> List[bytes]:
    datas = []
    for f in files:
        data = await f.read()
        if data and len(data) > _MAX_FILE_BYTES:
            raise HTTPException(400, f"檔案過大（>20MB）：{f.filename}")
        datas.append(data or b"")
    return datas


@router.post("/upload")
async def upload(request: Request, files: List[UploadFile] = File(...)):
    """上傳一批乘車證明 PDF → 解析 → 存入 buffer。回新增 / 重複 / 失敗統計。"""
    if not files:
        raise HTTPException(400, "沒有檔案")
    if len(files) > _MAX_FILES:
        raise HTTPException(400, f"一次最多 {_MAX_FILES} 個檔案")
    datas = await _read_datas(files)
    parsed, errors, blobs = _parse_uploads(files, datas)
    user = _request_user(request)
    # 原始 PDF 一起交給 buffer —— 檔案與 entry 在同一個鎖裡建立，
    # 不會出現「清單有一筆但檔案不見」或反過來的孤兒檔。
    result = buffer.add_entries(user, parsed, blobs)
    return {
        "ok": True,
        "added": len(result["added"]),
        "duplicates": result["duplicates"],
        "cap_reached": result["cap_reached"],
        "failed": errors,
        "entries": buffer.list_entries(user),
    }


@router.get("/file/{entry_id}")
async def entry_file(entry_id: str, request: Request):
    """看某一筆的**原始乘車證明**。

    **歸屬由路徑結構決定**：檔案放在 `<data_dir>/transit_proof_files/<使用者
    雜湊>/`，而那個雜湊是從**當前登入者**算出來的，不吃任何請求參數 ——
    所以 A 拿著 B 的 entry_id 也只會在自己的目錄裡找不到而得到 404。
    這比事後檢查 ACL 難寫錯（本專案在 fail-open 的 ACL 上吃過虧）。

    以 `inline` 回傳讓瀏覽器直接開；型別鎖死 `application/pdf`。
    """
    if not _ENTRY_ID_RE.match(entry_id or ""):
        raise HTTPException(400, "invalid id")
    path = buffer.entry_file(_request_user(request), entry_id)
    if not path:
        raise HTTPException(404, "找不到這筆的原始證明（可能已被清理，或上傳當時沒有留存）")
    return FileResponse(
        str(path), media_type="application/pdf",
        headers={"Content-Disposition": content_disposition(
            f"乘車證明-{entry_id[:8]}.pdf", "inline")})


@router.get("/buffer")
async def get_buffer(request: Request):
    return {"ok": True, "entries": buffer.list_entries(_request_user(request))}


@router.post("/entry/{entry_id}")
async def edit_entry(entry_id: str, request: Request):
    if not _ENTRY_ID_RE.match(entry_id or ""):
        raise HTTPException(400, "invalid id")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    updated = buffer.update_entry(_request_user(request), entry_id, body or {})
    if not updated:
        raise HTTPException(404, "找不到該筆")
    return {"ok": True, "entry": updated}


@router.delete("/entry/{entry_id}")
async def remove_entry(entry_id: str, request: Request):
    if not _ENTRY_ID_RE.match(entry_id or ""):
        raise HTTPException(400, "invalid id")
    ok = buffer.delete_entry(_request_user(request), entry_id)
    if not ok:
        raise HTTPException(404, "找不到該筆")
    return {"ok": True}


@router.delete("/buffer")
async def clear_buffer(request: Request):
    n = buffer.clear_all(_request_user(request))
    return {"ok": True, "cleared": n}


@router.post("/buffer/delete-batch")
async def delete_batch(request: Request):
    """批次刪除選取的乘車證明。body {ids: [entry_id, ...]}。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    ids = (body or {}).get("ids")
    if not isinstance(ids, list):
        raise HTTPException(400, "ids 必須是陣列")
    ids = [i for i in ids if isinstance(i, str) and _ENTRY_ID_RE.match(i)]
    n = buffer.delete_entries(_request_user(request), ids)
    return {"ok": True, "deleted": n,
            "entries": buffer.list_entries(_request_user(request))}


@router.get("/settings")
async def get_settings_endpoint(request: Request):
    s = user_settings.get_settings(_request_user(request))
    return {"ok": True, "settings": s, "fields": user_settings.FIELD_DEFINITIONS}


@router.post("/settings")
async def save_settings_endpoint(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    s = user_settings.save_settings(_request_user(request), body or {})
    return {"ok": True, "settings": s}


@router.post("/export")
async def export(request: Request):
    """匯出 buffer — body {format}. 欄位 / 順序 / 格式取使用者 settings。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    fmt = body.get("format", "")
    if fmt not in ("csv", "xlsx", "ods", "json", "xml", "txt", "md"):
        raise HTTPException(400, "format 必須是 csv / xlsx / ods / json / xml / txt / md")
    user = _request_user(request)
    entries = buffer.list_entries(user)
    if not entries:
        raise HTTPException(400, "清單為空，無資料可匯出")
    s = user_settings.get_settings(user)
    try:
        data, mimetype, suffix = exporter.build_export(
            entries, s["visible_columns"], s["column_order"],
            s.get("field_formats") or {}, fmt,
            export_labels=s.get("export_labels") or {})
    except RuntimeError as e:
        logger.warning("transit-proof 匯出失敗: %s", e)
        raise HTTPException(500, "匯出失敗，請稍後再試")
    except ValueError as e:
        logger.info("transit-proof 匯出參數錯誤: %s", e)
        raise HTTPException(400, "匯出參數不正確")
    filename = f"transit-proof-{datetime.now().strftime('%Y%m%d-%H%M%S')}.{suffix}"
    return StreamingResponse(io.BytesIO(data), media_type=mimetype,
                             headers={"Content-Disposition": content_disposition(filename)})


# ---- 對外 API：單次上傳一批 PDF，直接回解析 JSON（不進 buffer）----
@router.post("/api/transit-proof", include_in_schema=True)
async def api_transit_proof(request: Request, files: List[UploadFile] = File(...)):
    """解析一批台鐵 / 高鐵乘車證明 PDF，回結構化 JSON（不寫入使用者清單）。"""
    if not files:
        raise HTTPException(400, "沒有檔案")
    if len(files) > _MAX_FILES:
        raise HTTPException(400, f"一次最多 {_MAX_FILES} 個檔案")
    datas = await _read_datas(files)
    parsed, errors, blobs = _parse_uploads(files, datas)
    return JSONResponse({"ok": True, "count": len(parsed),
                         "entries": parsed, "failed": errors})
