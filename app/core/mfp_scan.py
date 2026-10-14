"""掃描作業：排隊 → 透過 eSCL 掃描 → 存進送出者的工作區。

* 每件掃描一條背景執行緒：先在 `mfp_queue` 拿這台設備的號碼牌（同一台一次只做一件，見那裡的說明），
  輪到才連設備；排隊中可以取消（不會碰到設備），掃描中取消會刪掉設備上的掃描工作。
* 紀錄存在 `mfp.sqlite` 的 `mfp_scans`（誰、哪一台、設定、頁數、大小、結果），進度另外放記憶體給畫面輪詢。
* 掃好的 PDF 存進**送出者自己**的工作區（`workspace.save_file_for_key`，路徑式、不整份讀進記憶體、
  驗證是打得開的 PDF）。工作區停用或額度不夠時，檔案留在暫存區 `RESULT_TTL` 秒給本人下載。
* 服務重啟時還在排隊或掃描中的，第一次用到這個模組時標成「中斷」（隊伍只存在記憶體裡）。
"""
from __future__ import annotations

import logging
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import mfp_escl, mfp_queue, mfp_store

logger = logging.getLogger("app.mfp_fetch")

#: 排隊最多等多久（秒）—— 前面的人都在掃很多頁時不要無限等。
MAX_QUEUE_WAIT = 30 * 60
#: 沒存進工作區的掃描檔在暫存區留多久（秒）。
RESULT_TTL = 24 * 3600

MESSAGES: dict[str, str] = {
    "SCAN_QUEUE_TIMEOUT": "排隊太久，已放棄這次掃描",
    "SCAN_INTERRUPTED": "服務重新啟動，這次掃描沒有完成",
    "SCAN_FAILED": "掃描失敗",
    "SCAN_NOT_FOUND": "找不到這次掃描",
    "SCAN_NO_FILE": "這次掃描沒有可以下載的檔案（已存進工作區，或已超過保留時間）",
    "WS_DISABLED": "工作區功能未啟用，掃描檔留在暫存區 24 小時，請下載",
    "WS_FAILED": "存進工作區失敗，掃描檔留在暫存區 24 小時，請下載",
}

_LOCK = threading.Lock()
#: scan_id → {"phase", "pages", "bytes", "ahead", "cancel": Event, "ticket"}
_LIVE: dict[str, dict] = {}
_INIT_DONE = False


def _init_once() -> None:
    global _INIT_DONE
    if _INIT_DONE:
        return
    with _LOCK:
        if _INIT_DONE:
            return
        try:
            mfp_store.mark_interrupted_scans()
        except Exception:  # noqa: BLE001 —— 標記失敗不影響新的掃描
            logger.warning("mfp：標記中斷的掃描失敗")
        _INIT_DONE = True


def _work_root() -> Path:
    from ..config import settings
    return settings.temp_dir / "mfp-scans"


_NAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def clean_file_name(name: str, fallback: str) -> str:
    """使用者填的檔名：去掉路徑與不能用的字元、補 `.pdf`、最多 120 字。"""
    s = _NAME_BAD.sub(" ", (name or "").strip()).strip(" .")
    if not s:
        s = fallback
    if s.lower().endswith(".pdf"):
        s = s[:-4].rstrip(" .") or fallback
    return s[:116] + ".pdf"


