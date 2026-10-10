"""知識庫的背景作業：匯入文件（抽字 → 切段 → 索引）、重建向量索引。

## 都走全站的作業佇列

`job_manager.submit(KB_JOB_ID, …)`：關掉分頁照樣跑完、「我的作業」看得到、
重啟之後標成「已中斷」。代號是「知識庫」（`job_labels.KB_JOB_ID`），**不是**公文撰擬
—— 原本借用公文撰擬的代號，通知信與「我的作業」就寫成「公文撰擬：知識庫匯入…」，
看起來像公文撰擬出了事；作業佇列也會把它標成 Office ＋ 外部服務（知識庫兩者都不用）。

## 同一時間只有一件在動索引

匯入與重建都拿 `_INDEX_LOCK`。兩件同時跑的話，重建把使用中的索引換掉的那一刻，
正在匯入的那一份還在用舊的設定嵌入 —— 它的向量會在切換時被當成舊索引刪掉，
那份文件就只剩關鍵字找得到，而畫面上看不出來。序列化之後就不會有這種交錯。

## 重建索引：完成而且驗證過才切換（規格 8.2、K08）

1. 用**目前的設定**嵌入一句話，拿到維度，算出新的 fingerprint。
2. 切段版本過舊的文件先重新切段（從原檔重新抽字）。
3. 每一段嵌入、寫進新 fingerprint 的向量（舊的那一份**完全不動**）。
4. 驗證：向量數 ＝ 段數、隨機抽一段重新嵌入要跟存進去的幾乎一樣（同一個模型、
   同一段文字，cosine 應該 ≈ 1；不是的話代表服務不穩定或中途換了模型）。
5. 全部通過才把設定檔的「使用中」換成新的（原子寫入），再刪舊的向量。

任何一步失敗：新 fingerprint 的半套向量刪掉，「使用中」照舊 —— 不會出現半套可用的狀態。
"""
from __future__ import annotations

import json
import random
import threading
import time
from typing import Callable, Optional

from ...logging_setup import get_logger
from ..job_labels import KB_JOB_ID
from . import chunker, embed, extract, law_text, store

logger = get_logger(__name__)

#: 作業代號（不是工具）—— 名稱與圖示在 `job_labels`
TOOL_ID = KB_JOB_ID

_INDEX_LOCK = threading.Lock()

#: 重建時驗證抽幾段重新嵌入。
VERIFY_SAMPLES = 3
#: 同一段重新嵌入，cosine 至少要這麼接近。
VERIFY_MIN_COS = 0.98


class RebuildError(RuntimeError):
    pass


def _chunk_texts(cli: embed.EmbedClient, rows: list[dict]) -> list[str]:
    return [cli.document_text(r["text"], r.get("heading") or "") for r in rows]


def _embed_rows(cli: embed.EmbedClient, fp: str, rows: list[dict],
                progress: Optional[Callable[[int, int], None]] = None,
                cancelled: Optional[Callable[[], bool]] = None) -> int:
    """把 `rows`（`id` / `text` / `heading`）嵌入並寫進 `fp`。回寫入筆數。"""
    import numpy as np
    done = 0
    step = max(1, cli.batch_size)
    for i in range(0, len(rows), step):
        if cancelled and cancelled():
            raise RebuildError("已取消。")
        batch = rows[i:i + step]
        m = cli.embed(_chunk_texts(cli, batch))
        store.put_vectors(fp, [(r["id"], np.ascontiguousarray(m[j], dtype=np.float32).tobytes())
                               for j, r in enumerate(batch)], int(m.shape[1]))
        done += len(batch)
        if progress:
            progress(done, len(rows))
    return done


# ---------------------------------------------------------------- 匯入一份
def process_version(version_id: str, *, job=None) -> dict:
    """抽字 → 切段 → 關鍵字索引 → 向量（有使用中的索引時）。結束時狀態是 ready 或 failed。"""
    with _INDEX_LOCK:
        return _process_locked(version_id, job=job)


def _set_job(job, progress: Optional[float] = None, message: Optional[str] = None) -> None:
    if job is None:
        return
    if progress is not None:
        job.progress = max(0.0, min(0.99, progress))
    if message is not None:
        job.message = message


