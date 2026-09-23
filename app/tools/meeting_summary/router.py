"""會議摘要的端點。

**分兩步是刻意的**：先上傳、看解析結果，再按「開始分析」。
一場三小時的會議要跑幾分鐘的 LLM —— 如果發言者判錯、或整份檔案根本沒讀對，
使用者應該在**花那幾分鐘之前**就看得出來。所以 `/upload` 會回一段預覽
（段落數、發言者、總長、前幾段長什麼樣），分析是另一個動作。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response

from ...config import settings
from ...core import meeting_charts as mc
from ...core import meeting_insight as mi
from ...core import atomic_json
from ...core import safe_paths as _sp, upload_owner as _uo
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


def _doc_themes() -> list[tuple[str, str]]:
    """匯出文件可以選的版面主題 —— 從「Markdown 轉辦公文件」讀。"""
    try:
        from ..markdown_to_doc import themes as _th
        return [(k, (v.get("name") if isinstance(v, dict) else str(v)) or k)
                for k, v in _th.THEMES.items()]
    except Exception:                            # noqa: BLE001
        return [("report", "商務報告")]


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
        "chart_kinds": json.dumps(
            {k: {"colour": v[0], "label": v[1]} for k, v in mc.KIND_STYLE.items()},
            ensure_ascii=False),
    })


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
    # **告訴使用者用了哪一種排法**，並附上可以改的清單 ——
    # 自動判斷錯的時候要有路可走（使用者 2026-09-18 要求）。
    info["shape"] = shape_used
    info["shapes"] = tp.SHAPES
    atomic_json.write_json(_meta_path(upload_id), {
        "filename": file.filename or "transcript",
        "pasted": bool(pasted),
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


def _run_job(job, upload_id: str, context: str = "",
             with_impacts: bool = True) -> None:
    segments = json.loads(_seg_path(upload_id).read_text(encoding="utf-8"))
    ask = _ask_for(job)

    # 進度要說得出**在做什麼** —— 一場三小時的會議跑好幾分鐘，
    # 只有百分比的話使用者不知道是在跑還是卡住（本專案記過很多次）。
    stage_no = {name: i for i, name in enumerate(mi.STAGES)}

    def on_stage(name: str, i: int, n: int) -> None:
        base = stage_no.get(name, 0) / len(mi.STAGES)
        span = 1.0 / len(mi.STAGES)
        job.progress = round(base + span * (i / max(1, n)), 3)
        job.message = f"{name} {i}/{n}" if n > 1 else name

    job.message = mi.STAGES[0]
    analysis = mi.full_analysis(segments, ask, context=context,
                                with_impacts=with_impacts, on_stage=on_stage)

    out = analysis.to_public()
    # **背景要跟著結果存下來** —— 匯出時的文件標題會從它取主題，
    # 而那時候已經沒有那個請求了（背景是送出分析時帶進來的）。
    if context:
        out["context"] = context
    out["source"] = json.loads(_meta_path(upload_id).read_text(encoding="utf-8"))
    out["llm_calls"] = analysis.calls
    path = _out_path(upload_id)
    atomic_json.write_json(path, out)

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

    def run(job) -> None:
        _run_job(job, upload_id, context, with_impacts)

    job = job_manager.submit(
        TOOL_ID, run,
        meta={"filename": meta["filename"], "segments": meta["segments"]},
        request=request,
    )
    job.meta["view_url"] = f"/tools/{TOOL_ID}/?job={job.id}"
    return {"job_id": job.id}


@router.get("/result/{upload_id}")
async def result(upload_id: str, request: Request):
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    return _read_json(_out_path(upload_id), "分析結果")


@router.get("/segments/{upload_id}")
async def segments(upload_id: str, request: Request):
    """整份逐字稿 —— 結果頁要靠它把引用的段號還原成原文。"""
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    return {"segments": _read_json(_seg_path(upload_id), "逐字稿")}


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

    # 分析結果裡的發言統計是**以代號當鍵**的 —— 不一起搬的話，
    # 圖上還是舊代號，而逐字稿已經是人名了（同一個畫面兩套名字）。
    out_path = _out_path(upload_id)
    if out_path.exists():
        try:
            out = json.loads(out_path.read_text(encoding="utf-8"))
        except ValueError:
            out = None
        if isinstance(out, dict) and isinstance(out.get("speaker_stats"), dict):
            stats = {}
            for k, v in out["speaker_stats"].items():
                stats[names.get(str(k), str(k))] = v
            out["speaker_stats"] = stats
            atomic_json.write_json(out_path, out)
    return {"ok": True, "renamed": n}


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


def _chart_md(out: dict, *, embed: bool,
              segments: Optional[list] = None) -> list[str]:
    """圖的 Markdown。

    **`embed=True` 時用 data URI** —— 這樣匯出的 `.md` 是**一個檔案就完整**，
    轉送到「Markdown 轉辦公文件」時圖還在。用相對路徑的話，那邊只收到一個
    `.md`，圖會全部變成破圖（而且畫面上看不出來，只有排版出來才發現）。
    """
    import base64
    lines: list[str] = []
    titles = {"mindmap": "討論結構", "speaker_share": "誰在什麼時候講話",
              "timeline": "各議題佔多少時間"}
    for name, svg in (mc.build_all(out, segments) or {}).items():
        # **不要再加一個 `##` 標題** —— 伺服器畫的那幾張圖自己就有標題，
        # 外面再包一層會變成同一句話連著出現兩次（2026-09-19 使用者截圖）。
        if embed:
            b64 = base64.b64encode(mc.to_png(svg)).decode("ascii")
            lines += [f"![{titles.get(name, name)}](data:image/png;base64,{b64})", ""]
        else:
            lines += [f"![{titles.get(name, name)}](images/{name}.png)", ""]
    return lines


def _md(out: dict, *, charts: bool = True, embed: bool = True,
        segments: Optional[list] = None) -> str:
    """整理成 Markdown。

    **要能直接丟進「Markdown 轉辦公文件」** —— 會議記錄多半要發出去，
    而那支工具已經會做版面。所以這裡只負責內容。
    """
    src = out.get("source") or {}
    lines = [f"# {meeting_title(out)}", ""]
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
    if chapters:
        lines += ["## 議題", ""]
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
        lines += ["## 誰講了多少", "",
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

    if charts:
        lines += _chart_md(out, embed=embed, segments=segments)
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


#: 匯出的文件格式 → （轉檔函式名, 副檔名, media type）。
#: **一份清單**：端點的參數檢查、副檔名、Content-Type 都讀它，
#: 分三個地方寫的話遲早會不一致（本專案在「同一份清單」上踩過很多次）。
DOC_FORMATS = {
    "pdf":  ("convert_to_pdf",  ".pdf",  "application/pdf"),
    "docx": ("convert_to_docx", ".docx",
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "odt":  ("convert_to_odt",  ".odt",  "application/vnd.oasis.opendocument.text"),
}


def _report_doc(out: dict, stem: str, fmt: str = "pdf", theme: str = "report",
                segments: Optional[list] = None) -> tuple[bytes, str, str]:
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
                theme = "report"
            html = _render_md_html(_md(out, embed=True, segments=segments),
                                   theme, stem)
            if fmt != "pdf":
                # **底色在 .odt / .docx 會掉，白字就變成看不見**
                # （見 `themes.office_safe_css` 的說明）。PDF 沒這問題。
                html = html.replace("</style>", _themes.office_safe_css() + "</style>", 1)
            if fmt == "pdf":
                # 只有列印才需要限制圖的大小；`.docx` / `.odt` 是可以捲的文件，
                # 把圖切片反而變成好幾張圖接不起來。
                html = _fit_images_for_print(html)
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


@router.get("/download/{upload_id}")
async def download(upload_id: str, request: Request, fmt: str = "md",
                   theme: str = "report"):
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
    # **圖要跟畫面上看到的一樣** —— 「誰在什麼時候講話」需要逐段資料，
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
        return FileResponse(_out_path(upload_id), media_type="application/json",
                            filename=f"{stem}-會議摘要.json")

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
            data, media, ext = _report_doc(out, stem, fmt, theme, segs)
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
                           _md(out, embed=False, segments=segs))
                for name, svg in mc.build_all(out, segs).items():
                    z.writestr(f"images/{name}.png", mc.to_png(svg))
                    z.writestr(f"images/{name}.svg", svg)
            path = settings.temp_dir / f"ms_{upload_id}_bundle.zip"
            path.write_bytes(buf.getvalue())
            return path, "application/zip", f"{stem}-會議記錄.zip"
        path = settings.temp_dir / f"ms_{upload_id}_summary.md"
        path.write_text(_md(out, segments=segs), encoding="utf-8")
        return path, "text/markdown; charset=utf-8", f"{stem}-會議記錄.md"

    path, media, name = await asyncio.to_thread(_build)
    return FileResponse(path, media_type=media, filename=name)


# ------------------------------------------------------------------ 公開 API

@router.post("/api/meeting-summary")
async def api_meeting_summary(request: Request,
                              file: UploadFile = File(...),
                              second_pass: str = Form("1"),
                              context: str = Form("")):
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
    out["source"] = {"filename": file.filename or "transcript",
                     "segments": len(segs)}
    return out