def start(*, owner: str, user_id: Optional[int], user_label: str, conn: dict, caps: dict,
          opts: dict, device: str, ip: str, scheme: str, port: int) -> dict:
    """排進這台的隊伍並開始背景掃描；回傳掃描紀錄。

    設定不在能力表裡丟 `mfp_escl.ScanError("SCAN_OPTION_INVALID")`；隊伍滿了丟 `mfp_queue.QueueError`
    —— 兩種都在排隊之前就檢查完，不會留下半套紀錄。"""
    _init_once()
    _xml, eff = mfp_escl.build_settings(caps, opts)          # 先驗設定（不合法就不排隊）
    # 走什麼協定也記下來給畫面顯示（使用者 2026-10-14：「掃描是走什麼協定 也要顯示」）
    via = {"protocol": "eSCL", "scheme": scheme, "port": int(port)}
    eff = dict(eff, via=via)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fallback = f"scan-{stamp}"
    file_name = clean_file_name(str(opts.get("file_name") or ""), fallback)
    ticket = mfp_queue.enqueue(device, owner, "scan")
    scan_id = secrets.token_hex(16)
    try:
        rec = mfp_store.create_scan(scan_id, conn_id=conn["id"], owner=owner, owner_user_id=user_id,
                                    device=device, conn_name=conn.get("display_name") or conn.get("host"),
                                    settings=eff, file_name=file_name)
    except BaseException:
        mfp_queue.done(ticket)
        raise
    cancel = threading.Event()
    with _LOCK:
        _LIVE[scan_id] = {"phase": "queued", "pages": 0, "bytes": 0,
                          "ahead": mfp_queue.position(ticket), "cancel": cancel, "ticket": ticket}
    th = threading.Thread(target=_run, name=f"mfp-scan-{scan_id[:8]}", daemon=True, kwargs=dict(
        scan_id=scan_id, ticket=ticket, cancel=cancel, user_id=user_id, user_label=user_label,
        host=conn["host"], ip=ip, scheme=scheme, port=port, caps=caps, opts=dict(opts, **eff),
        file_name=file_name, via=via))
    th.start()
    return rec


def _set(scan_id: str, **kw) -> None:
    with _LOCK:
        live = _LIVE.get(scan_id)
        if live is not None:
            live.update(kw)


def _run(*, scan_id: str, ticket, cancel: threading.Event, user_id: Optional[int], user_label: str,
         host: str, ip: str, scheme: str, port: int, caps: dict, opts: dict, file_name: str,
         via: dict) -> None:
    workdir = _work_root() / scan_id
    try:
        ok = mfp_queue.wait_turn(ticket, timeout=MAX_QUEUE_WAIT,
                                 on_wait=lambda ahead: _set(scan_id, ahead=ahead))
        if not ok:
            code = "SCAN_CANCELLED" if cancel.is_set() else "SCAN_QUEUE_TIMEOUT"
            mfp_store.finish_scan(scan_id, status="cancelled" if cancel.is_set() else "error",
                                  error_code=code)
            return
        _set(scan_id, phase="checking", ahead=0)
        mfp_store.update_scan(scan_id, status="running", started_at=time.time())
        try:
            res = mfp_escl.scan(host, ip, scheme, port, caps, opts, workdir,
                                on_progress=lambda d: _set(scan_id, **d),
                                is_cancelled=cancel.is_set)
        except mfp_escl.ScanError as e:
            mfp_store.finish_scan(scan_id, status="cancelled" if e.code == "SCAN_CANCELLED" else "error",
                                  error_code=e.code, error_detail=e.detail)
            return
        _set(scan_id, phase="saving", pages=res["pages"], bytes=res["bytes"])
        ws_id, ws_note, keep = _save_to_workspace(res["path"], file_name, user_id, user_label, scan_id)
        result_path = None
        if keep:
            result_path = str(res["path"])
        else:
            shutil.rmtree(workdir, ignore_errors=True)
        mfp_store.finish_scan(scan_id, status="done", pages=res["pages"], bytes=res["bytes"],
                              workspace_file_id=ws_id, workspace_note=ws_note, result_path=result_path,
                              settings=dict(res["settings"], via=via), tls_verified=res.get("tls_verified"))
    except Exception as e:  # noqa: BLE001 —— 背景執行緒：任何意外都要落成「失敗」，不可以停在掃描中
        logger.exception("mfp 掃描 %s 失敗", scan_id[:8])
        try:
            mfp_store.finish_scan(scan_id, status="error", error_code="SCAN_FAILED",
                                  error_detail={"error": e.__class__.__name__})
        except Exception:  # noqa: BLE001
            pass
    finally:
        mfp_queue.done(ticket)
        with _LOCK:
            _LIVE.pop(scan_id, None)
        rec = mfp_store.get_scan_any(scan_id)
        if rec and rec.get("status") != "done":
            shutil.rmtree(workdir, ignore_errors=True)


