"""會議摘要的端點。

**分兩步是刻意的**：先上傳、看解析結果，再按「開始分析」。
一場三小時的會議要跑幾分鐘的 LLM —— 如果發言者判錯、或整份檔案根本沒讀對，
使用者應該在**花那幾分鐘之前**就看得出來。所以 `/upload` 會回一段預覽
（段落數、發言者、總長、前幾段長什麼樣），分析是另一個動作。
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response

from ...config import settings
from ...core import meeting_charts as mc
from ...core import meeting_insight as mi
from ...core import atomic_json
from ...core import meeting_context_memory as _mcm
from ...core import safe_paths as _sp, upload_owner as _uo
from ...core import term_fix as tfx
from ...core import transcript_parse as tp
from ...core.job_manager import job_manager
from ...core.llm_settings import llm_settings

from ...logging_setup import get_logger

logger = get_logger(__name__)
router = APIRouter()

TOOL_ID = "meeting-summary"

#: 預覽給幾段。**夠看出「發言者對不對、斷句對不對」就好** ——
#: 整份倒出來的話使用者不會看，而看不完的預覽等於沒有預覽。
PREVIEW_SEGMENTS = 8


def _seg_path(upload_id: str) -> Path:
    return settings.temp_dir / f"ms_{upload_id}_segments.json"


def _out_path(upload_id: str) -> Path:
    return settings.temp_dir / f"ms_{upload_id}_result.json"


def _meta_path(upload_id: str) -> Path:
    return settings.temp_dir / f"ms_{upload_id}_meta.json"


def _doc_themes() -> list[dict]:
    """匯出文件可以選的版面主題 —— 從「Markdown 轉辦公文件」讀
    （`{id, name, desc, swatch}`，同一份清單，不在這裡另寫一份）。"""
    try:
        from ..markdown_to_doc import themes as _th
        return _th.theme_options()
    except Exception:                            # noqa: BLE001
        return [{"id": "classic", "name": "清爽（預設）", "desc": "", "swatch": []}]


def _read_json(path: Path, what: str):
    if not path.exists():
        raise HTTPException(410, f"{what}已經過期或被清掉了，請重新上傳。")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise HTTPException(500, f"{what}讀不回來") from e


def _summarise(segments: list[dict]) -> dict:
    """解析結果的摘要 —— 給畫面用，也給 API 回傳。"""
    speakers = sorted({s["speaker"] for s in segments if s.get("speaker")})
    times = [s["end_ms"] for s in segments if s.get("end_ms") is not None]
    chars = sum(len(s["text"]) for s in segments)
    return {
        "segments": len(segments),
        "speakers": speakers,
        "chars": chars,
        # 沒有時間是正常的（純文字逐字稿）—— 這時候發言者佔比與時間軸不會出現，
        # 而不是畫一張空的圖。要讓使用者事先知道。
        "duration_ms": max(times) if times else None,
        "has_times": bool(times),
        "preview": segments[:PREVIEW_SEGMENTS],
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "meeting_summary.html", {
        "request": request,
        "llm_enabled": llm_settings.is_enabled(),
        "llm_model": llm_settings.get_model_for(TOOL_ID),
        "llm_url": (llm_settings.get() or {}).get("base_url", ""),
        "accept": ",".join(tp.SUPPORTED),
        "supported": tp.SUPPORTED,
        # **配色只有一份**：前端畫圖、伺服器畫匯出用的圖，兩邊用同一組顏色
        # 才不會「畫面上是藍的、下載的 PNG 是綠的」。照本專案既有的做法
        # 用 `data-*` 把伺服器端的清單送進 DOM（同 `workspace_extensions()`
        # → `data-ws-exts`），不要讓前端自己抄一份。
        "chart_palette": json.dumps(list(mc.PALETTE)),
        # 版面主題直接取「Markdown 轉辦公文件」那一份 —— **不要自己抄一份**，
        # 那邊加主題時這裡不會跟著加（而且沒有人會發現）。
        "doc_themes": _doc_themes(),
        "default_theme": DEFAULT_THEME,
        "chart_kinds": json.dumps(
            {k: {"colour": v[0], "label": v[1]} for k, v in mc.KIND_STYLE.items()},
            ensure_ascii=False),
    })


# ------------------------------------------------------------------ 匯入先前匯出的結果
#
# 2026-10-02 使用者問「匯出過的 .json 可以再傳回來在這工具裡呈現嗎」。
# 匯出的 JSON 帶著格式標記與**整份逐字稿**，傳回來時直接呈現（不再送模型），
# 引用照樣點得回原文。舊版匯出沒有逐字稿 —— 照樣讀得進來，只是引用沒有原文可以跳。

#: 匯出的 JSON 帶這個標記，傳回來時才認得出「這是分析結果」不是逐字稿
#: （逐字稿的 JSON 也可能有 `segments`，光看欄位分不出來）。
EXPORT_FORMAT = "jtdt-meeting-summary"
EXPORT_VERSION = 1
#: 收進來的結果只留這些欄位（`Analysis.to_public()` 加上我們自己存的）。
_IMPORT_KEYS = ("summary", "items", "chapters", "mindmap", "charts", "speaker_stats",
                "dropped_count", "context", "source", "llm_calls", "replacements")
_MAX_IMPORT_SEGMENTS = 50_000
_MAX_IMPORT_STR = 20_000
_IMPORT_BAD = "這份 JSON 不是本工具匯出的分析結果，或內容被改過，讀不進來。"


def _clean(v, depth: int = 0):
    """匯入檔是使用者傳上來的，**一律重建一份**：只留 JSON 的基本型別、字串限長、
    層數與筆數有上限，不原樣存回去。"""
    if depth > 6:
        return None
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v if abs(v) < 10 ** 15 else None
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, str):
        return v[:_MAX_IMPORT_STR]
    if isinstance(v, list):
        return [_clean(x, depth + 1) for x in v[:_MAX_IMPORT_SEGMENTS]]
    if isinstance(v, dict):
        return {str(k)[:200]: _clean(x, depth + 1) for k, x in list(v.items())[:5000]}
    return None


def _ints(v) -> list[int]:
    return ([x for x in v if isinstance(x, int) and not isinstance(x, bool)][:500]
            if isinstance(v, list) else [])


def _normalise_import(out: dict) -> None:
    """畫面與圖會**當成文字讀**的欄位一律要是字串、段號一律是整數 —— 不對的那一條整條丟掉，
    不留一個形狀怪異的東西存起來（試畫抓不到這一類：下游多半會把它轉成字串照畫，
    畫面上就出現 `[object Object]`）。"""
    sm = out["summary"]
    if not isinstance(sm.get("text"), str):
        sm["text"] = ""
    sm["unsupported"] = [x for x in sm.get("unsupported") or [] if isinstance(x, str)] \
        if isinstance(sm.get("unsupported"), list) else []
    for k, rows in out["items"].items():
        keep = []
        for it in rows:
            if not isinstance(it.get("text"), str):
                continue
            for f in ("owner", "due_text", "due", "speaker"):
                if f in it and not isinstance(it[f], str):
                    del it[f]
            if "segment_ids" in it:
                it["segment_ids"] = _ints(it["segment_ids"])
            keep.append(it)
        out["items"][k] = keep
    nums = ("start_ms", "end_ms", "duration_ms", "start_seq", "end_seq")
    chs = []
    for c in out["chapters"]:
        if not isinstance(c.get("title"), str):
            continue
        c["segment_ids"] = _ints(c.get("segment_ids"))
        for f in nums:
            if f in c and not (isinstance(c[f], (int, float)) and not isinstance(c[f], bool)):
                del c[f]
        chs.append(c)
    out["chapters"] = chs
    out["mindmap"] = [n for n in out["mindmap"]
                      if isinstance(n.get("label"), str) and isinstance(n.get("node_id"), str)
                      and (n.get("parent_id") is None or isinstance(n.get("parent_id"), str))
                      and isinstance(n.get("type", ""), str)]
    for n in out["mindmap"]:
        n["segment_ids"] = _ints(n.get("segment_ids"))
    out["speaker_stats"] = {
        str(k)[:100]: {f: v for f, v in st.items()
                       if isinstance(v, (int, float)) and not isinstance(v, bool)}
        for k, st in out["speaker_stats"].items()}
    if "replacements" in out:
        reps = tfx.normalise_applied(out.get("replacements"))
        if reps:
            out["replacements"] = reps
        else:
            out.pop("replacements", None)


def _import_segments(rows) -> list[dict]:
    """逐字稿照解析器的形狀重建：`{"seq", "text"}`，有的話再加發言者與時間。"""
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for i, r in enumerate(rows[:_MAX_IMPORT_SEGMENTS], 1):
        if not isinstance(r, dict):
            continue
        text = r.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        seq = r.get("seq")
        seg: dict = {"seq": seq if isinstance(seq, int) and not isinstance(seq, bool) else i,
                     "text": text[:_MAX_IMPORT_STR]}
        sp = r.get("speaker")
        if isinstance(sp, str) and sp.strip():
            seg["speaker"] = sp.strip()[:100]
        for k in ("start_ms", "end_ms"):
            v = r.get(k)
            if (isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v) and 0 <= v < 10 ** 12):
                seg[k] = int(v)
        # 依會議背景替換過的段落，原文跟著回來（再勾一次別的替換時要從原文重新套）
        orig = r.get("orig_text")
        if isinstance(orig, str) and orig.strip() and orig != seg["text"]:
            seg["orig_text"] = orig[:_MAX_IMPORT_STR]
        out.append(seg)
    return out


def _try_import_export(data: bytes, filename: str):
    """認得出是本工具匯出的結果就回 `(結果, 逐字稿)`；不是就回 `None`（照逐字稿解析）。

    **讀不進來的匯出檔回 400**，不可以退回當逐字稿解析 —— 那樣會把一份分析結果
    當成逐字稿送去分析，使用者只會看到一堆莫名其妙的段落。

    **看內容不看副檔名**（2026-10-03）：工作區只收 `.txt` / `.md` 兩種文字檔名，
    JSON 存進去就變成 `…-會議摘要.txt`（使用者離開頁面時作業完成、自動存進工作區的
    那一份就是這樣）。原本只認 `.json`，從工作區載回來會被當成逐字稿解析 ——
    跟 v1.16.40 轉逐字稿那份 `.txt` 同一個病。認得出來的條件（標記，或 `items` ＋
    `summary` 兩個字典）不變，逐字稿 JSON 沒有那兩個鍵，不會被誤認。
    """
    if not filename.lower().endswith((".json", ".txt", ".md")):
        return None
    if data.lstrip(b"\xef\xbb\xbf \t\r\n")[:1] != b"{":
        return None
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    marked = obj.get("format") == EXPORT_FORMAT
    if not marked and not (isinstance(obj.get("items"), dict)
                           and isinstance(obj.get("summary"), dict)):
        return None
    out = {k: _clean(obj[k]) for k in _IMPORT_KEYS if k in obj}
    if not isinstance(out.get("items"), dict) or not isinstance(out.get("summary"), dict):
        raise HTTPException(400, _IMPORT_BAD)
    out["items"] = {k: [it for it in out["items"][k] if isinstance(it, dict)]
                    for k in mi.KINDS if isinstance(out["items"].get(k), list)}
    for k in ("chapters", "mindmap"):
        v = out.get(k)
        out[k] = [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []
    st = out.get("speaker_stats")
    out["speaker_stats"] = ({k: v for k, v in st.items() if isinstance(v, dict)}
                            if isinstance(st, dict) else {})
    if not isinstance(out.get("context", ""), str):
        out.pop("context", None)
    if not isinstance(out.get("llm_calls", 0), int):
        out["llm_calls"] = 0
    _normalise_import(out)
    segs = _import_segments(obj.get("segments"))
    # **試畫一次**：標題、圖、Markdown 都讀得進去才收 —— 欄位的形狀被改過的檔案，
    # 寧可在上傳時就講清楚，也不要收下來之後頁面或匯出才壞掉。
    try:
        meeting_title(out)
        mc.build_all(out, segs or None)
        _md(out, charts=False, embed=False, segments=segs)
    except Exception:                       # noqa: BLE001 — 什麼原因都一樣：讀不進來
        logger.info("會議摘要匯入：檔案形狀不對（%s）", filename[:80].replace("\r", " ").replace("\n", " "))
        raise HTTPException(400, _IMPORT_BAD)
    return out, segs


def _transcript_context(data: bytes) -> str:
    """轉逐字稿送來的 JSON 裡的 `context`（使用者在「專有名詞或會議背景」寫的原文）。

    看內容不看副檔名 —— 經工作區中轉時檔名會變成 `.txt`（v1.16.40 / v1.16.42 同一個家族）。
    **只讀這一個欄位**，逐字稿怎麼解析仍然是 `transcript_parse` 的事（那支 JTLW 也在用，
    不為了這件事改它）。"""
    head = data.lstrip(b"\xef\xbb\xbf \t\r\n")[:1]
    if head != b"{":
        return ""
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return ""
    ctx = obj.get("context") if isinstance(obj, dict) else None
    if not isinstance(ctx, str):
        return ""
    lines = ["".join(ch for ch in ln if ch.isprintable()).rstrip() for ln in ctx.splitlines()]
    return "\n".join(lines).strip()[:mi.MAX_CONTEXT_CHARS]


@router.post("/upload")
async def upload(request: Request, file: UploadFile = File(...),
                 shape: str = Form("auto"), pasted: bool = Form(False)):
    """`pasted` ＝ 這份是從貼上框送來的，不是使用者的檔案。

    **不要靠檔名判斷**：貼上時前端塞的檔名會照介面語言翻（畫面上那個名字
    使用者看得到），拿它去比字串的話，英 / 日介面下比對永遠不成立，
    標題就變成「Pasted transcript 會議記錄」—— 而畫面上完全看不出哪裡錯了
    （本專案記過的「翻掉一個拿去比較的字串」那一類）。
    """
    data = await file.read()
    if not data:
        raise HTTPException(400, "檔案是空的")
    imported = _try_import_export(data, file.filename or "")
    if imported is not None:
        return _store_import(request, *imported, file.filename or "meeting.json")
    try:
        segments, shape_used = tp.parse(data, file.filename or "transcript.txt", shape)
    except tp.TranscriptError as e:
        # 使用者送錯東西是 400 —— 不是伺服器壞了
        raise HTTPException(400, str(e)) from e

    upload_id = uuid.uuid4().hex
    _uo.record(upload_id, request)
    # **一律走原子寫入** —— 直接覆寫時寫到一半被中斷會留下截斷檔，
    # 而讀取端「剖析失敗就回預設值」，那份逐字稿就安靜地不見了。
    atomic_json.write_json(_seg_path(upload_id), segments)
    info = _summarise(segments)
    # **這份逐字稿上一次分析時填的會議背景**（2026-10-03 使用者要求）—— 前端預先填進框裡，
    # 使用者可以改、可以清掉。怎麼認「同一份」寫在 `meeting_context_memory` 的說明。
    remembered = _mcm.recall(_uo.current_user_id(request), _mcm.fingerprint(segments))
    if remembered:
        info["remembered_context"] = remembered
    # **轉逐字稿時填的「專有名詞或會議背景」**（2026-10-03 使用者要求）—— 轉送過來的逐字稿
    # JSON 帶著它。上一次分析這份逐字稿時填過背景的話，畫面以那一份為準（那是使用者在
    # 會議摘要這邊最後用的）；這一份只在框是空的、也沒有記住的背景時才帶進去。
    tctx = _transcript_context(data)
    if tctx:
        info["transcript_context"] = tctx
    # **告訴使用者用了哪一種排法**，並附上可以改的清單 ——
    # 自動判斷錯的時候要有路可走（使用者 2026-09-18 要求）。
    info["shape"] = shape_used
    info["shapes"] = tp.SHAPES
    atomic_json.write_json(_meta_path(upload_id), {
        "filename": file.filename or "transcript",
        "pasted": bool(pasted),
        # 用了哪一種排法 —— 之後從「我的作業」打開時，解析區要講得出來
        "shape": shape_used,
        "segments": info["segments"], "speakers": info["speakers"],
        "duration_ms": info["duration_ms"],
        # **「發言時間」是量到的還是推估的**，畫面上要講出來。
        # 字幕檔（.vtt / .srt）與 JSON 每一段都帶著自己的結束時間 —— 那是量到的。
        # 純文字逐字稿只有「誰在幾點幾分開始講」，結束時間是拿**下一段的開始**
        # 補上去的（見 `transcript_parse.parse_plain`），所以算出來的發言時間
        # 把中間的停頓也算了進去。
        # 不講的話，那一欄看起來就跟量到的一樣 —— 介面承諾了沒做到的事。
        "times_are_measured": shape_used in _MEASURED_SHAPES})
    return {"upload_id": upload_id} | info


def _store_import(request: Request, out: dict, segs: list[dict], filename: str) -> dict:
    """把匯入的結果存成一份新的（跟分析完成時同樣的三個檔），回傳給頁面的解析摘要。"""
    upload_id = uuid.uuid4().hex
    _uo.record(upload_id, request)
    src = out.get("source") if isinstance(out.get("source"), dict) else {}
    info = _summarise(segs)
    meta = {"filename": str(src.get("filename") or filename)[:255],
            "pasted": bool(src.get("pasted")), "shape": None,
            "segments": info["segments"], "speakers": info["speakers"],
            "duration_ms": info["duration_ms"],
            "times_are_measured": bool(src.get("times_are_measured")),
            "imported": True,
            "replacements": out.get("replacements") or []}
    out["source"] = meta
    atomic_json.write_json(_seg_path(upload_id), segs)
    atomic_json.write_json(_meta_path(upload_id), meta)
    atomic_json.write_json(_out_path(upload_id), out)
    return ({"upload_id": upload_id, "imported": True, "has_transcript": bool(segs),
             "shapes": tp.SHAPES, "shape": None} | info)


#: 每一段都帶著自己的結束時間的來源 —— 那些的發言時間是**量到的**。
#: 其餘（純文字的三種排法）只有開始時間，結束時間是推估的。
_MEASURED_SHAPES = frozenset({"cues", "json"})


def _ask_for(job) -> callable:
    """做一支送給 `meeting_insight` 的 `ask`。

    **取消要真的停下來**：`meeting_insight` 是一個從頭跑到尾的呼叫，
    它不知道作業被取消了 —— 但它每一次都會回來問我們。所以判斷放在這裡，
    被取消就丟例外把整條管線中斷掉。
    """
    client = llm_settings.make_client()
    if client is None:
        raise RuntimeError("LLM 服務未啟用")
    model = llm_settings.get_model_for(TOOL_ID)

    def ask(prompt: str) -> str:
        if getattr(job, "cancelled", False):
            raise RuntimeError("已取消")
        return client.text_query(prompt, model=model, think=False)

    return ask


def _write_full_export(upload_id: str) -> Path:
    """寫出「完整的」分析結果檔：格式標記 ＋ 結果 ＋ 整份逐字稿。

    **作業的結果檔就是它**（2026-10-03 使用者要求）：「我的作業」的下載、以及分析完成時
    人已經離開而自動存進工作區的那一份，原本是不含逐字稿的內部結果 —— 傳回來打得開，
    但引用點不到原文。跟「下載 JSON」同一支產生，三條路拿到的是同一份東西。
    改了發言者名字之後要重寫（不然「我的作業」下載到的還是舊名字）。"""
    out = _read_json(_out_path(upload_id), "分析結果")
    try:
        segs = json.loads(_seg_path(upload_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        segs = None
    exp = ({"format": EXPORT_FORMAT, "format_version": EXPORT_VERSION} | out
           | {"segments": segs or []})
    path = settings.temp_dir / f"ms_{upload_id}_export.json"
    atomic_json.write_json(path, exp)
    return path


def _run_job(job, upload_id: str, context: str = "",
             with_impacts: bool = True) -> None:
    segments = json.loads(_seg_path(upload_id).read_text(encoding="utf-8"))
    ask = _ask_for(job)

    # 進度要說得出**在做什麼** —— 一場三小時的會議跑好幾分鐘，
    # 只有百分比的話使用者不知道是在跑還是卡住（本專案記過很多次）。
    # 百分比由 `full_analysis` 依「還要呼叫幾次模型」算 —— 完成之前不會到 100%。
    def on_progress(frac: float, message: str) -> None:
        job.progress = round(frac, 3)
        job.message = message

    job.message = mi.STAGES[0]
    analysis = mi.full_analysis(segments, ask, context=context,
                                with_impacts=with_impacts, on_progress=on_progress)

    out = analysis.to_public()
    # **背景要跟著結果存下來** —— 匯出時的文件標題會從它取主題，
    # 而那時候已經沒有那個請求了（背景是送出分析時帶進來的）。
    if context:
        out["context"] = context
    src = json.loads(_meta_path(upload_id).read_text(encoding="utf-8"))
    # 依會議背景替換了哪些寫法 —— 跟著結果走（畫面與匯出要講出來），不放在來源資訊裡
    reps = src.pop("replacements", None)
    if reps:
        out["replacements"] = reps
    out["source"] = src
    out["llm_calls"] = analysis.calls
    atomic_json.write_json(_out_path(upload_id), out)
    path = _write_full_export(upload_id)

    # **`result_path` 一定要設** —— 沒設的話「我的作業」顯示已完成卻沒有下載鈕，
    # 自動存入工作區與保留期清理也都不認得那份產出（文件翻譯踩過）。
    # **要放 `Path` 不是字串** —— `Job.to_public()` 會對它呼叫 `.exists()`，
    # 放字串的話 `/api/jobs/{id}` 每次都 500，而**背景作業本身完全正常**
    # （結果檔真的產出來了），症狀只是進度輪詢壞掉、畫面停在跑一半。
    job.result_path = path
    job.result_filename = f"{Path(out['source']['filename']).stem}-會議摘要.json"
    job.meta["upload_id"] = upload_id
    job.meta["counts"] = {k: len(v) for k, v in out["items"].items()}
    job.progress = 1.0
    job.message = "完成"


@router.post("/start")
async def start(request: Request):
    if not llm_settings.is_enabled():
        raise HTTPException(503, "LLM 服務未啟用 —— 請先到「LLM 設定」啟用")
    body = await request.json()
    upload_id = str(body.get("upload_id") or "").strip()
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    meta = _read_json(_meta_path(upload_id), "逐字稿")
    # 背景資料（選填）：主題、與會者職稱、專有名詞說明，或使用者自己的交代。
    # **只用來讀懂逐字稿，不會變成項目的來源** —— 理由見
    # `meeting_insight.build_context_block` 的說明。
    context = str(body.get("context") or "")[:mi.MAX_CONTEXT_CHARS]
    # 「事件與影響」自己走一輪 —— **抽取的呼叫數會翻倍**（160 分鐘的會議
    # 29 → 58 次、147 → 303 秒）。預設開著，但要給得起關掉的路：
    # 規劃類的會議沒有事故可抓，多花的時間換不到東西。
    with_impacts = str(body.get("with_impacts", "1")) not in ("0", "false", "")
    # 使用者勾選的「建議替換」（依會議背景找出的錯字）—— **每次都從原文重新套**：
    # 上一次換過、這次沒勾的會換回原文。換過的段落把原文留在 `orig_text`。
    await asyncio.to_thread(_apply_replacements, upload_id, body.get("replacements"),
                            body.get("manual_replacements"))
    # 記下這次用的背景，下次同一份逐字稿進來時帶回框裡；**空的就是清掉**
    # （使用者把預先填好的刪了再分析，代表他不要那一段了）
    owner = _uo.current_user_id(request)

    def _remember() -> None:
        segs = _read_json(_seg_path(upload_id), "逐字稿")
        _mcm.remember(owner, _mcm.fingerprint(segs if isinstance(segs, list) else []), context)

    await asyncio.to_thread(_remember)

    def run(job) -> None:
        _run_job(job, upload_id, context, with_impacts)

    job = job_manager.submit(
        TOOL_ID, run,
        meta={"filename": meta["filename"], "segments": meta["segments"]},
        request=request,
    )
    job.meta["view_url"] = f"/tools/{TOOL_ID}/?job={job.id}"
    return {"job_id": job.id}


def _apply_replacements(upload_id: str, pairs: object, manual: object = None) -> list[dict]:
    segs = _read_json(_seg_path(upload_id), "逐字稿")
    new, applied = tfx.apply(segs if isinstance(segs, list) else [], pairs)
    if new != segs:
        atomic_json.write_json(_seg_path(upload_id), new)
    meta_p = _meta_path(upload_id)
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    # 「自己加替換」那幾列（使用者打的寫法，不是展開後的每一種大小寫）—— 從「我的作業」
    # 重新打開時畫回清單裡；不記的話再按一次分析，那幾個字會被換回原文
    rows = tfx.clean_rows(manual)
    if (meta.get("replacements") or []) != applied or (meta.get("manual_replacements") or []) != rows:
        meta["replacements"] = applied
        meta["manual_replacements"] = rows
        atomic_json.write_json(meta_p, meta)
    return applied


@router.post("/suggest-terms")
async def suggest_terms(request: Request):
    """依會議背景找出逐字稿裡可能寫錯的專有名詞（「建議替換」）。**只建議、不改** ——
    使用者勾選後由 `/start` 套用。比對的是替換前的原文，判斷說明在 `app/core/term_fix.py`。
    另附 `applied`：上一次分析時實際換了哪些（重新打開時畫面預先勾回來）。"""
    body = await request.json()
    upload_id = str(body.get("upload_id") or "").strip()
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    context = str(body.get("context") or "")[:mi.MAX_CONTEXT_CHARS]
    segs = _read_json(_seg_path(upload_id), "逐字稿")
    res = await asyncio.to_thread(tfx.suggest, segs if isinstance(segs, list) else [], context)
    try:
        meta = json.loads(_meta_path(upload_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    res["applied"] = tfx.normalise_applied(meta.get("replacements")) or []
    return res


@router.post("/find-term")
async def find_term(request: Request):
    """「自己加替換」：這個寫法在整份逐字稿（替換前的原文）出現幾處、第一處的上下文、實際的寫法。

    **只查不改** —— 使用者按「開始分析」時才跟勾選的建議一起套用（`/start` 的 `replacements`）。"""
    body = await request.json()
    upload_id = str(body.get("upload_id") or "").strip()
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    segs = _read_json(_seg_path(upload_id), "逐字稿")
    return await asyncio.to_thread(tfx.find, segs if isinstance(segs, list) else [],
                                   body.get("from"))


@router.post("/forget-context")
async def forget_context(request: Request):
    """清掉「這份逐字稿上一次分析時填的會議背景」—— 背景框旁的「清除」。

    **只收這次上傳的編號**（照樣驗歸屬），由伺服器端從那份逐字稿算指紋：
    收指紋的話，任何人送一個指紋就能試探別人分析過什麼。"""
    body = await request.json()
    upload_id = str(body.get("upload_id") or "").strip()
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    owner = _uo.current_user_id(request)

    def _forget() -> bool:
        segs = _read_json(_seg_path(upload_id), "逐字稿")
        return _mcm.forget(owner, _mcm.fingerprint(segs if isinstance(segs, list) else []))

    return {"ok": True, "forgot": await asyncio.to_thread(_forget)}


@router.get("/result/{upload_id}")
async def result(upload_id: str, request: Request):
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    out = _read_json(_out_path(upload_id), "分析結果")
    # 文件標題（匯出的 .html 也用它）—— **跟其他匯出格式同一個來源**，
    # 不在前端另外猜一份（只回應用，不寫回檔案）
    if isinstance(out, dict):
        out["title"] = meeting_title(out)
    return out


@router.get("/segments/{upload_id}")
async def segments(upload_id: str, request: Request):
    """整份逐字稿 —— 結果頁要靠它把引用的段號還原成原文。

    另附 `info`：跟上傳當下回的那一份**同一支**（`_summarise`）算出來的解析摘要。
    從「我的作業」打開時頁面手上沒有上傳的回應，要靠它把解析區（連同「開始分析」）
    畫回來（2026-10-02 使用者回報「分析那個按鈕怎麼不見了」）—— 不在前端另算一份。
    """
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    segs = _read_json(_seg_path(upload_id), "逐字稿")
    try:
        meta = json.loads(_meta_path(upload_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    info = _summarise(segs) | {"upload_id": upload_id, "shapes": tp.SHAPES,
                               "shape": meta.get("shape"),
                               "replacements": tfx.normalise_applied(
                                   meta.get("replacements")) or [],
                               "manual_replacements": tfx.clean_rows(
                                   meta.get("manual_replacements"))}
    return {"segments": segs, "info": info}


@router.post("/speakers/{upload_id}")
async def rename_speakers(upload_id: str, request: Request):
    """把發言者代號改成人名。

    **直接改存下來的那一份**（逐字稿與分析結果的統計），不做另一層對照表：
    畫面、下載、心智圖、發言佔比走的都是同一份資料，
    分兩個地方存一定會漂（「會議錄音轉逐字稿」那邊是同一個做法）。

    兩種範圍：`map` 是「這個代號以後都叫這個名字」（S1 → Jason），
    `overrides` 是「只有這一段」（`seq` → 名字）——
    辨識偶爾會把某一段掛錯人。

    **`seq` 一個都不動** —— 決議與待辦的引用綁的是 `seq`，
    改名字不可以讓任何一條引用失效。
    """
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    body = await request.json() or {}

    def _clean(name: object) -> str:
        # 名字會被寫進逐字稿、圖與下載的檔案 —— 控制字元與過長的值擋掉
        s = str(name or "").replace("\n", " ").replace("\r", " ").strip()
        return "".join(ch for ch in s if ch.isprintable())[:40]

    names = {str(k): _clean(v) for k, v in (body.get("map") or {}).items() if _clean(v)}
    overrides = {str(k): _clean(v) for k, v in (body.get("overrides") or {}).items()
                 if _clean(v)}
    if not names and not overrides:
        return {"ok": True, "renamed": 0}

    segs = _read_json(_seg_path(upload_id), "逐字稿")
    n = 0
    for s in segs:
        want = overrides.get(str(s.get("seq"))) or names.get(str(s.get("speaker") or ""))
        if want and want != s.get("speaker"):
            s["speaker"] = want
            n += 1
    atomic_json.write_json(_seg_path(upload_id), segs)

    # **分析結果裡每一個會出現發言者的地方都要跟著換**（v1.16.10，使用者回報：
    # 逐字稿改成「陳協理」，待辦還寫「負責：S1」、「發言統計」還是 S1）。
    # 回傳整份更新後的結果，前端直接拿去重畫 —— 不在前端另外補一份（兩份一定會漂）。
    out_path = _out_path(upload_id)
    out = None
    if out_path.exists():
        try:
            out = json.loads(out_path.read_text(encoding="utf-8"))
        except ValueError:
            out = None
        if isinstance(out, dict):
            _rename_in_result(out, names, segs)
            atomic_json.write_json(out_path, out)
            # 作業的結果檔（「我的作業」下載的那一份）也要跟著換名字
            await asyncio.to_thread(_write_full_export, upload_id)
    return {"ok": True, "renamed": n, "result": out, "segments": segs}


#: 「代號形狀」的發言者名字：S1、S12、SPEAKER_00、speaker_1、Speaker 2。
#: **只有這種會在自由文字裡被換掉** —— 真人名字做子字串取代的話，把「王」改名
#: 會連「王道」的王一起換掉。
_SPEAKER_CODE = re.compile(r"^(?:[A-Za-z]{1,3}\d{1,3}|(?:speaker|SPEAKER|Speaker)[ _-]?\d{1,3})$")


#: 分析結果裡「不是給人讀的字」的欄位 —— 改名時一律不碰
_STRUCTURAL_KEYS = frozenset({"id", "type", "kind", "color", "status"})


def _rename_in_result(out: dict, names: dict, segs: list[dict]) -> None:
    """把分析結果裡的發言者換成新名字（就地改）。

    * **發言統計依改名後的逐字稿重算** —— 只搬鍵名的話，單段改名的發言次數與時間
      不會跟著移，把兩個代號改成同一個人時也不會合併。
    * `owner` / `speaker` 這類欄位**整個等於**舊名字 → 換（整位改名才算）。
    * 自由文字（卡片內文、摘要、章節、心智圖節點）只換「代號形狀」的舊名字，
      而且前後不可以緊接英數字（`S12` 不可以被 `S1` 換掉一截）。
    * 單段改名（`overrides`）**不動自由文字** —— 文字裡的 S1 指的是整個人。
    """
    if "speaker_stats" in out:
        out["speaker_stats"] = mi.speaker_stats(segs)
    if not names:
        return
    exact = {str(k): str(v) for k, v in names.items()}
    code_res = [(re.compile(r"(?<![A-Za-z0-9_])" + re.escape(k) + r"(?![A-Za-z0-9_])"), v)
                for k, v in exact.items() if _SPEAKER_CODE.match(k)]

    def _text(v: str) -> str:
        for rx, new in code_res:
            v = rx.sub(new, v)
        return v

    def _walk(node):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                # 結構用的欄位（`node_id: c1`、`type`）不是給人讀的字 ——
                # 發言者代號剛好長得一樣時（`c1`）會把心智圖的連線弄斷
                if k in _STRUCTURAL_KEYS or k.endswith(("_id", "_ids")):
                    continue
                if isinstance(v, str):
                    if k in ("owner", "speaker", "who") and v in exact:
                        node[k] = exact[v]
                    else:
                        node[k] = _text(v)
                elif isinstance(v, (dict, list)):
                    _walk(v)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, str):
                    node[i] = _text(v)
                elif isinstance(v, (dict, list)):
                    _walk(v)

    # **只走這幾個區塊**：`source`（檔名）、`context`（使用者自己貼的背景）不可以動
    for key in ("summary", "items", "chapters", "mindmap"):
        if key in out:
            if isinstance(out[key], str):
                out[key] = _text(out[key])
            else:
                _walk(out[key])


# ------------------------------------------------------------------ 匯出

#: 從會議背景裡找主題的寫法。**只認明寫的**，不要從內文猜 ——
#: 猜錯的話文件標題會是一句不相干的話，而讀的人不會知道那是猜的。
_TITLE_KEYS = ("會議主題", "會議名稱", "主題", "議題名稱", "會議")

#: 貼上文字時我們自己塞的檔名 —— **那不是主題**，不可以拿來當標題。
_PASTED = "貼上的逐字稿"


def meeting_title(out: dict) -> str:
    """這份會議記錄的標題。

    優先序：**會議背景裡明寫的主題 → 檔名 → 一句通用的**。

    ⚠ 貼上逐字稿時檔名是我們自己塞的「貼上的逐字稿.txt」——
    拿它當標題會變成「貼上的逐字稿 會議記錄」，那是**系統的內部說法**
    出現在要發出去的文件上（2026-09-19 使用者回報）。

    **不從摘要猜主題**：摘要是一整段話，截前幾個字當標題會斷在半句，
    而且那個標題會隨著模型每次的講法變動。背景欄本來就是給使用者寫
    「會議主題：…」的地方 —— **有明寫的就用，沒有就不要編**。
    """
    ctx = str(out.get("context") or "")
    for line in ctx.splitlines()[:40]:
        line = line.strip()
        for key in _TITLE_KEYS:
            for sep in ("：", ":"):
                if line.startswith(key + sep):
                    v = line[len(key) + len(sep):].strip()
                    if v:
                        return v if v.endswith("會議記錄") else f"{v} 會議記錄"
    src = out.get("source") or {}
    # **先看旗標**（v1.16.6 起）。`_PASTED` 那條是**舊資料的退路** ——
    # 這個改動之前存下來的 meta 沒有這個欄位。
    if src.get("pasted"):
        return "會議記錄"
    name = Path(str(src.get("filename") or "")).stem.strip()
    if name and name != _PASTED:
        return f"{name} 會議記錄"
    return "會議記錄"


#: 圖在文件裡的章節標題（**跟畫面上的標題同一組字**）。圖本身不畫標題
#: （`build_all(titled=False)`）—— 文件有正式的章節標題，圖裡再畫一行 14px 粗體，
#: 看起來像一行沒排版的小字掛在圖上（2026-10-02 使用者回報「討論結構 標題不對」）。
def _chart_heading(name: str, out: dict, segments: Optional[list]) -> str:
    if name == "chapters":
        return "議題時間軸"
    if name == "timeline":
        chs = out.get("chapters") or []
        use_time = all(c.get("duration_ms") is not None for c in chs)
        return "各議題時間佔比" if use_time else "各議題發言段數佔比"
    if name == "speaker_share":
        return "發言者時間軸" if segments else "發言佔比"
    if name == "mindmap":
        return "討論結構"
    return name


def _img_md(name: str, svg: str, alt: str, *, embed: bool) -> list[str]:
    """一張圖的 Markdown。

    **`embed=True` 時用 data URI** —— 這樣匯出的 `.md` 是**一個檔案就完整**，
    轉送到「Markdown 轉辦公文件」時圖還在。用相對路徑的話，那邊只收到一個
    `.md`，圖會全部變成破圖（而且畫面上看不出來，只有排版出來才發現）。
    """
    import base64
    if embed:
        b64 = base64.b64encode(mc.to_png(svg)).decode("ascii")
        return [f"![{alt}](data:image/png;base64,{b64})", ""]
    return [f"![{alt}](images/{name}.png)", ""]


#: 逐字稿內文裡會被 Markdown 當成格式的字元 —— 跳脫掉，不然「*」「_」「|」會把
#: 一段話變成粗體、斜體或表格。換行一律收成空白（一段一行）。
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|~#])")


def _md_escape(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", " ".join(str(text or "").split()))


def _md_paragraphs(text: str) -> list[str]:
    """使用者自己打的多行文字原樣放進 Markdown：每一行跳脫格式字元（`*` `#` `|` 不會變成
    粗體、標題、表格），**行與行之間保留換行**（硬換行），空行分段。
    行首的 `-` / `1.` 不跳脫 —— 使用者用它們列點時，排成清單正是他要的樣子。"""
    paras, cur = [], []
    for ln in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        ln = ln.strip()
        if not ln:
            if cur:
                paras.append("  \n".join(cur))
                cur = []
            continue
        cur.append(_MD_SPECIAL.sub(r"\\\1", ln))
    if cur:
        paras.append("  \n".join(cur))
    return [x for para in paras for x in (para, "")]


def _transcript_md(segments: list) -> list[str]:
    """完整逐字稿（2026-10-02 使用者回報：匯出的檔案裡沒有逐字稿）。

    **每一條引用的「第 N 段」都要在同一份文件裡找得到** —— 只給段號、不附原文的話，
    收到文件的人沒有任何方法核對。

    **排成表格**（段號／時間／發言者／內容），不是一段一行的文字：內容長的時候只在
    自己那一欄折行，不會繞到段號底下，一整頁看下來欄位是對齊的（2026-10-02 使用者
    要求「逐字稿要整齊好看」）。沒有時間或沒有發言者的逐字稿，那一欄整欄不出現。
    """
    has_time = any(s.get("start_ms") is not None for s in segments)
    has_who = any(s.get("speaker") for s in segments)
    cols = ["段"] + (["時間"] if has_time else []) + (["發言者"] if has_who else []) + ["內容"]
    lines = ["## 逐字稿", "", f"共 {len(segments)} 段。", "",
             "| " + " | ".join(cols) + " |",
             "|" + "---:|" + ("---:|" if has_time else "") + ("---|" if has_who else "") + "---|"]
    for s in segments:
        row = [str(s.get("seq") if s.get("seq") is not None else "")]
        if has_time:
            row.append(mc._mmss(s.get("start_ms")) if s.get("start_ms") is not None else "")
        if has_who:
            row.append(_md_escape(mc._spk(s.get("speaker"))) if s.get("speaker") else "")
        row.append(_md_escape(s.get("text")))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return lines


def _md(out: dict, *, charts: bool = True, embed: bool = True,
        segments: Optional[list] = None, transcript: bool = True) -> str:
    """整理成 Markdown。

    **要能直接丟進「Markdown 轉辦公文件」** —— 會議記錄多半要發出去，
    而那支工具已經會做版面。所以這裡只負責內容。

    **順序跟畫面一樣**：會議背景 → 摘要 → 決議與待辦 → 議題時間軸（＋各議題佔比）→
    發言統計（＋發言者時間軸）→ 討論結構 → 逐字稿。每張圖放在它自己的章節底下，
    不再全部堆在最後面。
    """
    lines = [f"# {meeting_title(out)}", ""]
    # **會議背景原文照錄**（2026-10-02 使用者要求：存 JSON、存至工作區時也要有背景）：
    # 收到文件的人要知道這份記錄是在什麼前提下整理的（誰是主管、代號指什麼）。
    # 沒填就不出現這一節。所有文件格式與存至工作區都走這裡，一處處理。
    ctx = out.get("context")
    if isinstance(ctx, str) and ctx.strip():
        lines += ["## 會議背景", ""] + _md_paragraphs(ctx)
    # 逐字稿依會議背景換過哪些寫法 —— **要講出來**：讀的人看到的逐字稿不是辨識的原樣，
    # 而且引用比對、摘要都建立在換過的字上（原文留在 JSON 匯出的 `orig_text`）
    reps = tfx.normalise_applied(out.get("replacements"))
    if reps:
        lines += ["逐字稿換過這些寫法：" + "、".join(
            f"{_md_escape(r['from'])} → {_md_escape(r['to'])}（{r['count']} 處）"
            for r in reps) + "。", ""]
    svgs = mc.build_all(out, segments, titled=False) if charts else {}

    def chart(name: str, level: str) -> list[str]:
        if name not in svgs:
            return []
        h = _chart_heading(name, out, segments)
        return [f"{level} {h}", ""] + _img_md(name, svgs[name], h, embed=embed)

    # **鍵是 `text` 不是 `summary`**（`meeting_insight.build_summary` 回的是
    # `{"text", "grounded", "unsupported"}`）—— 取錯的話畫面與匯出都是空的，
    # 而那看起來只像「這次沒產生摘要」。
    sm = out.get("summary") or {}
    if sm.get("text"):
        lines += ["## 摘要", "", sm["text"], ""]
        # **驗不過要講出來，不可以安靜地拿掉**（那比標記出來更糟）
        if sm.get("grounded") is False and sm.get("unsupported"):
            lines += [f"> ⚠ 這幾個詞在逐字稿裡找不到依據："
                      f"{'、'.join(str(x) for x in sm['unsupported'])}", ""]

    # **鍵是複數**（`meeting_insight.KINDS`）—— 寫成單數的話這裡永遠取到空清單，
    # 而匯出的檔案看起來只是「這場會議沒有決議」，完全不像寫錯了。
    labels = mi.KIND_LABELS
    for kind, label in labels.items():
        items = (out.get("items") or {}).get(kind) or []
        if not items:
            continue
        lines += [f"## {label}", ""]
        for it in items:
            text = str(it.get("text") or "").strip()
            who = it.get("owner") or it.get("speaker")
            due = it.get("due_text") or it.get("due")
            extra = "，".join(x for x in [f"負責：{who}" if who else "",
                                          f"期限：{due}" if due else ""] if x)
            seqs = it.get("segment_ids") or it.get("seq") or []
            if isinstance(seqs, int):
                seqs = [seqs]
            cite = f"（第 {'、'.join(str(s) for s in seqs)} 段）" if seqs else ""
            lines.append(f"- {text}{('（' + extra + '）') if extra else ''}{cite}")
        lines.append("")

    chapters = out.get("chapters") or []
    if "chapters" in svgs:
        lines += chart("chapters", "##")
        lines += chart("timeline", "###")
    elif chapters:
        # 沒有圖（`charts=False`）時退回文字清單
        lines += ["## 議題時間軸", ""]
        for c in chapters:
            span = ""
            ids = c.get("segment_ids") or []
            if ids:
                span = f"（第 {ids[0]}–{ids[-1]} 段）"
            lines.append(f"- {c.get('title') or ''}{span}")
        lines.append("")

    stats = out.get("speaker_stats") or {}
    if len(stats) >= 2:
        use_time = any(v.get("speaking_ms") for v in stats.values())
        # **排序與欄名要跟畫面一致**：畫面依發言時間排（有時間的話），
        # 而「佔比」那一欄一直是**字數** —— 不寫清楚的話，旁邊那張圖用的是
        # 時間，同一個人兩個百分比並排會讓人以為有一邊算錯了。
        by_time = all(v.get("speaking_ms") is not None for v in stats.values())
        ordered = sorted(
            stats.items(),
            key=lambda kv: -((kv[1].get("speaking_ms") or 0) if by_time
                             else (kv[1].get("chars") or 0)))
        lines += ["## 發言統計", "",
                  "| 發言者 | 發言次數 | 字數 | 字數佔比 |"
                  + ("  發言時間 |" if use_time else "")]
        lines.append("|---|---:|---:|---:|" + ("---:|" if use_time else ""))
        for name, v in ordered:
            # `unknown` 是我們塞的代號不是名字 —— 顯示的時候換掉（資料裡留著）
            row = (f"| {mc._spk(name)} | {v.get('turn_count', 0)} | "
                   f"{v.get('chars', 0):,} | {v.get('char_pct', 0)}% |")
            if use_time:
                ms = v.get("speaking_ms")
                row += f" {mc._mmss(ms) if ms else '—'} |"
            lines.append(row)
        lines.append("")
        lines += chart("speaker_share", "###")
    else:
        lines += chart("speaker_share", "##")

    lines += chart("mindmap", "##")

    if transcript and segments:
        lines += _transcript_md(segments)
    return "\n".join(lines).rstrip() + "\n"


def _stack_png(pngs: list[bytes]) -> bytes:
    """幾張 PNG 疊成一張長圖（等寬、窄的置中）。"""
    import io
    from PIL import Image
    ims = [Image.open(io.BytesIO(b)).convert("RGB") for b in pngs]
    w = max(i.width for i in ims)
    gap = 24
    h = sum(i.height for i in ims) + gap * (len(ims) + 1)
    canvas = Image.new("RGB", (w, h), "white")
    y = gap
    for im in ims:
        canvas.paste(im, ((w - im.width) // 2, y))
        y += im.height + gap
    buf = io.BytesIO()
    canvas.save(buf, "PNG")
    return buf.getvalue()


#: 列印時一張圖最多這麼寬 / 這麼高（來源像素）。
#:
#: soffice 把 HTML 的 `<img>` 當 96 dpi 放（實測 `width="600"` → 450pt，
#: 剛好 0.75），所以 A4／Letter 扣掉 20mm 邊界之後的可用範圍換算回來大約是
#: 寬 660 px、高 900 px。取 640 × 880 留一點餘裕。
_PRINT_IMG_W = 640
_PRINT_IMG_H = 880


def _limit_image_width(html: str) -> str:
    """只限制圖寬、不切片（給 `.docx` / `.odt`）。寬度上限跟 PDF 同一個數字；
    本來就比上限窄的圖照原寸，不放大。"""
    import base64
    import io

    from PIL import Image

    def _one(m: "re.Match[str]") -> str:
        try:
            w = Image.open(io.BytesIO(base64.b64decode(m.group(1)))).width
        except Exception:                        # noqa: BLE001 — 認不得就原樣留著
            return m.group(0)
        return (f'<img src="data:image/png;base64,{m.group(1)}" '
                f'width="{min(w, _PRINT_IMG_W)}">')
    return re.sub(r'<img src="data:image/png;base64,([^"]+)"[^>]*>', _one, html)


def _fit_images_for_print(html: str) -> str:
    """把內嵌的圖調成**印得出來**的樣子：限寬 ＋ 太高的切成好幾張。

    兩件事都是量出來的（2026-09-19 使用者回報「pdf，圖超過範圍」）：

    | 寫法 | soffice 的反應 |
    |---|---|
    | `<img>` 不限制 | 照原始像素放 —— 980px 的心智圖右邊**整片被切掉** |
    | `style="max-width:100%"` | **完全不理**（實測仍是 980） |
    | **`width="600"` 屬性** | **有效**（→ 450pt） |

    跟表格框線同一課：**soffice 的 HTML 匯入是 HTML 4 時代的實作，
    CSS 走不通時先試表現屬性。**

    **光限寬還不夠**：心智圖動輒兩三千像素高，縮到頁寬之後仍然比一頁高，
    soffice 不會自己分頁 —— 它就**放一張、超出的部分裁掉**（實測圖底
    849pt > 頁高 792pt，下面全部不見）。所以太高的要自己切成好幾張，
    每一張都放得進一頁。
    """
    import base64
    import io
    import re

    from PIL import Image

    def _one(m: "re.Match[str]") -> str:
        b64 = m.group(1)
        try:
            im = Image.open(io.BytesIO(base64.b64decode(b64)))
        except Exception:                        # noqa: BLE001 — 認不得就原樣留著
            return m.group(0)
        # 縮到頁寬之後，一頁放得下多高的來源像素
        scale = min(1.0, _PRINT_IMG_W / im.width)
        chunk_src_h = int(_PRINT_IMG_H / scale) if scale else im.height
        if im.height <= chunk_src_h:
            return f'<img src="data:image/png;base64,{b64}" width="{_PRINT_IMG_W}">'
        out = []
        for top in range(0, im.height, chunk_src_h):
            part = im.crop((0, top, im.width, min(top + chunk_src_h, im.height)))
            buf = io.BytesIO()
            part.save(buf, "PNG")
            piece = base64.b64encode(buf.getvalue()).decode("ascii")
            out.append(f'<img src="data:image/png;base64,{piece}" width="{_PRINT_IMG_W}">')
        return "".join(out)

    return re.sub(r'<img src="data:image/png;base64,([^"]+)"[^>]*>', _one, html)


#: 預設的版面主題 —— **跟「Markdown 轉辦公文件」同一個**（清爽）。原本這裡預設商務報告，
#: 而下拉選單裡的名字寫著「清爽（預設）」—— 選單說一套、選中的是另一套
#: （2026-10-02 使用者回報「清爽不是預設嗎，為何每次進來都選在商務報告」）。
DEFAULT_THEME = "classic"

#: 主題預覽用的示範內容（**只用來看配色**，不是真的會議）。
_PREVIEW_MD = """# 專案週會 會議記錄

