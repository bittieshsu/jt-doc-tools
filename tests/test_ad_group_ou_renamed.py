"""AD 的 OU 改名（或群組搬到別的 OU）之後，那個 OU 底下的人照樣登得進來（客戶 2026-10-28 回報）。

本地 `groups` 用 DN 對應目錄群組，而 DN 含 OU 名稱。OU 改名後新 DN 找不到 → 用同一個名稱新增 →
撞 `UNIQUE(source, name)` → 整個登入失敗；管理頁的目錄同步也因為同一個原因整批失敗。
客戶是手動刪掉舊的那一列才恢復（那一列剛好沒有任何權限）。

判準：
* 同一個群組改了 OU → 沿用原本那一列（id 不變），DN 換成新的；設在它身上的角色、成員都還在；寫稽核。
* 兩個不同的群組剛好同名（舊 DN 這次也還在目錄清單裡）→ 不合併，另建一列，名稱加上所在 OU。
* 目錄同步走同一條規則，不會整批失敗。
（使用者帳號搬 OU 早就這樣處理：issue #47。）
"""
from __future__ import annotations

import time
import uuid

import pytest

from app.core import audit_db, auth_db, auth_ldap, permissions

OLD = "CN={g},OU=Repair Dept,OU=Ops Center,DC=example,DC=test"
NEW = "CN={g},OU=Service Dept,OU=Ops Center,DC=example,DC=test"


@pytest.fixture
def ad_group():
    auth_db.init()
    audit_db.init()
    from app.core import roles
    roles.seed_builtin_roles()
    g = "grp-" + uuid.uuid4().hex[:8]
    conn = auth_db.conn()
    with conn:
        gid = conn.execute("INSERT INTO groups(name, source, external_dn, created_at) VALUES (?,?,?,?)",
                           (g, "ad", OLD.format(g=g), time.time())).lastrowid
        uid = conn.execute(
            "INSERT INTO users(username, display_name, password_hash, source, external_dn, enabled, created_at) "
            "VALUES (?,?, '', 'ad', ?, 1, ?)",
            ("u-" + g, "u", f"CN=u-{g},OU=Repair Dept,OU=Ops Center,DC=example,DC=test", time.time())).lastrowid
    permissions.set_subject_roles("group", str(gid), ["clerk"])
    yield g, gid, uid
    with conn:
        conn.execute("DELETE FROM group_members WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM groups WHERE name LIKE ?", (g + "%",))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
    permissions.set_subject_roles("group", str(gid), [])


def test_login_after_the_ou_was_renamed_keeps_the_group(ad_group):
    g, gid, uid = ad_group
    auth_ldap._sync_groups(uid, [NEW.format(g=g)], backend="ad")     # 原本這裡丟 IntegrityError → 登入失敗
    conn = auth_db.conn()
    rows = conn.execute("SELECT id, external_dn FROM groups WHERE source='ad' AND name LIKE ?",
                        (g + "%",)).fetchall()
    assert [(r["id"], r["external_dn"]) for r in rows] == [(gid, NEW.format(g=g))], "沿用原本那一列，DN 換新"
    assert conn.execute("SELECT 1 FROM group_members WHERE group_id=? AND user_id=?", (gid, uid)).fetchone()
    assert permissions.list_roles_for_subject("group", str(gid)) == ["clerk"], "設在群組上的權限要保留"
    ev = None
    for _ in range(50):                 # 稽核是非同步寫入：等到真的寫進去
        ev = audit_db.conn().execute(
            "SELECT details_json FROM audit_events WHERE event_type='ldap_group_moved' AND target=? "
            "ORDER BY id DESC LIMIT 1", (g,)).fetchone()
        if ev:
            break
        time.sleep(0.1)
    assert ev and OLD.format(g=g) in ev["details_json"] and NEW.format(g=g) in ev["details_json"]


def test_two_different_groups_with_the_same_name_are_not_merged(ad_group):
    g, gid, uid = ad_group
    other = f"CN={g},OU=Taipei,OU=Sales,DC=example,DC=test"
    auth_ldap._sync_groups(uid, [OLD.format(g=g), other], backend="ad")
    conn = auth_db.conn()
    rows = {r["external_dn"]: (r["id"], r["name"]) for r in conn.execute(
        "SELECT id, name, external_dn FROM groups WHERE source='ad' AND name LIKE ?", (g + "%",))}
    assert rows[OLD.format(g=g)] == (gid, g), "原本那一列不動"
    assert rows[other][1] == f"{g} (Taipei)", "另一個同名群組另建一列，名稱帶所在 OU"
    assert len({v[0] for v in rows.values()}) == 2


def test_directory_sync_follows_the_same_rule(ad_group, monkeypatch):
    g, gid, uid = ad_group

    class _Std:
        def paged_search(self, **kw):
            return [{"dn": NEW.format(g=g), "type": "searchResEntry",
                     "attributes": {"cn": g, "memberOf": []}}]

    class _Conn:
        extend = type("E", (), {"standard": _Std()})()

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(auth_ldap.auth_settings, "get", lambda: {
        "backend": "ad", "ldap": {"service_dn": "CN=svc,DC=example,DC=test", "service_password": "x",
                                  "group_search_base": "DC=example,DC=test"}})
    monkeypatch.setattr(auth_ldap, "_build_server", lambda cfg: None)
    import ldap3
    monkeypatch.setattr(ldap3, "Connection", _Conn)
    out = auth_ldap.sync_all_groups(name_contains=g)        # 原本整批因 UNIQUE 失敗
    assert out["total_seen"] == 1
    row = auth_db.conn().execute("SELECT id, external_dn FROM groups WHERE source='ad' AND name=?",
                                 (g,)).fetchone()
    assert (row["id"], row["external_dn"]) == (gid, NEW.format(g=g))
    assert permissions.list_roles_for_subject("group", str(gid)) == ["clerk"]
