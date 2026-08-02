"""在線人數、某人的登入裝置清單、強制登出。

## 由來

客戶反映「AD 帳號管理還有精進空間」。管理員答不出兩個很基本的問題：

* 「現在有誰登入著？」—— `sessions` 表只有 `created_at`，一個記住我的 session
  可以躺 30 天，看 created_at 完全推不出「這個人現在在不在」。
* 「這個人的帳號被別人拿去用了，怎麼把他踢下來？」—— 原本只有「刪帳號」與
  「重設 2FA」會連帶清 session，沒有單純的強制登出。

## 三個一定要守住的地方

* **節流**：`last_seen_at` 若每個請求都寫，就是每請求一次寫入 —— SQLite WAL 下
  仍然只有單一 writer，尖峰時會變成競爭點。
* **不外流憑證**：清單給的識別碼不可以是 token 或其完整雜湊。
* **踢人要綁 user_id**：只認短碼的話，猜中前綴就能踢別人。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from app.core import auth_db, db, sessions


def _mk_user(conn, username: str) -> int:
    cur = conn.execute(
        "INSERT INTO users(username, display_name, source, external_dn, enabled, "
        "is_admin_seed, created_at) VALUES (?,?,?,?,1,0,?)",
        (username, username, "local", None, time.time()))
    return cur.lastrowid


@pytest.fixture
def two_users(auth_off):
    auth_db.init()
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM users")
        a = _mk_user(conn, "alice")
        b = _mk_user(conn, "bob")
    return a, b


# ------------------------------------------------------------ schema

def test_migration_added_last_seen(auth_off):
    auth_db.init()
    cols = {r["name"] for r in
            auth_db.conn().execute("PRAGMA table_info(sessions)").fetchall()}
    assert "last_seen_at" in cols


def test_existing_sessions_are_backfilled(auth_off):
    """升級時既有 session 的 last_seen_at 不可以是 NULL 又被當成「從沒活動」。

    回填成 created_at 至少是個誠實的下界；查詢也一律 COALESCE 兜底。
    """
    auth_db.init()
    src = Path(auth_db.__file__).read_text(encoding="utf-8")
    i = src.index("_m16_session_last_seen")
    assert "created_at" in src[i:i + 800], "沒有回填既有 session"


# ------------------------------------------------------------ touch 節流

def test_touch_updates_last_seen(two_users):
    a, _ = two_users
    tok, _ = sessions.issue(a, remember=False, ip="1.2.3.4", ua="UA")
    # 先往回撥，否則節流會擋住
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("UPDATE sessions SET last_seen_at=?", (time.time() - 999,))
    sessions.touch(tok)
    row = conn.execute("SELECT last_seen_at FROM sessions").fetchone()
    assert time.time() - row["last_seen_at"] < 5


def test_touch_is_throttled(two_users):
    """**這是效能的核心**：每個請求都寫 DB 會變成每請求一次寫入競爭。

    這個測試用「連續兩次 touch 之間的值沒有變」來驗證節流真的生效。
    """
    a, _ = two_users
    tok, _ = sessions.issue(a, remember=False)
    sessions.touch(tok)
    conn = auth_db.conn()
    first = conn.execute("SELECT last_seen_at FROM sessions").fetchone()[0]
    time.sleep(0.05)
    sessions.touch(tok)
    second = conn.execute("SELECT last_seen_at FROM sessions").fetchone()[0]
    assert first == second, "沒有節流 —— 每個請求都會寫一次 DB"
    assert 0 < sessions._TOUCH_THROTTLE_SECONDS <= 300


@pytest.mark.parametrize("bad", ["", "no-such-token", None])
def test_touch_never_raises(two_users, bad):
    """在每個請求的路徑上 —— 記錄失敗絕不可以讓請求失敗。"""
    sessions.touch(bad)


def test_touch_is_wired_into_the_auth_gate():
    """中介層沒有呼叫的話，last_seen_at 永遠停在建立時間，整個功能是死的。"""
    src = (Path(sessions.__file__).resolve().parent.parent /
           "main.py").read_text(encoding="utf-8")
    assert "sessions.touch(" in src, "認證中介層沒有更新 last_seen"


# ------------------------------------------------------------ 在線人數

def test_online_counts_people_not_sessions(two_users):
    """同一個人開三個瀏覽器只算一位 —— 算 session 會讓人數虛胖。"""
    a, _ = two_users
    for _ in range(3):
        sessions.issue(a, remember=False)
    assert sessions.online_user_count() == 1


def test_idle_sessions_drop_out_of_the_count(two_users):
    a, b = two_users
    sessions.issue(a, remember=False)
    tok_b, _ = sessions.issue(b, remember=False)
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("UPDATE sessions SET last_seen_at=? WHERE user_id=?",
                     (time.time() - sessions.ONLINE_WINDOW_SECONDS - 60, b))
    assert sessions.online_user_count() == 1
    assert sessions.online_user_ids() == {a}


def test_expired_sessions_never_count(two_users):
    a, _ = two_users
    sessions.issue(a, remember=False)
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("UPDATE sessions SET expires_at=?", (time.time() - 1,))
    assert sessions.online_user_count() == 0


def test_online_count_is_only_shown_when_auth_is_on():
    """單機模式不顯示 —— 那裡沒有帳號概念，一個永遠是 1 的人數只會誤導。"""
    src = (Path(sessions.__file__).resolve().parent.parent / "admin" /
           "auth_router.py").read_text(encoding="utf-8")
    i = src.index("online_user_ids()")
    ctx = src[max(0, i - 300):i + 100]
    assert "auth_on" in ctx, "沒有用啟用認證與否來 gate 在線人數"

    tpl = (Path(sessions.__file__).resolve().parent.parent / "admin" /
           "templates" / "admin_users.html").read_text(encoding="utf-8")
    m = re.search(r"\{%\s*if auth_on\s*%\}(?:(?!\{%).){0,400}人在線", tpl, re.S)
    assert m, "畫面上的在線人數沒有包在 auth_on 判斷裡"


# ------------------------------------------------------------ 清單

def test_list_never_leaks_the_token_or_full_hash(two_users):
    """清單給前端的識別碼不可以是憑證本身，也不可以是完整雜湊。"""
    a, _ = two_users
    tok, _ = sessions.issue(a, remember=False, ip="10.0.0.1", ua="Chrome")
    rows = sessions.list_for_user(a)
    assert len(rows) == 1
    blob = repr(rows)
    assert tok not in blob
    assert sessions._hash(tok) not in blob, "把完整雜湊送到前端了"
    assert rows[0]["sid"] == sessions._hash(tok)[:12]
    assert rows[0]["ip"] == "10.0.0.1"
    assert rows[0]["online"] is True


def test_list_is_scoped_to_the_user(two_users):
    a, b = two_users
    sessions.issue(a, remember=False)
    sessions.issue(b, remember=False)
    assert len(sessions.list_for_user(a)) == 1


def test_list_hides_expired(two_users):
    a, _ = two_users
    sessions.issue(a, remember=False)
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("UPDATE sessions SET expires_at=?", (time.time() - 1,))
    assert sessions.list_for_user(a) == []


def test_user_agent_is_capped(two_users):
    """UA 是使用者送的，長度不設限等於讓人塞任意大小的字串進畫面。"""
    a, _ = two_users
    sessions.issue(a, remember=False, ua="x" * 5000)
    assert len(sessions.list_for_user(a)[0]["user_agent"]) <= 200


# ------------------------------------------------------------ 強制登出

def test_revoke_one_kills_only_that_session(two_users):
    a, _ = two_users
    t1, _ = sessions.issue(a, remember=False)
    t2, _ = sessions.issue(a, remember=False)
    sid = sessions._hash(t1)[:12]
    assert sessions.revoke_one(a, sid) is True
    assert sessions.lookup(t1) is None
    assert sessions.lookup(t2) is not None


def test_revoke_one_requires_the_matching_user(two_users):
    """**只認短碼的話，猜中別人的前綴就能踢別人。**"""
    a, b = two_users
    tok, _ = sessions.issue(b, remember=False)
    sid = sessions._hash(tok)[:12]
    assert sessions.revoke_one(a, sid) is False
    assert sessions.lookup(tok) is not None, "被別的帳號踢掉了"


@pytest.mark.parametrize("bad", ["", "abc", "____________", "%%%%%%%%%%%%",
                                 "zzzzzzzzzzzz"])
def test_revoke_one_rejects_junk_and_wildcards(two_users, bad):
    """LIKE 的 `_` 是萬用字元 —— 一串底線就等於「全踢」。"""
    a, _ = two_users
    tok, _ = sessions.issue(a, remember=False)
    assert sessions.revoke_one(a, bad) is False
    assert sessions.lookup(tok) is not None


# ------------------------------------------------------------ 端點

def test_endpoints_require_admin():
    """路由掛在 admin router 下（啟用認證時由 require_admin 擋）。"""
    from app.admin import auth_router
    src = Path(auth_router.__file__).read_text(encoding="utf-8")
    assert '@router.get("/users/{uid}/sessions")' in src
    assert '@router.post("/users/{uid}/sessions/revoke")' in src


def test_sessions_endpoint_returns_the_list(admin_session):
    client, admin_name, _ = admin_session
    from app.core import user_manager
    me = next(u for u in user_manager.list_users("all")
              if u["username"] == admin_name)
    r = client.get(f"/admin/users/{me['id']}/sessions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sessions"], "管理員自己正登入著，清單卻是空的"
    assert "window_minutes" in body


def test_unknown_user_is_404(admin_session):
    client, _, _ = admin_session
    assert client.get("/admin/users/999999/sessions").status_code == 404
    assert client.post("/admin/users/999999/sessions/revoke",
                       json={}).status_code == 404


def test_revoke_is_audited():
    """強制登出是會被問「誰做的」的動作。"""
    from app.admin import auth_router
    src = Path(auth_router.__file__).read_text(encoding="utf-8")
    i = src.index("users_sessions_revoke")
    assert "session_revoke" in src[i:i + 1200], "強制登出沒有寫稽核"


# ------------------------------------------------------------ 畫面

def test_every_account_kind_can_be_kicked():
    """內建管理員 / 稽核員 / 一般帳號三條分支都要有按鈕。

    漏掉內建帳號那條 = 管理員的帳號外洩時反而踢不了 —— 最需要的那一個。
    """
    tpl = (Path(sessions.__file__).resolve().parent.parent / "admin" /
           "templates" / "admin_users.html").read_text(encoding="utf-8")
    assert tpl.count('data-act="sessions"') == 3


def test_device_string_is_not_injected_as_html():
    """UA 由使用者控制 —— 一律 textContent。"""
    tpl = (Path(sessions.__file__).resolve().parent.parent / "admin" /
           "templates" / "admin_users.html").read_text(encoding="utf-8")
    i = tpl.index("function openSessions")
    body = tpl[i:tpl.index("async function revokeSession")]
    assert "innerHTML" not in body, "把使用者送的 UA 當 HTML 塞進 DOM"
    assert "textContent" in body