def _process_locked(version_id: str, *, job=None, keep_status: bool = False) -> dict:
    try:
        v = store.get_version(version_id)
    except store.KBNotFound:
        return {"ok": False, "error": "gone"}
    final_status = v["status"] if keep_status else "ready"
    if not keep_status:
        store.set_status(version_id, "indexing", note="抽取文字…", warning="")
    try:
        data = store.stored_path(v).read_bytes()
        fmt = store.gov_format(version_id)
        if fmt in law_text.FORMATS:
            # 政府公開資料匯入的法規 / 行政規則：照它自己的結構一條一段（見 `law_text`）
            store.set_note(version_id, "切段…")
            try:
                chunks = law_text.chunks_for(fmt, data)
            except (ValueError, UnicodeDecodeError):
                raise extract.ExtractError("這份法規的原檔格式不對，請到「政府公開資料」重新匯入。") from None
            page_count, chars = 0, law_text.char_count(chunks)
        else:
            ex = extract.extract(data, v["ext"])
            store.set_note(version_id, "切段…")
            chunks = chunker.chunk(ex.lines)
            page_count, chars = ex.page_count, ex.chars
        if not chunks:
            raise extract.ExtractError("這份文件切不出任何段落。")
        ids = store.replace_chunks(version_id, chunks,
                                   chunker_version=law_text.chunker_version(fmt or None),
                                   page_count=page_count, chars=chars)
    except extract.ExtractError as e:
        if not keep_status:
            store.set_status(version_id, "failed", error=str(e))
        return {"ok": False, "error": str(e)}
    except store.KBNotFound:
        return {"ok": False, "error": "gone"}
    except Exception:
        logger.exception("知識庫：處理文件 %s 失敗", version_id)
        if not keep_status:
            store.set_status(version_id, "failed", error="處理時發生錯誤，詳細原因在服務記錄。")
        return {"ok": False, "error": "internal"}

    warning = ""
    snap = embed.active_snapshot()
    if snap is not None and not keep_status:
        store.set_note(version_id, "建立向量索引…")
        rows = [{"id": cid, "text": ch["text"], "heading": ch.get("heading", "")}
                for cid, ch in zip(ids, chunks)]
        try:
            cli = embed.EmbedClient.from_snapshot(snap)
            _embed_rows(cli, snap["fingerprint"], rows)
        except embed.EmbedError as e:
            logger.warning("知識庫：文件 %s 的向量沒有建起來：%s", version_id, e)
            warning = ("向量沒有建立（" + str(e).rstrip("。") + "）。這份文件目前只能用關鍵字找到；"
                       "嵌入服務恢復後按「重建索引」補上。")
    if not keep_status:
        store.set_status(version_id, final_status, warning=warning, note="")
    return {"ok": True, "chunks": len(chunks), "warning": warning}


def submit_import(version_ids: list[str], *, request=None) -> Optional[str]:
    """把剛上傳的幾份文件丟進背景處理。回作業編號。"""
    from ..job_manager import job_manager
    ids = [v for v in version_ids if store.is_id(v)]
    if not ids:
        return None

    def run(job) -> None:
        from . import retrieval
        with retrieval.bulk_update():      # 一份一份寫向量，查詢不必每份都整份重載
            _run_import(job, ids)

    def _run_import(job, ids) -> None:
        n = len(ids)
        failed = 0
        for i, vid in enumerate(ids):
            if job.cancelled:
                for rest in ids[i:]:
                    try:
                        if store.get_version(rest)["status"] in ("uploaded", "indexing"):
                            store.set_status(rest, "failed", error="匯入已取消。")
                    except store.KBNotFound:
                        pass
                return
            _set_job(job, i / n, f"處理第 {i + 1}/{n} 份…")
            res = process_version(vid, job=job)
            if not res.get("ok") and res.get("error") != "gone":
                failed += 1
        _set_job(job, 1.0, f"完成：{n - failed} 份可以啟用、{failed} 份失敗" if failed
                 else f"完成：{n} 份可以啟用")

    job = job_manager.submit(TOOL_ID, run,
                             meta={"filename": f"公文知識庫匯入（{len(ids)} 份）",
                                   "kb": "import", "view_url": "/admin/knowledge"},
                             request=request)
    # **只記作業編號、不動狀態** —— 作業一送出就可能已經在跑（甚至跑完），
    # 這時候再把狀態寫回 uploaded 會蓋掉 indexing / ready，畫面就永遠停在「排隊中」。
    for vid in ids:
        store.set_job_id(vid, job.id)
    return job.id


