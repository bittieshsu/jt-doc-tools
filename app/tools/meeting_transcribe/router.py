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
   而我們其實沒寫進去。**v1.16.41 起要過校正的作業延後 ACK**（最多 24 小時，
   給使用者補專有名詞只重跑校正），由 `jtlw_ack` 負責到期送出。
3. **`Idempotency-Key` 用我們自己的作業編號**：重送不會變成兩件，
   而且對方回的是原本那一件。
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from ...config import settings
from ...core import atomic_json, jtlw_ack, jtlw_client, jtlw_settings
from ...core import audio_peaks as _audio_peaks
from ...core import safe_paths as _sp, upload_owner as _uo
from ...core import meeting_insight as _mi
from ...core import self_intro as _si
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
_MSG_RECONNECTING = "暫時連不上 JTLW，{0} 秒後重試"
_MSG_OUTAGE = ("JTLW 連續 {0} 分鐘連不上，已停止等待。"
               "請確認語音服務是否正常，稍後再送一次。")
PROGRESS_TEMPLATES = (_MSG_QUEUED_AHEAD, _MSG_CORRECTING_ITEMS, _MSG_QUEUE_FULL,
                      _MSG_TIMEOUT, _MSG_QUEUE_CAP, _MSG_RECONNECTING, _MSG_OUTAGE)

#: 對方短暫連不上時，**只讀的查詢**（輪詢狀態、取逐字稿）要撐過去。
#:
#: 對方重啟服務約 5 秒（2026-09-27 v2.17 通知），網路也會閃一下 —— 原本輪詢碰到
#: 一次連不上就把整件判失敗，而對方那邊其實還在跑：一場三小時的會議白轉，
#: 我們也不會再回去取結果（沒 ACK，對方 7 天後清掉）。
#:
#: **只重試「連不上 / 逾時 / 429 / 502 / 503 / 504」**：其他錯誤（找不到作業、
#: 權限不對）重問一百次也一樣，照舊立刻失敗 —— 什麼都重試的話，真的壞掉要晚
#: 5 分鐘才看得到，而且訊息會變成「連不上」而不是真正的原因。
#: **送件（POST）不走這條** —— 那一段失敗時使用者當下就看得到，重送一次即可。
_OUTAGE_GRACE_S = 5 * 60.0
_OUTAGE_RETRY_FIRST = 3.0
_OUTAGE_RETRY_MAX = 30.0
_TRANSIENT_STATUS = frozenset({429, 502, 503, 504})


def _is_transient(exc: "jtlw_client.JtlwError") -> bool:
    return (isinstance(exc, jtlw_client.JtlwUnavailable)
            or exc.status in _TRANSIENT_STATUS)


