"""Session token issue / verify / revoke.

Cookie value = 256-bit random token (`secrets.token_urlsafe(32)`). The DB
stores ``sha256(token)`` so a DB breach can't directly resume sessions.

Cookie attributes (set by the route layer, not here):
    HttpOnly = True              prevent JS access
    SameSite = Lax               default; CSRF protection on top-level POSTs
    Secure   = (request scheme == 'https' or X-Forwarded-Proto == 'https')

Lifetime: 7 days default, 30 days when "remember me" checked.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import Optional

from . import auth_db, auth_settings, db

logger = logging.getLogger(__name__)


COOKIE_NAME = "jtdt_session"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(user_id: int, *, remember: bool, ip: str = "", ua: str = "") -> tuple[str, float]:
    """Create a new session row, return (raw_token, expires_at)."""
    s = auth_settings.get()
    days = s["remember_max_age_days"] if remember else s["session_max_age_days"]
    now = time.time()
    expires_at = now + days * 86400
    raw = secrets.token_urlsafe(32)   # 256 bits of entropy
    th = _hash(raw)
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, created_at, expires_at, "
            "remember, ip, user_agent) VALUES (?,?,?,?,?,?,?)",
            (th, user_id, now, expires_at, 1 if remember else 0,
             (ip or "")[:64], (ua or "")[:256]),
        )
    return raw, expires_at


def lookup(raw_token: str) -> Optional[dict]:
    """Return user dict if session valid, None otherwise. Touches expires_at
    purely on read so we don't extend lifetime sliding-window style — sessions
    have a fixed expiry from issue time (simpler reasoning, easier audit)."""
    if not raw_token:
        return None
    th = _hash(raw_token)
    conn = auth_db.conn()
    row = conn.execute(
        "SELECT s.user_id, s.expires_at, u.username, u.display_name, "
        "       u.source, u.enabled, u.is_admin_seed, u.email "
        "FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ?",
        (th,),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] < time.time():
        # Expired — clean up opportunistically.
        revoke(raw_token)
        return None
    if not row["enabled"]:
        # Account disabled while session was alive — drop the session too.
        revoke(raw_token)
        return None
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "source": row["source"],
        "is_admin_seed": bool(row["is_admin_seed"]),
        # 作業完成通知的預設收件信箱（AD / LDAP / SSO 帶進來，或本人自己填）
        "email": row["email"] or "",
    }


def user_label(user: Optional[dict]) -> str:
    """Format a session-user dict as `username@realm` for audit / history /
    UI display. Same name `jason` may exist in both `local` and `ldap`
    realms, so the realm suffix is essential to know who acted.

    Returns "" if user is None / lacks expected fields. Empty source
    falls back to plain username (back-compat for old session shapes /
    callers that pass partial dicts)."""
    if not user:
        return ""
    if isinstance(user, dict):
        username = user.get("username") or ""
        source = user.get("source") or ""
    else:
        username = getattr(user, "username", "") or ""
        source = getattr(user, "source", "") or ""
    if not username:
        return ""
    return f"{username}@{source}" if source else username


def revoke(raw_token: str) -> None:
    """Delete the session row matching this token (idempotent)."""
    if not raw_token:
        return
    th = _hash(raw_token)
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (th,))


def revoke_all_for_user(user_id: int) -> int:
    """Revoke every session belonging to a user (e.g. on password change /
    role change). Returns number of rows removed."""
    conn = auth_db.conn()
    with db.tx(conn):
        cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return cur.rowcount


def cleanup_expired() -> int:
    """Drop any session past its expires_at. Called by retention sweep."""
    now = time.time()
    conn = auth_db.conn()
    with db.tx(conn):
        cur = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    return cur.rowcount


# --------------------------------------------------------------------------
# 在線狀態
# --------------------------------------------------------------------------

#: 多久沒有活動就不算「在線」（秒）。
#: 15 分鐘是常見的閒置判定 —— 比它短會讓只是去開會的人一直閃掉。
ONLINE_WINDOW_SECONDS = 15 * 60

#: `last_seen_at` 至少隔多久才寫一次 DB（秒）。
#: 每個請求都寫會變成每請求一次寫入競爭（WAL 下仍是單一 writer）；
#: 對「最近有沒有活動」的判斷來說，60 秒的解析度綽綽有餘。
_TOUCH_THROTTLE_SECONDS = 60


def touch(raw_token: str) -> None:
    """更新這個 session 的最後活動時間（節流）。

    **絕不丟例外** —— 這是在每個請求的路徑上，記錄失敗不該讓請求失敗。
    """
    if not raw_token:
        return
    try:
        now = time.time()
        conn = auth_db.conn()
        with db.tx(conn):
            conn.execute(
                "UPDATE sessions SET last_seen_at=? "
                "WHERE token_hash=? AND COALESCE(last_seen_at,0) < ?",
                (now, _hash(raw_token), now - _TOUCH_THROTTLE_SECONDS))
    except Exception as exc:  # noqa: BLE001
        logger.debug("session touch 失敗（略過）：%s", exc)


def online_user_count(window_seconds: int = ONLINE_WINDOW_SECONDS) -> int:
    """最近 N 秒內有活動的**去重使用者數**。

    算「人」不算 session —— 同一個人開三個瀏覽器不該算成三個人在線。
    """
    try:
        now = time.time()
        row = auth_db.conn().execute(
            "SELECT COUNT(DISTINCT user_id) AS c FROM sessions "
            "WHERE expires_at > ? AND COALESCE(last_seen_at, created_at) > ?",
            (now, now - window_seconds)).fetchone()
        return int(row["c"] if row else 0)
    except Exception:  # noqa: BLE001
        return 0


def list_for_user(user_id: int) -> list[dict]:
    """某個帳號目前有效的 session（新到舊）。

    **不回傳 token 或其雜湊** —— 那是憑證。要踢人請用 `revoke_one`，它以
    「這個帳號的第 N 個 session」為單位，避免把可重放的東西送到前端。
    """
    now = time.time()
    rows = auth_db.conn().execute(
        "SELECT token_hash, created_at, expires_at, remember, ip, user_agent, "
        "COALESCE(last_seen_at, created_at) AS seen FROM sessions "
        "WHERE user_id=? AND expires_at > ? ORDER BY seen DESC",
        (user_id, now)).fetchall()
    out = []
    for r in rows:
        out.append({
            # 給前端當識別用的短碼（雜湊的前 12 碼）。不是憑證，猜到也用不了 ——
            # 它只能拿來對這個帳號踢人，而踢人本來就要管理員權限。
            "sid": r["token_hash"][:12],
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
            "last_seen_at": r["seen"],
            "online": (now - r["seen"]) <= ONLINE_WINDOW_SECONDS,
            "remember": bool(r["remember"]),
            "ip": r["ip"] or "",
            "user_agent": (r["user_agent"] or "")[:200],
        })
    return out


def revoke_one(user_id: int, sid_prefix: str) -> bool:
    """踢掉某個帳號的**單一** session（用 `list_for_user` 給的短碼）。

    一定要同時比對 user_id —— 只認短碼的話，猜中別人的前綴就能踢別人。
    """
    # 只收十六進位 —— LIKE 的 `_` 是萬用字元，放行的話一串底線就等於「全踢」。
    # 那雖然只影響同一個帳號（user_id 也有比對），但不該讓前端能表達那個意思。
    if (not sid_prefix or len(sid_prefix) < 8
            or not all(c in "0123456789abcdef" for c in sid_prefix.lower())):
        return False
    conn = auth_db.conn()
    with db.tx(conn):
        cur = conn.execute(
            "DELETE FROM sessions WHERE user_id=? AND token_hash LIKE ?",
            (user_id, sid_prefix + "%"))
    return cur.rowcount > 0


def online_user_ids(window_seconds: int = ONLINE_WINDOW_SECONDS) -> set[int]:
    """最近 N 秒內有活動的 user_id 集合。

    給清單頁一次撈完標「在線」用 —— 逐列去問會變成 N 次查詢。
    """
    try:
        now = time.time()
        rows = auth_db.conn().execute(
            "SELECT DISTINCT user_id FROM sessions "
            "WHERE expires_at > ? AND COALESCE(last_seen_at, created_at) > ?",
            (now, now - window_seconds)).fetchall()
        return {int(r["user_id"]) for r in rows}
    except Exception:  # noqa: BLE001
        return set()