def _save_to_workspace(path: Path, file_name: str, user_id: Optional[int], user_label: str,
                       scan_id: str) -> tuple[Optional[str], Optional[str], bool]:
    """回傳（工作區檔案 id、沒存進去的原因代碼、要不要把暫存檔留著給人下載）。"""
    from . import workspace as ws
    try:
        key = ws.key_for_user_id(user_id)
        meta = ws.save_file_for_key(key, path, file_name, source_tool="mfp-fetch", user_label=user_label,
                                    verify=True, expect={".pdf"}, origin={"mfp_scan_id": scan_id})
        return str(meta.get("file_id")), None, False
    except ws.WorkspaceDisabled:
        return None, "WS_DISABLED", True
    except ws.WorkspaceError as e:
        logger.info("mfp 掃描 %s 存進工作區失敗：%s", scan_id[:8], e)
        return None, "WS_FAILED", True


def public(rec: dict) -> dict:
    """給畫面與 API 的掃描狀態（含排隊位置與即時進度）。"""
    out = {k: rec.get(k) for k in ("id", "conn_id", "conn_name", "status", "settings", "pages", "bytes",
                                   "file_name", "workspace_file_id", "workspace_note", "error_code",
                                   "error_detail", "created_at", "started_at", "finished_at", "tls_verified")}
    out["has_file"] = bool(rec.get("result_path")) and Path(rec["result_path"]).exists() and \
        (time.time() - (rec.get("finished_at") or 0)) < RESULT_TTL
    with _LOCK:
        live = _LIVE.get(rec["id"])
        if live:
            out.update({"phase": live.get("phase"), "ahead": live.get("ahead"),
                        "live_pages": live.get("pages"), "live_bytes": live.get("bytes")})
    code = out.get("error_code")
    out["error_message"] = (mfp_escl.MESSAGES.get(code) or mfp_queue.MESSAGES.get(code)
                            or MESSAGES.get(code) or code) if code else None
    note = out.get("workspace_note")
    out["workspace_message"] = MESSAGES.get(note) if note else None
    return out


def cancel(owner: str, scan_id: str) -> Optional[dict]:
    rec = mfp_store.get_scan(owner, scan_id)
    if not rec:
        return None
    with _LOCK:
        live = _LIVE.get(scan_id)
    if live and rec["status"] in ("queued", "running"):
        live["cancel"].set()
        mfp_queue.cancel(live["ticket"])
    return mfp_store.get_scan(owner, scan_id)


def result_file(owner: str, scan_id: str) -> Optional[tuple[Path, str]]:
    rec = mfp_store.get_scan(owner, scan_id)
    if not rec or not rec.get("result_path"):
        return None
    p = Path(rec["result_path"])
    root = _work_root().resolve()
    try:
        p.resolve().relative_to(root)           # 只給暫存區裡的檔（紀錄被竄改也不會指到別處）
    except ValueError:
        return None
    if not p.is_file() or time.time() - (rec.get("finished_at") or 0) >= RESULT_TTL:
        return None
    return p, rec.get("file_name") or "scan.pdf"


def prune(max_age: float = RESULT_TTL) -> int:
    """清掉超過保留時間的暫存掃描檔（排程清理會呼叫；列清單時也順手）。"""
    root = _work_root()
    if not root.is_dir():
        return 0
    n = 0
    now = time.time()
    with _LOCK:
        live = set(_LIVE)
    for d in root.iterdir():
        if d.name in live or not d.is_dir():
            continue
        try:
            if now - d.stat().st_mtime > max_age:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except OSError:
            pass
    return n


def _reset_for_tests() -> None:
    global _INIT_DONE
    with _LOCK:
        _LIVE.clear()
    _INIT_DONE = False
