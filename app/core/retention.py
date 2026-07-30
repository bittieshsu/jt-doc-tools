"""File retention settings + background sweeper.

Each retention category has a default in days (-1 = keep forever). The
sweeper runs at startup + every 6 hours, walking each category's storage
location and deleting entries older than the cutoff.

Categories:
  - fill_history       (data/fill_history/)
  - stamp_history      (data/stamp_history/)
  - watermark_history  (data/watermark_history/)
  - temp               (data/temp/) — short TTL (hours, not days)
  - jobs               (data/jobs/) — also short TTL
  - audit              (data/audit.sqlite — DELETE rows by ts)
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from . import audit_db, db, history_manager, sessions

logger = logging.getLogger(__name__)


_DEFAULTS: dict[str, Any] = {
    "fill_history_days":      365,
    "stamp_history_days":     365,
    "watermark_history_days": 365,
    "temp_hours":             2,        # data/temp/
    "jobs_hours":             24,       # data/jobs/
    # 工作紀錄（jobs.sqlite 的列，不是結果檔）。結果檔照 jobs_hours 清掉之後，
    # 紀錄仍保留一段時間，讓使用者在「我的工作」看得到做過什麼、管理員查得到
    # 誰在什麼時候轉了什麼。**這張表沒有人清就會無限長大** —— 8000 人規模下
    # 每天約 4 萬筆，一年就是 1,500 萬筆。
    "job_records_days":       30,
    "audit_days":             90,       # audit_events table rows
    "updated_at":             0.0,
}


def _path() -> Path:
    from ..config import settings
    return settings.data_dir / "retention.json"


_LOCK = threading.Lock()
_CACHE: dict[str, Any] | None = None


def get() -> dict[str, Any]:
    global _CACHE
    with _LOCK:
        if _CACHE is None:
            p = _path()
            if p.exists():
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    merged = json.loads(json.dumps(_DEFAULTS))
                    merged.update({k: v for k, v in raw.items() if k in _DEFAULTS})
                    _CACHE = merged
                except Exception:
                    _CACHE = json.loads(json.dumps(_DEFAULTS))
            else:
                _CACHE = json.loads(json.dumps(_DEFAULTS))
        return json.loads(json.dumps(_CACHE))


def save(new: dict[str, Any]) -> None:
    """Merge + persist. Any int field with value -1 means "no expiry"."""
    global _CACHE
    with _LOCK:
        merged = json.loads(json.dumps(_DEFAULTS))
        for k in _DEFAULTS:
            if k in new:
                if k == "updated_at":
                    continue
                v = new[k]
                if not isinstance(v, (int, float)):
                    raise ValueError(f"{k} 必須是數字")
                merged[k] = int(v)
        merged["updated_at"] = time.time()
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(p)
        _CACHE = merged


# ---------- stat collection (admin "目前佔用空間" display) ----------

def _dir_size(p: Path) -> int:
    if not p.exists():
        return 0
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def _oldest_entry_age_days(p: Path) -> float | None:
    """Walk subdirs of p (history layout), return oldest entry's age in days."""
    if not p.exists():
        return None
    oldest_ts = None
    for d in p.iterdir():
        if not d.is_dir():
            continue
        mf = d / "meta.json"
        if mf.exists():
            try:
                ts = json.loads(mf.read_text(encoding="utf-8")).get("saved_at")
                if ts and (oldest_ts is None or ts < oldest_ts):
                    oldest_ts = ts
            except Exception:
                pass
    if oldest_ts is None:
        return None
    return (time.time() - oldest_ts) / 86400.0


def collect_stats() -> dict[str, Any]:
    from ..config import settings as _s
    stats = {}
    for key, sub in [("fill_history", "fill_history"),
                     ("stamp_history", "stamp_history"),
                     ("watermark_history", "watermark_history"),
                     ("temp", "temp"), ("jobs", "jobs")]:
        d = _s.data_dir / sub
        stats[key] = {
            "size_mb": _dir_size(d) / 1024 / 1024,
            "oldest_days": _oldest_entry_age_days(d),
        }
    stats["audit"] = {
        "size_mb": db.db_size_bytes(audit_db.audit_db_path()) / 1024 / 1024,
        "oldest_days": _audit_oldest_days(),
    }
    stats["job_records"] = _job_records_stats()
    return stats


def _job_records_stats() -> dict[str, Any]:
    """jobs.sqlite 的大小與最舊一筆（給保留設定頁顯示）。"""
    from . import job_store
    try:
        size = db.db_size_bytes(job_store.db_path()) / 1024 / 1024
    except Exception:
        size = 0.0
    oldest = None
    try:
        row = db.fetchone(db.get_conn(job_store.db_path()),
                          "SELECT MIN(created_at) FROM jobs")
        if row and row[0]:
            oldest = (time.time() - float(row[0])) / 86400.0
    except Exception:
        pass
    return {"size_mb": size, "oldest_days": oldest}


def _audit_oldest_days() -> float | None:
    try:
        row = audit_db.conn().execute(
            "SELECT MIN(ts) FROM audit_events").fetchone()
        ts = row[0]
        if not ts:
            return None
        return (time.time() - ts) / 86400.0
    except Exception:
        return None


# ---------- sweepers ----------