def submit_reprocess(version_id: str, *, request=None) -> Optional[str]:
    v = store.get_version(version_id)
    if v["status"] not in ("failed", "ready", "inactive"):
        raise store.KBError("啟用中的文件要先停用才能重新處理。")
    store.set_status(version_id, "uploaded", warning="")
    return submit_import([version_id], request=request)


# ---------------------------------------------------------------- 重建索引
def rebuild_state() -> dict:
    raw = store.meta_get("rebuild_state")
    if not raw:
        return {"running": False}
    try:
        d = json.loads(raw)
    except ValueError:
        return {"running": False}
    return d if isinstance(d, dict) else {"running": False}


def _save_state(**kw) -> None:
    st = rebuild_state()
    st.update(kw)
    store.meta_set("rebuild_state", json.dumps(st, ensure_ascii=False))


def rebuild(job=None, *, snapshot: Optional[dict] = None) -> dict:
    """重建向量索引（同步；背景作業裡呼叫）。成功回 `{"ok": True, …}`，失敗丟 `RebuildError`。"""
    from . import retrieval
    with _INDEX_LOCK, retrieval.bulk_update():
        return _rebuild_locked(job, snapshot=snapshot)


def _all_index_rows() -> list[dict]:
    rows = store.conn().execute(
        "SELECT k.id, k.text, k.heading FROM kb_chunks k JOIN kb_versions v ON v.id=k.version_id "
        "WHERE v.status IN ('ready','active','inactive') ORDER BY k.rid").fetchall()
    return [dict(r) for r in rows]


def _rebuild_locked(job=None, *, snapshot: Optional[dict] = None) -> dict:
    snap = snapshot or embed.configured_snapshot()
    if snap is None:
        raise RebuildError("還沒填好嵌入服務的位址與模型。")
    old = embed.active_snapshot()
    old_fp = old["fingerprint"] if old else None
    fp = None
    started = time.time()
    _save_state(running=True, started_at=started, done=0, total=0, error="",
                finished_at=None, model=snap.get("model", ""))
    cancelled = (lambda: bool(job and job.cancelled))
    try:
        cli = embed.EmbedClient.from_snapshot(snap)
        _set_job(job, 0.01, "測試嵌入服務…")
        dim = int(cli.embed([cli.query_text("測試")]).shape[1])
        fp = embed.fingerprint(snap, dim)

        # 切段版本過舊的先重新切段（換了切段規則之後的第一次重建）。
        # 政府公開資料的法規有自己的切法與版本（`law_text.chunker_version`），逐份比對。
        fmts = store.gov_formats()
        stale = [r for r in store.conn().execute(
            "SELECT id, chunker_version FROM kb_versions "
            "WHERE status IN ('ready','active','inactive')").fetchall()
            if r["chunker_version"] != law_text.chunker_version(fmts.get(r["id"]) or None)]
        for i, r in enumerate(stale):
            _set_job(job, 0.02, f"重新切段 {i + 1}/{len(stale)}…")
            res = _process_locked(r["id"], keep_status=True)
            if not res.get("ok") and res.get("error") != "gone":
                raise RebuildError("有文件重新切段失敗，索引沒有切換。")

        rows = _all_index_rows()
        _save_state(total=len(rows))

        def prog(done: int, total: int) -> None:
            _set_job(job, 0.05 + 0.9 * done / max(1, total), f"建立向量 {done}/{total}…")
            if done == total or done % (cli.batch_size * 5) < cli.batch_size:
                _save_state(done=done)

        _embed_rows(cli, fp, rows, progress=prog, cancelled=cancelled)

        # 中途有新文件匯入的話把它們也補上（匯入與重建拿同一把鎖，理論上不會有；
        # 這裡是第二道，避免切換之後有段落沒有向量）
        missing = store.conn().execute(
            "SELECT k.id, k.text, k.heading FROM kb_chunks k JOIN kb_versions v "
            "ON v.id=k.version_id WHERE v.status IN ('ready','active','inactive') "
            "AND NOT EXISTS (SELECT 1 FROM kb_vectors x WHERE x.chunk_id=k.id "
            "AND x.fingerprint=?)", (fp,)).fetchall()
        if missing:
            _embed_rows(cli, fp, [dict(r) for r in missing], cancelled=cancelled)
            rows = _all_index_rows()

        _set_job(job, 0.96, "驗證索引…")
        _verify(cli, fp, rows, dim)
        # 向量下限用的「無關問題」向量也在這裡先算好（不然第一次查詢要多等一次嵌入）
        from . import retrieval
        retrieval.null_vectors(cli, fp)
        embed.set_active(snap, dim=dim, fp=fp, chunks=len(rows))
        removed = store.delete_vectors_except(fp)
        store.delete_meta_prefix_except("null_qv:", "null_qv:" + fp)
        _save_state(running=False, done=len(rows), finished_at=time.time(), error="",
                    fingerprint=fp)
        logger.info("知識庫：向量索引重建完成（%d 段、維度 %d、fingerprint %s，清掉舊向量 %d 筆）",
                    len(rows), dim, fp, removed)
        _set_job(job, 1.0, f"完成：{len(rows)} 段")
        return {"ok": True, "chunks": len(rows), "dim": dim, "fingerprint": fp}
    except Exception as e:
        # 新的那一份半套向量丟掉；使用中的索引（舊的 fingerprint）完全沒動
        if fp and fp != old_fp:
            try:
                store.delete_vectors(fp)
                store.delete_meta_prefix_except("null_qv:" + fp, "")
            except Exception:
                logger.exception("知識庫：清除失敗的重建結果時出錯")
        msg = str(e) if isinstance(e, (RebuildError, embed.EmbedError)) else "重建時發生錯誤，詳細原因在服務記錄。"
        if not isinstance(e, (RebuildError, embed.EmbedError)):
            logger.exception("知識庫：重建向量索引失敗")
        _save_state(running=False, finished_at=time.time(), error=msg)
        raise RebuildError(msg) from e


