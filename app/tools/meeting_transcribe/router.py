"""會議錄音轉逐字稿 —— 送去 jtlw、等它跑完、把逐字稿收回來。

## 這一版用**輪詢**不用 webhook

對方支援 webhook（我們收事件），但那條路要先註冊端點、驗簽章、去重、
補漏號 —— 而 `GET /jobs/{id}` 本來就查得到狀態。**先把整條路跑起來**，
webhook 之後當成最佳化加上去（也才有東西可以比對）。

## 三件跟別的工具不一樣的事

1. **錄音檔是對方來拉的**，不是我們推過去。所以送件前要先算 sha256 與大小
   （對方會核對），並用**寫定的對外位址**組一個短效簽章網址
   —— 不可以拿請求的 Host 組（見 `jtlw_settings` 的說明）。
2. **ACK 只能在逐字稿已經落地之後送。** `ack` 的意思是「你可以刪了」，
   順序必須是「寫完 → 才 ACK」。反過來的話我們回報成功、他們刪掉、
   而我們其實沒寫進去。
3. **`Idempotency-Key` 用我們自己的作業編號**：重送不會變成兩件，
   而且對方回的是原本那一件。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from ...config import settings
from ...core import atomic_json, jtlw_client, jtlw_settings
from ...core import safe_paths as _sp, upload_owner as _uo
from ...core.job_manager import job_manager
from ...logging_setup import get_logger
from ...web import speech_routes as _speech

logger = get_logger(__name__)
router = APIRouter()

TOOL_ID = "meeting-transcribe"

#: 收得下的副檔名。**伺服器端是唯一來源**，用 `data-*` 送進 DOM ——
#: 前端自己抄一份的話，檔案選擇器會把收得下的檔案濾掉，而且沒有錯誤訊息
#: （工作區那次就是這樣，v1.15.88）。
ACCEPT_EXTS = (".m4a", ".mp3", ".wav", ".aac", ".ogg", ".opus", ".flac",
               ".mp4", ".mov", ".mkv", ".webm")

#: 輪詢節奏：一開始密一點（短檔案幾十秒就好了），之後拉長。
_POLL_FIRST = 3.0
_POLL_MAX = 15.0

#: 牆鐘上限（對方的接入清單第 6 節）：含校正 = 音訊長度 × 0.5、下限 15 分鐘。
_POLL_FACTOR = 0.5
_POLL_FLOOR_S = 15 * 60.0
#: **排隊寬限**：對方的 GPU 一次只跑一件（2026-09-23 起），排隊時與辨識中都回
#: `running`、進度完全不動（對方 v2.7 實測）—— 我們分不出「在排隊」與「卡住」。
#: 前面排一場 3 小時的中文會議要等約 18～30 分鐘，超過 15 分鐘的下限，碰到就會被
#: 我們誤判逾時、還主動請對方取消。先多給 60 分鐘（前面排兩場長會議也撐得住）：
#: 真的卡住要晚一小時才發現，但那只是等久一點；誤殺是整件白做。
#:
#: **對方 api_revision 2.2（2026-09-23 上線）起不再需要**：排隊時 `progress.waiting`
#: 有值，排隊時間直接不算進上限（見 `_run_job`）。這個寬限只留給**回應裡完全沒有
#: `waiting` 這個鍵**的舊版服務。
_QUEUE_GRACE_S = 60 * 60.0

#: 排隊的**總**上限。排隊時間不算進處理上限，但不可以無限期排下去 ——
#: GPU 伺服器掛住時，前面的作業永遠不會做完，我們這件會一直佔著一個「外部服務」名額。
#: 對方單件錄音上限 6 小時、GPU 一次一件；4 小時是「前面排了好幾場長會議」也撐得住的長度。
_QUEUE_CAP_S = 4 * 3600.0

# 伺服器端產生、送到前端再翻譯的訊息（背景執行緒裡沒有 request，不知道使用者的語言）。
# **前端 `tr()` 會把連續數字換成 `{0}` `{1}` 再查** —— 所以這幾句的鍵是下面的樣板，
# 一律寫成常數，翻譯檢查（`test_i18n_dynamic_labels`）才掃得到。
_MSG_QUEUED_AHEAD = "排隊中（前面還有 {0} 件）"
_MSG_CORRECTING_ITEMS = "校正中（已完成 {0} / {1} 批）"
_MSG_QUEUE_FULL = "對方佇列滿了，{0} 秒後再試"
_MSG_TIMEOUT = ("等了 {0} 分鐘，對方還沒有回報結果 —— 已經請對方取消。"
                "請確認 JTLW 那側的佇列與工作機狀態。")
_MSG_QUEUE_CAP = ("排隊超過 {0} 小時還沒輪到 —— 已經請對方取消。"
                  "請稍後再送一次，或請語音服務那側確認佇列狀態。")
PROGRESS_TEMPLATES = (_MSG_QUEUED_AHEAD, _MSG_CORRECTING_ITEMS, _MSG_QUEUE_FULL,
                      _MSG_TIMEOUT, _MSG_QUEUE_CAP)


def _work_budget_s(total_audio_ms: Optional[float]) -> float:
    """處理本身最多花多久（秒）：15 分鐘與錄音長度的一半取大。**不含排隊。**"""
    work = _POLL_FLOOR_S
    if isinstance(total_audio_ms, (int, float)) and total_audio_ms > 0:
        work = max(_POLL_FLOOR_S, float(total_audio_ms) / 1000.0 * _POLL_FACTOR)
    return work


def _deadline_s(total_audio_ms: Optional[float]) -> float:
    """**舊版服務**（回應裡沒有 `waiting`）從送件起算最多等多久：處理上限 ＋ 排隊寬限。"""
    return _work_budget_s(total_audio_ms) + _QUEUE_GRACE_S


def _in_queue(info: dict) -> bool:
    """這一刻對方是不是在排隊（jtlw 自己的佇列，或 GPU 伺服器的隊伍）。"""
    if str(info.get("status") or "") == "queued":
        return True
    prog = info.get("progress")
    return isinstance(prog, dict) and isinstance(prog.get("waiting"), dict)

#: 佇列滿了最多重送幾次（依對方回的 `retry_after_ms` 等待）。
_QUEUE_RETRIES = 3

#: 終態 —— 對方文件第 7 節。
_TERMINAL = {"succeeded", "partially_succeeded", "failed", "cancelled"}


def _meta_path(upload_id: str) -> Path:
    return settings.temp_dir / f"mt_{upload_id}_meta.json"


def _out_path(upload_id: str) -> Path:
    return settings.temp_dir / f"mt_{upload_id}_transcript.json"


def _read_json(path: Path, what: str):
    if not path.is_file():
        raise HTTPException(410, f"{what}已經過期或不存在")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(500, f"{what}讀不回來") from e


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "meeting_transcribe.html", {
        "request": request,
        "configured": jtlw_settings.is_configured(),
        "accept_exts": ",".join(ACCEPT_EXTS),
    })


@router.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    """收錄音檔。**還不送件** —— 送件要先算雜湊、組簽章網址，那是下一步。"""
    if not jtlw_settings.is_configured():
        # 部署問題不是使用者送錯東西 → 503（同「缺相依回 503」那條）
        raise HTTPException(503, "還沒設定語音服務（JTLW）—— 請管理員先到設定頁填好")
    name = file.filename or "recording"
    ext = Path(name).suffix.lower()
    if ext not in ACCEPT_EXTS:
        raise HTTPException(400, f"收不下 {ext or '這種'} 檔案，支援的是："
                                 + "、".join(ACCEPT_EXTS))
    file_id = uuid.uuid4().hex
    dest = _speech.audio_path(file_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    # 串流寫入 —— 一場三小時的會議不可以整個讀進記憶體。
    with dest.open("wb") as f:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            f.write(chunk)
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "檔案是空的")

    upload_id = file_id                     # 兩邊用同一個編號，少一層對照
    _uo.record(upload_id, request)
    facts = _speech.file_facts(file_id)
    atomic_json.write_json(_meta_path(upload_id), {
        "filename": name, "content_type": file.content_type or "application/octet-stream",
        **facts,
    })
    return {"upload_id": upload_id, "filename": name, **facts}


#: 常見但語音服務不收的寫法 → 它認得的 BCP-47。
#:
#: 對方的 `/profiles` 列的是 `zh-Hant` / `en` / `ja` / `und`（台語那組另有
#: `nan-Hant`）。**不在這張表裡的值原樣送出**，由對方決定收不收 —— 我們不自己
#: 維護一份「支援哪些語言」的清單（那份清單在對方，寫第二份一定會漂）。
#: **簡體（`zh-CN` / `zh-Hans`）刻意不換成 `zh-Hant`**：那是不同的東西，
#: 換掉等於替使用者做了一個他沒做的選擇。
_LANGUAGE_ALIASES = {
    "zh": "zh-Hant", "zh-tw": "zh-Hant", "zh_tw": "zh-Hant", "zh-hant": "zh-Hant",
    "zh-hant-tw": "zh-Hant",
}


#: 錄音最後超過幾秒沒有任何文字，就在結果頁**提示**「可能不完整」。
#:
#: 由來（語音服務 2026-09-23 v2.9 / v2.10）：對方的 GPU 伺服器在傳結果途中重啟時，
#: 可能把**只傳了一半**的逐字稿當成功回來（`.223` 升級後修掉）。0 段的我們本來就判
#: 失敗；少一截的樣子是「最後一段停在切斷那一刻」。對方拿 25 場錄音量「錄音長度 −
#: 最後一段結束時間」：中位數 12.4 秒、最長 41.8 秒 —— 60 秒時 0 誤報。
#:
#: **只提示，不判失敗**：那 25 場都是整理過的會議錄音，照不到「錄音忘了停」
#: 「結尾是掌聲或音樂」—— 那種錄音尾端空白可以好幾分鐘，判失敗就是把一份完整的
#: 逐字稿丟掉（比少一截更糟）。每一件的空白都寫進記錄，之後拿我們自己的真實作業
#: 回頭校這個門檻。
_TAIL_GAP_HINT_S = 60.0


def _tail_gap_ms(summary: dict, segments: list[dict]) -> Optional[int]:
    """錄音長度減掉最後一段的結束時間；拿不到其中一個就回 None（不猜）。"""
    dur = (summary or {}).get("duration_ms")
    ends = [s["end_ms"] for s in segments
            if isinstance(s.get("end_ms"), (int, float))]
    if not isinstance(dur, (int, float)) or dur <= 0 or not ends:
        return None
    return max(0, int(dur - max(ends)))


def _normalize_language(value: object) -> str:
    """請求裡的語言代碼 → 送給語音服務的值（空的一律 `auto`）。"""
    v = str(value or "").strip()[:16]
    if not v:
        return "auto"
    low = v.lower()
    if low in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[low]
    # `en-US` / `ja-JP` 這類帶地區的：對方只列了 `en` / `ja`
    for base in ("en", "ja"):
        if low.startswith(base + "-") or low.startswith(base + "_"):
            return base
    return v


def _build_body(upload_id: str, meta: dict, *, language: str,
                num_speakers: Optional[int]) -> dict:
    base = jtlw_settings.audio_base_url()
    if not base:
        raise HTTPException(503, "還沒設定「錄音檔對外位址」—— "
                                 "對方要靠它回來拉錄音檔，請管理員到設定頁補上")
    cfg = jtlw_settings.get()
    body: dict = {
        "profile_id": cfg.get("profile_id") or jtlw_settings.DEFAULT_PROFILE,
        "tasks": list(cfg.get("tasks") or jtlw_settings.DEFAULT_TASKS),
        "source": {
            "type": "url",
            "url": _speech.sign_url(upload_id, base),
            "sha256": meta["sha256"],
            "size_bytes": meta["size_bytes"],
            "content_type": meta.get("content_type") or "application/octet-stream",
        },
        "language": language or "auto",
        # 對方的清單第 3 節：「`correction_level`：你們決定預設 `punctuation_only`」。
        # **不要留空讓對方挑** —— 校正改得越多，摘要與決議就越可能建立在
        # 被改過的字上（我們量過傳播率，見 `tools/meeting_eval/contamination.py`）。
        "correction_level": cfg.get("correction_level") or "punctuation_only",
        "external_ref": {"system": "jtdt", "job_id": upload_id},
    }
    if num_speakers:
        body["hints"] = {"num_speakers": int(num_speakers)}
    return body


def _assemble(client: jtlw_client.JtlwClient, remote_id: str,
              got: Optional[dict] = None) -> list[dict]:
    """把三層併成我們自己的 `{seq, text, speaker, start_ms, end_ms}`。

    **`raw` 有時間、`final` 有校正過的文字、`speakers` 有發言者** ——
    三層靠 `seq` 對起來（對方文件第 8 節）。
    **對不上的 seq 不可以硬湊**：寧可那一段沒有發言者，也不要把 A 的發言者
    貼到 B 的話上（同文件翻譯「段數對不上絕不硬湊」那條）。
    """
    got = got if got is not None else {}
    layers: dict[str, dict[int, dict]] = {}
    for layer in ("raw", "final", "speakers"):
        rows: dict[int, dict] = {}
        after = 0
        while True:
            try:
                page = client.segments(remote_id, layer, after_seq=after, limit=500)
            except jtlw_client.JtlwError as exc:
                # **沒要求過的層會回 400 `task_not_requested` —— 那不是錯誤**
                #（對方的清單第 5 節明寫「不要當成錯誤重試」）。
                # 校正失敗的作業（`partially_succeeded`）也會少掉 `final`。
                if exc.code in ("task_not_requested", "task_not_completed"):
                    break
                raise
            items = page.get("segments") or page.get("items") or []
            if not items:
                break
            for it in items:
                seq = it.get("seq")
                if seq is None:
                    continue
                rows[int(seq)] = it
                after = max(after, int(seq))
            if not page.get("has_more"):
                break
        layers[layer] = rows

    got.clear()
    got.update({k: len(v) for k, v in layers.items()})
    out = []
    for seq in sorted(layers["raw"] or layers["final"]):
        raw = layers["raw"].get(seq, {})
        fin = layers["final"].get(seq, {})
        spk = layers["speakers"].get(seq, {})
        text = (fin.get("text") or raw.get("text") or "").strip()
        if not text:
            continue
        row = {"seq": seq, "text": text}
        if spk.get("speaker_id"):
            row["speaker"] = str(spk["speaker_id"])
        # 時間只在 raw 那一層（final 刻意不帶，對方文件說明過）
        for k in ("start_ms", "end_ms"):
            if raw.get(k) is not None:
                row[k] = int(raw[k])
        out.append(row)
    return out


class TranscribeTimeout(RuntimeError):
    """等太久（含排隊超過上限）。同步 API 要回 504，跟「對方說失敗了」分開。"""


def _run_job(job, upload_id: str, language: str, num_speakers: Optional[int]) -> None:
    meta = json.loads(_meta_path(upload_id).read_text(encoding="utf-8"))
    client = jtlw_client.JtlwClient()

    job.message = "送件中"
    job.progress = 0.02
    body = _build_body(upload_id, meta, language=language, num_speakers=num_speakers)
    idem = f"jtdt-{upload_id}"
    # **佇列滿了是「等一下」不是「失敗」**（對方的清單第 6 節：依
    # `retry_after_ms` 重試）。重送用同一把 `Idempotency-Key`，
    # 所以就算對方其實已經收下了也不會變成兩件。
    for attempt in range(_QUEUE_RETRIES + 1):
        try:
            created = client.submit(body, idempotency_key=idem)
            break
        except jtlw_client.JtlwError as exc:
            if exc.code != "queue_full" or attempt == _QUEUE_RETRIES:
                raise
            wait_ms = 0
            try:
                wait_ms = int((exc.details or {}).get("retry_after_ms") or 0)
            except (TypeError, ValueError):
                wait_ms = 0
            delay = min(60.0, max(5.0, wait_ms / 1000.0))
            job.message = _MSG_QUEUE_FULL.format(int(delay))
            logger.info("JTLW queue_full，%.0f 秒後重送（第 %d 次）", delay, attempt + 1)
            time.sleep(delay)
    remote_id = str(created.get("job_id") or "")
    if not remote_id:
        raise RuntimeError("JTLW 沒有回作業編號")
    job.meta["remote_job_id"] = remote_id

    wait = _POLL_FIRST
    # **長時間等待一定要有牆鐘上限**（同「串流的 timeout 是每個 chunk
    # 不是整次生成」那一條）：對方卡住的話，我們這件作業會永遠輪詢下去，
    # 而症狀是「進度不動、不會失敗」——本專案最難查的那一類。
    #
    # 上限照對方清單第 6 節：**含校正 = 音訊長度 × 0.5，下限 15 分鐘**。
    # 音訊長度要等對方開始處理才知道（`progress.total_audio_ms`），先用下限。
    #
    # **排隊時間不算進上限**（對方 api_revision 2.2 起 `progress.waiting` 在 GPU
    # 排隊時有值）：上一輪看到在排隊，這一段等待就記進 `queued_s`。排隊另有總上限
    # （`_QUEUE_CAP_S`）。回應裡**從沒出現過 `waiting` 這個鍵**的是舊版服務 ——
    # 分不出排隊與卡住，退回「從送件起算 ＋ 60 分鐘寬限」。
    #
    # **不拿「進度多久沒動」當取消條件**：對方說輪到之後 `waiting` 會晚幾秒才消失、
    # 校正只在每批做完時更新、長會議的間隔沒量過 —— 用停多久來判卡住會誤殺長會議。
    started = time.monotonic()
    last = started
    queued_s = 0.0
    in_queue = False
    knows_waiting = False
    total_ms: Optional[float] = None
    while True:
        now = time.monotonic()
        if in_queue:
            queued_s += now - last
        last = now
        if knows_waiting:
            timed_out = (now - started - queued_s) > _work_budget_s(total_ms)
        else:
            timed_out = (now - started) > _deadline_s(total_ms)
        over_cap = queued_s > _QUEUE_CAP_S
        if timed_out or over_cap:
            try:
                client.cancel(remote_id)
            except jtlw_client.JtlwError:
                logger.warning("逾時後取消 JTLW 作業 %s 失敗", remote_id, exc_info=True)
            if over_cap:
                raise TranscribeTimeout(_MSG_QUEUE_CAP.format(int(_QUEUE_CAP_S / 3600)))
            raise TranscribeTimeout(_MSG_TIMEOUT.format(int((now - started) / 60)))
        if getattr(job, "cancelled", False):
            # **取消要真的傳過去** —— 只停輪詢的話對方照樣算完，
            # GPU 白燒（同「中止請求不等於中止工作」那條）。
            try:
                client.cancel(remote_id)
            except jtlw_client.JtlwError:
                logger.warning("取消 JTLW 作業 %s 失敗（已忽略）", remote_id, exc_info=True)
            raise RuntimeError("已取消")
        time.sleep(wait)
        wait = min(_POLL_MAX, wait * 1.5)
        info = client.job(remote_id)
        status = str(info.get("status") or "")
        # 進度照轉就好，不要自己再加密（對方最多每 5 秒一筆）
        pct = _percent(info)
        if pct is not None:
            job.progress = max(0.02, min(0.95, pct))
        prog = info.get("progress") if isinstance(info.get("progress"), dict) else {}
        reported = prog.get("total_audio_ms")
        if isinstance(reported, (int, float)) and reported > 0:
            total_ms = reported
        if "waiting" in prog:
            knows_waiting = True
        in_queue = _in_queue(info)
        job.message = _stage_label(info)
        if status in _TERMINAL:
            break

    if status in ("failed", "cancelled"):
        raise RuntimeError(_failure_text(info))

    job.message = "取回逐字稿"
    job.progress = 0.96
    got: dict[str, int] = {}
    segments = _assemble(client, remote_id, got)
    if not segments:
        raise RuntimeError(jtlw_client.MESSAGES["empty_result"])

    # 摘要（`duration_ms` / `languages` / `speakers` / 各層筆數 / 校正統計）。
    # **拿不到不算失敗** —— 逐字稿本身已經在手上了，這只是補充資訊。
    try:
        summary = client.result(remote_id) or {}
    except jtlw_client.JtlwError:
        logger.warning("取 JTLW 作業 %s 的摘要失敗（逐字稿已取回）", remote_id, exc_info=True)
        summary = {}

    # **校正失敗時要講出「你看到的是原始辨識結果」**（對方清單第 6 節）。
    # 判準有兩個，缺一不可：對方回的狀態，以及**我們實際拿到幾段校正後的文字**
    # —— 只看狀態的話，`final` 那一層拉不回來時畫面上仍然寫著「已校正」。
    uncorrected = status == "partially_succeeded" or not got.get("final")

    tail_gap = _tail_gap_ms(summary, segments)
    if tail_gap is not None:
        logger.info("JTLW 作業 %s：錄音 %.1f 秒，最後一段之後 %.1f 秒沒有文字",
                    remote_id, summary["duration_ms"] / 1000, tail_gap / 1000)

    out = {
        "source": {"filename": meta["filename"], "size_bytes": meta["size_bytes"]},
        "remote_job_id": remote_id,
        "status": status,
        "result": info.get("result") or {},
        "summary": summary,
        "uncorrected": uncorrected,
        # 伺服器端判斷要不要提示（同 `uncorrected`：前端不要自己猜門檻）
        "tail_gap_ms": tail_gap,
        "tail_hint": tail_gap is not None and tail_gap >= _TAIL_GAP_HINT_S * 1000,
        "layers": got,
        "segments": segments,
    }
    path = _out_path(upload_id)
    atomic_json.write_json(path, out)          # 內含 fsync

    # **ACK 只能在這之後** —— 上面那一行寫完（而且 fsync 過）才輪到它。
    # 對方文件寫明可以重複呼叫，所以失敗了記一行就好，不要讓整件作業紅掉。
    try:
        client.ack(remote_id)
        out_acked = True
    except jtlw_client.JtlwError:
        logger.warning("ACK JTLW 作業 %s 失敗 —— 逐字稿已經存好了，"
                       "對方會在 72 小時後自己清掉", remote_id, exc_info=True)
        out_acked = False
    job.meta["acked"] = out_acked

    job.result_path = path                     # 一定要放 Path 不是字串
    job.result_filename = f"{Path(meta['filename']).stem}-逐字稿.json"
    job.meta["upload_id"] = upload_id
    job.meta["segments"] = len(segments)
    if tail_gap is not None:
        job.meta["tail_gap_ms"] = tail_gap
    job.progress = 1.0
    job.message = "完成"


#: 對方的階段名 → 人看得懂的一句話。
#:
#: **⚠ 這幾個是「階段」不是「任務」。** 第一版我接成 `transcribe` / `diarize` /
#: `correct` —— 那是 `tasks` 物件的鍵，不是階段名；而且**頂層根本沒有 `stage`
#: 欄位**（頂層的 `stage` 只出現在 `errors[].stage`，標示錯在哪一階段）。
#: 接錯的症狀是「階段文字永遠是『處理中』、進度條從頭到尾不動」——
#: 又一次「什麼都沒發生」型的無聲失敗，是對方看到我們的說明才指出來的。
#: 這六個值對方說是程式裡的固定常數，不會有別的。
_STAGES = {
    "fetch": "取得錄音檔中",
    "normalize": "轉檔中",
    "asr": "辨識中",
    "diarization": "分辨發言者中",
    "correction": "校正中",
    "finalize": "整理結果中",
}


def _percent(info: dict) -> float | None:
    """0~1 的進度。**`progress` 是物件不是數字**（第一版當成數字，於是
    `isinstance` 永遠不成立、進度條從來沒動過）。"""
    prog = info.get("progress")
    if not isinstance(prog, dict):
        return None
    pct = prog.get("percent")
    if isinstance(pct, (int, float)):
        return float(pct) / 100.0
    done, total = prog.get("processed_audio_ms"), prog.get("total_audio_ms")
    if isinstance(done, (int, float)) and isinstance(total, (int, float)) and total > 0:
        return float(done) / float(total)
    return None


def _stage_label(info: dict) -> str:
    """把對方的狀態變成人看得懂的一句話。

    **排隊與處理中要分得出來** —— 使用者看到「處理中」三分鐘會以為卡住，
    看到「排隊中（前面還有 3 件）」就知道在等什麼。
    `queue_position` 是 `null` 代表已經在跑了。
    """
    status = str(info.get("status") or "")
    if status == "queued":
        pos = info.get("queue_position")
        return _MSG_QUEUED_AHEAD.format(pos) if pos else "排隊中"
    prog = info.get("progress")
    if not isinstance(prog, dict):
        return "處理中"
    # GPU 伺服器的隊伍（api_revision 2.2）：`status` 維持 running，排隊寫在這裡。
    # `ahead` 含別人的作業、含正在跑的那一件。
    waiting = prog.get("waiting")
    if isinstance(waiting, dict):
        ahead = waiting.get("ahead")
        return (_MSG_QUEUED_AHEAD.format(ahead)
                if isinstance(ahead, int) and ahead > 0 else "排隊中")
    stage = str(prog.get("stage") or "")
    if stage == "correction":
        done, total = prog.get("items_done"), prog.get("items_total")
        if isinstance(done, int) and isinstance(total, int) and total > 0:
            return _MSG_CORRECTING_ITEMS.format(done, total)
    return _STAGES.get(stage, stage or "處理中")


def _failure_text(info: dict) -> str:
    """終態作業的失敗原因 → 人話。

    **措辭在 `jtlw_client.describe_error` 一處**：送件當下被拒絕走的是例外，
    終態失敗走這裡，兩條路一定要講同一句話（2026-09-22 正式機踩到：
    `source_not_allowed` 在例外那條變成一行原始錯誤碼，
    而對照表裡明明就有「請管理員把位址加進允許清單」）。
    """
    err = info.get("error") or {}
    # 終態的錯誤在 `errors` 陣列裡；單一 `error` 是送件當下就被拒絕的形狀。
    if not err:
        rows = info.get("errors") or []
        err = rows[0] if rows else {}
    details = err.get("details") or {}
    msg = jtlw_client.describe_error(
        str(err.get("code") or ""),
        field=str(details.get("field") or ""),
        retryable=bool(err.get("retryable")),
        http_status=str(details.get("http_status") or ""),
        host=str(details.get("host") or ""))
    return msg or "JTLW 回報失敗，但沒有說原因"


@router.post("/start")
async def start(request: Request):
    if not jtlw_settings.is_configured():
        raise HTTPException(503, "還沒設定語音服務（JTLW）")
    body = await request.json()
    upload_id = str(body.get("upload_id") or "").strip()
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    meta = _read_json(_meta_path(upload_id), "錄音檔資訊")
    language = _normalize_language(body.get("language"))
    try:
        num_speakers = int(body.get("num_speakers") or 0) or None
    except (TypeError, ValueError):
        num_speakers = None

    def run(job) -> None:
        _run_job(job, upload_id, language, num_speakers)

    job = job_manager.submit(TOOL_ID, run,
                             meta={"filename": meta["filename"]}, request=request)
    job.meta["view_url"] = f"/tools/{TOOL_ID}/?job={job.id}"
    return {"job_id": job.id}


@router.post("/api/meeting-transcribe")
async def api_meeting_transcribe(request: Request,
                                 file: UploadFile = File(...),
                                 language: str = Form("auto"),
                                 num_speakers: str = Form("0")):
    """一次做完：上傳錄音 → 送件 → 等到好 → 回整份逐字稿。

    **這是同步的**：37 分鐘的會議實測 7~8 分鐘（辨識 ＋ 發言者分離 ＋ 校正），
    呼叫端的逾時要放寬。要背景處理請走網頁那條路
    （`/upload` → `/start` → 拿作業編號輪詢 `/api/jobs/{id}`）。

    **`num_speakers` 預設 0（讓對方自己判），而且建議就留 0** ——
    這裡問的是「發言量足以辨認的人數」，不是與會人數。
    20 場中文會議上，指定正確人數與不指定**分不出勝負**（信賴區間跨過 0）；
    但同一批裡**最佳的群數從來沒有大於實際人數**，所以「填正確的人頭數」
    本身就不是最佳解 —— 理由在樣板那段註解裡。
    """
    if not jtlw_settings.is_configured():
        raise HTTPException(503, "還沒設定語音服務（JTLW）—— 請管理員先到設定頁填好")
    name = file.filename or "recording"
    ext = Path(name).suffix.lower()
    if ext not in ACCEPT_EXTS:
        raise HTTPException(400, f"收不下 {ext or '這種'} 檔案，支援的是："
                                 + "、".join(ACCEPT_EXTS))
    file_id = uuid.uuid4().hex
    dest = _speech.audio_path(file_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with dest.open("wb") as f:                 # 串流寫入，三小時的會議不進記憶體
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            f.write(chunk)
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "檔案是空的")
    _uo.record(file_id, request)
    facts = _speech.file_facts(file_id)
    atomic_json.write_json(_meta_path(file_id), {
        "filename": name,
        "content_type": file.content_type or "application/octet-stream",
        **facts,
    })
    try:
        n = int(num_speakers or 0) or None
    except (TypeError, ValueError):
        n = None

    class _Sync:
        """`_run_job` 要一個作業物件放進度 —— 同步呼叫沒有那個東西，
        給它一個只吞不吐的替身。**不要為了 API 另寫一條處理邏輯** ——
        抄第二份就是抄錯的機會（本專案第 N 次）。"""
        cancelled = False
        message = ""
        progress = 0.0
        meta: dict = {}
        result_path = None
        result_filename = ""

    # **失敗要照「是誰的問題」回狀態碼**（v1.16.10）。原本 `JtlwError` 沒人接，
    # 一律冒成 500 —— 而 API 手冊寫的是「對方不支援的語言回 400」。
    # 500 會讓呼叫端以為服務壞了而一直重試，其實是他送的參數要改。
    try:
        _run_job(_Sync(), file_id, _normalize_language(language), n)
    except jtlw_client.JtlwUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except jtlw_client.JtlwError as exc:
        # 呼叫端自己帶的參數被對方退回 → 他要改的是參數
        if exc.field in _CALLER_FIELDS:
            raise HTTPException(400, str(exc)) from exc
        raise HTTPException(502, str(exc)) from exc
    except TranscribeTimeout as exc:
        raise HTTPException(504, str(exc)) from exc
    except RuntimeError as exc:
        # 對方回報辨識失敗、結果是空的 —— 是上游的結果，不是我們壞了
        raise HTTPException(502, str(exc)) from exc
    return _read_json(_out_path(file_id), "逐字稿")


#: 同步 API 裡**呼叫端自己帶的**參數 —— 對方退回這幾個欄位時回 400。
_CALLER_FIELDS = frozenset({"language", "num_speakers"})


#: **`@router.get` 不會自動加 HEAD**（Starlette 的 `Route` 會，FastAPI 不會）。
#: 探測「檔案還在不在」用 HEAD 很自然 —— 沒列的話回 405，
#: 而探測失敗在畫面上長得跟「沒有錄音檔」一模一樣（2026-09-22 實際踩到：
#: 波形與播放器整組不出現，沒有任何錯誤）。
@router.api_route("/audio/{upload_id}", methods=["GET", "HEAD"])
async def audio(upload_id: str, request: Request):
    """把原始錄音交回瀏覽器播放。

    **走 `upload_owner`，不是簽章網址**：簽章那條是給**對方的伺服器**拉檔用的
    （短效、無須登入），這一條是給**已登入的本人**在畫面上播放。
    兩者混用的話，要嘛把短效網址塞進頁面（會過期），
    要嘛把播放網址做成永久公開（那就漏了）。

    `FileResponse` 自己處理 Range，所以拖動進度列不會整檔重拉。
    """
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    p = _speech.audio_path(upload_id)
    if not p.is_file():
        raise HTTPException(404, "錄音檔已經被清掉了")
    meta = {}
    try:
        meta = json.loads(_meta_path(upload_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return FileResponse(str(p),
                        media_type=meta.get("content_type") or "application/octet-stream",
                        headers={"Cache-Control": "no-store"})


@router.post("/speakers/{upload_id}")
async def rename_speakers(upload_id: str, request: Request):
    """存發言者的名字。

    **兩種範圍**：`map` 是「這個代號以後都叫這個名字」（S1 → Jason），
    `overrides` 是「只有這一段」（`seq` → 名字）。使用者改一個名字時
    預設是前者 —— 一場會議裡同一個人被標成 S1 幾百次，一段一段改沒有意義；
    但辨識偶爾會把某一段掛錯人，所以單段的路也要留。

    **存回逐字稿本身**，不另外開一個檔：下載、複製、轉送「會議摘要」
    走的都是同一份資料，分兩個地方存一定會漂。
    """
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    body = await request.json() or {}
    path = _out_path(upload_id)
    data = _read_json(path, "逐字稿")

    def _clean(name: object) -> str:
        # 名字會被寫進逐字稿、下載的檔案與摘要 —— 控制字元與過長的值擋掉
        s = str(name or "").replace("\n", " ").replace("\r", " ").strip()
        return "".join(ch for ch in s if ch.isprintable())[:40]

    names = {str(k): _clean(v) for k, v in (body.get("map") or {}).items() if _clean(v)}
    overrides = {str(k): _clean(v) for k, v in (body.get("overrides") or {}).items()
                 if _clean(v)}
    data["speaker_names"] = names
    data["speaker_overrides"] = overrides
    atomic_json.write_json(path, data)
    return {"ok": True, "map": names, "overrides": overrides}


@router.get("/result/{upload_id}")
async def result(upload_id: str, request: Request):
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    return _read_json(_out_path(upload_id), "逐字稿")