def _sweep_temp_dir(seconds: int) -> int:
    if seconds <= 0:
        return 0
    from ..config import settings as _s
    cutoff = time.time() - seconds
    n = 0
    for sub in ("temp", "jobs"):
        d = _s.data_dir / sub
        if not d.exists():
            continue
        for child in d.iterdir():
            # `.owners/` is a special dir for upload-owner ACL sidecars
            # — sweep individual records inside it (the dir itself stays
            # fresh as long as new uploads are happening).
            if child.is_dir() and child.name == ".owners":
                for owner_file in child.iterdir():
                    try:
                        if owner_file.stat().st_mtime < cutoff:
                            owner_file.unlink(missing_ok=True)
                            n += 1
                    except OSError:
                        pass
                continue
            try:
                if child.stat().st_mtime < cutoff:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
                    n += 1
            except OSError:
                pass
    return n


def _sweep_audit(days: int) -> int:
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    conn = audit_db.conn()
    with db.tx(conn):
        cur = conn.execute("DELETE FROM audit_events WHERE ts < ?", (cutoff,))
    return cur.rowcount


_DB_BACKUP_INTERVAL = 20 * 3600      # 每天一份（略小於 24h，避開排程漂移）


def _maybe_backup_dbs() -> dict:
    """距離上一份備份超過一天才做，否則跳過（排程每 6 小時跑一次）。"""
    from . import db_health
    newest = 0.0
    for m in db_health.MANAGED:
        if not m["backup"]:
            continue
        for b in db_health.list_backups(m["file"])[:1]:
            try:
                newest = max(newest, b.stat().st_mtime)
            except OSError:
                pass
    if newest and (time.time() - newest) < _DB_BACKUP_INTERVAL:
        return {"skipped": "備份仍在有效期內"}
    return db_health.backup_all()


def _sweep_job_records(days: int) -> int:
    """清掉舊的工作紀錄列（結果檔另由 _sweep_temp_dir 依 jobs_hours 處理）。

    只清已結束的 —— 正在跑或排隊中的不管多舊都不能刪掉，否則使用者的工作會
    在進行中從清單上消失。
    """
    if days <= 0:
        return 0
    from . import job_store
    return job_store.delete_older_than(time.time() - days * 86400)


def sweep_all() -> dict[str, Any]:
    """Run every sweeper once, return a report dict."""
    s = get()
    report: dict[str, Any] = {}
    report["fill"] = history_manager.history_manager.sweep_older_than(
        s["fill_history_days"] * 86400 if s["fill_history_days"] > 0 else 0)
    report["stamp"] = history_manager.stamp_history.sweep_older_than(
        s["stamp_history_days"] * 86400 if s["stamp_history_days"] > 0 else 0)
    report["watermark"] = history_manager.watermark_history.sweep_older_than(
        s["watermark_history_days"] * 86400 if s["watermark_history_days"] > 0 else 0)
    # temp_hours is in HOURS not days
    report["temp"] = _sweep_temp_dir(s["temp_hours"] * 3600
                                     if s["temp_hours"] > 0 else 0)
    # Workspace has its own settings file (data/workspace.json); retention is
    # in hours, -1 = keep forever.
    try:
        from . import workspace as _ws
        ws_hours = int(_ws.get_settings().get("retention_hours", -1))
        report["workspace"] = _ws.sweep_older_than(
            ws_hours * 3600 if ws_hours > 0 else 0)
    except Exception:
        logger.exception("workspace sweep failed")
    report["audit"] = _sweep_audit(s["audit_days"])
    report["job_records"] = _sweep_job_records(s["job_records_days"])
    # 資料庫熱備份。掛在既有的 6 小時排程上（而不是另開一個排程執行緒），並用
    # 時間戳自己節流成每天一份 —— 備份是**遇到磁碟毀損時唯一的救命索**，寧可
    # 跟著清理一起跑也不要另外多一個可能沒起來的背景執行緒。
    try:
        report["db_backup"] = _maybe_backup_dbs()
    except Exception:
        logger.exception("database backup failed")
    # Expired sessions
    report["sessions"] = sessions.cleanup_expired()
    logger.info("retention sweep report: %s", report)
    return report


# ---------- background scheduler ----------

_SCHED_THREAD: threading.Thread | None = None
_SCHED_STOP = threading.Event()
_INTERVAL = 6 * 3600   # every 6h


def start_scheduler() -> None:
    global _SCHED_THREAD
    with _LOCK:
        if _SCHED_THREAD is not None and _SCHED_THREAD.is_alive():
            return
        _SCHED_STOP.clear()
        _SCHED_THREAD = threading.Thread(
            target=_loop, name="retention-sweeper", daemon=True,
        )
        _SCHED_THREAD.start()


def stop_scheduler() -> None:
    _SCHED_STOP.set()
    if _SCHED_THREAD is not None:
        _SCHED_THREAD.join(timeout=5)


def _loop() -> None:
    # Run once immediately on startup
    try:
        sweep_all()
    except Exception:
        logger.exception("initial sweep failed")
    while not _SCHED_STOP.is_set():
        if _SCHED_STOP.wait(_INTERVAL):
            break
        try:
            sweep_all()
        except Exception:
            logger.exception("scheduled sweep failed")
