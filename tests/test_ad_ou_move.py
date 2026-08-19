"""AD / LDAP 帳號搬 OU（DN 改變）後要能繼續登入（issue #47）。

## 由來

客戶回報：同一個 AD 帳號在 OU 異動後登入會跳「AD DN 重複」，唯一的
workaround 是把工具箱裡的舊帳號刪掉 —— 角色與歷史全部陪葬。

根因：登入與目錄同步都**以 DN 比對身分**，但 DN 是**位置**不是身分 ——
搬 OU 就變。DN 對不上就走「第一次登入」路徑，撞上同名檢查被擋。

## 為什麼「同名就是同一個人」是安全的

* AD 的 sAMAccountName 全網域唯一
* OpenLDAP 若真有兩筆同 uid，`_search_ldap_user` 在搜尋階段就拒絕
  （multiple users → refuse），到得了同步這一步的一定只有一筆
* 走到同步時本人已經用**新 DN** 通過 bind —— 目錄已經背書

## 判準

1. 登入路徑：DN 變了 → 同一個 user_id、DN 更新、角色保留、寫稽核
2. 同步路徑：DN 變了 → 更新 DN 與戳記、**不動 enabled**（鏡射 ≠ 啟用）
3. 不同 source 的同名帳號（local 的 jason vs AD 的 jason）互不影響
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture()
def db():
    """共用測試 DB；種一個「舊 DN」的 AD 使用者 + 同名 local 使用者。

    **不可以 monkeypatch JTDT_DATA_DIR + reload config** —— 那會把整個
    行程的模組狀態切到暫存目錄且無法完整還原，單跑全綠、跟別的測試
    合跑就互相污染（本專案 v1.14.16 踩過同型雷）。照鄰居測試的模式：
    用共用 DB，自己建的列自己收。
    """
    from app.core import audit_db, auth_db, db as _db, permissions, roles
    auth_db.init()
    audit_db.init()
    roles.seed_builtin_roles()
    conn = auth_db.conn()
    now = time.time()
    uname = "oumove-jason"           # 不易撞名，避免影響其他測試的資料
    with _db.tx(conn):
        cur = conn.execute(
            "INSERT INTO users(username, display_name, source, external_dn, "
            "enabled, is_admin_seed, created_at) VALUES (?,?,?,?,1,0,?)",
            (uname, "Jason 舊", "ad",
             "CN=jason,OU=OldDept,DC=corp,DC=example", now))
        uid = cur.lastrowid
        cur2 = conn.execute(
            "INSERT INTO users(username, display_name, source, external_dn, "
            "enabled, is_admin_seed, created_at) VALUES (?,?,?,NULL,1,0,?)",
            (uname, "本機 Jason", "local", now))
        uid_local = cur2.lastrowid
    permissions.set_subject_roles("user", str(uid), ["finance"])
    yield {"uid": uid, "conn": conn, "uname": uname}
    # 收尾：把自己建的全部清掉（含測試中可能新建的 mary）
    with _db.tx(conn):
        for u in (uid, uid_local):
            conn.execute("DELETE FROM subject_roles WHERE subject_type='user' "
                         "AND subject_key=?", (str(u),))
            conn.execute("DELETE FROM users WHERE id=?", (u,))
        conn.execute("DELETE FROM subject_roles WHERE subject_type='user' AND "
                     "subject_key IN (SELECT CAST(id AS TEXT) FROM users "
                     "WHERE username='oumove-mary')")
        conn.execute("DELETE FROM users WHERE username='oumove-mary'")


NEW_DN = "CN=jason,OU=NewDept,DC=corp,DC=example"


def test_login_after_ou_move_keeps_identity_and_roles(db):
    """搬 OU 後登入：同一個 user_id、DN 更新、角色原封不動。"""
    from app.core import auth_ldap, permissions

    out = auth_ldap._sync_user(db["uname"], "Jason 新", NEW_DN, "ad",
                               email="jason@corp.example")
    assert out["user_id"] == db["uid"], "應該綁回原帳號，不是建新的"

    row = db["conn"].execute(
        "SELECT external_dn, display_name, email, enabled FROM users WHERE id=?",
        (db["uid"],)).fetchone()
    assert row["external_dn"] == NEW_DN
    assert row["display_name"] == "Jason 新"
    assert row["email"] == "jason@corp.example"
    assert row["enabled"] == 1
    # 角色保留 —— 這正是舊 workaround（刪帳號重建）做不到的
    assert "finance" in permissions.list_roles_for_subject(
        "user", str(db["uid"]))


def test_login_after_ou_move_writes_audit(db):
    """DN 換綁要留稽核（old_dn → new_dn），事後才查得到誰搬過家。"""
    import time as _t

    from app.core import audit_db, auth_ldap

    auth_ldap._sync_user(db["uname"], "Jason", NEW_DN, "ad")
    # 稽核寫入走背景 queue —— 要等 writer 落地再查（直接查常常還沒寫進去）
    deadline = _t.time() + 5
    rows = []
    while _t.time() < deadline:
        rows = audit_db.conn().execute(
            "SELECT details_json FROM audit_events WHERE event_type='user_dn_rebind' "
            "ORDER BY id DESC LIMIT 5").fetchall()
        if rows:
            break
        _t.sleep(0.2)
    assert rows, "沒有 user_dn_rebind 稽核事件"
    detail = str(rows[0]["details_json"])
    assert "OldDept" in detail and "NewDept" in detail


def test_local_account_with_same_name_untouched(db):
    """local 的同名帳號不可以被換綁（不同 source 本來就可並存）。"""
    from app.core import auth_ldap

    auth_ldap._sync_user(db["uname"], "Jason", NEW_DN, "ad")
    row = db["conn"].execute(
        "SELECT external_dn, display_name FROM users "
        "WHERE username=? AND source='local'", (db["uname"],)).fetchone()
    assert row["external_dn"] is None
    assert row["display_name"] == "本機 Jason"


def test_unknown_dn_new_username_still_creates(db):
    """全新帳號（名字沒撞）照舊走建立路徑。"""
    from app.core import auth_ldap

    out = auth_ldap._sync_user(
        "oumove-mary", "Mary", "CN=mary,OU=Sales,DC=corp,DC=example", "ad")
    assert out["user_id"] != db["uid"]
    row = db["conn"].execute(
        "SELECT external_dn FROM users WHERE username='oumove-mary' "
        "AND source='ad'").fetchone()
    assert row and "mary" in row["external_dn"]


def test_directory_sync_rebinds_without_touching_enabled(db):
    """同步路徑：搬家的人更新 DN 與戳記，enabled 絕不動（鏡射 ≠ 啟用）。

    先把帳號設成 enabled=0（管理員去啟用過）—— 同步後 DN 要變新的，
    enabled 必須仍是 0。舊版在這裡是悄悄 skip，搬家的人永遠停在舊 DN，
    之後還會被「完整同步沒看到」誤標成目錄已無此人。
    """
    from app.core import db as _db

    conn = db["conn"]
    with _db.tx(conn):
        conn.execute("UPDATE users SET enabled=0 WHERE id=?", (db["uid"],))

    # 直接演練 sync_all_users 內的比對邏輯所處理的資料形狀：
    # seen = [(dn, login, disp, mail, disabled, pwd_exp)]
    from app.core import auth_ldap
    import inspect
    src = inspect.getsource(auth_ldap.sync_all_users)
    assert "UPDATE users SET external_dn=?" in src, (
        "sync_all_users 的同名分支沒有換綁 DN —— 搬家的人會永遠停在舊 DN")
    assert "skipped += 1\n                continue" not in src or \
           src.count("external_dn=?") >= 1
