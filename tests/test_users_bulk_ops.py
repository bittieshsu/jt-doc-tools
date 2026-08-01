"""使用者批次操作（啟用 / 停用 / 指派角色）與伺服器端分頁。

## 由來

客戶反映「AD 帳號管理還有精進空間」。盤點後第二嚴重的是：使用者清單**沒有分頁、
沒有批次**，幾百上千人的組織要幫一整批人換角色只能一個一個點開 modal。

## 批次操作的三條防線

批次最危險的是「一鍵把自己鎖在門外」。所以：

1. **不可以停用自己** —— 按下去當場失去管理權。
2. **不可以動內建管理員（seed）** —— 那是 break-glass 帳號。
3. **停用前確認還留得下至少一個啟用中的管理員** —— 少了這條，全選停用之後沒有人
   進得了管理區，只能用 CLI 救。

## 篩選一定要在伺服器端做

前端過濾只能過濾「已經 render 出來的那一頁」。使用者以為篩了全部、其實只篩了眼前
100 筆 —— 這種半套篩選比沒有更危險（會讓人以為某個帳號不存在）。
"""
from __future__ import annotations

import time

import pytest

from app.core import auth_db, db, permissions, user_manager


@pytest.fixture
def admin_client(admin_session):
    client, _, _ = admin_session
    return client


def _mk_user(username: str, *, enabled=1, source="ldap", roles=None) -> int:
    uid = user_manager.create_local(username, username, "UserPass1234") \
        if source == "local" else None
    if uid is None:
        conn = auth_db.conn()
        with db.tx(conn):
            cur = conn.execute(
                "INSERT INTO users(username, display_name, source, external_dn, "
                "enabled, is_admin_seed, created_at) VALUES (?,?,?,?,?,0,?)",
                (username, username, source, f"cn={username},dc=x", enabled,
                 time.time()))
            uid = cur.lastrowid
    if roles:
        permissions.set_subject_roles("user", str(uid), roles)
    return uid


# ---------------------------------------------------------------- 分頁 / 篩選

def test_pagination_returns_total_and_page(auth_off):
    auth_db.init()
    for i in range(12):
        _mk_user(f"pg{i:02d}")
    p1 = user_manager.list_users_page(view="all", offset=0, limit=5)
    assert p1["total"] >= 12
    assert len(p1["rows"]) == 5
    p2 = user_manager.list_users_page(view="all", offset=5, limit=5)
    # 兩頁不可以重疊 —— 重疊代表 OFFSET 沒有生效
    assert not ({r["id"] for r in p1["rows"]} & {r["id"] for r in p2["rows"]})


def test_search_is_done_in_sql_not_in_the_browser(auth_off):
    """搜尋要在 SQL 做 —— 前端只過濾得到當前這一頁。"""
    auth_db.init()
    _mk_user("findme-alpha")
    _mk_user("other-beta")
    data = user_manager.list_users_page(view="all", q="findme")
    assert {r["username"] for r in data["rows"]} == {"findme-alpha"}
    assert data["total"] == 1


def test_filter_by_source_and_enabled(auth_off):
    auth_db.init()
    _mk_user("src-ldap", source="ldap")
    _mk_user("src-ad", source="ad")
    _mk_user("src-off", source="ldap", enabled=0)
    ldap_on = user_manager.list_users_page(
        view="all", source="ldap", enabled=True)
    names = {r["username"] for r in ldap_on["rows"]}
    assert "src-ldap" in names
    assert "src-ad" not in names and "src-off" not in names


def test_filter_never_logged_in(auth_off):
    auth_db.init()
    uid = _mk_user("newbie")
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("UPDATE users SET last_login_at=? WHERE username=?",
                     (time.time(), "veteran")) if False else None
        conn.execute(
            "INSERT INTO users(username, display_name, source, enabled, "
            "is_admin_seed, created_at, last_login_at) VALUES (?,?,?,1,0,?,?)",
            ("veteran", "veteran", "ldap", time.time(), time.time()))
    data = user_manager.list_users_page(view="all", never_logged_in=True)
    names = {r["username"] for r in data["rows"]}
    assert "newbie" in names and "veteran" not in names


