"""辦公文件格式互轉。

同一個家族內任意格式互轉：文書檔↔文書檔、試算表↔試算表、簡報↔簡報。
可用的目標格式由 :mod:`app.core.office_formats` 從 soffice 自己的註冊表列出來
（見該模組開頭的說明：清單寫死會讓使用者選到永遠轉不出來的格式）。

## 為什麼要在伺服器端再擋一次家族

前端會依副檔名只顯示對應家族的選項，但那只是方便，**不是安全邊界**。
直接打 API 可以把 `.xlsx` 配上 `odt` 的目標送進來，soffice 會「成功」產出一份
內容全毀的檔案（試算表被當文書檔開）。所以這裡一定要自己比對一次。
"""
from __future__ import annotations

import time
import uuid
import zipfile
from pathlib import Path
from typing import List

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse

from ...config import settings
from ...core import office_formats, upload_owner
from ...core.job_manager import job_manager
from ...core.office_convert import convert_with_filter

router = APIRouter()

#: 單檔上限。轉檔是 soffice 開檔重存，記憶體大致與檔案大小同級。
_MAX_BYTES = 200 * 1024 * 1024


def _accept() -> str:
    return ",".join(office_formats.accepted_extensions())


def _resolve_target(target: str):
    """把 target_id 換成 (家族, 目標)，找不到就是 400。"""
    got = office_formats.resolve(target)
    if not got:
        raise HTTPException(400, "沒有這個目標格式。可用清單見 /tools/"
                                 "office-convert/formats")
    return got


async def _stage(files: List[UploadFile], fam, request: Request):
    """存檔到暫存目錄，並確認每一份都屬於這個家族。"""
    if not files:
        raise HTTPException(400, "沒有檔案")

    bid = uuid.uuid4().hex
    upload_owner.record(bid, request)
    bdir = settings.temp_dir / f"ofc_{bid}"
    bdir.mkdir(parents=True, exist_ok=True)

    saved: list[tuple[Path, str]] = []
    for i, f in enumerate(files):
        name = f.filename or ""
        ext = Path(name).suffix.lower().lstrip(".")
        if not ext:
            raise HTTPException(400, f"檔名沒有副檔名，無法判斷格式：{name}")
        src_fam = office_formats.family_for_ext(ext)
        if src_fam is None:
            raise HTTPException(400, f"不支援的檔案格式：.{ext}")
        if src_fam.id != fam.id:
            raise HTTPException(
                400,
                f"「{name}」是{src_fam.name}，不能轉成{fam.name}的格式。"
                f"同一批請只放同一類文件。")
        data = await f.read()
        if not data:
            raise HTTPException(400, f"空檔：{name}")
        if len(data) > _MAX_BYTES:
            raise HTTPException(
                400, f"「{name}」超過 {_MAX_BYTES // (1024 * 1024)} MB 上限")
        sp = bdir / f"{i:03d}_{Path(name).name}"
        sp.write_bytes(data)
        saved.append((sp, name))
    return bid, bdir, saved


def _make_runner(bdir: Path, saved: list, target):
    """組出背景作業要跑的函式。"""

    def run(job):
        outs: list[Path] = []
        used: set[str] = set()
        for fi, (sp, orig) in enumerate(saved):
            job.message = f"轉換 {orig}"
            job.progress = (fi / len(saved)) * 0.95

            stem = Path(orig).stem
            out_name = f"{stem}.{target.ext}"
            # 同一批裡可能有同名不同副檔名的檔（報表.odt / 報表.docx），
            # 轉成同一個目標之後會撞名，撞了就補序號。
            if out_name in used:
                out_name = f"{stem} ({fi + 1}).{target.ext}"
            used.add(out_name)

            op = bdir / "out" / out_name
            convert_with_filter(sp, op, target.ext, target.filter)
            outs.append(op)

        if len(outs) == 1:
            job.result_path = outs[0]
            job.result_filename = outs[0].name
        else:
            zname = f"converted_{time.strftime('%Y%m%d_%H%M%S')}.zip"
            zp = bdir / zname
            with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in outs:
                    zf.write(p, arcname=p.name)
            job.result_path = zp
            job.result_filename = zname
        job.progress = 1.0
        job.message = f"完成（{len(outs)} 份 → {target.label}）"

    return run


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "office_convert.html", {
        "request": request,
        "accept": _accept(),
        "families": office_formats.catalogue(),
    })


@router.get("/formats")
async def formats():
    """這台機器上可用的家族與目標格式。

    **target_id 會因安裝而異**（沒裝 Impress 就不會有簡報那一組），所以走
    API 的人應該先問這裡，不要把 id 寫死在自己的程式裡。
    """
    return {"families": office_formats.as_dict()}


@router.post("/submit")
async def submit(request: Request, target: str = Form(...),
                 file: List[UploadFile] = File(...)):
    fam, tgt = _resolve_target(target)
    _bid, bdir, saved = await _stage(file or [], fam, request)
    job = job_manager.submit("office-convert", _make_runner(bdir, saved, tgt),
                             meta={"count": len(saved), "target": tgt.label})
    return {"job_id": job.id}


@router.post("/convert", include_in_schema=True)
async def convert_api(request: Request, target: str = Form(...),
                      file: List[UploadFile] = File(...)):
    """對外 API：轉換辦公文件格式。"""
    fam, tgt = _resolve_target(target)
    _bid, bdir, saved = await _stage(file or [], fam, request)
    job = job_manager.submit("office-convert", _make_runner(bdir, saved, tgt),
                             meta={"count": len(saved), "target": tgt.label})
    return {"job_id": job.id, "download_url": f"/api/jobs/{job.id}/download"}
