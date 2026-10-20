"""一次性傳輸票證：讓 AI 代理自己用 `curl` 把檔案傳進 / 傳出工作區，**檔案內容不經過模型**。

規則（MCP 規劃 v1 第 11 節）：

* 票證放在標頭 `X-JTDT-Ticket`，**不放網址**（網址會進反代的存取記錄）。
* `secrets.token_urlsafe(32)`；資料庫只存 SHA-256，比對用 `compare_digest`（同 API Token）。
* 綁定：工作區（使用者）、用途（upload / download）、下載綁檔案編號、上傳綁大小上限與（選填）雜湊。
* 15 分鐘有效。上傳**成功存好才算用掉**（斷線可以用同一張重傳）；用掉之後再送回 409 ＋ 原本的檔案編號。
  下載在效期內最多 5 次（續傳）。
* 可選：綁發出時的來源 IP。
* 存在 SQLite（`data/mcp.sqlite`），服務重啟後未過期的照樣能用。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from ..config import settings as _app_settings

TTL = 15 * 60
DOWNLOAD_MAX_USES = 5
MAX_ACTIVE_PER_KEY = 20

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_conn_path: Optional[Path] = None


def _db() -> sqlite3.Connection:
    """資料目錄換了（測試）就重開。"""
    global _conn, _conn_path
    path = _app_settings.data_dir / "mcp.sqlite"
    if _conn is None or _conn_path != path:
        path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE IF NOT EXISTS tickets (
            hash TEXT PRIMARY KEY, purpose TEXT NOT NULL, ws_key TEXT NOT NULL, user_id INTEGER,
            user_label TEXT NOT NULL DEFAULT '', token_label TEXT NOT NULL DEFAULT '',
            file_id TEXT, filename TEXT, max_bytes INTEGER NOT NULL DEFAULT 0, sha256 TEXT,
            ip TEXT NOT NULL DEFAULT '', created REAL NOT NULL, expires REAL NOT NULL,
            uses INTEGER NOT NULL DEFAULT 0, done_file_id TEXT)""")
        _conn, _conn_path = c, path
    return _conn


def _digest(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def _purge(c: sqlite3.Connection, now: float) -> None:
    c.execute("DELETE FROM tickets WHERE expires < ?", (now - 60,))


class TicketError(Exception):
    def __init__(self, code: str, status: int = 403, detail: Optional[dict] = None):
        super().__init__(code)
        self.code, self.status, self.detail = code, status, detail or {}


def issue(purpose: str, *, ws_key: str, user_id: Optional[int], user_label: str, token_label: str,
          ip: str, file_id: Optional[str] = None, filename: str = "", max_bytes: int = 0,
          sha256: Optional[str] = None) -> str:
    """發一張票證，回原文（只在這一次出現）。同一個工作區同時有效的票證有上限。"""
    if purpose not in ("upload", "download"):
        raise ValueError("purpose")
    now = time.time()
    t = secrets.token_urlsafe(32)
    with _lock:
        c = _db()
        _purge(c, now)
        n = c.execute("SELECT COUNT(*) FROM tickets WHERE ws_key=? AND expires>=? AND done_file_id IS NULL",
                      (ws_key, now)).fetchone()[0]
        if n >= MAX_ACTIVE_PER_KEY:
            raise TicketError("TOO_MANY_TICKETS", 429, {"limit": MAX_ACTIVE_PER_KEY})
        c.execute("INSERT INTO tickets (hash, purpose, ws_key, user_id, user_label, token_label, file_id, "
                  "filename, max_bytes, sha256, ip, created, expires) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (_digest(t), purpose, ws_key, user_id, user_label[:120], token_label[:64], file_id,
                   filename[:200], int(max_bytes), (sha256 or None), ip[:64], now, now + TTL))
    return t


def take(presented: str, purpose: str, *, ip: str, bind_ip: bool) -> dict:
    """驗票證（不改狀態）。錯的、過期的、用途不對的、IP 不符的一律 403，不說是哪一種（避免被試探）。"""
    if not presented or len(presented) > 200:
        raise TicketError("TICKET_INVALID")
    want = _digest(presented)
    now = time.time()
    with _lock:
        c = _db()
        row = c.execute("SELECT * FROM tickets WHERE hash=?", (want,)).fetchone()
    if row is None or not hmac.compare_digest(row["hash"], want):
        raise TicketError("TICKET_INVALID")
    if row["purpose"] != purpose or row["expires"] < now:
        raise TicketError("TICKET_INVALID")
    if bind_ip and row["ip"] and ip and row["ip"] != ip:
        raise TicketError("TICKET_INVALID", detail={"reason": "ip"})
    if purpose == "download" and row["uses"] >= DOWNLOAD_MAX_USES:
        raise TicketError("TICKET_INVALID", detail={"reason": "uses"})
    return dict(row)


def upload_done(row: dict, file_id: str) -> None:
    with _lock:
        _db().execute("UPDATE tickets SET done_file_id=?, uses=uses+1 WHERE hash=? AND done_file_id IS NULL",
                      (file_id, row["hash"]))


def download_used(row: dict) -> None:
    with _lock:
        _db().execute("UPDATE tickets SET uses=uses+1 WHERE hash=?", (row["hash"],))


def _reset_for_tests() -> None:
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn, _conn_path = None, None