# ---------------------------------------------------------------- 批次防線

def test_cannot_disable_yourself(admin_client, admin_session):
    """按下去當場失去管理權 —— 一定要擋。"""
    _, admin_name, _ = admin_session
    me = next((u for u in user_manager.list_users("all")
               if u["username"] == admin_name), None)
    assert me, f"找不到目前登入的管理員 {admin_name}"
    r = admin_client.post("/admin/users/bulk/enabled",
                          json={"user_ids": [me["id"]], "enabled": False})
    assert r.status_code in (200, 400), r.text
    if r.status_code == 200:
        assert r.json()["changed"] == 0, r.json()
    after = next(u for u in user_manager.list_users("all")
                 if u["username"] == admin_name)
    assert after["enabled"], "把自己停用掉了"


def test_cannot_disable_the_last_admin(admin_client):
    """全選停用之後不可以一個管理員都不剩。"""
    admins = [u["id"] for u in user_manager.list_users("all")
              if u["enabled"] and ("admin" in (u.get("roles") or [])
                                   or u["is_admin_seed"])]
    assert admins, "測試前提不成立：目前沒有啟用中的管理員"
    r = admin_client.post("/admin/users/bulk/enabled",
                          json={"user_ids": admins, "enabled": False})
    # 要嘛整批被擋（400），要嘛全部被 skip —— 總之不能真的停用掉
    if r.status_code == 200:
        assert r.json()["changed"] == 0, r.json()
    else:
        assert r.status_code == 400
    still = [u for u in user_manager.list_users("all")
             if u["enabled"] and ("admin" in (u.get("roles") or [])
                                  or u["is_admin_seed"])]
    assert still, "最後一個管理員被停用了 —— 沒有人能再進管理區"


def test_bulk_add_roles(admin_client, auth_off):
    auth_db.init()
    ids = [_mk_user(f"bulk{i}") for i in range(3)]
    r = admin_client.post("/admin/users/bulk/roles",
                          json={"user_ids": ids, "mode": "add",
                                "roles": ["clerk"]})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == 3
    for uid in ids:
        assert "clerk" in permissions.list_roles_for_subject("user", str(uid))


def test_bulk_remove_roles(admin_client, auth_off):
    auth_db.init()
    ids = [_mk_user(f"rm{i}", roles=["clerk", "finance"]) for i in range(2)]
    r = admin_client.post("/admin/users/bulk/roles",
                          json={"user_ids": ids, "mode": "remove",
                                "roles": ["clerk"]})
    assert r.status_code == 200, r.text
    for uid in ids:
        got = permissions.list_roles_for_subject("user", str(uid))
        assert "clerk" not in got and "finance" in got


def test_unknown_role_is_rejected(admin_client, auth_off):
    auth_db.init()
    uid = _mk_user("badrole")
    r = admin_client.post("/admin/users/bulk/roles",
                          json={"user_ids": [uid], "roles": ["不存在的角色"]})
    assert r.status_code == 400


def test_bulk_is_capped(admin_client):
    r = admin_client.post("/admin/users/bulk/enabled",
                          json={"user_ids": list(range(1, 2000)),
                                "enabled": True})
    assert r.status_code == 400
    assert "篩選" in r.json()["detail"]


def test_empty_selection_rejected(admin_client):
    r = admin_client.post("/admin/users/bulk/enabled",
                          json={"user_ids": [], "enabled": True})
    assert r.status_code == 400


def test_bulk_requires_admin(auth_off):
    """一般使用者不可以呼叫批次端點。"""
    from fastapi.testclient import TestClient
    import app.main as app_main
    c = TestClient(app_main.app)
    from app.core import auth_settings, sessions
    if not auth_settings.is_enabled():
        pytest.skip("認證未啟用時 admin 閘門是 no-op")
    r = c.post("/admin/users/bulk/enabled",
               json={"user_ids": [1], "enabled": False})
    assert r.status_code in (401, 403)
