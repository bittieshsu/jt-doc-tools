"""Endpoints for 文件差異比對 (formerly PDF 差異比對).

Accepts PDF directly, or Word / Excel / PowerPoint / ODF — non-PDF inputs
are first converted to PDF via the shared OxOffice / LibreOffice helper,
then the same line-level diff runs against the rendered text.
"""
from __future__ import annotations

import difflib
import json
import re
import uuid
from pathlib import Path

import fitz
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from ...config import settings
from ...core import office_convert, pdf_preview
from ...core.safe_paths import require_uuid_hex
from . import highlight as _hl


router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    templates = request.app.state.templates
    from ...core.llm_settings import llm_settings
    return templates.TemplateResponse(request, "pdf_diff.html", {
        "request": request,
        "llm_enabled": llm_settings.is_enabled(),
        "llm_model": llm_settings.get_model_for("doc-diff") if llm_settings.is_enabled() else "",
    })


async def _llm_summarize_diff(pages_out: list[dict], totals: dict,
                              fname_a: str, fname_b: str) -> dict:
    """Build a compact diff text and ask LLM for a Chinese change summary.

    Returns {summary, highlights: [str], model} or {error, model} on failure.
    """
    from ...core.llm_settings import llm_settings as _llms
    import asyncio as _asyncio
    client = _llms.make_client("doc-diff")
    if client is None:
        return {}
    model = _llms.get_model_for("doc-diff")
    # Build a textual diff (only non-equal lines, capped at ~12k chars).
    snippets: list[str] = []
    cap = 12000
    used = 0
    for pg in pages_out:
        d = pg.get("diff") or {}
        a_lines = d.get("a") or []
        b_lines = d.get("b") or []
        page_chunks: list[str] = []
        for i in range(min(len(a_lines), len(b_lines))):
            ta = a_lines[i].get("tag")
            tb = b_lines[i].get("tag")
            if ta == "equal" and tb == "equal":
                continue
            la = a_lines[i].get("text") or ""
            lb = b_lines[i].get("text") or ""
            if ta in ("delete", "replace") and la:
                page_chunks.append(f"- {la}")
            if tb in ("insert", "replace") and lb:
                page_chunks.append(f"+ {lb}")
        if page_chunks:
            block = f"\n[第 {pg['index']} 頁]\n" + "\n".join(page_chunks)
            if used + len(block) > cap:
                snippets.append(block[:max(0, cap - used)])
                snippets.append("\n…（後續省略）")
                break
            snippets.append(block)
            used += len(block)
    diff_text = "".join(snippets).strip()
    if not diff_text:
        return {"summary": "兩份文件內容完全相同，沒有差異需要描述。",
                "highlights": [], "model": model}
    prompt = (
        f"你是文件審閱助手。下面是「{fname_a}」與「{fname_b}」"
        "之間的差異 (- 表示舊版有、+ 表示新版有)。"
        "請用繁體中文寫出 3-5 句話的整體變動摘要 (不超過 200 字)，"
        "並列出最重要的 3-7 個重點 (條列短句)。\n"
        f"統計：新增 {totals.get('added',0)} 行 / 刪除 {totals.get('removed',0)} 行 / 修改 {totals.get('changed',0)} 行。\n"
        "**只能回 JSON，不要 markdown / 解釋 / ```json``` 包裝。**\n"
        "格式：{\"summary\": \"...\", \"highlights\": [\"...\", \"...\"]}\n\n"
        f"差異內容：\n{diff_text}"
    )
    def _call():
        return client.text_query(prompt=prompt, model=model,
                                  temperature=0.0, think=False)
    try:
        resp = await _asyncio.to_thread(_call)
    except Exception as e:
        return {"error": str(e), "model": model}
    raw = (resp or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE)
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return {"error": "LLM 回應非 JSON object", "model": model}
        return {
            "summary":    str(parsed.get("summary") or "").strip(),
            "highlights": [str(x) for x in (parsed.get("highlights") or [])][:10],
            "model":      model,
        }
    except Exception as e:
        return {"error": f"LLM 回應解析失敗：{e}", "model": model,
                "raw": raw[:300]}


