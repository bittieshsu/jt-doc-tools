"""目錄瀏覽：指派角色給**單一使用者**與**群組**（原本只能指派給 OU）。

## 由來

客戶反映「AD 帳號管理還有精進空間」。目錄瀏覽做得很完整 —— 樹狀 OU、使用者清單、
屬性細節 —— 但唯一能指派權限的對象是 **OU**。

「整個 OU 都給財務權限」跟「這個 OU 裡只有這兩個人是財務」是完全不同的事，而後者
才是實務上最常見的。以前只能等對方**先登入一次**，本站才有那一列可以指派 ——
新人報到當天要用工具，管理員卻只能說「你先登入一次我再幫你開」。

## 關鍵：鏡射列 ≠ 啟用

還沒登入過的人，指派角色時要建一列 `enabled=0`、不設密碼的鏡射列（跟排程同步建
出來的完全一樣）。本人真的登入時 JIT 會啟用它，而且 **`_sync_user` 只在「一個角色
都沒有」時才塞預設角色** —— 所以管理員先指派的東西不會被蓋掉。這個不變量壞掉的話，
這個功能就等於沒有做。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.core import auth_db, auth_settings, db, permissions, roles, user_manager


@pytest.fixture
def ldap_admin(admin_session, monkeypatch):
    """管理員 session + 假裝後端是 AD（目錄端點需要）。

    收尾時把這一支測試建出來的鏡射列刪掉 —— 這些端點會**新增** users / groups，
    留著會污染別的測試（`test_directory_sync` 會去數「有幾個目錄群組」，多出來的
    列讓它算出不同的數字，而且只在整包一起跑時才會失敗，最難查的那一種）。
    """
    client, name, _ = admin_session
    roles.seed_builtin_roles()
    real = auth_settings.get

    def fake_get():
        s = dict(real() or {})
        s["backend"] = "ad"
        return s

    monkeypatch.setattr(auth_settings, "get", fake_get)
    conn = auth_db.conn()
    before_u = {r["id"] for r in conn.execute("SELECT id FROM users")}
    before_g = {r["id"] for r in conn.execute("SELECT id FROM groups")}
    yield client
    with db.tx(conn):
        for table, before in (("users", before_u), ("groups", before_g)):
            ids = [r["id"] for r in conn.execute(f"SELECT id FROM {table}")
                   if r["id"] not in before]
            for i in ids:
                conn.execute(f"DELETE FROM {table} WHERE id=?", (i,))
    permissions.invalidate_cache()


def _row_for(dn):
    return auth_db.conn().execute(
        "SELECT * FROM users WHERE external_dn=?", (dn,)).fetchone()


# ------------------------------------------------------------ 使用者

def test_assign_to_a_user_who_never_logged_in(ldap_admin):
    """**這就是原本做不到的事。**"""
    dn = "CN=Alice,OU=RD,DC=example,DC=com"
    r = ldap_admin.post("/admin/directory/user-roles",
                        json={"dn": dn, "username": "alice",
                              "display_name": "Alice", "roles": ["clerk"]})
    assert r.status_code == 200, r.text
    uid = r.json()["user_id"]
    assert "clerk" in permissions.list_roles_for_subject("user", str(uid))


def test_the_created_row_is_mirrored_not_activated(ldap_admin):
    """建出來的是**鏡射列**：未啟用、沒有密碼 —— 不可以憑空多一個能登入的帳號。"""
    dn = "CN=Bob,OU=RD,DC=example,DC=com"
    ldap_admin.post("/admin/directory/user-roles",
                    json={"dn": dn, "username": "bob", "roles": ["clerk"]})
    row = _row_for(dn)
    assert row["enabled"] == 0, "憑空建出一個已啟用的帳號"
    assert row["password_hash"] is None, "鏡射列不可以有密碼"
    assert row["source"] in ("ldap", "ad")


def test_preassigned_roles_survive_the_first_login(ldap_admin):
    """本人第一次登入時**不可以**被塞預設角色蓋掉先前的指派。

    這是整個功能的成立前提 —— 壞掉的話管理員先指派的東西會在對方登入當下消失。
    """
    import inspect

    from app.core import auth_ldap
    src = inspect.getsource(auth_ldap._sync_user)
    assert 'if not permissions.list_roles_for_subject("user", str(row["id"])):' in src


def test_second_assignment_reuses_the_same_row(ldap_admin):
    dn = "CN=Carol,OU=RD,DC=example,DC=com"
    a = ldap_admin.post("/admin/directory/user-roles",
                        json={"dn": dn, "username": "carol", "roles": ["clerk"]})
    b = ldap_admin.post("/admin/directory/user-roles",
                        json={"dn": dn, "username": "carol", "roles": ["finance"]})
    assert a.json()["user_id"] == b.json()["user_id"], "同一個 DN 建出了兩列"
    uid = b.json()["user_id"]
    assigned = permissions.list_roles_for_subject("user", str(uid))
    assert assigned == ["finance"], "角色是覆蓋不是累加"


def test_existing_user_is_matched_by_dn(ldap_admin):
    """已經登入過的人要對到既有那一列，不可以另外建一列。"""
    dn = "CN=Dave,OU=RD,DC=example,DC=com"
    conn = auth_db.conn()
    with db.tx(conn):
        cur = conn.execute(
            "INSERT INTO users(username, display_name, source, external_dn, "
            "enabled, is_admin_seed, created_at, last_login_at) "
            "VALUES ('dave','Dave','ad',?,1,0,?,?)",
            (dn, time.time(), time.time()))
        existing = cur.lastrowid
    r = ldap_admin.post("/admin/directory/user-roles",
                        json={"dn": dn, "username": "dave", "roles": ["clerk"]})
    assert r.json()["user_id"] == existing
    assert len(auth_db.conn().execute(
        "SELECT id FROM users WHERE external_dn=?", (dn,)).fetchall()) == 1


def test_same_username_different_dn_is_refused(ldap_admin):
    """同名不同 DN 要擋 —— 無聲接管別人的身分是最糟的失敗方式。"""
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute(
            "INSERT INTO users(username, display_name, source, external_dn, "
            "enabled, is_admin_seed, created_at) "
            "VALUES ('eve','Eve','ad','CN=Eve,OU=A,DC=x',1,0,?)", (time.time(),))
    r = ldap_admin.post(
        "/admin/directory/user-roles",
        json={"dn": "CN=Eve,OU=B,DC=x", "username": "eve", "roles": ["clerk"]})
    assert r.status_code == 409, r.text


def test_no_username_is_a_clear_error(ldap_admin):
    """沒有帳號名稱就建不出可登入的對應 —— 要講清楚，不要建一列半殘的資料。"""
    r = ldap_admin.post("/admin/directory/user-roles",
                        json={"dn": "CN=X,OU=A,DC=x", "roles": ["clerk"]})
    assert r.status_code == 400


def test_get_reports_the_local_state(ldap_admin):
    dn = "CN=Frank,OU=RD,DC=example,DC=com"
    before = ldap_admin.get(
        f"/admin/directory/user-roles?dn={dn}").json()
    assert before["mirrored"] is False and before["roles"] == []
    ldap_admin.post("/admin/directory/user-roles",
                    json={"dn": dn, "username": "frank", "roles": ["clerk"]})
    after = ldap_admin.get(f"/admin/directory/user-roles?dn={dn}").json()
    assert after["mirrored"] is True
    assert after["roles"] == ["clerk"]
    assert after["logged_in"] is False and after["enabled"] is False


def test_missing_dn_is_400(ldap_admin):
    assert ldap_admin.get("/admin/directory/user-roles?dn=").status_code == 400
    assert ldap_admin.post("/admin/directory/user-roles",
                           json={"roles": []}).status_code == 400


# ------------------------------------------------------------ 群組

def test_assign_to_a_group(ldap_admin):
    dn = "CN=RD,OU=Groups,DC=example,DC=com"
    r = ldap_admin.post("/admin/directory/group-roles",
                        json={"dn": dn, "roles": ["clerk"]})
    assert r.status_code == 200, r.text
    gid = r.json()["group_id"]
    assert "clerk" in permissions.list_roles_for_subject("group", str(gid))


def test_group_name_falls_back_to_the_rdn(ldap_admin):
    """還沒同步到的群組要看得懂 —— 用 DN 第一段當名稱比留空好。"""
    ldap_admin.post("/admin/directory/group-roles",
                    json={"dn": "CN=財務部,OU=Groups,DC=x", "roles": []})
    row = auth_db.conn().execute(
        "SELECT name FROM groups WHERE external_dn=?",
        ("CN=財務部,OU=Groups,DC=x",)).fetchone()
    assert row["name"] == "財務部"


def test_existing_group_is_reused(ldap_admin):
    dn = "CN=Sales,OU=Groups,DC=x"
    conn = auth_db.conn()
    with db.tx(conn):
        cur = conn.execute(
            "INSERT INTO groups(name, source, external_dn, created_at) "
            "VALUES ('Sales','ad',?,?)", (dn, time.time()))
        gid = cur.lastrowid
    r = ldap_admin.post("/admin/directory/group-roles",
                        json={"dn": dn, "roles": ["clerk"]})
    assert r.json()["group_id"] == gid


# ------------------------------------------------------------ 邊界

def test_endpoints_need_a_directory_backend(admin_session):
    """local 後端一律 400 —— 這些端點會建目錄鏡射列，不該在非目錄環境作用。"""
    c, _, _ = admin_session
    assert c.get("/admin/directory/user-roles?dn=CN=a,DC=x").status_code == 400
    assert c.post("/admin/directory/user-roles",
                  json={"dn": "CN=a,DC=x", "username": "a",
                        "roles": []}).status_code == 400
    assert c.get("/admin/directory/group-roles?dn=CN=a,DC=x").status_code == 400
    assert c.post("/admin/directory/group-roles",
                  json={"dn": "CN=a,DC=x", "roles": []}).status_code == 400


def test_assignments_are_audited(ldap_admin):
    """權限指派是稽核一定會問的動作。"""
    from app.core import audit_db
    ldap_admin.post("/admin/directory/user-roles",
                    json={"dn": "CN=Zed,OU=A,DC=x", "username": "zed",
                          "roles": ["clerk"]})
    ldap_admin.post("/admin/directory/group-roles",
                    json={"dn": "CN=G,OU=A,DC=x", "roles": ["clerk"]})
    # 稽核是背景 writer thread 寫的 —— 要等它把 queue 清完
    deadline = time.time() + 5
    events = set()
    while time.time() < deadline:
        rows = audit_db.conn().execute(
            "SELECT event_type FROM audit_events ORDER BY id DESC LIMIT 50"
        ).fetchall()
        events = {r["event_type"] for r in rows}
        if {"perm_user_set", "perm_group_set"} <= events:
            break
        time.sleep(0.1)
    assert "perm_user_set" in events, "使用者權限指派沒有進稽核"
    assert "perm_group_set" in events, "群組權限指派沒有進稽核"


# ------------------------------------------------------------ 畫面

def _tpl() -> str:
    return (Path(user_manager.__file__).resolve().parent.parent / "admin" /
            "templates" / "admin_directory.html").read_text(encoding="utf-8")


def test_ui_has_a_user_role_panel():
    tpl = _tpl()
    assert 'id="udRolesBox"' in tpl
    assert "/admin/directory/user-roles" in tpl


def test_ui_explains_what_saving_will_do():
    """會憑空建一個帳號 —— 一定要先講。"""
    tpl = _tpl()
    i = tpl.index("udRolesBox")
    assert "未啟用" in tpl[i:i + 900]


def test_ui_takes_the_username_from_the_payload_not_the_dom():
    """從畫好的表格逆推帳號會被中文標籤與篩選字串影響 —— 要用回傳的屬性。"""
    tpl = _tpl()
    i = tpl.index("getElementById('udSaveRoles')")
    body = tpl[i:i + 1200]
    assert "_udLogin" in body
    assert "querySelectorAll('#udBody" not in body


def test_ui_offers_group_roles_from_memberof():
    tpl = _tpl()
    assert "ud-grp-btn" in tpl
    assert "/admin/directory/group-roles" in tpl


def test_username_is_reset_between_users():
    """不清掉的話，第二位使用者會沿用上一位的帳號名 —— 建錯人的帳號。"""
    tpl = _tpl()
    i = tpl.index("function openUserDetail")
    assert "_udLogin = ''" in tpl[i:i + 300]
