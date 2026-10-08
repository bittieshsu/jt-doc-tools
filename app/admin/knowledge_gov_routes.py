"""管理區：知識庫 → 政府公開資料（`/admin/knowledge/gov`，Beta）。

邏輯全在 `app/core/kb/gov.py`；這裡只做 HTTP 那一層：

* **只限管理員**：由 `knowledge_routes.build_knowledge_router()` 掛進同一個 router
  （那個 router 自己掛 `require_admin`，併進 `/admin` 時再繼承一次）。
* **開頁面、查狀態、搜尋清單都不連外**；只有「下載資料」「檢查更新」會連出去，
  而且是背景作業（立刻回來，畫面輪詢狀態）。
* 會讀檔、解析的一律丟到執行緒（`asyncio.to_thread`）。
* 改設定寫 `settings_change`、下載 / 匯入寫 `kb_import` 的稽核紀錄。
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from ..core.kb import gov
from ..logging_setup import get_logger

logger = get_logger(__name__)

_AUDIT_TARGET = "knowledge_gov"


def _audit(request: Request, event: str, details: dict) -> None:
    from ..core import audit_db, client_ip
    user = getattr(request.state, "user", None) or {}
    try:
        audit_db.log_event(event, username=user.get("username", "") if isinstance(user, dict) else "",
                           ip=client_ip.real_client_ip(request), target=_AUDIT_TARGET,
                           details=details)
    except Exception:       # 稽核寫不進去不可以讓操作本身失敗
        logger.exception("knowledge gov audit failed")


def _actor(request: Request) -> str:
    from ..core.sessions import user_label
    user = getattr(request.state, "user", None)
    if not user or (isinstance(user, dict) and user.get("source") == "off"):
        return ""
    return user_label(user)


async def _run(fn, *args, **kw):
    try:
        return await asyncio.to_thread(fn, *args, **kw)
    except gov.NeedsConfirm:
        raise
    except gov.GovNotFound as e:
        raise HTTPException(404, str(e))
    except gov.GovBusy as e:
        raise HTTPException(409, str(e))
    except gov.GovError as e:
        raise HTTPException(400, str(e))


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "送來的資料格式不對。")
    if not isinstance(body, dict):
        raise HTTPException(400, "送來的資料格式不對。")
    return body


def _gid(gid: str) -> str:
    if gid not in gov.GROUPS:
        raise HTTPException(404, gov.MESSAGES["not_found"])
    return gid


def _pid(pid: str) -> str:
    if pid not in gov.PACKAGES:
        raise HTTPException(404, gov.MESSAGES["not_found"])
    return pid


def add_gov_routes(router: APIRouter, templates) -> None:
    """把政府公開資料的頁面與端點掛到知識庫的 router 上。"""

    @router.get("/knowledge/gov", response_class=HTMLResponse)
    async def kb_gov_page(request: Request):
        st = await _run(gov.status)
        return templates.TemplateResponse(request, "admin_knowledge_gov.html", {
            "request": request, "initial": st,
        })

    @router.get("/knowledge/api/gov/status")
    async def kb_gov_status():
        return await _run(gov.status)

    @router.get("/knowledge/api/gov/{gid}/search")
    async def kb_gov_search(gid: str, q: str = "", limit: int = 50, offset: int = 0,
                            browse: bool = False, level: str = "", keys_only: bool = False):
        # browse＝整份清單翻頁看；keys_only＝「全選 / 取消全選」要的整個範圍（見 gov.search）
        return await _run(gov.search, _gid(gid), q[:100], limit=limit, offset=offset,
                          browse=browse, level=level[:40], keys_only=keys_only)

    @router.get("/knowledge/api/gov/{gid}/selection")
    async def kb_gov_selection(gid: str):
        _gid(gid)

        def _do() -> dict:
            return {"items": gov.selected_items(gid), "is_default": gov.selection_is_default(gid)}
        return await _run(_do)

    @router.post("/knowledge/api/gov/{gid}/selection")
    async def kb_gov_selection_save(gid: str, request: Request):
        _gid(gid)
        body = await _body(request)
        keys = body.get("keys")
        confirm = body.get("confirm") is True
        try:
            out = await _run(gov.set_selection, gid, keys, confirm=confirm)
        except gov.NeedsConfirm as e:
            return JSONResponse(status_code=409, content={
                "detail": str(e), "need_confirm": True, "count": e.count, "chars": e.chars})
        _audit(request, "settings_change", {"action": "gov_selection", "group": gid,
                                            "count": len(out), "confirmed": confirm})
        return {"keys": out}

    @router.post("/knowledge/api/gov/{gid}/selection/reset")
    async def kb_gov_selection_reset(gid: str, request: Request):
        out = await _run(gov.reset_selection, _gid(gid))
        _audit(request, "settings_change", {"action": "gov_selection_reset", "group": gid})
        return {"keys": out}

    @router.post("/knowledge/api/gov/packages/{pid}")
    async def kb_gov_package_save(pid: str, request: Request):
        _pid(pid)
        body = await _body(request)
        fields = {k: body[k] for k in ("url", "alt_url") if k in body}
        p = await _run(gov.set_package_urls, pid, fields)
        _audit(request, "settings_change", {"action": "gov_package_url", "package": pid,
                                            "url": p["url"], "alt_url": p["alt_url"]})
        return p

    @router.post("/knowledge/api/gov/packages/{pid}/reset")
    async def kb_gov_package_reset(pid: str, request: Request):
        p = await _run(gov.reset_package, _pid(pid))
        _audit(request, "settings_change", {"action": "gov_package_reset", "package": pid})
        return p

    async def _start(request: Request, gid: str, action: str,
                     upload: Optional[tuple] = None) -> dict:
        job_id = await _run(gov.start, gid, action, request=request, actor=_actor(request),
                            upload=upload)
        details = {"action": action, "group": gid, "job_id": job_id}
        if upload:
            details.update(package=upload[0], size=len(upload[1]))
        _audit(request, "kb_import", details)
        return {"job_id": job_id, "started": True}

    @router.post("/knowledge/api/gov/{gid}/download")
    async def kb_gov_download(gid: str, request: Request):
        return await _start(request, _gid(gid), "download")

    @router.post("/knowledge/api/gov/{gid}/import")
    async def kb_gov_import(gid: str, request: Request):
        return await _start(request, _gid(gid), "import")

    @router.post("/knowledge/api/gov/{gid}/update")
    async def kb_gov_update(gid: str, request: Request):
        return await _start(request, _gid(gid), "update")

    @router.post("/knowledge/api/gov/packages/{pid}/upload")
    async def kb_gov_upload(pid: str, request: Request, file: UploadFile = File(...)):
        """不能連外的機關：自己把同一個檔案下載下來再上傳（**跟下載走同一套驗證**）。"""
        _pid(pid)
        limit = gov.PACKAGES[pid]["max_mb"] * 1024 * 1024
        data = await file.read(limit + 1)
        if not data:
            raise HTTPException(400, "檔案是空的")
        if len(data) > limit:
            raise HTTPException(413, f"檔案太大（上限 {gov.PACKAGES[pid]['max_mb']} MB）")
        name = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1][:120]
        return await _start(request, gov.PACKAGES[pid]["group"], "install",
                            upload=(pid, data, name))