def _ensure_pdf(upload: UploadFile, data: bytes, uid: str, slot: str) -> Path:
    """Persist `data` and return a Path to a PDF representation of it.

    PDFs pass through unchanged; Office / ODF inputs are converted via
    soffice. Raises HTTPException(400) for unsupported types and
    HTTPException(500) if soffice itself fails or isn't installed.
    """
    name = (upload.filename or "").lower()
    is_pdf = name.endswith(".pdf")
    is_office = office_convert.is_office_file(name)
    if not (is_pdf or is_office):
        raise HTTPException(
            400,
            f"不支援的檔案類型：{upload.filename}（只接受 PDF / Word / Excel / "
            "PowerPoint / ODT / ODS / ODP）",
        )
    if is_pdf:
        out = settings.temp_dir / f"diff_{uid}_{slot}.pdf"
        out.write_bytes(data)
        return out
    # Office / ODF → write source, convert to PDF.
    suffix = Path(upload.filename or "in.bin").suffix or ".bin"
    src = settings.temp_dir / f"diff_{uid}_{slot}_src{suffix}"
    out = settings.temp_dir / f"diff_{uid}_{slot}.pdf"
    src.write_bytes(data)
    try:
        office_convert.convert_to_pdf(src, out)
    except office_convert.OfficeUnavailableError:
        # **不要包成 500** —— 那是「這台機器缺東西」不是「使用者送錯東西」，
        # 500 會讓人以為服務掛了而一直重試，監控端也全是假警報。
        # 全域處理器（`app/main.py`）會把它變成 503 並說出要裝什麼；
        # 包成 HTTPException 的話處理器根本看不到這個例外（v1.14.x 踩過）。
        raise
    except Exception as e:
        raise HTTPException(
            500,
            f"Office 檔轉 PDF 失敗：{upload.filename}（{e}）",
        ) from e
    finally:
        # source bytes no longer needed
        src.unlink(missing_ok=True)
    if not out.exists() or out.stat().st_size == 0:
        raise HTTPException(500, f"Office 檔轉 PDF 後檔案為空：{upload.filename}")
    return out


def _page_lines(doc: "fitz.Document") -> list[list[str]]:
    """Return a list[page][line_text]."""
    pages = []
    for pno in range(doc.page_count):
        text = doc[pno].get_text("text") or ""
        lines = [ln.rstrip() for ln in text.splitlines()]
        pages.append(lines)
    return pages


def _page_boxes(doc: "fitz.Document",
                pages: list[list[str]]) -> list[list[list | None] | None]:
    """每一行的**每個字**的座標，跟 `_page_lines` 的行索引對齊。

    比對本身完全不碰 —— 行還是從 `get_text("text")` 來（行為零改變），
    這裡只是另外用 `rawdict` 取座標，靠「第幾個非空行」把兩份對起來
    （實測 5 份真實 PDF 15 頁 ＋ 4 份 Office 檔 6 頁，非空行逐行相同）。

    **對不起來就整頁回 `None`** —— 沒有框只是少一個功能，
    **框畫錯位置是騙人**（使用者會以為那裡改過）。
    """
    out: list[list[list | None] | None] = []
    for pno in range(doc.page_count):
        try:
            rich = _hl.line_char_boxes(doc[pno])
        except Exception:                      # 壞掉的字型表之類
            out.append(None)
            continue
        text_lines = pages[pno] if pno < len(pages) else []
        per_line: list[list | None] = [None] * len(text_lines)
        ri = 0
        ok = True
        for li, ln in enumerate(text_lines):
            if not ln.strip():
                continue
            if ri >= len(rich) or rich[ri][0] != ln:
                ok = False
                break
            per_line[li] = rich[ri][1]
            ri += 1
        out.append(per_line if ok and ri == len(rich) else None)
    return out


