"""Scheduled AD / LDAP directory sync into the local cache.

Why: the 群組管理 page used to fire **one live LDAP query per group row** on
every load (to show each group's real member count). With thousands of groups
that is thousands of round-trips → the page "等很久". This module mirrors the
directory groups into the local `groups` table and **caches each group's member
count** there, so the page reads the local DB (milliseconds) and never touches
LDAP on load.

Runs once at startup + every N hours (configurable), and can be triggered
manually from the admin UI ("立即同步"). Only does anything when the auth
backend is `ldap` / `ad`.

Settings live in `data/directory_sync.json`:
    { enabled, interval_hours, name_contains, last_run_at, last_result }
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_SCHED_THREAD: Optional[threading.Thread] = None
_SCHED_STOP = threading.Event()
_RUN_LOCK = threading.Lock()          # never run two syncs at once
_running = False

_DEFAULTS = {
    "enabled": True,
    "interval_hours": 6,
    "name_contains": "",              # optional filter to skip system groups
    "sync_users": False,             # also mirror all directory users → local
                                     # (catalog only; opt-in — off by default)
    "last_run_at": None,              # epoch seconds
    "last_result": None,             # dict from the last run
    "last_error": None,
    # 上一次**完整**使用者掃描的時間。判定「這個帳號在目錄裡已經找不到」只能
    # 相對於一次完整掃描 —— 帶名稱過濾的同步只看得到一部分目錄，拿它當基準會把
    # 整個組織誤標成離職。沒有值就代表這個功能還不能下任何結論。
    "last_full_scan_at": None,
    "last_history": [],              # 最近幾次的結果（見 _HISTORY_KEEP）
}


# --------------------------------------------------------------------- settings

def _path():
    from ..config import settings
    return settings.data_dir / "directory_sync.json"


def get_settings() -> dict[str, Any]:
    p = _path()
    data = dict(_DEFAULTS)
    try:
        if p.exists():
            data.update(json.loads(p.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        logger.warning("directory_sync settings unreadable; using defaults")
    # clamp interval to a sane range (1h .. 7d)
    try:
        data["interval_hours"] = max(1, min(168, int(data.get("interval_hours", 6))))
    except Exception:  # noqa: BLE001
        data["interval_hours"] = 6
    return data


def save_settings(*, enabled: Optional[bool] = None,
                  interval_hours: Optional[int] = None,
                  name_contains: Optional[str] = None,
                  sync_users: Optional[bool] = None) -> dict[str, Any]:
    data = get_settings()
    if enabled is not None:
        data["enabled"] = bool(enabled)
    if interval_hours is not None:
        data["interval_hours"] = max(1, min(168, int(interval_hours)))
    if name_contains is not None:
        data["name_contains"] = str(name_contains).strip()[:128]
    if sync_users is not None:
        data["sync_users"] = bool(sync_users)
    _write(data)
    return data


def _write(data: dict[str, Any]) -> None:
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(p)
    except Exception:  # noqa: BLE001
        logger.exception("failed to persist directory_sync settings")


# ------------------------------------------------------------------- the sync

def is_directory_backend() -> bool:
    try:
        from . import auth_settings
        return (auth_settings.get() or {}).get("backend", "off") in ("ldap", "ad")
    except Exception:  # noqa: BLE001
        return False


def run_sync(name_contains: Optional[str] = None) -> dict[str, Any]:
    """Mirror directory groups + cache each group's member count. Returns a
    report dict. Safe to call from the scheduler or a manual trigger; a second
    concurrent call returns ``{"skipped": "already running"}``."""
    global _running
    if not is_directory_backend():
        return {"skipped": "backend is not ldap/ad"}
    if not _RUN_LOCK.acquire(blocking=False):
        return {"skipped": "already running"}
    _running = True
    started = time.time()
    from . import auth_ldap, auth_db
    settings_now = get_settings()
    if name_contains is None:
        name_contains = settings_now.get("name_contains", "") or ""
    report: dict[str, Any] = {"groups_mirrored": None, "counts_updated": 0,
                              "counts_failed": 0, "users_synced": None,
                              "started_at": started}
    try:
        # 1) mirror the directory group list into the local table
        mirror = auth_ldap.sync_all_groups(name_contains=name_contains)
        report["groups_mirrored"] = mirror
        # 2) cache each ldap/ad group's real member count
        conn = auth_db.conn()
        rows = conn.execute(
            "SELECT id, name, external_dn FROM groups "
            "WHERE source IN ('ldap','ad') AND external_dn<>''"
        ).fetchall()
        for r in rows:
            dn = (r["external_dn"] or "").strip()
            if not dn:
                continue
            try:
                n = auth_ldap.count_group_members(dn)
                conn.execute(
                    "UPDATE groups SET member_count=?, member_count_synced_at=? "
                    "WHERE id=?", (int(n), time.time(), r["id"]))
                report["counts_updated"] += 1
            except Exception as exc:  # noqa: BLE001
                # 只數不記的話，同步失敗時管理員只看得到「失敗 37 筆」，
                # 完全不知道是哪些群組、什麼原因（原本連 log 都沒寫）。
                report["counts_failed"] += 1
                detail = report.setdefault("failed_detail", [])
                if len(detail) < 20:
                    detail.append({"group": r["name"] if "name" in r.keys() else "",
                                   "dn": dn[:200],
                                   "error": f"{type(exc).__name__}: {exc}"[:200]})
                logger.warning("群組成員數同步失敗 %s：%s", dn, exc)
        conn.commit()
        # 3) mirror all directory users into the local users table (so 使用者管理
        #    shows everyone + admin can pre-assign roles). Best-effort — a user
        #    sync failure must not lose the group results already committed.
        if settings_now.get("sync_users", True):
            try:
                report["users_synced"] = auth_ldap.sync_all_users(
                    name_contains=name_contains)
            except Exception as uexc:  # noqa: BLE001
                report["users_synced"] = {"error": f"{type(uexc).__name__}: {uexc}"}
                logger.exception("user sync failed (group sync kept)")
        report["elapsed_sec"] = round(time.time() - started, 1)
        _stamp(ok=True, result=report)
        logger.info("directory sync done: %s", report)
        _notify_if_degraded(report)
        return report
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["elapsed_sec"] = round(time.time() - started, 1)
        _stamp(ok=False, result=report, error=report["error"])
        _notify_if_degraded(report, error=report["error"])
        logger.exception("directory sync failed")
        return report
    finally:
        _running = False
        _RUN_LOCK.release()


#: 保留最近幾次的同步結果。只留「上一次」的話，看不出「從什麼時候開始失敗的」
#: —— 而那正是排查同步問題時第一個要回答的。
_HISTORY_KEEP = 20


def _notify_if_degraded(report: dict, *, error: Optional[str] = None) -> None:
    """同步整個失敗、或大量子項失敗時通知管理員。

    原本同步壞掉**不通知任何人** —— 要發現它壞了，必須有人主動去開群組管理頁看
    那一行字。service account 密碼一過期就是全公司登不進來，而系統既不主動通知、
    事後也查不到記錄。

    **絕不丟例外**：通知寄不出去不該把一次成功的同步標記成失敗。
    """
    try:
        failed = int((report or {}).get("counts_failed") or 0)
        users = (report or {}).get("users_synced")
        user_err = users.get("error") if isinstance(users, dict) else None
        if not error and not user_err and failed == 0:
            return
        lines = ["目錄同步出現問題："]
        if error:
            lines.append(f"• 整體失敗：{error}")
        if user_err:
            lines.append(f"• 使用者同步失敗：{user_err}")
        if failed:
            lines.append(f"• 群組成員數同步失敗 {failed} 筆")
            for d in (report.get("failed_detail") or [])[:3]:
                lines.append(f"    - {d.get('group') or d.get('dn')}：{d.get('error')}")
        lines.append("詳情請看「群組管理」頁的同步狀態。")
        from . import notify_channels, notify_settings
        cfg = notify_settings.get(reveal=True)
        if not cfg.get("enabled"):
            return
        notify_channels.broadcast(cfg, notify_settings.enabled_channels(),
                                  "目錄同步異常", "\n".join(lines))
    except Exception as exc:  # noqa: BLE001
        logger.warning("目錄同步異常通知寄送失敗：%s", exc)


def _stamp(*, ok: bool, result: dict, error: Optional[str] = None) -> None:
    data = get_settings()
    now = time.time()
    data["last_run_at"] = now
    data["last_result"] = result
    data["last_error"] = None if ok else error
    # 完整掃描才更新基準時間（見 last_full_scan_at 的說明）
    us = (result or {}).get("users_synced")
    if isinstance(us, dict) and us.get("full_scan") and us.get("scanned_at"):
        data["last_full_scan_at"] = us["scanned_at"]
    hist = list(data.get("last_history") or [])
    hist.insert(0, {
        "at": now, "ok": ok, "error": error,
        # 只留摘要，不要把整包結果都塞進歷史（設定檔會越長越大）
        "groups_mirrored": (result or {}).get("groups_mirrored"),
        "counts_updated": (result or {}).get("counts_updated"),
        "counts_failed": (result or {}).get("counts_failed"),
        "failed_detail": ((result or {}).get("failed_detail") or [])[:5],
        "users_synced": us if isinstance(us, dict) else None,
        "elapsed_sec": (result or {}).get("elapsed_sec"),
    })
    data["last_history"] = hist[:_HISTORY_KEEP]
    _write(data)


def is_running() -> bool:
    return _running


# ------------------------------------------------------------------ scheduler

def start_scheduler() -> None:
    global _SCHED_THREAD
    with _LOCK:
        if _SCHED_THREAD is not None and _SCHED_THREAD.is_alive():
            return
        _SCHED_STOP.clear()
        _SCHED_THREAD = threading.Thread(
            target=_loop, name="directory-sync", daemon=True)
        _SCHED_THREAD.start()


def stop_scheduler() -> None:
    _SCHED_STOP.set()
    if _SCHED_THREAD is not None:
        _SCHED_THREAD.join(timeout=5)


def _loop() -> None:
    # Slight startup delay so we don't pile onto the boot sequence; the pages
    # read whatever is already cached until the first run completes.
    if _SCHED_STOP.wait(30):
        return
    while not _SCHED_STOP.is_set():
        try:
            s = get_settings()
            if s.get("enabled") and is_directory_backend():
                run_sync()
        except Exception:  # noqa: BLE001
            logger.exception("scheduled directory sync failed")
        interval = max(1, int(get_settings().get("interval_hours", 6))) * 3600
        if _SCHED_STOP.wait(interval):
            break