def _verify(cli: embed.EmbedClient, fp: str, rows: list[dict], dim: int) -> None:
    import numpy as np
    n = store.count_vectors(fp)
    if n < len(rows):
        raise RebuildError(f"向量數（{n}）比段落數（{len(rows)}）少，索引沒有切換。")
    if not rows:
        return
    sample = random.sample(rows, min(VERIFY_SAMPLES, len(rows)))
    again = cli.embed(_chunk_texts(cli, sample))
    if again.shape[1] != dim:
        raise RebuildError("驗證時嵌入服務回的維度變了（中途換了模型？），索引沒有切換。")
    c = store.conn()
    for j, r in enumerate(sample):
        blob = c.execute("SELECT vec FROM kb_vectors WHERE chunk_id=? AND fingerprint=?",
                         (r["id"], fp)).fetchone()
        if not blob:
            raise RebuildError("驗證時找不到剛寫進去的向量，索引沒有切換。")
        stored = np.frombuffer(blob["vec"], dtype=np.float32)
        cos = float(stored @ again[j])
        if cos < VERIFY_MIN_COS:
            raise RebuildError("同一段文字兩次嵌入的結果差很多（服務不穩定或中途換了模型），索引沒有切換。")


def submit_rebuild(*, request=None) -> str:
    from ..job_manager import job_manager
    if embed.configured_snapshot() is None:
        raise store.KBError("還沒填好嵌入服務的位址與模型。")
    if rebuild_state().get("running"):
        raise store.KBError("已經有一次重建正在進行。")
    _save_state(running=True, started_at=time.time(), done=0, total=0, error="",
                finished_at=None)

    def run(job) -> None:
        try:
            rebuild(job)
        except RebuildError as e:
            job.status = "error"
            job.error = str(e)

    job = job_manager.submit(TOOL_ID, run,
                             meta={"filename": "公文知識庫重建索引", "kb": "rebuild",
                                   "view_url": "/admin/knowledge"},
                             request=request)
    return job.id