def _pair_pages(a_pages: list[list[str]],
                b_pages: list[list[str]]) -> list[tuple[int | None, int | None]]:
    """把兩邊的頁面配對起來，回 `[(舊版頁索引, 新版頁索引)]`（0 起算，可能是 `None`）。

    ## 為什麼不能依索引配

    原本是 `for i in range(max(頁數))` 硬配 —— **插一頁之後，後面每一頁都會被
    判成整頁不同**。實測 20 頁的文件在第 3 頁插一頁：依索引只有 **2 / 21 頁**
    對得上，其餘 19 頁全部變成「整頁刪掉 ＋ 整頁新增」。使用者看到的是「整份
    都改了」，而實際上只多了一頁。

    ## 做法

    跟行的比對**同一個結構，只是高一層**：用 `difflib` 對「每頁的文字」做
    序列比對，`equal` 的直接 1:1 配起來（那是錨點），`replace` 的段落內再依
    位置配、長度不同的一邊補 `None`。

    **不需要模糊相似度** —— 沒改的頁面本來就逐字相同，拿它們當錨點就夠了；
    改過的頁面夾在錨點之間，位置自然對得上。加相似度只會多一個要調的門檻。
    """
    a_keys = ["\n".join(x) for x in a_pages]
    b_keys = ["\n".join(x) for x in b_pages]
    pairs: list[tuple[int | None, int | None]] = []
    sm = difflib.SequenceMatcher(None, a_keys, b_keys, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            pairs += [(i1 + k, j1 + k) for k in range(i2 - i1)]
        elif tag == "delete":
            pairs += [(k, None) for k in range(i1, i2)]
        elif tag == "insert":
            pairs += [(None, k) for k in range(j1, j2)]
        else:                                    # replace：段落內依位置配
            for k in range(max(i2 - i1, j2 - j1)):
                pairs.append((i1 + k if k < i2 - i1 else None,
                              j1 + k if k < j2 - j1 else None))
    return pairs


def _page_marks(rows: list[dict], boxes: list | None,
                page_rect) -> list[dict]:
    """把一頁的差異列換成**頁面上的框**（0~1 的比例）。

    `boxes` 是 `None` 時（抽不到座標、或行對不起來）回空清單 ——
    **寧可沒有框，也不要畫錯位置**。
    """
    if not boxes or page_rect is None:
        return []
    out: list[dict] = []
    for row in rows:
        tag = row.get("tag")
        if tag not in ("delete", "insert", "replace"):
            continue
        li = row.get("i")
        if li is None or li >= len(boxes):
            continue
        chars = boxes[li]
        if not chars:
            continue
        ops = row.get("ops")
        if tag == "replace" and ops:
            rects = []
            for a, b in ops:
                rects += _hl.char_range_rects(chars, a, b)
        else:                                   # 整行不見 / 整行新增
            rects = _hl.char_range_rects(chars, 0, len(chars))
        if rects:
            out.append({"tag": tag, "line": li,
                        "rects": [_hl.normalise(r, page_rect) for r in rects]})
    return out


def _diff_pages(a_lines: list[str], b_lines: list[str]) -> dict:
    """Return a line-level diff structure for two pages:

        {
            "a":   [{text, tag}],   # tag ∈ {"equal","delete","replace"}
            "b":   [{text, tag}],   # tag ∈ {"equal","insert","replace"}
            "added":  int,
            "removed": int,
            "changed": int,
        }

    The ``replace`` tag pairs up across a/b at the same visual row so the
    UI can align them side-by-side.
    """
    sm = difflib.SequenceMatcher(None, a_lines, b_lines, autojunk=False)
    a_out: list[dict] = []
    b_out: list[dict] = []
    added = removed = changed = 0
    # Char-level deltas alongside the line-level counts. Useful when the
    # line counts look small but each line has a lot of changed text.
    chars_added = chars_removed = chars_changed = 0
    chars_a = chars_b = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i1, i2):
                a_out.append({"text": a_lines[k], "tag": "equal", "i": k})
                b_out.append({"text": b_lines[j1 + (k - i1)], "tag": "equal",
                              "i": j1 + (k - i1)})
                chars_a += len(a_lines[k])
                chars_b += len(b_lines[j1 + (k - i1)])
        elif tag == "delete":
            for k in range(i1, i2):
                a_out.append({"text": a_lines[k], "tag": "delete", "i": k})
                b_out.append({"text": "", "tag": "blank"})
                removed += 1
                chars_removed += len(a_lines[k])
                chars_a += len(a_lines[k])
        elif tag == "insert":
            for k in range(j1, j2):
                a_out.append({"text": "", "tag": "blank"})
                b_out.append({"text": b_lines[k], "tag": "insert", "i": k})
                added += 1
                chars_added += len(b_lines[k])
                chars_b += len(b_lines[k])
        elif tag == "replace":
            la = i2 - i1
            lb = j2 - j1
            # Pair up top rows, fill shorter side with blanks
            rows = max(la, lb)
            for k in range(rows):
                ai = i1 + k if k < la else None
                bi = j1 + k if k < lb else None
                a_text = a_lines[ai] if ai is not None else ""
                b_text = b_lines[bi] if bi is not None else ""
                a_row = {"text": a_text,
                         "tag": "replace" if ai is not None else "blank"}
                b_row = {"text": b_text,
                         "tag": "replace" if bi is not None else "blank"}
                if ai is not None:
                    a_row["i"] = ai
                if bi is not None:
                    b_row["i"] = bi
                a_out.append(a_row)
                b_out.append(b_row)
                chars_a += len(a_text)
                chars_b += len(b_text)
                if ai is not None and bi is not None:
                    changed += 1
                    # On a paired replace, count the per-char edit distance
                    # between the two lines so a 1-char tweak doesn't show
                    # up the same as a fully-rewritten paragraph.
                    s = difflib.SequenceMatcher(None, a_text, b_text,
                                                autojunk=False)
                    # 這些範圍**本來就算出來了，只是算完就丟掉** ——
                    # 留著才標得出「這一行的第幾個字改了」（頁面模式用）。
                    a_row["ops"], b_row["ops"] = [], []
                    for t2, ai2, ai3, bi2, bi3 in s.get_opcodes():
                        if t2 == "equal":
                            continue
                        a_row["ops"].append([ai2, ai3])
                        b_row["ops"].append([bi2, bi3])
                        if t2 == "delete":
                            chars_removed += ai3 - ai2
                        elif t2 == "insert":
                            chars_added += bi3 - bi2
                        elif t2 == "replace":
                            chars_changed += max(ai3 - ai2, bi3 - bi2)
                elif ai is not None:
                    removed += 1
                    chars_removed += len(a_text)
                else:
                    added += 1
                    chars_added += len(b_text)
    return {"a": a_out, "b": b_out,
            "added": added, "removed": removed, "changed": changed,
            "chars_added": chars_added,
            "chars_removed": chars_removed,
            "chars_changed": chars_changed,
            "chars_a": chars_a, "chars_b": chars_b}