def _through_outage(job, client, remote_id: str, call):
    """呼叫 `call()`；對方暫時連不上就退避重試，連續斷超過 `_OUTAGE_GRACE_S` 才放棄。

    斷線期間仍然看得到「取消」：使用者按停止時照樣把取消傳過去（傳不過去就記一行）。
    """
    down_since: Optional[float] = None
    prev_msg = ""
    wait = _OUTAGE_RETRY_FIRST
    while True:
        try:
            out = call()
        except jtlw_client.JtlwError as exc:
            if not _is_transient(exc):
                raise
            now = time.monotonic()
            if down_since is None:
                down_since = now
                prev_msg = getattr(job, "message", "")
                logger.warning("JTLW 暫時連不上（作業 %s），重試中：%s", remote_id, exc)
            if now - down_since >= _OUTAGE_GRACE_S:
                raise jtlw_client.JtlwUnavailable(
                    _MSG_OUTAGE.format(int(_OUTAGE_GRACE_S // 60)),
                    status=exc.status) from exc
            if getattr(job, "cancelled", False):
                try:
                    client.cancel(remote_id)
                except jtlw_client.JtlwError:
                    logger.warning("取消 JTLW 作業 %s 失敗（對方連不上）", remote_id,
                                   exc_info=True)
                raise RuntimeError("已取消")
            job.message = _MSG_RECONNECTING.format(int(wait))
            time.sleep(wait)
            wait = min(_OUTAGE_RETRY_MAX, wait * 1.5)
            continue
        if down_since is not None:
            logger.info("JTLW 恢復連線（作業 %s），斷了 %.0f 秒", remote_id,
                        time.monotonic() - down_since)
            job.message = prev_msg
        return out


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


@router.get("/speaker-mode")
async def speaker_mode():
    """頁面用：這台語音服務會用哪一種發言者分離 —— 決定人數那一格怎麼問。

    **不放在頁面渲染時算**：那要連到對方，對方慢或連不上時整頁會跟著卡住。
    """
    return {"engine": await asyncio.to_thread(_speaker_engine_for_page)}


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
#: 對方的 `/profiles` 列的是 `zh-Hant` / `en` / `ja` / `ko` / `und`（台語那組是
#: `nan-Hant` / `zh-Hant`；`ko` 是 `api_revision` 2.3 起才收的）。**不在這張表裡的值原樣送出**，由對方決定收不收 —— 我們不自己
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
    # `en-US` / `ja-JP` / `ko-KR` 這類帶地區的：對方只列了 `en` / `ja` / `ko`
    for base in ("en", "ja", "ko"):
        if low.startswith(base + "-") or low.startswith(base + "_"):
            return base
    return v


#: 發言者分離用 NVIDIA Nemotron（`hints.diarize_engine: "auto"`）。
#:
#: JTLW `api_revision` 2.5 起收這個欄位（2026-10-01，對方 v2.23）。同一批**真實辨識段落**、
#: 不指定人數時，中文 AISHELL-4 20 場「發言者搞錯」18.52% → 2.92%、人數判對 2/20 → 18/20；
#: 英文 AMI 16 場 12.31% → 4.65%。`auto` 在做不到時（指定超過 8 人、偵測到 8 人全部用滿、
#: GPU 伺服器連不上而改在本機做）**由對方自動退回原本的方法**，原因寫在
#: `Result.diarization.note` —— 我們顯示出來，不另外判斷。
#:
#: **同一個人數在兩種方法下的意思相反**：原本的方法是「填幾個就硬分成幾群」
#:（填多了會把主要發言者拆開），Nemotron 是「填的數字只當上限」（填多了沒有影響、
#: 填少了會把不同的人併在一起）。所以頁面的問法要跟著這裡決定的結果走（`/speaker-mode`）。
_DIARIZE_ENGINE = "auto"
_ENGINE_MIN_REVISION = (2, 5)
#: 頁面問「會用哪一種」時的快取 —— 送件本身每次都重查，不吃這份。
_ENGINE_CACHE_S = 300.0
_engine_cache: dict[str, tuple[float, str]] = {}
#: 頁面那次查詢的逾時。對方的 `/capabilities` **不快取、每次即時去問後端**（各 2 秒），
#: 後端掛掉時要 4～6 秒才回（對方 v2.24）—— 原本設 5 秒，正好會在那種時候逾時。
_PAGE_CAPS_TIMEOUT_S = 10
#: 上一次成功讀到的介面版本（依送件位址）。問不到時**沿用它**，不要直接當舊版：
#: 對方後端一時出狀況時，那幾件會沒有任何提示地改用原本的方法（對方 v2.24 指出）。
#: 對方承諾升版前會通知、版本不會往下掉。只記在行程裡 —— 重啟後第一次要真的問得到。
_last_revision: dict[str, str] = {}

#: Nemotron 改用原本方法時，對方 `Result.diarization.reason` 的代碼 → 畫面上的句子
#:（api_revision 2.6 起，對方 v2.24）。**認得的代碼才翻**；不認得的（對方日後會加）
#: 一律用 `_FALLBACK_GENERIC` 並附上對方的 `note` —— 不可以因為不認得就什麼都不說。
DIARIZE_FALLBACK_TEXT = {
    "too_many_speakers": "指定的發言者超過 8 位，這次改用原本的方法分辨。",
    "speakers_saturated": "偵測到超過 8 位發言者，這次改用原本的方法分辨。",
    "nemotron_unavailable": "語音服務那側沒有新的發言者分辨模型，這次改用原本的方法分辨。",
    "nemotron_failed": "新的發言者分辨模型執行失敗，這次改用原本的方法分辨。",
}
_FALLBACK_GENERIC = "這次的發言者改用原本的方法分辨，沒有用 Nemotron。"


def _diarize_fallback(requested: Optional[str], diar: object) -> Optional[dict]:
    """要求了 Nemotron 卻改用原本的方法時，結果頁要講的話。沒退回就回 None。"""
    if requested != _DIARIZE_ENGINE or not isinstance(diar, dict):
        return None
    if diar.get("engine") != "legacy":
        return None
    text = DIARIZE_FALLBACK_TEXT.get(str(diar.get("reason") or ""))
    return {"reason": diar.get("reason"),
            "text": text or _FALLBACK_GENERIC,
            # 認得代碼時不重複附中文說明（它在英日介面不會翻）；不認得才附上
            "note": None if text else (diar.get("note") or None)}


#: 新方法的 8 個位置全部用到、**而且照用新方法**時的提醒（JTLW `api_revision` 2.7，對方 v2.26）。
#: 沒指定人數時，原本的方法分出 8 位以下就照用 Nemotron、最多 8 位 —— 實際超過 8 人的話
#: 會有人被併在一起，而結果跟一般的完全一樣（`reason` 是 null）。判斷式照對方給的：
#: `saturated && engine == "nemotron"`。退回原本方法的那一種（`speakers_saturated`）
#: 已經由 `_diarize_fallback` 講了，不重複。
#: **措辭有條件**（「實際發言者更多時」）：對方拿 36 場公開語料重算，一場都沒有用滿 8 個，
#: 會出現這句的是至少聽到 8 個不同聲音的會議，其中可能有幾位只講了幾句。
DIARIZE_SATURATED_TEXT = "新方法的 8 個位置都用滿了；實際發言者更多時，請填人數後重送。"


def _diarize_saturated(requested: Optional[str], diar: object) -> bool:
    if requested != _DIARIZE_ENGINE or not isinstance(diar, dict):
        return False
    return diar.get("saturated") is True and diar.get("engine") == "nemotron"


def _api_revision(client) -> str:
    """對方的介面版本（`api_revision`）。問不到時沿用上一次讀到的；**連上一次都沒有回空字串**
    —— 呼叫端一律把空字串當舊版：少送一個選填欄位只是照原本的做法；送錯了是整件被退回
    （對方的 `hints` 與 glossary 的每一筆都是 `additionalProperties: false`）。"""
    try:
        key = jtlw_settings.base_url()
    except Exception:
        key = ""
    try:
        rev = client.capabilities().api_revision
        if rev:
            _last_revision[key] = rev
        return rev or ""
    except Exception:                          # 連不上、逾時、權限不足、舊版服務沒有這支
        rev = _last_revision.get(key, "")
        if rev:
            logger.warning("讀不到 JTLW 的介面版本，沿用上一次讀到的 %s", rev, exc_info=True)
        else:
            logger.warning("讀不到 JTLW 的介面版本，這一件當成舊版（不送新欄位）",
                           exc_info=True)
        return rev


def _engine_for(rev: str) -> str:
    return _DIARIZE_ENGINE if jtlw_client.revision_at_least(rev, _ENGINE_MIN_REVISION) else "legacy"


def _speaker_engine(client) -> str:
    """這台語音服務會用哪一種發言者分離：`auto`（Nemotron）或 `legacy`（原本的方法）。

    **對 2.4 以前的服務送 `diarize_engine` 會被 400 退回**（對方的 `hints` 是
    `additionalProperties: false`），所以先問版本（`_api_revision`）。
    """
    return _engine_for(_api_revision(client))


def _speaker_engine_for_page() -> str:
    """給頁面決定問法用（有快取）。沒設定或連不上一律 `legacy`，跟送件時的判斷一致。"""
    if not jtlw_settings.is_configured():
        return "legacy"
    key = jtlw_settings.base_url()
    hit = _engine_cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < _ENGINE_CACHE_S:
        return hit[1]
    try:
        engine = _speaker_engine(jtlw_client.JtlwClient(timeout=_PAGE_CAPS_TIMEOUT_S))
    except jtlw_client.JtlwError:
        engine = "legacy"
    _engine_cache[key] = (now, engine)
    return engine


def _tasks_for(client, profile_id: str, wanted: list[str]) -> tuple[list[str], list[str]]:
    """依所選辨識模式**實際做得到的處理**決定要送哪些 `tasks`。回 (要送的, 拿掉的)。

    **能做什麼以對方的 `/profiles` 為準，不在我們這裡抄一份。**
    台語模式沒有發言者分離（`capabilities` 只有 `transcribe` / `correct`，對方 v2.15），
    而我們一律送 `diarize` —— 管理員在設定頁的下拉選了台語，之後**每一件**都被
    422 `task_not_supported` 退回。那個模式就擺在下拉裡，這不是邊角情況。

    讀不到清單、找不到這個模式、或它沒寫 `capabilities` 時**照原樣送**，由對方決定：
    猜錯的代價是安靜地少做一項處理（逐字稿沒有發言者，而且沒有任何錯誤訊息），
    比被退回一次更難發現。`transcribe` 永遠不拿掉 —— 連轉錄都不做的模式，
    送出去被退回才是對的。
    """
    wanted = list(wanted)
    try:
        rows = client.profiles()
    except Exception:                          # 連不上、權限不足、舊版服務沒有這支
        logger.warning("讀不到 JTLW 的辨識模式清單，照設定的處理送出：%s", wanted,
                       exc_info=True)
        return wanted, []
    prof = next((r for r in rows if isinstance(r, dict) and r.get("id") == profile_id), None)
    caps = prof.get("capabilities") if prof else None
    if not isinstance(caps, list) or not caps:
        return wanted, []
    keep = [t for t in wanted if t == "transcribe" or t in caps]
    return keep, [t for t in wanted if t not in keep]


#: 專有名詞 → 送件時帶給 JTLW 當 `glossary`（2026-10-02 使用者決定：專有名詞要拿來修錯字）。
#: 對方的契約（v0.4 / v0.5）：內嵌最多 500 筆、每筆 1~200 字；`mode: "keep"` 的詞**校正時保持原樣**，
#: 拼法相近的誤聽**校正時改成清單上的寫法**（對方 v2.27，`api_revision` 2.8，`punctuation_only` 也做）。
#:
#: **辨識時不參考這份清單**（對方 2026-10-03 實測後決定不做）：清單上的詞在那場會議沒出現時，
#: 辨識會憑空把它插進逐字稿（真實會議裡冒出十幾次「李主任」，還讓辨識慢 5.5 倍、段落數少一半），
#: 而且插在看起來很正常的位置 —— 比聽錯一個產品名更難發現。`glossary.asr_bias_terms` 照實回報：
#: 走 GPU 伺服器時是 0；只有 GPU 伺服器不能用、改在對方本機辨識時才大於 0（最多 50，照收到的順序取）。
#: 仍然照使用者輸入的順序送（本機辨識那條路看順序）。
MAX_TERMS = 500
#: 一行一個；也收頓號、逗號、分號（從會議背景或名單貼過來最常見的寫法）
_TERM_SPLIT = re.compile(r"[、，,；;]+")
#: 2026-10-03 使用者把這一欄改成「**專有名詞或會議背景**」—— 寫成句子的那幾行**不當成專有名詞送出**，
#: 只在轉送會議摘要時整段帶過去當會議背景。句子當成專有名詞的話，校正時「照原樣保留」一整句、
#: 拿一整句去比對拼法相近的寫法都沒有意義（對方本機辨識那條路還會把真正的人名擠出提示）。
#: 判斷一律以**行**為單位（一行要嘛是一串詞、要嘛是一句話），規則說得出來：
#: * 有句號、驚嘆號、問號 → 句子；
#: * 拆開之後有一段太長（超過 `MAX_TERM_LIKE` 字或 `MAX_TERM_CJK` 個漢字）→ 句子；
#: * `#` 開頭的是標題（從 Markdown 筆記貼過來的「# 2026/10/02」）→ 背景；
#: * 一個字母都沒有的那一段（日期、時間、純數字）不是專有名詞 → 不送。
#:   2026-10-03 正式機上「# 2026/10/02」真的被當成專有名詞送出去過。
_SENTENCE_END = re.compile(r"[。！？!?]")
#: 「與會者：王小明、Bianca」—— 冒號前面那個短標籤拿掉，後面照一串詞處理（`://` 不算）
_LINE_LABEL = re.compile(r"^\s*[^：:]{1,10}[：:](?!//)\s*")
_CJK_CHAR = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
MAX_TERM_LIKE = 40
MAX_TERM_CJK = 10
#: **已知的錯寫法**（JTLW v2.28，`api_revision` 2.9）：一行寫成「錯寫法 → 正確寫法」，
#: 對方在校正**之前**照表換掉 —— 確定性的、不經過模型、任何校正等級都做；`raw` 層不動、
#: `final` 層換掉。拼法差很多的誤聽（校正認不出來的）靠這個才改得回來。
#: 方向跟會議摘要的「自己加替換」一樣（左邊是逐字稿裡的、右邊是要換成的）。
#: 一行只能有**一個**箭頭（兩個以上多半是在寫流程，當背景）；`#` 開頭與句子照舊當背景。
_VARIANT_ARROW = re.compile(r"\s*(?:→|->|=>|⇒)\s*")
#: 對方的上限（v2.28）：每筆最多 20 個錯寫法、每個 2~200 字
MAX_VARIANTS = 20
VARIANT_MIN, VARIANT_MAX = 2, 200
TERM_MAX = 200
_VARIANTS_MIN_REVISION = (2, 9)
#: **JTLW 怎麼把一筆 `source` 拆成好幾個詞**（對方 2026-10-04 回覆 v1.16.49 給的規則）：
#: `、` `，` `,` `；` `;` `|` `｜` `／` 與換行一律拆；半形 `/` **一邊有空白就拆**（`TCP/IP`、`I/O` 不拆）；
#: 一般空白與括號不拆。拆完去掉頭尾的空白與引號、包住整個詞的那一對括號與落單的半邊括號，
#: **只剩一個字的不算**。照這個判斷才擋得到對方會退回的那幾種（他們的 `variants_need_single_term`、
#: `variant_is_a_glossary_term` 比的都是拆開之後的詞）。
_JTLW_SPLIT = re.compile(r"[、，,；;|｜／\n]|\s/|/\s")
_JTLW_QUOTES = "\"'「」『』"
_BRACKETS = {"(": ")", "（": "）", "[": "]", "【": "】"}
_CLOSERS = {v: k for k, v in _BRACKETS.items()}


def _balanced(s: str) -> bool:
    return all(s.count(o) == s.count(c) for o, c in _BRACKETS.items())


def _strip_brackets(p: str) -> str:
    """包住整個詞的那一對、與落單的半邊括號拿掉；詞裡成對的（`Proxmox (PVE)`）留著。"""
    p = p.strip()
    while p:
        o, c = p[0], p[-1]
        if o in _BRACKETS and c == _BRACKETS[o] and _balanced(p[1:-1]):
            p = p[1:-1].strip()
        elif o in _BRACKETS and p.count(o) > p.count(_BRACKETS[o]):
            p = p[1:].strip()
        elif c in _CLOSERS and p.count(c) > p.count(_CLOSERS[c]):
            p = p[:-1].strip()
        else:
            break
    return p


def _jtlw_pieces(term: str) -> list[str]:
    """一筆專有名詞在 JTLW 那邊會被拆成哪幾個詞（不分大小寫去重、只剩一個字的不算）。"""
    out: list[str] = []
    seen: set[str] = set()
    for piece in _JTLW_SPLIT.split(term):
        piece = _strip_brackets(piece.strip().strip(_JTLW_QUOTES).strip())
        if len(piece) > 1 and piece.casefold() not in seen:
            seen.add(piece.casefold())
            out.append(piece)
    return out


def _line_terms(line: str) -> list[str]:
    """一行 → 這一行裡的詞；是句子的話回空清單（那一行只當會議背景）。"""
    line = "".join(ch for ch in line if ch.isprintable())
    if not line.strip() or _SENTENCE_END.search(line) or line.lstrip().startswith("#"):
        return []
    # 有箭頭的行：剛好一個箭頭的「錯寫法 → 正確寫法」在 `_variant_line` 處理過了，
    # 走到這裡的是兩個以上（「上傳 → 轉檔 → 下載」這種流程）—— 背景，不可以整行當成一個詞送出
    if _VARIANT_ARROW.search(line):
        return []
    # 只有標籤的那一行（「與會人員如下:」）是標題，不是詞 —— 2026-10-03 正式機上真的被送出去過
    rest = _LINE_LABEL.sub("", line, count=1)
    out: list[str] = []
    for piece in _TERM_SPLIT.split(rest):
        term = " ".join(piece.split())
        if not term:
            continue
        if len(term) > MAX_TERM_LIKE or len(_CJK_CHAR.findall(term)) > MAX_TERM_CJK:
            return []                       # 有一段像句子 → 整行當背景，不拆一半送出去
        if not any(ch.isalpha() for ch in term):
            continue                        # 日期、時間、純數字（漢字算字母，`isalpha` 是 True）
        out.append(term)
    return out


def _raw_lines(raw: object) -> list[str]:
    lines: list[str] = []
    if isinstance(raw, (list, tuple)):
        for x in raw:
            if isinstance(x, str):
                lines += x.splitlines()
    elif isinstance(raw, str):
        lines = raw.splitlines()
    return lines


def _variant_line(line: str) -> Optional[tuple[str, list[str]]]:
    """「錯寫法 → 正確寫法」那一行 → `(正確寫法, [錯寫法…])`；不是這種行回 None。

    認得出是這種行、但寫得不對（右邊寫了好幾個、少了一邊、長度不對）時回 400 並講出是哪一行 ——
    **不可以安靜地當成背景**：使用者明明寫了一條替換，結果什麼都沒換，而畫面上看不出來。"""
    line = "".join(ch for ch in line if ch.isprintable()).strip()
    if (not line or line.startswith("#") or _SENTENCE_END.search(line)
            or len(_VARIANT_ARROW.findall(line)) != 1):
        return None
    left, right = _VARIANT_ARROW.split(line, maxsplit=1)
    target = " ".join(right.split())
    wrongs: list[str] = []
    for piece in _JTLW_SPLIT.split(left):
        w = " ".join(piece.split())
        if w and w.casefold() not in {x.casefold() for x in wrongs}:
            wrongs.append(w)
    if not target or not wrongs:
        raise HTTPException(400, f"「{line}」：箭頭左邊寫聽錯的寫法、右邊寫正確的寫法，兩邊都要有")
    # 「Proxmox VE / PVE」「Proxmox|PVE」也是好幾個詞（JTLW 會拆開、回 `variants_need_single_term`）——
    # 斜線一邊有空白就算，`TCP/IP` 這種是一個詞。**比對方嚴一點**：右邊有分隔就擋
    # （對方會把只剩一個字的那一段丟掉、照收；那種寫法多半是打錯，擋下來講清楚比較好）
    if _JTLW_SPLIT.search(target):
        raise HTTPException(400, f"「{line}」：箭頭右邊只能寫一個正確的寫法（不知道要換成哪一個）")
    if len(_jtlw_pieces(target)) != 1:
        raise HTTPException(400, f"「{line}」：箭頭右邊的正確寫法至少要兩個字")
    if len(target) > TERM_MAX:
        raise HTTPException(400, f"「{target[:20]}…」太長了（專有名詞最多 {TERM_MAX} 字）")
    for w in wrongs:
        if not VARIANT_MIN <= len(w) <= VARIANT_MAX:
            raise HTTPException(400, f"「{w[:20]}」：聽錯的寫法要 {VARIANT_MIN}～{VARIANT_MAX} 個字")
    return target, wrongs


def parse_glossary(raw: object) -> tuple[list[str], dict[str, list[str]]]:
    """使用者在「專有名詞或會議背景」裡寫的 → `(要送的詞, {詞: [已知的錯寫法…]})`。

    詞去掉重複、**保留順序**；寫成句子的行不送（見上面 `_SENTENCE_END` 那一段），整段原文另外存成
    會議背景（`context_text`）。錯寫法照 JTLW 的三條規則**先在這裡擋**（v2.28）：
    錯寫法剛好是清單上的另一個詞（或就是自己）、同一個錯寫法對到兩個詞、一個詞超過 20 個 ——
    擋在這裡才講得出是哪一行；送出去才被退回的話，使用者只看到「送件失敗」。
    詞超過上限時回 400、講出數字 —— **不可以安靜地截掉**。
    """
    out: list[str] = []
    seen: set[str] = set()
    variants: dict[str, list[str]] = {}

    def add(term: str) -> str:
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            out.append(term)
            return term
        return next(t for t in out if t.casefold() == key)

    for line in _raw_lines(raw):
        hit = _variant_line(line)
        if hit:
            term = add(hit[0])
            have = variants.setdefault(term, [])
            for w in hit[1]:
                if w.casefold() not in {x.casefold() for x in have}:
                    have.append(w)
            continue
        for term in _line_terms(line):
            add(term)
    if len(out) > MAX_TERMS:
        raise HTTPException(400, f"專有名詞最多 {MAX_TERMS} 個（目前 {len(out)} 個）")
    owner: dict[str, str] = {}
    # 「清單上的詞」照 JTLW 拆開之後算：另一行寫了 `Proxmox VE / PVE` 的話，`PVE` 也是清單上的詞
    listed = seen | {x.casefold() for t in out for x in _jtlw_pieces(t)}
    for term, wrongs in variants.items():
        if len(wrongs) > MAX_VARIANTS:
            raise HTTPException(400, f"「{term}」的聽錯寫法最多 {MAX_VARIANTS} 個（目前 {len(wrongs)} 個）")
        for w in wrongs:
            key = w.casefold()
            if key in listed:
                raise HTTPException(400, f"「{w}」也是清單上的專有名詞，不能同時當成聽錯的寫法"
                                         "（照表換會把寫對的換掉）")
            if key in owner and owner[key] != term:
                raise HTTPException(400, f"「{w}」同時寫成「{owner[key]}」與「{term}」的聽錯寫法，"
                                         "不知道要換成哪一個")
            owner[key] = term
    return out, {t: w for t, w in variants.items() if w}


def parse_terms(raw: object) -> list[str]:
    """只要詞（不含錯寫法）—— 同一套解析，錯寫法寫錯一樣會 400。"""
    return parse_glossary(raw)[0]


def context_text(raw: object) -> str:
    """「專有名詞或會議背景」的原文 —— 轉送會議摘要時帶過去當會議背景（不論送了哪些詞）。

    只留可列印字元與換行、長度跟會議摘要的背景上限一樣。"""
    if isinstance(raw, (list, tuple)):
        raw = "\n".join(x for x in raw if isinstance(x, str))
    if not isinstance(raw, str):
        return ""
    lines = []
    for ln in raw.splitlines():
        ln = "".join(ch for ch in ln if ch.isprintable()).rstrip()
        try:
            hit = _variant_line(ln)
        except HTTPException:
            hit = None
        # 「錯寫法 → 正確寫法」只把正確寫法帶過去 —— 錯寫法進了會議背景的話，
        # 會議摘要的替換建議會把它當成「背景就是這樣寫的」而不再提醒
        lines.append(hit[0] if hit else ln)
    return "\n".join(lines).strip()[:_mi.MAX_CONTEXT_CHARS]


def _build_body(upload_id: str, meta: dict, *, language: str,
                num_speakers: Optional[int],
                tasks: Optional[list[str]] = None,
                diarize_engine: Optional[str] = None,
                terms: Optional[list[str]] = None,
                variants: Optional[dict] = None) -> dict:
    base = jtlw_settings.audio_base_url()
    if not base:
        raise HTTPException(503, "還沒設定「錄音檔對外位址」—— "
                                 "對方要靠它回來拉錄音檔，請管理員到設定頁補上")
    cfg = jtlw_settings.get()
    if tasks is None:
        tasks = list(cfg.get("tasks") or jtlw_settings.DEFAULT_TASKS)
    body: dict = {
        "profile_id": cfg.get("profile_id") or jtlw_settings.DEFAULT_PROFILE,
        "tasks": list(tasks),
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
    # 人數與分離方法都是**發言者分離的**提示 —— 沒要求分離時送出去沒有意義
    hints: dict = {}
    if "diarize" in body["tasks"]:
        if num_speakers:
            hints["num_speakers"] = int(num_speakers)
        # `legacy` 就是不送（對方的預設），對舊版服務才不會因為多一個欄位被退回
        if diarize_engine and diarize_engine != "legacy":
            hints["diarize_engine"] = diarize_engine
    if hints:
        body["hints"] = hints
    if terms:
        body["glossary"] = _glossary(terms, variants)
    return body


def _glossary(terms: list[str], variants: Optional[dict] = None) -> dict:
    """送件與重跑校正共用（抄兩份的話一定會漂）。

    全部用 `keep`：使用者給的是「這個詞就是這樣寫」—— 校正時不可以改它。
    `variants`（已知的錯寫法）只在對方是 2.9 以上時才傳進來（`_variants_ok`）——
    舊版的每一筆是 `additionalProperties: false`，多一個欄位整件被退回。"""
    variants = variants or {}
    out = []
    for t in terms:
        e = {"source": t, "mode": "keep"}
        if variants.get(t):
            e["variants"] = list(variants[t])
        out.append(e)
    return {"entries": out}


def _variants_ok(rev: str) -> bool:
    return jtlw_client.revision_at_least(rev, _VARIANTS_MIN_REVISION)


def _assemble(client: jtlw_client.JtlwClient, remote_id: str,
              got: Optional[dict] = None, call=None) -> list[dict]:
    """把三層併成我們自己的 `{seq, text, speaker, start_ms, end_ms}`。

    **`raw` 有時間、`final` 有校正過的文字、`speakers` 有發言者** ——
    三層靠 `seq` 對起來（對方文件第 8 節）。
    **對不上的 seq 不可以硬湊**：寧可那一段沒有發言者，也不要把 A 的發言者
    貼到 B 的話上（同文件翻譯「段數對不上絕不硬湊」那條）。
    """
    got = got if got is not None else {}
    fetch = call or (lambda f: f())      # `_run_job` 傳進來的會撐過短暫斷線
    layers: dict[str, dict[int, dict]] = {}
    for layer in ("raw", "final", "speakers"):
        rows: dict[int, dict] = {}
        after = 0
        while True:
            try:
                page = fetch(lambda: client.segments(remote_id, layer,
                                                     after_seq=after, limit=500))
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


def _run_job(job, upload_id: str, language: str, num_speakers: Optional[int],
             terms: Optional[list[str]] = None, context: str = "",
             variants: Optional[dict] = None) -> None:
    meta = json.loads(_meta_path(upload_id).read_text(encoding="utf-8"))
    client = jtlw_client.JtlwClient()

    job.message = "送件中"
    job.progress = 0.02
    cfg = jtlw_settings.get()
    tasks, dropped = _tasks_for(
        client, cfg.get("profile_id") or jtlw_settings.DEFAULT_PROFILE,
        list(cfg.get("tasks") or jtlw_settings.DEFAULT_TASKS))
    if dropped:
        logger.info("辨識模式 %s 做不到 %s，這一件不送", cfg.get("profile_id"), dropped)
        job.meta["tasks_dropped"] = dropped
    # 介面版本只問一次（對方的 `/capabilities` 每次即時去問後端，各要幾秒）
    rev = _api_revision(client) if ("diarize" in tasks or variants) else ""
    engine = _engine_for(rev) if "diarize" in tasks else None
    # 錯寫法要 2.9 以上才送；舊版（或問不到版本）時只送詞，結果頁講出「錯寫法沒有送出」
    send_variants = bool(variants) and _variants_ok(rev)
    if variants and not send_variants:
        logger.info("JTLW 介面版本 %s 不收錯寫法（要 2.9 以上），這一件只送專有名詞", rev or "?")
    body = _build_body(upload_id, meta, language=language, num_speakers=num_speakers,
                       tasks=tasks, diarize_engine=engine, terms=terms,
                       variants=variants if send_variants else None)
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

    status, info = _wait_until_done(job, client, remote_id)
    if status in ("failed", "cancelled"):
        raise RuntimeError(_failure_text(info))
    _finish_job(job, client, upload_id, meta, remote_id, status, info,
                tasks=tasks, dropped=dropped, engine=engine, terms=terms, context=context,
                variants=variants, variants_sent=send_variants)


def _wait_until_done(job, client, remote_id: str, *, total_ms: Optional[float] = None,
                     cancel_remote: bool = True) -> tuple[str, dict]:
    """輪詢到對方的作業進入終態，回 `(status, info)`。送件與重跑校正共用。

    `cancel_remote=False`（重跑校正）：逾時或使用者停止時**只停止等待、不請對方取消**
    —— 那件作業的辨識早就做完了，取消的話對方那份逐字稿會變成「已取消」。
    """
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
            if cancel_remote:
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
            if cancel_remote:
                try:
                    client.cancel(remote_id)
                except jtlw_client.JtlwError:
                    logger.warning("取消 JTLW 作業 %s 失敗（已忽略）", remote_id, exc_info=True)
            raise RuntimeError("已取消")
        time.sleep(wait)
        wait = min(_POLL_MAX, wait * 1.5)
        info = _through_outage(job, client, remote_id, lambda: client.job(remote_id))
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
            return status, info


def _asr(summary: object) -> Optional[dict]:
    """對方 `Result.asr` → `{model, location, device}`（只留這三個、都是字串或 None）。

    舊版的對方沒有這個欄位、辨識失敗時是 null → 回 None。只收字串（限長）：
    對方的資料原樣存進逐字稿 JSON 的話，多出來的欄位與型別會一路帶進匯出與工作區。"""
    a = summary.get("asr") if isinstance(summary, dict) else None
    if not isinstance(a, dict) or not isinstance(a.get("model"), str) or not a["model"].strip():
        return None
    return {k: (a[k].strip()[:100] if isinstance(a.get(k), str) and a[k].strip() else None)
            for k in ("model", "location", "device")}


def _finish_job(job, client, upload_id: str, meta: dict, remote_id: str, status: str,
                info: dict, *, tasks: list[str], dropped: list[str],
                engine: Optional[str], terms: Optional[list[str]],
                context: str = "", variants: Optional[dict] = None,
                variants_sent: bool = False) -> None:
    job.message = "取回逐字稿"
    job.progress = 0.96
    got: dict[str, int] = {}
    segments = _assemble(client, remote_id, got,
                         call=lambda f: _through_outage(job, client, remote_id, f))
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
        # **這份逐字稿是在什麼條件下產生的**（2026-10-03 使用者問「有沒有記用什麼模型」）：
        # 辨識模式與它的版本（對方的 `Job.profile_id` / `profile_version`）。校正用的模型在
        # `summary.correction.model`、發言者分離實際用的方法在 `diarization.engine`。
        "profile": ({"id": info.get("profile_id"), "version": info.get("profile_version")}
                    if info.get("profile_id") else None),
        # 語音辨識用的模型（對方 `Result.asr`，`api_revision` 2.8 起；我們 2026-10-03 請對方加的）。
        # **只存不顯示、不送回去**（規格：一般使用者的畫面不顯示底層模型，送件只傳辨識模式）。
        "asr": _asr(summary),
        "status": status,
        "result": info.get("result") or {},
        "summary": summary,
        "uncorrected": uncorrected,
        # 伺服器端判斷要不要提示（同 `uncorrected`：前端不要自己猜門檻）
        "tail_gap_ms": tail_gap,
        "tail_hint": tail_gap is not None and tail_gap >= _TAIL_GAP_HINT_S * 1000,
        # 這次的辨識模式不做發言者分離 —— **要講出來**，不然逐字稿沒有發言者
        # 看起來像是分離失敗了（而其實根本沒做）
        "diarize_skipped": "diarize" in dropped,
        # 這一件要求的分離方法（`auto` / `legacy`），以及對方實際用了哪一種
        #（`Result.diarization`，api_revision 2.5 起才有）。`auto` 退回原本的方法時
        # 對方會在 `note` 寫原因（例如超過 8 人）—— 畫面要講出來，不然使用者會以為
        # 用的是 Nemotron。
        "speaker_engine": engine,
        "diarization": (summary.get("diarization")
                        if isinstance(summary.get("diarization"), dict) else None),
        "diarize_fallback": _diarize_fallback(engine, summary.get("diarization")),
        # 新方法 8 個位置用滿、照用新方法（api_revision 2.7）→ 提醒實際更多人時填人數重送
        "diarize_saturated": _diarize_saturated(engine, summary.get("diarization")),
        # 送出去的專有名詞，與對方回報實際怎麼用（`entries` / `keep_terms` / `asr_bias_terms`）
        "terms": list(terms or []),
        "glossary": info.get("glossary") if isinstance(info.get("glossary"), dict) else None,
        # 已知的錯寫法（`{詞: [錯寫法…]}`）與有沒有真的送出去（對方 2.9 以上才收）——
        # 重跑校正時要原樣帶回去（不然補一個詞，前一次寫的錯寫法就不見了）；
        # 換了幾處在 `summary.correction.variant_replacements`
        "variants": dict(variants or {}),
        "variants_sent": bool(variants_sent),
        # 「專有名詞或會議背景」的原文（含沒送去辨識的句子）—— 轉送會議摘要時帶進會議背景
        "context": context or "",
        "layers": got,
        # 送了哪些處理 —— 「能不能補專有名詞重跑校正」要看有沒有 `correct`
        "tasks": list(tasks),
        "segments": segments,
    }
    path = _out_path(upload_id)
    atomic_json.write_json(path, out)          # 內含 fsync

    # **ACK 只能在這之後** —— 上面那一行寫完（而且 fsync 過）才輪到它。
    #
    # **要過校正的作業先不 ACK**（v1.16.41）：留最多 24 小時給使用者補專有名詞、
    # 只重跑校正 —— ACK 之後對方的內容就清掉了，retry 會回 409 `content_cleared`。
    # 到期由 `jtlw_ack` 的巡檢送出；使用者按「不用再改了」就立刻送。
    # 沒有校正（`correct` 不在處理裡）就沒有東西可以重跑，照舊立刻 ACK ——
    # 那是會議內容，沒有理由在對方那邊多留。
    due = None
    if "correct" in tasks:
        try:
            due = jtlw_ack.defer(remote_id, upload_id)
        except OSError:
            logger.warning("記不下延後 ACK 的清單，改成立刻 ACK", exc_info=True)
            due = None
    out_acked = False
    if due is None:
        # 對方文件寫明可以重複呼叫，所以失敗了記一行就好，不要讓整件作業紅掉。
        try:
            client.ack(remote_id)
            out_acked = True
        except jtlw_client.JtlwError:
            logger.warning("ACK JTLW 作業 %s 失敗 —— 逐字稿已經存好了，"
                           "對方會在 7 天後自己清掉", remote_id, exc_info=True)
    job.meta["acked"] = out_acked
    if due is not None:
        job.meta["ack_due_at"] = due

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

# ------------------------------------------------------------------ 補專有名詞、只重跑校正

#: 重跑校正之前，離「請 JTLW 刪除」至少要剩這麼久。
#:
#: 對方只重跑校正（實測 5 分鐘的錄音 88 秒），長會議要多久沒量過。剩下的時間太短的話，
#: 重跑還沒做完就到了該刪的時間 —— 對方會以「還在處理」退回 ACK，要等重跑做完才送得出去，
#: 那就超過我們答應的 24 小時了。
_RETRY_MIN_LEFT_S = 15 * 60.0

#: 正在重跑校正的（upload_id → 作業編號）。**放記憶體就好**：服務重啟時作業本身也會
#: 被標成中斷，記在檔案裡的話反而會一直擋住下一次重跑。
_RETRYING: dict[str, str] = {}
_RETRY_LOCK = threading.Lock()

#: 不能重跑時給使用者的話（`retry_state.reason` → 訊息）。**一律講得出下一步。**
_RETRY_REFUSED = {
    "running": "retry_running",
    "no_correct": "retry_no_correct",
    "cleared": "content_cleared",
    "too_late": "retry_too_late",
}


def _retrying_job(upload_id: str) -> Optional[str]:
    """正在重跑的作業編號；那件作業其實已經結束（例如排隊時被取消、根本沒跑到）就清掉。"""
    with _RETRY_LOCK:
        jid = _RETRYING.get(upload_id)
    if not jid:
        return None
    if jid != "pending":
        j = job_manager.get(jid)
        if j is None or j.status not in ("pending", "running"):
            with _RETRY_LOCK:
                if _RETRYING.get(upload_id) == jid:
                    _RETRYING.pop(upload_id, None)
            return None
    return jid


def _retry_state(upload_id: str, data: dict) -> dict:
    """這份逐字稿現在能不能補專有名詞重跑校正 —— **判斷在伺服器端**，前端照著顯示。

    `reason`：`running` 正在重跑、`no_correct` 這次沒有校正這一步、
    `cleared` 已經請 JTLW 刪除（或從來沒有延後過）、`too_late` 離刪除不到 15 分鐘。
    """
    running = _retrying_job(upload_id)
    if running:
        return {"possible": False, "reason": "running",
                "job_id": None if running == "pending" else running}
    if "correct" not in (data.get("tasks") or []):
        return {"possible": False, "reason": "no_correct"}
    due = jtlw_ack.due_at(str(data.get("remote_job_id") or ""))
    if due is None:
        return {"possible": False, "reason": "cleared"}
    if due - time.time() < _RETRY_MIN_LEFT_S:
        return {"possible": False, "reason": "too_late", "due_at": due}
    return {"possible": True, "due_at": due}


def _merge_context(ctx: object, terms: list[str]) -> str:
    text = str(ctx or "").strip()
    low = text.casefold()
    for term in terms:
        if term.casefold() not in low:
            text = (text + "\n" + term).strip()
            low = text.casefold()
    return text[:_mi.MAX_CONTEXT_CHARS]


def _run_retry(job, upload_id: str, terms: list[str],
               variants: Optional[dict] = None) -> None:
    """補專有名詞、只重跑校正。

    對方只重跑校正：`raw` 與發言者逐段相同、`seq` 不變，`final` 換成新的結果。
    我們只換掉「跟校正有關的那幾個鍵」，**使用者改過的發言者名字**（另外存的鍵）不動 ——
    寫回去之前要重新讀一次檔案，重跑期間使用者可能又改了名字。
    """
    try:
        path = _out_path(upload_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        remote_id = str(data.get("remote_job_id") or "")
        client = jtlw_client.JtlwClient()
        job.message = "送出專有名詞"
        job.progress = 0.02
        send_variants = bool(variants) and _variants_ok(_api_revision(client))
        try:
            client.retry(remote_id, {"glossary": _glossary(
                terms, variants if send_variants else None)})
        except jtlw_client.JtlwError as exc:
            if exc.status == 409:
                if str((exc.details or {}).get("reason") or "") == "content_cleared":
                    # 對方那邊已經清掉了（到期、或別處送過 ACK）—— 我們的清單也拿掉
                    jtlw_ack.forget(remote_id)
                    raise RuntimeError(jtlw_client.MESSAGES["content_cleared"]) from exc
                raise RuntimeError(jtlw_client.MESSAGES["retry_refused"]) from exc
            raise
        total = (data.get("summary") or {}).get("duration_ms")
        status, info = _wait_until_done(
            job, client, remote_id,
            total_ms=float(total) if isinstance(total, (int, float)) and total > 0 else None,
            cancel_remote=False)
        if status in ("failed", "cancelled"):
            raise RuntimeError(_failure_text(info))
        job.message = "取回逐字稿"
        job.progress = 0.96
        got: dict[str, int] = {}
        segments = _assemble(client, remote_id, got,
                             call=lambda f: _through_outage(job, client, remote_id, f))
        if not segments:
            raise RuntimeError(jtlw_client.MESSAGES["empty_result"])
        try:
            summary = client.result(remote_id) or {}
        except jtlw_client.JtlwError:
            logger.warning("取 JTLW 作業 %s 的摘要失敗（重跑的逐字稿已取回）", remote_id,
                           exc_info=True)
            summary = {}

        fresh = json.loads(path.read_text(encoding="utf-8"))
        prev = fresh.get("retry") if isinstance(fresh.get("retry"), dict) else {}
        fresh.update({
            "segments": segments,
            "layers": got,
            "status": status,
            "uncorrected": status == "partially_succeeded" or not got.get("final"),
            "terms": list(terms),
            "variants": dict(variants or {}),
            "variants_sent": bool(send_variants),
            "glossary": (info.get("glossary") if isinstance(info.get("glossary"), dict)
                         else fresh.get("glossary")),
            # 補的詞也要跟著轉送會議摘要 —— 原本的背景留著，原文裡還沒有的詞接在後面
            "context": _merge_context(fresh.get("context"), terms),
            "retry": {"count": int(prev.get("count") or 0) + 1, "at": time.time()},
        })
        if summary:
            fresh["summary"] = summary
            # 只重跑校正不會重新辨識，對方的 `asr` 照理不變；拿不到時留著原本記的
            fresh["asr"] = _asr(summary) or fresh.get("asr")
        atomic_json.write_json(path, fresh)

        job.result_path = path
        job.result_filename = f"{Path(str((fresh.get('source') or {}).get('filename') or 'transcript')).stem}-逐字稿.json"
        job.meta["upload_id"] = upload_id
        job.meta["segments"] = len(segments)
        # 重跑拖到該刪的時間之後（或下一輪巡檢之前就到期）→ 現在就送，不等巡檢
        due = jtlw_ack.due_at(remote_id)
        if due is not None and due - time.time() <= jtlw_ack.SWEEP_S:
            jtlw_ack.ack_now(remote_id, client)
        job.progress = 1.0
        job.message = "完成"
    finally:
        with _RETRY_LOCK:
            _RETRYING.pop(upload_id, None)


@router.post("/retry")
async def retry(request: Request):
    """補專有名詞、只重跑校正（JTLW `POST /jobs/{id}/retry`）。回作業編號。

    `terms` 是**整份**專有名詞（不是只有新增的）。能不能重跑看 `_retry_state`。
    """
    if not jtlw_settings.is_configured():
        raise HTTPException(503, "還沒設定語音服務（JTLW）")
    body = await request.json() or {}
    upload_id = str(body.get("upload_id") or "").strip()
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    data = _read_json(_out_path(upload_id), "逐字稿")
    terms, variants = parse_glossary(body.get("terms"))
    if not terms:
        raise HTTPException(400, "請至少填一個專有名詞")
    state = _retry_state(upload_id, data)
    if not state["possible"]:
        raise HTTPException(409, jtlw_client.MESSAGES[_RETRY_REFUSED[state["reason"]]])
    with _RETRY_LOCK:
        if upload_id in _RETRYING:
            raise HTTPException(409, jtlw_client.MESSAGES["retry_running"])
        _RETRYING[upload_id] = "pending"
    try:
        filename = str((data.get("source") or {}).get("filename") or "")

        def run(job) -> None:
            _run_retry(job, upload_id, terms, variants)

        job = job_manager.submit(TOOL_ID, run, meta={"filename": filename, "retry": True,
                                                     "upload_id": upload_id},
                                 request=request)
    except BaseException:
        with _RETRY_LOCK:
            _RETRYING.pop(upload_id, None)
        raise
    with _RETRY_LOCK:
        if _RETRYING.get(upload_id) == "pending":
            _RETRYING[upload_id] = job.id
    job.meta["view_url"] = f"/tools/{TOOL_ID}/?job={job.id}"
    return {"job_id": job.id}


@router.post("/done")
async def done(request: Request):
    """不用再改了 → **立刻**請 JTLW 刪除它那份逐字稿（JTLW 的條件：不需要時立刻 ACK）。

    我們這邊的逐字稿不受影響。送不出去（連不上）時留在清單裡、下一輪巡檢再送，
    回 `acked: false`，而且從這一刻起就不能再重跑（使用者已經說不用了）。
    """
    body = await request.json() or {}
    upload_id = str(body.get("upload_id") or "").strip()
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    data = _read_json(_out_path(upload_id), "逐字稿")
    if _retrying_job(upload_id):
        raise HTTPException(409, jtlw_client.MESSAGES["retry_running"])
    remote_id = str(data.get("remote_job_id") or "")
    if jtlw_ack.due_at(remote_id) is None:
        return {"ok": True, "acked": True}
    ok = await asyncio.to_thread(jtlw_ack.ack_now, remote_id)
    return {"ok": True, "acked": bool(ok)}


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
    terms, variants = parse_glossary(body.get("terms"))   # 不合格的在送件前就講出來
    # 「專有名詞或會議背景」的原文 —— 轉送會議摘要時帶過去當會議背景（作業完成時寫進逐字稿；
    # 每一次送件帶自己的，所以重送時沒填就是空的，不會留著上一次的）
    ctx = context_text(body.get("terms"))

    def run(job) -> None:
        _run_job(job, upload_id, language, num_speakers, terms, ctx, variants)

    job = job_manager.submit(TOOL_ID, run,
                             meta={"filename": meta["filename"]}, request=request)
    job.meta["view_url"] = f"/tools/{TOOL_ID}/?job={job.id}"
    return {"job_id": job.id}


@router.post("/api/meeting-transcribe")
async def api_meeting_transcribe(request: Request,
                                 file: UploadFile = File(...),
                                 language: str = Form("auto"),
                                 num_speakers: str = Form("0"),
                                 terms: str = Form("")):
    """一次做完：上傳錄音 → 送件 → 等到好 → 回整份逐字稿。

    **這是同步的**：37 分鐘的會議實測 7~8 分鐘（辨識 ＋ 發言者分離 ＋ 校正），
    呼叫端的逾時要放寬。要背景處理請走網頁那條路
    （`/upload` → `/start` → 拿作業編號輪詢 `/api/jobs/{id}`）。

    **`num_speakers` 預設 0（讓對方自己判），而且建議就留 0。**
    它的意思跟著分離方法走（`_speaker_engine`）：Nemotron 下只當**上限**
    （寧可多填、不要少填），原本的方法下是「硬分成那麼多群」（只講一兩句的人不要算）。
    兩種方法下指定正確人數都只是「不輸」，理由在樣板那兩段註解裡。

    `terms`：專有名詞（一行一個，也收頓號 / 逗號 / 分號），送給對方當 `glossary`。
    """
    if not jtlw_settings.is_configured():
        raise HTTPException(503, "還沒設定語音服務（JTLW）—— 請管理員先到設定頁填好")
    term_list, variants = parse_glossary(terms)   # 不合格的在收檔之前就講出來
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
        _run_job(_Sync(), file_id, _normalize_language(language), n, term_list,
                 context_text(terms), variants)
    except jtlw_client.JtlwUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except jtlw_client.JtlwError as exc:
        # 呼叫端自己帶的參數被對方退回 → 他要改的是參數
        # 欄位可能是 `glossary.entries[3].variants` 這種 —— 看最前面那一段
        if re.split(r"[.\[]", exc.field or "")[0] in _CALLER_FIELDS:
            raise HTTPException(400, str(exc)) from exc
        raise HTTPException(502, str(exc)) from exc
    except TranscribeTimeout as exc:
        raise HTTPException(504, str(exc)) from exc
    except RuntimeError as exc:
        # 對方回報辨識失敗、結果是空的 —— 是上游的結果，不是我們壞了
        raise HTTPException(502, str(exc)) from exc
    out = _read_json(_out_path(file_id), "逐字稿")
    # 補專有名詞重跑校正（`POST /retry`）要帶 `upload_id`；`retry_until` 是最晚還能重跑的時間
    st = _retry_state(file_id, out)
    out["upload_id"] = file_id
    out["retry_until"] = st.get("due_at") if st.get("possible") else None
    return out


#: 同步 API 裡**呼叫端自己帶的**參數 —— 對方退回這幾個欄位時回 400。
_CALLER_FIELDS = frozenset({"language", "num_speakers", "glossary"})


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


def _peaks_path(upload_id: str) -> Path:
    # 檔名帶 upload_id → 保留期內的暫存清理認得它（`job_store.keep_alive_keys`）
    return settings.temp_dir / f"mt_{upload_id}_peaks.json"


@router.get("/peaks/{upload_id}")
async def peaks(upload_id: str, request: Request):
    """錄音的波形峰值，**WAV 由伺服器直接讀**（2026-10-03 使用者回報「波形只剩一條線」）。

    瀏覽器解碼要先下載整份、再把整份 PCM 放進記憶體 —— 一小時的 WAV 有 350 MB，
    畫面因此設了 60 MB 的門檻，超過就只剩一條時間軸。WAV 不需要解碼器，
    伺服器逐段讀、每段取最大值，記憶體跟檔案大小無關（`app/core/audio_peaks.py`）。

    其他格式回 `{"peaks": null}`，由瀏覽器照原本的方式處理。算過一次就存起來。
    """
    _sp.require_uuid_hex(upload_id, "upload_id")
    _uo.require(upload_id, request)
    p = _speech.audio_path(upload_id)
    if not p.is_file():
        raise HTTPException(404, "錄音檔已經被清掉了")
    cache = _peaks_path(upload_id)
    try:
        got = json.loads(cache.read_text(encoding="utf-8"))
        if isinstance(got, dict) and isinstance(got.get("peaks"), list):
            return {"peaks": got["peaks"]}
    except (OSError, ValueError):
        pass
    out = await asyncio.to_thread(_audio_peaks.wav_peaks, p)
    if out:
        try:
            atomic_json.write_json(cache, {"peaks": out})
        except OSError as e:
            logger.warning("waveform cache write failed: %s", e)
    return {"peaks": out}


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
    data = _read_json(_out_path(upload_id), "逐字稿")
    # 能不能補專有名詞重跑 —— 會隨時間變（到期、按了「不用再改了」），**不存進檔案**、每次現算
    data["retry_state"] = _retry_state(upload_id, data)
    # 發言者自己報過的名字（「我是 Wendy」）—— 只是建議，標籤上按一下才換；同樣每次現算、不存檔
    data["name_hints"] = _si.hints(data.get("segments") or [])
    return data
