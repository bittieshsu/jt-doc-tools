"""會議摘要的「自己加替換」送回轉逐字稿那件作業（JTLW `variants`，`api_revision` 2.9）。

使用者在會議摘要裡加了「聽錯的寫法 → 正確寫法」（`Proksmox → Proxmox`），那只換得到
會議摘要自己那一份逐字稿。這份逐字稿如果是「會議錄音轉逐字稿」轉的、那件作業又還在
延後 ACK 的保留時間內（最多 24 小時，`jtlw_ack`），就可以把這幾條當成**已知的錯寫法**
送回去、請 JTLW 只重跑校正 —— 轉逐字稿那邊下載與存進工作區的那一份也一起改好
（JTLW v2.28 信裡建議的那條路）。

## 四件不可以放鬆的

1. **使用者送來的作業編號只是提示**：會議摘要的逐字稿是使用者上傳的檔案，裡面的
   `remote_job_id` 誰都寫得出來。只拿它查**我們自己的延後 ACK 清單**（`jtlw_ack`），
   找到那件轉逐字稿之後還要確認**是這個人送的**（`upload_owner.is_owner`，管理員也不例外 ——
   這是替別人的作業做事，不是讀檔），而且那件逐字稿記的編號對得上。
   對不上一律回同一句話（`gone`），不說「那件是別人的」—— 不讓人拿編號試探別人的作業。
2. **整份送**：JTLW 的 retry 是**取代**詞彙表，不是累加（對方 2026-10-02 確認）。
   只送新加的那幾條的話，轉逐字稿時寫的專有名詞與錯寫法在對方那邊就消失了。
   所以是「那件作業原本的整份 ＋ 這次的替換」。
3. **送之前照同一套規則先擋**：組成「錯寫法 → 正確寫法」的行，交給轉逐字稿那邊的
   `parse_glossary`（跟使用者在轉逐字稿頁打字是同一條路）—— 擋在這裡才講得出是哪一條；
   送出去才被退回的話，使用者只看到「重跑失敗」。
4. **一條都不可以安靜地掉**：箭頭、句號、`#` 開頭的寫法在那套解析裡會被當成會議背景
   （不送）。組完之後逐條確認每一條替換真的在送出去的清單上，不在就 400 講出是哪一條。
"""
from __future__ import annotations

import importlib
import json
import time
from typing import Optional

from fastapi import HTTPException, Request

from ...core import jtlw_ack, jtlw_client, jtlw_settings
from ...core import term_fix as tfx
from ...core import upload_owner as _uo
from ...logging_setup import get_logger

logger = get_logger(__name__)

TOOL_ID = "meeting-transcribe"

#: 不能送回去的原因 → 給使用者的話（前端 `tr()` 再翻，所以一律寫成常數）。
#: **每一句都講得出「那現在怎麼辦」** —— 這裡的替換照樣會用在分析上，只是轉逐字稿那邊不會跟著改。
REASONS = {
    "not_configured": "還沒設定語音服務（JTLW），沒辦法送回轉逐字稿。這裡的替換照樣會用在分析上。",
    "no_permission": "你沒有「會議錄音轉逐字稿」的使用權限，沒辦法送回那件作業。這裡的替換照樣會用在分析上。",
    "gone": ("轉逐字稿那件作業已經不能重跑校正了（JTLW 已經刪除它那份逐字稿，或那件不是你送的）。"
             "這裡的替換照樣會用在分析上。"),
    "no_correct": "轉逐字稿那件作業沒有校正這一步，沒有東西可以重跑。這裡的替換照樣會用在分析上。",
    "running": "轉逐字稿那件作業正在重跑校正，請等它做完再送。",
    "too_late": ("再過不到 15 分鐘就會請 JTLW 刪除它那份逐字稿，來不及重跑校正了。"
                 "這裡的替換照樣會用在分析上。"),
    "old_version": ("語音服務版本較舊，不收「聽錯的寫法」，沒辦法送回去。"
                    "這裡的替換照樣會用在分析上。"),
    "unreachable": "暫時連不上 JTLW，問不到它收不收「聽錯的寫法」—— 請稍後再試。",
}
#: 不是從轉逐字稿來的逐字稿（貼上的、字幕檔、自己的 JSON）
NOT_FROM_TRANSCRIBE = "這份逐字稿不是從「會議錄音轉逐字稿」送來的，沒有作業可以送回去。"
NO_ROWS = "請先在「自己加替換」加一條、並勾著，才有東西可以送回去。"
MESSAGES = (NOT_FROM_TRANSCRIBE, NO_ROWS) + tuple(REASONS.values())

#: `_retry_state` 的原因 → 這裡的原因（`cleared` 跟「找不到」講同一句）
_FROM_RETRY = {"running": "running", "no_correct": "no_correct",
               "cleared": "gone", "too_late": "too_late"}