def _metadata_diff(a_meta: dict, b_meta: dict) -> list[dict]:
    keys = sorted(set(a_meta) | set(b_meta))
    rows = []
    for k in keys:
        av = a_meta.get(k) or ""
        bv = b_meta.get(k) or ""
        if av == bv:
            continue
        rows.append({"key": k, "old": str(av), "new": str(bv)})
    return rows


@router.post("/compare")
async def compare(
    request: Request,
    file_a: UploadFile = File(...),
    file_b: UploadFile = File(...),
    llm_summarize: str = Form(""),
):
    data_a = await file_a.read()
    data_b = await file_b.read()
    if not data_a or not data_b:
        raise HTTPException(400, "empty file")

    uid = uuid.uuid4().hex
    from ...core import upload_owner as _uo
    _uo.record(uid, request)
    pa = _ensure_pdf(file_a, data_a, uid, "a")
    pb = _ensure_pdf(file_b, data_b, uid, "b")

    import asyncio as _asyncio
    def _do_diff():
        with fitz.open(str(pa)) as da, fitz.open(str(pb)) as db:
            a_pages = _page_lines(da)
            b_pages = _page_lines(db)
            # 座標：頁面模式用。抽不到就整頁回 None，畫面自動退回只有文字模式。
            a_boxes = _page_boxes(da, a_pages)
            b_boxes = _page_boxes(db, b_pages)
            a_rects = [fitz.Rect(da[i].rect) for i in range(da.page_count)]
            b_rects = [fitz.Rect(db[i].rect) for i in range(db.page_count)]
            meta_diff = _metadata_diff(dict(da.metadata or {}),
                                       dict(db.metadata or {}))
            a_page_count = da.page_count
            b_page_count = db.page_count
        pages_out = []
        totals = {
            "added": 0, "removed": 0, "changed": 0,
            "chars_added": 0, "chars_removed": 0, "chars_changed": 0,
            "chars_a": 0, "chars_b": 0,
        }
        for row, (ai, bi) in enumerate(_pair_pages(a_pages, b_pages)):
            ap = a_pages[ai] if ai is not None else []
            bp = b_pages[bi] if bi is not None else []
            d = _diff_pages(ap, bp)
            for k in totals:
                totals[k] += d.get(k, 0)
            pages_out.append({
                "index": row + 1,
                # **`a_page` / `b_page` 是真正的頁碼**（1 起算，可能 null）——
                # 插過頁之後兩邊的頁碼會錯開，前端抓頁面圖要用這個，
                # 不可以用 `index`（那只是畫面上第幾列）。
                "a_page": (ai + 1) if ai is not None else None,
                "b_page": (bi + 1) if bi is not None else None,
                "a_exists": ai is not None,
                "b_exists": bi is not None,
                "diff": d,
                "marks": {
                    "a": _page_marks(d["a"],
                                     a_boxes[ai] if ai is not None else None,
                                     a_rects[ai] if ai is not None else None),
                    "b": _page_marks(d["b"],
                                     b_boxes[bi] if bi is not None else None,
                                     b_rects[bi] if bi is not None else None),
                },
                "size": {
                    "a": ([round(a_rects[ai].width, 2), round(a_rects[ai].height, 2)]
                          if ai is not None else None),
                    "b": ([round(b_rects[bi].width, 2), round(b_rects[bi].height, 2)]
                          if bi is not None else None),
                },
            })
        return a_page_count, b_page_count, pages_out, totals, meta_diff
    a_page_count, b_page_count, pages_out, totals, meta_diff = await _asyncio.to_thread(_do_diff)

    out = {
        # 頁面模式要拿它去抓頁面圖（`/page-image/{uid}/{slot}/{page}`）。
        # 歸屬驗證走的是 `_uo.record(uid, request)` 那一筆，不是這個值。
        "uid": uid,
        "filename_a": file_a.filename,
        "filename_b": file_b.filename,
        "pages": pages_out,
        "page_count_a": a_page_count,
        "page_count_b": b_page_count,
        "totals": totals,
        "metadata_diff": meta_diff,
    }
    if str(llm_summarize).lower() in ("1", "true", "on", "yes"):
        try:
            llm_extra = await _llm_summarize_diff(
                pages_out, totals,
                file_a.filename or "(舊版)",
                file_b.filename or "(新版)")
            if llm_extra:
                out["llm"] = llm_extra
        except Exception as exc:
            import logging as _lg
            _lg.getLogger(__name__).warning("LLM diff summarize failed: %s", exc)
            out["llm"] = {"error": "LLM 加值處理失敗（詳見伺服器日誌）"}
    return out


