"""管理區：知識庫（`/admin/knowledge`）。

資料集、文件上傳與啟用、擷取預覽、檢索測試、embedding 設定與重建索引。
儲存與檢索都在 `app/core/kb/`；這裡只是管理介面。

* **只限管理員**：掛在 `/admin` 底下（父 router 的 `require_admin` 會套到這裡），
  這支自己也再掛一次 —— 單獨被 include 到別處時不會變成沒有把關。
* 每一支都把讀寫 SQLite / 檔案 / 嵌入服務的工作丟到執行緒（`asyncio.to_thread`）
  —— 抽字、切段、嵌入都可能好幾秒，留在事件迴圈上會卡住整個網站。
* 錯誤訊息是固定的句子（`KBError`），不把例外字串原樣回給畫面。
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from ..core import audit_db
from ..core.kb import embed, extract, indexer, store
from ..core.kb import retrieval
from ..logging_setup import get_logger
from ..web.deps import require_admin

logger = get_logger(__name__)

#: 單一檔案上限。手冊類 PDF 多半幾 MB；100 MB 已經是幾千頁的掃描檔。
MAX_FILE_BYTES = 100 * 1024 * 1024
#: 一次最多上傳幾份。
MAX_FILES = 20


def _actor(request: Request) -> str:
    from ..core.sessions import user_label
    user = getattr(request.state, "user", None)
    if not user or (isinstance(user, dict) and user.get("source") == "off"):
        return ""
    return user_label(user)


def _uid(request: Request) -> Optional[int]:
    from ..core.upload_owner import current_user_id
    return current_user_id(request)


def _ip(request: Request) -> str:
    from ..core import client_ip
    return client_ip.real_client_ip(request)


def _audit(request: Request, event: str, target: str, details: dict) -> None:
    user = getattr(request.state, "user", None) or {}
    audit_db.log_event(event, username=user.get("username", "") if isinstance(user, dict) else "",
                       ip=_ip(request), target=target, details=details)


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "送來的資料格式不對。")
    if not isinstance(body, dict):
        raise HTTPException(400, "送來的資料格式不對。")
    return body


async def _run(fn, *args, **kw):
    """丟到執行緒跑；`KBError` 轉成 400 / 404（固定訊息）。"""
    try:
        return await asyncio.to_thread(fn, *args, **kw)
    except store.KBNotFound as e:
        raise HTTPException(404, str(e))
    except store.KBError as e:
        raise HTTPException(400, str(e))


def _group_names(ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    try:
        from ..core import auth_db
        import json as _j
        rows = auth_db.conn().execute(
            "SELECT id, name FROM groups WHERE id IN (SELECT value FROM json_each(?))",
            (_j.dumps(sorted(set(ids))),)).fetchall()
        return {r["id"]: r["name"] for r in rows}
    except Exception:
        return {}


def _overview() -> dict:
    from ..core import auth_settings
    from ..core import kb
    datasets = store.list_datasets_admin()
    names = _group_names([g for d in datasets for g in d["group_ids"]])
    for d in datasets:
        d["groups"] = [{"id": g, "name": names.get(g, f"#{g}")} for g in d["group_ids"]]
    return {
        "datasets": datasets,
        "status": kb.status(),
        "embedding": embed.get_public(),
        "rebuild": indexer.rebuild_state(),
        "categories": [{"id": k, "label": v[0], "purpose": v[1],
                        "purpose_label": store.PURPOSES[v[1]]} for k, v in store.CATEGORIES.items()],
        "auth_enabled": auth_settings.is_enabled(),
        "limits": {"max_file_mb": MAX_FILE_BYTES // (1024 * 1024), "max_files": MAX_FILES,
                   "exts": list(store.ALLOWED_EXTS)},
        "score_note": retrieval.SCORE_NOTE,
    }


#: 重建索引的狀態給畫面看的欄位（同 `kb.status()` 那一份）。
_REBUILD_KEYS = ("running", "done", "total", "error", "started_at", "finished_at")


def _embedding_panel() -> dict:
    """「LLM 設定」頁的 Embedding 那一區要的東西：設定（金鑰只說有沒有）、沿用的 LLM 伺服器、重建進度。

    **知識庫的資料庫還不存在時不去開它** —— 開了就會建出 `data/knowledge/kb.sqlite`，
    而管理員只是打開 LLM 設定頁、從來沒用過知識庫。
    """
    out = dict(embed.get_public())
    rb = indexer.rebuild_state() if store.db_path().exists() else {"running": False}
    out["rebuild"] = {k: rb.get(k) for k in _REBUILD_KEYS}
    return out


def _version_out(v: dict) -> dict:
    out = dict(v)
    out["sha256_short"] = (v.get("sha256") or "")[:12]
    return out


def _gov_summary() -> Optional[dict]:
    """「知識庫」頁的政府公開資料那張卡片：總共可以匯入幾項、已經下載並匯入幾項
    （2026-10-08 使用者：「這裡要顯示 總共可以匯入的有 XXXXX　目前已下載與匯入 XXX」）。

    **不連外** —— 讀的是管理員按「下載資料」時存下來的清單（`gov.status()`）。
    清單還沒下載的來源不知道有幾項，畫面上要講出來，不可以算成 0 項。
    讀不到就回 None（卡片照樣顯示「前往」按鈕，不讓整頁壞掉）。"""
    try:
        from ..core.kb import gov
        st = gov.status()
    except Exception as e:  # noqa: BLE001
        logger.warning("知識庫頁：讀不到政府公開資料的狀態（%s）", type(e).__name__)
        return None
    groups = [{"name": g["name"], "downloaded": bool(g["downloaded"]),
               "available": int(g["available"] or 0), "imported": int(g["imported_count"] or 0),
               "needs_update": int(g["needs_update_count"] or 0)} for g in st["groups"]]
    return {
        "groups": groups,
        "available": sum(g["available"] for g in groups),
        "imported": sum(g["imported"] for g in groups),
        "needs_update": sum(g["needs_update"] for g in groups),
        "not_downloaded": sum(1 for g in groups if not g["downloaded"]),
        "running": bool(st.get("running")),
    }


def build_knowledge_router(templates) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_admin)])

    @router.get("/knowledge", response_class=HTMLResponse)
    async def knowledge_page(request: Request):
        gov_sum = await asyncio.to_thread(_gov_summary)
        return templates.TemplateResponse(request, "admin_knowledge.html", {
            "request": request, "gov_sum": gov_sum,
            "categories": store.CATEGORIES, "purposes": store.PURPOSES,
            "accept": ",".join(store.ALLOWED_EXTS),
            "max_file_mb": MAX_FILE_BYTES // (1024 * 1024), "max_files": MAX_FILES,
        })

    @router.get("/knowledge/api/overview")
    async def kb_overview():
        return await _run(_overview)

    # ---------------------------------------------------------- 資料集
    @router.post("/knowledge/api/datasets")
    async def kb_dataset_create(request: Request):
        body = await _json(request)
        d = await _run(store.create_dataset, body, actor=_actor(request))
        _audit(request, "kb_change", d["id"], {"action": "dataset_create", "name": d["name"],
                                               "category": d["category"], "access": d["access"]})
        return d

    @router.post("/knowledge/api/datasets/{dataset_id}")
    async def kb_dataset_update(dataset_id: str, request: Request):
        body = await _json(request)
        d = await _run(store.update_dataset, dataset_id, body, actor=_actor(request))
        _audit(request, "kb_change", dataset_id, {"action": "dataset_update", "name": d["name"],
                                                  "access": d["access"], "groups": d["group_ids"],
                                                  "enabled": d["enabled"]})
        return d

    @router.post("/knowledge/api/datasets/{dataset_id}/delete")
    async def kb_dataset_delete(dataset_id: str, request: Request):
        n = await _run(store.delete_dataset, dataset_id)
        _audit(request, "kb_change", dataset_id, {"action": "dataset_delete", "documents": n})
        return {"ok": True, "deleted_documents": n}

    @router.get("/knowledge/api/datasets/{dataset_id}/versions")
    async def kb_versions(dataset_id: str):
        from ..core.kb import gov
        rows = await _run(store.list_versions, dataset_id)
        # 政府公開資料匯入的版本帶著出處（顯名文字）與施行日期說明
        origin = await _run(gov.gov_info_for_versions, dataset_id)
        out = []
        for v in rows:
            d = _version_out(v)
            if v["id"] in origin:
                d["gov"] = origin[v["id"]]
            out.append(d)
        return {"versions": out}

    @router.post("/knowledge/api/datasets/{dataset_id}/upload")
    async def kb_upload(dataset_id: str, request: Request,
                        files: list[UploadFile] = File(...),
                        version_label: str = Form(""), source_url: str = Form(""),
                        publisher: str = Form(""), published_on: str = Form(""),
                        effective_on: str = Form("")):
        if not store.is_id(dataset_id):
            raise HTTPException(404, "找不到這個資料集。")
        await _run(store.get_dataset, dataset_id)
        if not files:
            raise HTTPException(400, "沒有選擇檔案。")
        if len(files) > MAX_FILES:
            raise HTTPException(400, f"一次最多上傳 {MAX_FILES} 份。")
        meta = {"version_label": version_label, "source_url": source_url, "publisher": publisher,
                "published_on": published_on, "effective_on": effective_on}
        # 欄位先驗一次（不合格就整批不收，不要收了一半）
        await _run(lambda: (store.clean_url(source_url), store.clean_date(published_on, "發布日期"),
                            store.clean_date(effective_on, "生效日期")))
        created, skipped = [], []
        actor = _actor(request)
        for f in files:
            name = (f.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip() or "未命名"
            ext = Path(name).suffix.lower()
            if ext not in store.ALLOWED_EXTS:
                skipped.append({"filename": name, "reason": "不支援的檔案格式（只收 PDF、Word .docx、ODF .odt、純文字 .txt、Markdown .md）。"})
                continue
            data = await f.read(MAX_FILE_BYTES + 1)
            if not data:
                skipped.append({"filename": name, "reason": "檔案是空的。"})
                continue
            if len(data) > MAX_FILE_BYTES:
                skipped.append({"filename": name, "reason": f"檔案超過 {MAX_FILE_BYTES // (1024 * 1024)} MB 上限。"})
                continue
            ok = await asyncio.to_thread(extract.sniff_ok, data, ext)
            if not ok:
                skipped.append({"filename": name, "reason": "檔案內容跟副檔名對不上（或檔案已毀損）。"})
                continue
            sha = hashlib.sha256(data).hexdigest()
            dup = await asyncio.to_thread(store.find_duplicate, dataset_id, sha)
            if dup:
                skipped.append({"filename": name, "reason": "這個資料集裡已經有內容完全相同的文件，不重複建立。",
                                "duplicate_of": dup["id"], "duplicate_title": dup["title"]})
                continue
            title = re.sub(r"\s+", " ", Path(name).stem).strip()[:store.MAX_TITLE] or "未命名"
            try:
                v = await _run(store.create_version, dataset_id, title=title, filename=name[:255],
                               ext=ext, data=data, sha256=sha, meta=meta, actor=actor)
            except HTTPException as e:
                skipped.append({"filename": name, "reason": e.detail})
                continue
            created.append(v["id"])
        job_id = None
        if created:
            job_id = await asyncio.to_thread(indexer.submit_import, created, request=request)
            _audit(request, "kb_change", dataset_id, {"action": "upload", "documents": len(created),
                                                      "skipped": len(skipped)})
        return {"created": created, "skipped": skipped, "job_id": job_id}

    # ---------------------------------------------------------- 文件
    @router.post("/knowledge/api/versions/{version_id}/activate")
    async def kb_activate(version_id: str, request: Request):
        v = await _run(store.transition, version_id, allowed_from=("ready", "inactive"),
                       to="active", activated_by=_actor(request) or "admin")
        _audit(request, "kb_change", version_id, {"action": "activate", "title": v["title"]})
        return _version_out(v)

    @router.post("/knowledge/api/versions/{version_id}/deactivate")
    async def kb_deactivate(version_id: str, request: Request):
        v = await _run(store.transition, version_id, allowed_from=("active",), to="inactive")
        _audit(request, "kb_change", version_id, {"action": "deactivate", "title": v["title"]})
        return _version_out(v)

    @router.post("/knowledge/api/versions/{version_id}/delete")
    async def kb_version_delete(version_id: str, request: Request):
        v = await _run(store.get_version, version_id)
        if v["status"] in ("uploaded", "indexing"):
            raise HTTPException(400, "這份文件還在處理中，等處理完再刪除。")
        await _run(store.delete_version, version_id)
        _audit(request, "kb_change", version_id, {"action": "delete", "title": v["title"]})
        return {"ok": True}

    @router.post("/knowledge/api/versions/{version_id}/reprocess")
    async def kb_reprocess(version_id: str, request: Request):
        job_id = await _run(indexer.submit_reprocess, version_id, request=request)
        _audit(request, "kb_change", version_id, {"action": "reprocess"})
        return {"job_id": job_id}

    @router.post("/knowledge/api/versions/{version_id}/meta")
    async def kb_version_meta(version_id: str, request: Request):
        body = await _json(request)
        v = await _run(store.update_version_meta, version_id, body)
        _audit(request, "kb_change", version_id, {"action": "meta", "title": v["title"]})
        return _version_out(v)

    @router.get("/knowledge/api/versions/{version_id}/preview")
    async def kb_preview(version_id: str, offset: int = 0, limit: int = 10):
        v = await _run(store.get_version, version_id)
        chunks = await _run(store.list_chunks, version_id, limit=min(max(1, limit), 50),
                            offset=max(0, offset))
        return {"version": _version_out(v), "chunks": chunks}

    @router.get("/knowledge/api/versions/{version_id}/file")
    async def kb_file(version_id: str, request: Request):
        from ..core.http_utils import content_disposition
        info = await asyncio.to_thread(retrieval.open_original, version_id, user_id=_uid(request))
        if not info:
            raise HTTPException(404, "找不到這份文件。")
        return FileResponse(info["path"], media_type=info["media_type"],
                            headers={"Content-Disposition": content_disposition(info["filename"])})

    # ---------------------------------------------------------- 檢索測試
    @router.post("/knowledge/api/search")
    async def kb_search(request: Request):
        body = await _json(request)
        q = body.get("query")
        if not isinstance(q, str) or not q.strip():
            raise HTTPException(400, "請輸入問題。")
        ds = body.get("dataset_ids")
        if ds is not None and (not isinstance(ds, list) or not all(isinstance(x, str) for x in ds)):
            raise HTTPException(400, "資料集清單格式不對。")
        try:
            k = int(body.get("k") or 8)
        except (TypeError, ValueError):
            k = 8
        return await asyncio.to_thread(retrieval.search_detail, q, user_id=_uid(request),
                                       dataset_ids=ds or None, k=k, highlight=True)

    # ---------------------------------------------------------- embedding
    # 設定的畫面在「LLM 設定」頁（`/admin/llm-settings#embedding`）；端點留在這裡不搬 ——
    # 設定檔、稽核目標、設定備份的分類都屬於知識庫，搬網址只會讓書籤與既有呼叫壞掉。
    @router.get("/knowledge/api/embedding")
    async def kb_embedding_get():
        return await asyncio.to_thread(_embedding_panel)

    @router.post("/knowledge/api/embedding")
    async def kb_embedding_save(request: Request):
        body = await _json(request)
        allowed = {k: body[k] for k in ("kind", "base_url", "model", "use_llm_server",
                                        "batch_size", "timeout_seconds")
                   if k in body}
        key_in = body.get("api_key") if "api_key" in body else None
        if key_in is not None:
            allowed["key_input"] = key_in
        if allowed.get("use_llm_server"):
            # 沿用 LLM 伺服器：API 種類照對方是不是 Ollama（存檔時問一次記下來，見 embed 模組說明）；
            # 問不到就照畫面送來的 / 原本存的，不猜
            kind = await asyncio.to_thread(embed.detect_llm_kind)
            if kind:
                allowed["kind"] = kind
        try:
            out = await asyncio.to_thread(embed.save, allowed)
        except ValueError as e:
            raise HTTPException(400, _embed_error_text(e))
        detail = {k: allowed[k] for k in ("kind", "base_url", "model", "use_llm_server", "batch_size",
                                         "timeout_seconds") if k in allowed}
        if key_in is not None:
            # 金鑰本身絕不寫進紀錄，只記有沒有動到
            detail["key_status"] = "不變" if key_in == embed.SECRET_KEPT else "已更新"
        _audit(request, "settings_change", "knowledge", detail)
        return out

    @router.post("/knowledge/api/embedding/test")
    async def kb_embedding_test(request: Request):
        """測試連線：照畫面上（還沒存檔的）設定真的嵌入一句。

        金鑰欄位是替身字串時用存著的金鑰 —— **但只送給存檔的那個位址**：
        頁面看不到金鑰之後，「改位址再按測試」是唯一能把金鑰送到任意主機的路
        （LLM 設定頁 v1.16.17 記過同一件事）。位址不同就不帶金鑰。
        沿用 LLM 伺服器時位址與金鑰都是 LLM 設定裡那一份（送去的就是那台本來就在送的地方）。
        """
        body = await _json(request)

        def _do() -> dict:
            cfg = _embed_cfg_from_body(body)
            if not embed.is_complete(cfg):
                raise embed.EmbedError("請先在上面設定 LLM 伺服器，並選嵌入用的模型。"
                                       if cfg.get("use_llm_server") else "請先填嵌入服務的位址與模型。")
            return embed.probe(cfg)

        try:
            return await asyncio.to_thread(_do)
        except embed.EmbedError as e:
            return {"ok": False, "error": str(e)}

    @router.post("/knowledge/api/embedding/models")
    async def kb_embedding_models(request: Request):
        """「嵌入模型」下拉的清單：照畫面上的設定（沿用 LLM 伺服器、或另外指定的那一台）問對方有哪些模型。

        金鑰的規則跟測試連線同一份（`_embed_cfg_from_body`）；只讀清單，不會讓對方載入模型。
        """
        body = await _json(request)

        def _do() -> dict:
            cfg = _embed_cfg_from_body(body)
            if not str(cfg.get("base_url") or "").strip():
                raise embed.EmbedError("上面還沒有設定 LLM 伺服器。" if cfg.get("use_llm_server")
                                       else "請先填嵌入服務的位址。")
            return embed.list_models(cfg)

        try:
            return await asyncio.to_thread(_do)
        except embed.EmbedError as e:
            return {"ok": False, "error": str(e), "models": []}

    @router.post("/knowledge/api/rebuild")
    async def kb_rebuild(request: Request):
        job_id = await _run(indexer.submit_rebuild, request=request)
        _audit(request, "kb_change", "index", {"action": "rebuild"})
        return {"job_id": job_id}

    @router.post("/knowledge/api/vectors/disable")
    async def kb_vectors_disable(request: Request):
        await asyncio.to_thread(embed.clear_active)
        _audit(request, "kb_change", "index", {"action": "vectors_disable"})
        return {"ok": True}

    @router.get("/knowledge/api/groups")
    async def kb_groups(q: str = ""):
        from ..core import group_manager
        q = (q or "").strip()[:60]
        rows = await asyncio.to_thread(group_manager.list_group_names, q, 30)
        return {"groups": rows}

    # 政府公開資料（全國法規資料庫、國發會行政規則、行政院釋例）—— 同一個 router（同一道管理員閘）
    from .knowledge_gov_routes import add_gov_routes
    add_gov_routes(router, templates)

    return router


def _embed_cfg_from_body(body: dict) -> dict:
    """畫面送來的（還沒存檔的）embedding 設定 → 要連的那一份（`secret` 是明文金鑰，只在記憶體裡）。

    測試連線與列出模型共用：
    * 沿用 LLM 伺服器 → 位址與金鑰照 LLM 設定（公文撰擬指定的那台不見了就丟出原因，不退回全站那台）；
      API 種類問對方一次，問不到用畫面上的。
    * 另外指定 → 金鑰欄位是替身字串時**只有位址跟存檔的一樣才帶存著的金鑰**；
      存著的是沿用那台的金鑰時一律不帶。
    """
    cfg = dict(embed.DEFAULT_EMBED)
    cfg.update({k: body[k] for k in ("kind", "base_url", "model") if k in body})
    if "use_llm_server" in body:
        cfg["use_llm_server"] = bool(body["use_llm_server"])
    elif str(cfg.get("base_url") or "").strip():
        cfg["use_llm_server"] = False     # 只給位址＝另外指定那一台（同存檔的規則）
    if cfg["use_llm_server"]:
        src = embed.llm_source() or {}
        if src.get("problem"):
            raise embed.EmbedError(src["problem"])
        cfg["kind"] = embed.detect_llm_kind() or cfg.get("kind") or "ollama"
        return embed.resolved(cfg)
    saved = embed.configured_snapshot() or {}
    if saved.get("use_llm_server"):
        saved = {}                   # 存著的是沿用那台的金鑰：不帶去畫面上另外填的位址
    key = body.get("api_key", "")
    if key == embed.SECRET_KEPT:
        same = (str(saved.get("base_url") or "").strip()
                == str(cfg.get("base_url") or "").strip())
        key = saved.get("secret", "") if same else ""
    cfg["secret"] = key if isinstance(key, str) else ""
    return cfg


def _embed_error_text(e: Exception) -> str:
    """embed.save 丟的 ValueError：自己寫的驗證訊息原樣回，其他（url_safety 的英文訊息）換成固定句子。"""
    msg = str(e)
    if re.search(r"[一-鿿]", msg):
        return msg
    return "嵌入服務的位址不合格：只收 http:// 或 https:// 開頭、要有主機名稱、不可以是雲端中繼資料位址。"