## 會議背景

第二季上線準備會議；王小明是專案經理。

## 摘要

會議確認了第二季的上線時程，決定先完成資料移轉再切換正式環境。

## 待辦

- 整理移轉清單並寄給專案小組（負責：王小明，期限：下週三）（第 12 段）

## 發言統計

| 發言者 | 發言次數 | 字數 | 字數佔比 |
|---|---:|---:|---:|
| 王小明 | 12 | 2,340 | 52.1% |
| 李美華 | 9 | 1,520 | 33.8% |
| 陳志強 | 4 | 630 | 14.1% |

## 逐字稿

**1 ｜ 0:12 ｜ 王小明**　各位早，今天先看上線時程。
"""

#: 匯出文件裡的表格樣式（2026-10-02 使用者回報「表格不夠好看」）：
#: 原本照內容寬度擠在左半邊、四邊與每一欄都畫框線、數字貼著框。
#: 改成**滿版寬、只畫橫線、表頭跟欄位同向對齊、內距加大**。
#: 只套在會議記錄上 —— 「Markdown 轉辦公文件」的表格維持原樣（那是使用者自己的內容）。
REPORT_TABLE = {"width": "100%", "cellpadding": 7, "rules": "rows", "align_th": True,
                "valign": "top"}

#: 匯出的文件格式 → （轉檔函式名, 副檔名, media type）。
#: **一份清單**：端點的參數檢查、副檔名、Content-Type 都讀它，
#: 分三個地方寫的話遲早會不一致（本專案在「同一份清單」上踩過很多次）。
DOC_FORMATS = {
    "pdf":  ("convert_to_pdf",  ".pdf",  "application/pdf"),
    "docx": ("convert_to_docx", ".docx",
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "odt":  ("convert_to_odt",  ".odt",  "application/vnd.oasis.opendocument.text"),
}


def _report_doc(out: dict, stem: str, fmt: str = "pdf", theme: str = DEFAULT_THEME,
                segments: Optional[list] = None,
                transcript: bool = True) -> tuple[bytes, str, str]:
    """整份會議記錄，轉成 PDF / Word / ODF。回 `(內容, media type, 副檔名)`。

    **走 Office 引擎，而且要先把 Markdown 轉成 HTML**
    —— 跟「Markdown 轉辦公文件」完全同一條路，連渲染器與主題都共用。
    自己排版會變成第二套排版程式，而那一套一定會比既有的差。

    ## ⚠ 原本直接把 `.md` 丟給 soffice（2026-09-19 使用者回報）

    使用者：「網頁上的會議摘要很美 但是匯出 pdf 怎會這麼差」。
    拿到的 PDF 只有三頁圖、**一個字的內文都沒有**，頁面尺寸是 980×2640
    —— 那是圖的原始大小，不是紙張。兩個錯疊在一起：

    1. `convert_to_*()` 的第二個參數是**目標檔案**，而且**回傳 `None`**。
       原本寫成 `pdf = convert_to_pdf(src, Path(td))` 再 `if pdf and pdf.exists()`
       —— 那個 if **從來沒有成立過一次**。
    2. **soffice 不認得 Markdown**，要先渲染成 HTML。

    而回 `None` 不是例外，所以 `except` 沒觸發 ——
    **退回只有圖的版本時，日誌裡一句話都沒有**。
    「安靜地退化」是本專案記過很多次的形狀：失敗比成功難看得多，
    但兩者在使用者眼裡都只是「檔案下載好了」。
    """
    from ...core import office_convert
    import tempfile
    fn_name, ext, media = DOC_FORMATS.get(fmt, DOC_FORMATS["pdf"])
    svgs = list(mc.build_all(out, segments).values())
    why = ""
    try:
        if not office_convert.find_soffice():
            why = "找不到 Office 引擎"
        else:
            # **共用同一支渲染器與主題** —— 另寫一份的話，那一份的排版遲早會比
            # 「Markdown 轉辦公文件」差（而且沒有人會發現）。
            from ..markdown_to_doc.router import _render_md_html
            from ..markdown_to_doc import themes as _themes
            if theme not in _themes.THEMES:
                theme = DEFAULT_THEME
            html = _render_md_html(
                _md(out, embed=True, segments=segments, transcript=transcript),
                theme, stem, table_opts=REPORT_TABLE)
            if fmt != "pdf":
                # **底色在 .odt / .docx 會掉，白字就變成看不見**
                # （見 `themes.office_safe_css` 的說明）。PDF 沒這問題。
                html = html.replace("</style>", _themes.office_safe_css() + "</style>", 1)
            if fmt == "pdf":
                html = _fit_images_for_print(html)
            else:
                # `.docx` / `.odt` 不切片（可以捲的文件，切開反而接不起來），
                # **但寬度一樣要限制** —— 原本不限，760～980 px 的圖照原始大小放，
                # 右邊整片超出頁面（2026-10-02 實際產出看到）。
                html = _limit_image_width(html)
            with tempfile.TemporaryDirectory() as td:
                src = Path(td) / f"{stem}.html"
                src.write_text(html, encoding="utf-8")
                dst = Path(td) / f"{stem}{ext}"
                getattr(office_convert, fn_name)(src, dst)
                if dst.exists() and dst.stat().st_size > 0:
                    return dst.read_bytes(), media, ext
                why = "Office 引擎沒有產出檔案"
    except Exception as e:                      # noqa: BLE001
        why = f"{type(e).__name__}: {e}"
    if fmt != "pdf" or not svgs:
        raise HTTPException(
            503, f"需要 Office 引擎（OxOffice / LibreOffice）才能匯出 {ext[1:]}")
    # **退化一定要留下紀錄**：拿到「只有圖」的 PDF 跟拿到完整的那一份，
    # 在使用者眼裡都只是「檔案下載好了」。
    logger.warning("會議記錄轉 %s 失敗，退回只有圖的版本：%s", fmt, why or "原因不明")
    return mc.to_pdf_pages(svgs), media, ext


@router.get("/chart/{upload_id}/{name}.{ext}")
async def chart(upload_id: str, name: str, ext: str, request: Request):
    """單張圖。畫面直接嵌這個網址（SVG 向量、放大不糊）。

    **圖是伺服器端畫的，畫面、Markdown、PDF 用的是同一份** ——
    前後端各畫一份的話，匯出的圖遲早會跟畫面上看到的不一樣。
    """
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    if ext not in ("svg", "png"):
        raise HTTPException(400, "只支援 svg 或 png")
    try:
        _segs = json.loads(_seg_path(upload_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _segs = None
    charts = mc.build_all(_read_json(_out_path(upload_id), "分析結果"), _segs)
    if name not in charts:
        raise HTTPException(404, "這場會議沒有這張圖")
    if ext == "svg":
        return Response(charts[name], media_type="image/svg+xml")
    return Response(mc.to_png(charts[name]), media_type="image/png")


@router.get("/charts/{upload_id}")
async def charts_list(upload_id: str, request: Request):
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    try:
        _segs = json.loads(_seg_path(upload_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _segs = None
    return {"charts": sorted(
        mc.build_all(_read_json(_out_path(upload_id), "分析結果"), _segs))}


#: 主題預覽左右的內距（縮放之前的 px）。主題裡伸到頁邊的元素在預覽裡伸這麼多。
_PREVIEW_PAD_X = 26


@router.get("/theme-preview/{theme}", response_class=HTMLResponse)
async def theme_preview(theme: str, request: Request):
    """版面主題的配色預覽（2026-10-02 使用者要求：選之前要看得出會長怎樣）。

    **用匯出時同一支渲染器、同一份主題 CSS、同一組表格設定** —— 另外畫一份示意圖的話，
    預覽與實際檔案遲早會不一樣。差別只有這是瀏覽器畫的（實際檔案是 Office 引擎排的），
    字距與換行可能略有不同，配色是一樣的。
    """
    from ..markdown_to_doc.router import _render_md_html
    from ..markdown_to_doc import themes as _themes
    if theme not in _themes.THEMES:
        raise HTTPException(404, "沒有這個主題")
    html = await asyncio.to_thread(_render_md_html, _PREVIEW_MD, theme, "preview",
                                   "default", REPORT_TABLE)
    nonce = getattr(request.state, "csp_nonce", "") or ""
    # CSP 的 style-src 只認帶 nonce 的 `<style>`。
    # **縮成一頁紙的縮圖，不捲動**（2026-10-02 使用者回報預覽排得不好看）：
    # 預覽框是 210 × 297（A4 比例），縮放 .385 之後版面寬約 545px，
    # 標題、摘要、待辦、表格剛好落在第一頁 —— 要看的是配色，不是讀內文。
    html = html.replace("<style>", f'<style nonce="{nonce}">')
    # 主題裡「伸到頁邊」的元素（商務報告的標題色帶）在文件裡伸的是頁邊距；
    # 預覽沒有頁邊距、邊距是 body 的內距 —— 照原樣伸出去的話，`overflow:hidden`
    # 把標題的第一個字切掉（2026-10-04 使用者截圖回報）。只換主題 CSS 那一段。
    cut = html.find("</style>")
    if cut > 0:
        html = (html[:cut].replace(f"-{_themes.PAGE_MARGIN_X}", f"-{_PREVIEW_PAD_X}px")
                + html[cut:])
    html = html.replace("</head>",
                        f'<style nonce="{nonce}">html{{zoom:.385;overflow:hidden}}'
                        f'body{{margin:0 !important;padding:22px {_PREVIEW_PAD_X}px !important;'
                        f'overflow:hidden}}</style></head>', 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/download/{upload_id}")
async def download(upload_id: str, request: Request, fmt: str = "md",
                   theme: str = DEFAULT_THEME, transcript: int = 1):
    """下載結果。**不是背景作業** —— 幾秒鐘的事，進「我的作業」只是雜訊。

    但**重活一定要丟出事件迴圈**：把圖算成 PNG（PyMuPDF）、打包 ZIP 都是
    CPU 工作，留在 `async def` 裡會**卡住整個網站** —— 別人連首頁都打不開。
    `tests/test_no_blocking_endpoints.py` 的靜態掃描看不到這種形狀
    （重活藏在 `mc.to_png()` / `_report_doc()` 裡面），所以這支另外列進
    那份測試的 `MUST_OFFLOAD` 清單釘住。

    使用者那一側看到的是按鈕變「產生中…」——「按了沒反應」是這一類最常見的
    回報，而慢的原因（真的要算）跟壞掉長得一模一樣。
    """
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    out = _read_json(_out_path(upload_id), "分析結果")
    # 檔名跟文件標題同一個來源 —— 兩邊不一樣的話，存下來的檔案
    # 叫「貼上的逐字稿」而裡面寫著別的主題。
    stem = meeting_title(out).replace(" 會議記錄", "") or "會議"
    if fmt not in ("md", "json", "png", "zip") and fmt not in DOC_FORMATS:
        raise HTTPException(
            400, "fmt 只接受 md / json / png / zip / "
                 + " / ".join(DOC_FORMATS))
    # **圖要跟畫面上看到的一樣** —— 「發言者時間軸」需要逐段資料，
    # 沒讀進來的話會退回舊的長條圖，而畫面上早就不是那張了
    # （2026-09-19 使用者回報「網頁改了 匯出時沒改到」）。
    try:
        segs = json.loads(_seg_path(upload_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        segs = None

    # **`FileResponse(filename=…)` 自己就會做 RFC 5987 那一套**，
    # `content_disposition()` 回的是**標頭的值（字串）**不是 dict ——
    # 傳進 `headers=` 會在送回應時才炸，端點本身看起來完全正常。
    if fmt == "json":
        # **帶格式標記與整份逐字稿** —— 這份檔案可以再傳回這支工具直接呈現
        # （2026-10-02 使用者問），引用要點得回原文就得有逐字稿。
        # 跟作業的結果檔同一支產生（`_write_full_export`）。
        return FileResponse(await asyncio.to_thread(_write_full_export, upload_id),
                            media_type="application/json", filename=f"{stem}-會議摘要.json")

    def _build() -> tuple[Path, str, str]:
        if fmt == "png":
            # 幾張圖疊成一張長圖 —— 貼到聊天室或簡報裡最方便
            svgs = list(mc.build_all(out, segs).values())
            if not svgs:
                raise HTTPException(404, "這場會議沒有可以匯出的圖")
            path = settings.temp_dir / f"ms_{upload_id}_charts.png"
            path.write_bytes(_stack_png([mc.to_png(v) for v in svgs]))
            return path, "image/png", f"{stem}-會議圖表.png"
        if fmt in DOC_FORMATS:
            data, media, ext = _report_doc(out, stem, fmt, theme, segs, bool(transcript))
            path = settings.temp_dir / f"ms_{upload_id}_report{ext}"
            path.write_bytes(data)
            return path, media, f"{stem}-會議記錄{ext}"
        if fmt == "zip":
            # `.md` ＋ `images/` —— 要自己改圖的人用這個（同 pdf-to-markdown 的做法）
            import io as _io
            import zipfile as _zf
            buf = _io.BytesIO()
            with _zf.ZipFile(buf, "w", _zf.ZIP_DEFLATED) as z:
                z.writestr(f"{stem}-會議記錄.md",
                           _md(out, embed=False, segments=segs,
                               transcript=bool(transcript)))
                # 跟 `.md` 一起用的圖 —— `.md` 自己有章節標題，圖就不再畫標題
                for name, svg in mc.build_all(out, segs, titled=False).items():
                    z.writestr(f"images/{name}.png", mc.to_png(svg))
                    z.writestr(f"images/{name}.svg", svg)
            path = settings.temp_dir / f"ms_{upload_id}_bundle.zip"
            path.write_bytes(buf.getvalue())
            return path, "application/zip", f"{stem}-會議記錄.zip"
        path = settings.temp_dir / f"ms_{upload_id}_summary.md"
        path.write_text(_md(out, segments=segs, transcript=bool(transcript)),
                        encoding="utf-8")
        return path, "text/markdown; charset=utf-8", f"{stem}-會議記錄.md"

    path, media, name = await asyncio.to_thread(_build)
    return FileResponse(path, media_type=media, filename=name)


# ------------------------------------------------------------------ 公開 API

@router.post("/api/meeting-summary")
async def api_meeting_summary(request: Request,
                              file: UploadFile = File(...),
                              second_pass: str = Form("1"),
                              context: str = Form(""),
                              replacements: str = Form("")):
    """一次做完：上傳逐字稿 → 回整份分析。

    **這是同步的**，一場長會議要跑好幾分鐘 —— 呼叫端的逾時要放寬。
    要背景處理請走網頁那條路（`/upload` → `/start` → 作業編號）。
    """
    if not llm_settings.is_enabled():
        raise HTTPException(503, "LLM 服務未啟用")
    data = await file.read()
    if not data:
        raise HTTPException(400, "檔案是空的")
    try:
        segs, _ = tp.parse(data, file.filename or "transcript.txt")
    except tp.TranscriptError as e:
        raise HTTPException(400, str(e)) from e
    pairs = _parse_pairs_form(replacements)
    applied: list[dict] = []
    if pairs:
        segs, applied = tfx.apply(segs, pairs)

    client = llm_settings.make_client()
    if client is None:
        raise HTTPException(503, "LLM 服務未啟用")
    model = llm_settings.get_model_for(TOOL_ID)

    def ask(prompt: str) -> str:
        return client.text_query(prompt, model=model, think=False)

    analysis = mi.full_analysis(segs, ask,
                                context=context[:mi.MAX_CONTEXT_CHARS],
                                second_pass=str(second_pass) not in ("0", "false", ""))
    out = analysis.to_public()
    out["llm_calls"] = analysis.calls
    # 背景跟著結果回去 —— 跟網頁那條（`_run_job`）一致（2026-10-02 使用者要求）
    ctx = context[:mi.MAX_CONTEXT_CHARS].strip()
    if ctx:
        out["context"] = ctx
    if applied:
        out["replacements"] = applied
    out["source"] = {"filename": file.filename or "transcript",
                     "segments": len(segs)}
    return out


def _parse_pairs_form(raw: str) -> list:
    """API 的 `replacements`：JSON 陣列 `[{"from": …, "to": …}]`。寫錯回 400 —— 安靜地不換的話，
    呼叫端會以為換過了。"""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except ValueError as e:
        raise HTTPException(400, "replacements 要是 JSON 陣列，例如 "
                                 '[{"from": "Bianka", "to": "Bianca"}]') from e
    if not isinstance(val, list):
        raise HTTPException(400, "replacements 要是 JSON 陣列")
    return val


@router.post("/api/term-suggestions")
async def api_term_suggestions(request: Request,
                               file: UploadFile = File(...),
                               context: str = Form("")):
    """依會議背景找出逐字稿裡可能寫錯的專有名詞（只建議，不改、不存）。
    挑好的再用 `/api/meeting-summary` 的 `replacements` 送進去。不需要 LLM。"""
    data = await file.read()
    if not data:
        raise HTTPException(400, "檔案是空的")
    try:
        segs, _ = tp.parse(data, file.filename or "transcript.txt")
    except tp.TranscriptError as e:
        raise HTTPException(400, str(e)) from e
    return await asyncio.to_thread(tfx.suggest, segs, context[:mi.MAX_CONTEXT_CHARS])