# ---- 對外 API：單次 upload 兩份文件 + JSON 回傳差異 ----
#: 頁面模式的解析度。150 dpi 實測暖機後彩色 7.3 ms／頁、6.5 MB／頁，
#: 而且看得清楚小字；再高只是變慢。
_PAGE_DPI = 150
#: 一次最多算幾頁圖 —— 使用者只看得到眼前幾頁，整份先算完是白費工。
_MAX_PAGE_IMAGE = 400


@router.get("/page-image/{uid}/{slot}/{page}")
async def page_image(uid: str, slot: str, page: int, request: Request):
    """頁面模式左右兩邊的頁面圖。

    **PDF 與 Office 在這裡已經收斂成同一種東西** —— `/compare` 會把 Office
    先轉成 `diff_{uid}_{slot}.pdf`，所以這裡不必分兩條路。

    歸屬驗證用的是 `/compare` 當下就寫好的那筆 owner record；
    `slot` 走白名單、`uid` 走固定格式，兩個都不能讓使用者自由組路徑。
    """
    from ...core import upload_owner as _uo
    require_uuid_hex(uid, "uid")
    if slot not in ("a", "b"):
        raise HTTPException(404, "not found")
    if page < 1 or page > _MAX_PAGE_IMAGE:
        raise HTTPException(404, "page out of range")
    _uo.require(uid, request)
    src = settings.temp_dir / f"diff_{uid}_{slot}.pdf"
    if not src.exists():
        raise HTTPException(410, "檔案已過期，請重新上傳比對")
    out = settings.temp_dir / f"diff_{uid}_{slot}_p{page}_{_PAGE_DPI}.png"
    if not out.exists():
        # 超出頁數時 `render_page_png` 會丟 `PageOutOfRange`，全域處理器回 404
        await pdf_preview.render_page_png_async(src, out, page - 1,
                                                dpi=_PAGE_DPI)
    return FileResponse(str(out), media_type="image/png",
                        headers={"Cache-Control": "no-store"})


@router.post("/api/doc-diff", include_in_schema=True)
async def api_doc_diff(
    request: Request,
    file_a: UploadFile = File(...),
    file_b: UploadFile = File(...),
):
    """單次上傳兩份 PDF / Office，比對後回 JSON：每頁差異 + 統計。"""
    return await compare(request=request, file_a=file_a, file_b=file_b,
                         llm_summarize="")