#: 頁面問「收不收錯寫法」的快取（對方的 `/capabilities` 每次即時去問後端，要幾秒）
_REV_CACHE_S = 300.0
_rev_cache: dict[str, tuple[float, str]] = {}


def _mt():
    """轉逐字稿的 router **模組**（不是 `APIRouter` 物件 —— 套件的 `__init__` 把同名的
    子模組遮住了，`from . import router` 拿到的是 `APIRouter`）。"""
    return importlib.import_module(__name__.rsplit(".", 1)[0] + ".router")


def _may_use_transcribe(request: Request) -> bool:
    from ...core import auth_settings
    try:
        if not auth_settings.is_enabled():
            return True
    except Exception:  # noqa: BLE001 —— 讀不到設定時照「啟用」處理（fail-secure）
        pass
    uid = _uo.current_user_id(request)
    if uid is None:
        return False
    try:
        from ...core import permissions
        return bool(permissions.user_can_use_tool(uid, TOOL_ID))
    except Exception:  # noqa: BLE001
        logger.warning("查不到使用者 %s 的工具權限，當成沒有", uid, exc_info=True)
        return False


def _resolve(remote_job_id: object, request: Request) -> tuple[Optional[str], Optional[str], dict]:
    """→ `(不能送的原因或 None, 轉逐字稿的 upload_id, 那份逐字稿)`。不問 JTLW。"""
    if not jtlw_settings.is_configured():
        return "not_configured", None, {}
    if not _may_use_transcribe(request):
        return "no_permission", None, {}
    rid = str(remote_job_id or "")
    upload_id = jtlw_ack.upload_id_for(rid)
    # 不在延後 ACK 清單上（已經 ACK、從來沒延後、或根本沒有這件）與「不是你的」講同一句
    if not upload_id or not _uo.is_owner(upload_id, request):
        return "gone", None, {}
    mt = _mt()
    try:
        data = json.loads(mt._out_path(upload_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "gone", None, {}
    if not isinstance(data, dict) or str(data.get("remote_job_id") or "") != rid:
        return "gone", None, {}
    st = mt._retry_state(upload_id, data)
    if not st["possible"]:
        return _FROM_RETRY.get(st["reason"], "gone"), upload_id, data
    return None, upload_id, data


def _revision(*, cached: bool) -> Optional[str]:
    """對方的介面版本；**連不上、以前也沒讀到過**時回 None（跟「版本太舊」分開講）。"""
    mt = _mt()
    try:
        key = jtlw_settings.base_url()
    except Exception:  # noqa: BLE001
        return None
    now = time.monotonic()
    hit = _rev_cache.get(key)
    if cached and hit and now - hit[0] < _REV_CACHE_S:
        return hit[1]
    try:
        client = jtlw_client.JtlwClient(timeout=mt._PAGE_CAPS_TIMEOUT_S)
    except jtlw_client.JtlwError:
        return None
    rev = mt._api_revision(client)          # 問不到時沿用上一次讀到的；連那個都沒有回空字串
    if not rev:
        return None
    _rev_cache[key] = (now, rev)
    return rev


def _last(data: dict) -> Optional[dict]:
    """轉逐字稿那份**最近一次重跑校正**的結果（不論是從哪一頁送的）—— 畫面講「換了幾處」用。"""
    r = data.get("retry") if isinstance(data.get("retry"), dict) else None
    if not r:
        return None
    corr = ((data.get("summary") or {}).get("correction") or {}) \
        if isinstance(data.get("summary"), dict) else {}
    n = corr.get("variant_replacements") if isinstance(corr, dict) else None
    return {"at": r.get("at"), "count": r.get("count"),
            "variants_sent": bool(data.get("variants_sent")),
            "variant_replacements": n if isinstance(n, int) and not isinstance(n, bool) else None}


def state(remote_job_id: object, request: Request) -> dict:
    """會議摘要那一頁要不要畫「送回轉逐字稿」、能不能按、為什麼不能。**判斷全在這裡**。

    `available` 為 False ＝ 這份逐字稿不是從轉逐字稿來的（整塊不畫）。
    """
    if not remote_job_id:
        return {"available": False}
    reason, upload_id, data = _resolve(remote_job_id, request)
    out: dict = {"available": True, "possible": False}
    if data:
        out["last"] = _last(data)
    if reason is None:
        rev = _revision(cached=True)
        if rev is None:
            reason = "unreachable"
        elif not _mt()._variants_ok(rev):
            reason = "old_version"
    if reason:
        out["reason"] = reason
        out["message"] = REASONS[reason]
        if reason == "running" and upload_id:
            jid = _mt()._retrying_job(upload_id)
            if jid and jid != "pending":
                out["job_id"] = jid
        return out
    st = _mt()._retry_state(upload_id, data)
    out.update(possible=True, due_at=st.get("due_at"))
    return out


def _bad_piece(s: str) -> bool:
    mt = _mt()
    return bool(s.startswith("#") or mt._VARIANT_ARROW.search(s)
                or mt._SENTENCE_END.search(s) or any(not c.isprintable() for c in s))


def build_glossary(data: dict, rows: object, segments: list) -> tuple[list[str], dict]:
    """那件作業原本的整份專有名詞與錯寫法 ＋ 這次的替換 → `(terms, variants)`。

    錯寫法用**逐字稿裡實際出現的寫法**（`term_fix.find`，跟「自己加替換」同一套比對）——
    使用者打的是小寫、逐字稿裡是大寫時，送的是逐字稿裡的那一種。
    """
    mt = _mt()
    if not isinstance(rows, list):
        rows = []
    wanted: list[tuple[str, str, list[str]]] = []
    for r in rows[:tfx.MAX_PAIRS]:
        if not isinstance(r, dict):
            continue
        f, t = r.get("from"), r.get("to")
        if not isinstance(f, str) or not isinstance(t, str):
            continue
        f, t = f.strip(), t.strip()
        if not tfx.clean_pairs([{"from": f, "to": t}]):
            raise HTTPException(400, f"「{f[:40]} → {t[:40]}」不能送：原本的寫法至少 2 個字、"
                                     "兩邊不可以一樣，也不可以有控制字元")
        if _bad_piece(f) or mt._JTLW_SPLIT.search(f):
            raise HTTPException(400, f"「{f[:40]}」裡有分隔符號、箭頭或句號，"
                                     "不能當成一個聽錯的寫法送出")
        if _bad_piece(t):
            raise HTTPException(400, f"「{t[:40]}」裡有箭頭或句號，不能當成正確寫法送出")
        found = tfx.find(segments if isinstance(segments, list) else [], f)
        if not found.get("count"):
            raise HTTPException(400, f"整份逐字稿裡找不到「{f[:40]}」，不能送回去")
        wanted.append((f, t, [v for v in found.get("variants") or [] if isinstance(v, str)] or [f]))
    if not wanted:
        raise HTTPException(400, NO_ROWS)

    vmap = data.get("variants") if isinstance(data.get("variants"), dict) else {}
    entries: list[list] = []
    index: dict[str, int] = {}
    for term in data.get("terms") or []:
        if isinstance(term, str) and term.strip() and term.casefold() not in index:
            index[term.casefold()] = len(entries)
            ws = vmap.get(term)
            entries.append([term, [w for w in ws if isinstance(w, str)] if isinstance(ws, list) else []])
    for _f, t, ws in wanted:
        k = t.casefold()
        if k not in index:
            index[k] = len(entries)
            entries.append([t, []])
        entries[index[k]][1].extend(ws)
    lines = [("、".join(ws) + " → " + term) if ws else term for term, ws in entries]
    terms, variants = mt.parse_glossary(lines)     # 同一套規則；不合格的 400 講出是哪一行

    # 一條都不可以安靜地掉（模組說明第 4 點）
    got_terms = {x.casefold(): x for x in terms}
    for f, t, ws in wanted:
        term = got_terms.get(entries[index[t.casefold()]][0].casefold())
        sent = {w.casefold() for w in (variants.get(term) or [])} if term else set()
        if not term or not all(w.casefold() in sent for w in ws):
            raise HTTPException(400, f"「{f[:40]} → {t[:40]}」沒辦法照 JTLW 的規則送出")
    return terms, variants


def prepare(remote_job_id: object, request: Request, rows: object,
            segments: list) -> tuple[str, dict, list[str], dict]:
    """送出前的所有檢查（含問一次 JTLW 的版本，不用快取）→ `(upload_id, 逐字稿, terms, variants)`。

    有任何一關不過就丟 HTTPException：不是從轉逐字稿來的 409、不能重跑 409（講出原因）、
    替換寫得不對 400（講出是哪一條）。"""
    if not remote_job_id:
        raise HTTPException(409, NOT_FROM_TRANSCRIBE)
    reason, upload_id, data = _resolve(remote_job_id, request)
    if reason:
        raise HTTPException(409, REASONS[reason])
    terms, variants = build_glossary(data, rows, segments)
    rev = _revision(cached=False)
    if rev is None:
        raise HTTPException(503, REASONS["unreachable"])
    if not _mt()._variants_ok(rev):
        raise HTTPException(409, REASONS["old_version"])
    return upload_id, data, terms, variants


def submit(request: Request, upload_id: str, data: dict, terms: list[str],
           variants: dict) -> str:
    """送出「只重跑校正」—— 跟轉逐字稿頁的「重跑校正」是**同一支**（`submit_retry`）。"""
    return _mt().submit_retry(request, upload_id, data, terms, variants,
                              meta={"from": "meeting-summary"})
