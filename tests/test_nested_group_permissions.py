"""巢狀群組的權限要往上繼承。

## 由來

客戶反映「AD 帳號管理還有精進空間」。盤點時發現：群組管理頁**畫了樹狀縮排**
（同步時由 `memberOf` 推出 `groups.parent_dn`），但權限解析只認 `group_members`
的**直接成員**，也沒用 AD 的 `LDAP_MATCHING_RULE_IN_CHAIN`。

結果是：企業 AD 幾乎都把「角色群組」巢在「部門群組」底下，管理員照直覺把權限指派
給上層部門群組，然後發現**沒有生效**。而畫面上那棵樹更會讓人相信它會繼承 ——
「看起來支援其實不支援」比明講不支援更傷。

## 兩個一定要守住的邊界

* **環要擋得住**：目錄端設錯造成 A→B→A 時，沒有 seen 集合就會無窮迴圈把請求卡死。
* **深度要有上限**：避免病態資料把每次權限查詢拖成長鏈走訪。
"""
from __future__ import annotations

import time

import pytest

from app.core import auth_db, db, permissions, roles as _roles


def _group(conn, name: str, dn: str, parent_dn: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO groups(name, source, external_dn, parent_dn, created_at) "
        "VALUES (?,?,?,?,?)", (name, "ldap", dn, parent_dn, time.time()))
    return cur.lastrowid


def _user(conn, username: str) -> int:
    cur = conn.execute(
        "INSERT INTO users(username, display_name, source, external_dn, enabled, "
        "is_admin_seed, created_at) VALUES (?,?,?,?,1,0,?)",
        (username, username, "ldap", f"cn={username},dc=x", time.time()))
    return cur.lastrowid


@pytest.fixture
def nested(auth_off):
    """RD 部門群組  ←  RD-工程師 子群組  ←  alice

    權限指派在**上層**的 RD 部門群組。
    """
    auth_db.init()
    _roles.seed_builtin_roles()   # role_perms 有外鍵指向 roles
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("DELETE FROM group_members")
        conn.execute("DELETE FROM groups")
        conn.execute("DELETE FROM users")
        parent = _group(conn, "RD部門", "cn=rd,dc=x")
        child = _group(conn, "RD工程師", "cn=rd-eng,dc=x", parent_dn="cn=rd,dc=x")
        uid = _user(conn, "alice")
        conn.execute("INSERT INTO group_members(group_id, user_id) VALUES (?,?)",
                     (child, uid))
    permissions.invalidate_cache()
    return {"parent": parent, "child": child, "uid": uid}


def test_permission_on_parent_group_reaches_child_member(nested):
    """權限給上層群組，子群組的成員要拿得到 —— 這就是原本壞掉的地方。"""
    permissions.set_subject_roles("group", str(nested["parent"]), ["clerk"])
    permissions.invalidate_cache()
    tools = permissions.effective_tools(nested["uid"])
    assert tools != "ALL"
    assert "pdf-merge" in tools, (
        "權限指派在上層群組卻沒有繼承到子群組成員 —— 畫面上還畫著樹")


def test_direct_group_permission_still_works(nested):
    permissions.set_subject_roles("group", str(nested["child"]), ["clerk"])
    permissions.invalidate_cache()
    assert "pdf-merge" in permissions.effective_tools(nested["uid"])


def test_unrelated_group_does_not_leak(nested, auth_off):
    """不在這條鏈上的群組不可以外溢權限。"""
    conn = auth_db.conn()
    with db.tx(conn):
        other = _group(conn, "財務", "cn=fin,dc=x")
    permissions.set_subject_roles("group", str(other), ["finance"])
    permissions.invalidate_cache()
    tools = permissions.effective_tools(nested["uid"])
    assert tools != "ALL"
    assert "pdf-fill" not in tools, "不相干的群組把權限漏給了別人"


def test_three_levels_deep(auth_off):
    """A ← B ← C ← user：權限給最上層也要到得了最底下的人。"""
    auth_db.init()
    _roles.seed_builtin_roles()   # role_perms 有外鍵指向 roles
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("DELETE FROM group_members")
        conn.execute("DELETE FROM groups")
        conn.execute("DELETE FROM users")
        top = _group(conn, "全公司", "cn=all,dc=x")
        mid = _group(conn, "研發", "cn=rd,dc=x", parent_dn="cn=all,dc=x")
        leaf = _group(conn, "後端", "cn=be,dc=x", parent_dn="cn=rd,dc=x")
        uid = _user(conn, "bob")
        conn.execute("INSERT INTO group_members(group_id, user_id) VALUES (?,?)",
                     (leaf, uid))
    permissions.set_subject_roles("group", str(top), ["clerk"])
    permissions.invalidate_cache()
    assert "pdf-merge" in permissions.effective_tools(uid)


def test_cycle_does_not_hang(auth_off):
    """目錄端設錯造成 A→B→A 時不可以無窮迴圈。

    沒有 seen 集合的話這個測試會直接卡死（而正式環境上是整個請求卡住）。
    """
    auth_db.init()
    _roles.seed_builtin_roles()   # role_perms 有外鍵指向 roles
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("DELETE FROM group_members")
        conn.execute("DELETE FROM groups")
        conn.execute("DELETE FROM users")
        a = _group(conn, "A", "cn=a,dc=x", parent_dn="cn=b,dc=x")
        b = _group(conn, "B", "cn=b,dc=x", parent_dn="cn=a,dc=x")
        uid = _user(conn, "carol")
        conn.execute("INSERT INTO group_members(group_id, user_id) VALUES (?,?)",
                     (a, uid))
    permissions.set_subject_roles("group", str(b), ["clerk"])
    permissions.invalidate_cache()
    t0 = time.time()
    tools = permissions.effective_tools(uid)
    assert time.time() - t0 < 5, "環狀巢狀把權限查詢卡住了"
    assert "pdf-merge" in tools


def test_depth_is_bounded():
    assert 1 <= permissions._NESTED_GROUP_MAX_DEPTH <= 32
