"""「這個人最終有哪些工具、從哪來」的檢視。

## 由來

客戶反映「AD 帳號管理還有精進空間」。盤點時發現 admin 端**完全沒有**呼叫過
`effective_tools` —— 權限來源散在四個地方（直接指派給帳號的角色、群組含巢狀上層、
OU、直接工具授權），管理員只看得到「這個 subject 有哪些角色」，看不到「A 這個人
加總後實際能用什麼、是哪一條規則給的」。

出事時無法排查、稽核時無法自證、交接時無法說明 —— 這三件事企業都會要。

## 這份守住什麼

* 結果要跟真正執行期用的 `effective_tools` **一致**（解釋跟實際不符比沒有解釋更糟）。
* 每個工具都要說得出來源。
* 稽核員是硬牆：一律 0 個工具，即使同時有 admin 角色。
"""
from __future__ import annotations

import time

import pytest

from app.core import auth_db, db, permissions, roles as _roles


def _mk(conn, username="u1"):
    cur = conn.execute(
        "INSERT INTO users(username, display_name, source, external_dn, enabled, "
        "is_admin_seed, created_at) VALUES (?,?,?,?,1,0,?)",
        (username, username, "ldap", f"cn={username},dc=x", time.time()))
    return cur.lastrowid


def _group(conn, name, dn, parent_dn=""):
    cur = conn.execute(
        "INSERT INTO groups(name, source, external_dn, parent_dn, created_at) "
        "VALUES (?,?,?,?,?)", (name, "ldap", dn, parent_dn, time.time()))
    return cur.lastrowid


@pytest.fixture
def env(auth_off):
    auth_db.init()
    _roles.seed_builtin_roles()
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("DELETE FROM group_members")
        conn.execute("DELETE FROM groups")
        conn.execute("DELETE FROM users")
    permissions.invalidate_cache()
    return conn


def test_explanation_matches_effective_tools(env):
    """解釋出來的清單要跟執行期真正用的一致 —— 不一致比沒有更糟。"""
    conn = env
    with db.tx(conn):
        uid = _mk(conn, "alice")
    permissions.set_subject_roles("user", str(uid), ["clerk"])
    permissions.invalidate_cache()
    real = permissions.effective_tools(uid)
    exp = permissions.explain_effective_tools(uid)
    assert real != "ALL"
    assert set(exp["tool_ids"]) == set(real), (
        f"解釋 {sorted(exp['tool_ids'])[:5]}… 與實際 {sorted(real)[:5]}… 不一致")


def test_every_tool_has_a_source(env):
    conn = env
    with db.tx(conn):
        uid = _mk(conn, "bob")
    permissions.set_subject_roles("user", str(uid), ["clerk"])
    permissions.invalidate_cache()
    exp = permissions.explain_effective_tools(uid)
    assert exp["tools"], "一個工具都沒解釋出來"
    for tid, vias in exp["tools"].items():
        assert vias, f"{tid} 說不出來源"


def test_nested_group_source_is_labelled_as_inherited(env):
    """從上層群組繼承來的權限，來源要標明是巢狀繼承。

    不標的話，管理員看到「群組 X」卻在群組 X 的成員名單裡找不到這個人，
    只會更困惑。
    """
    conn = env
    with db.tx(conn):
        parent = _group(conn, "RD部門", "cn=rd,dc=x")
        child = _group(conn, "RD工程師", "cn=eng,dc=x", parent_dn="cn=rd,dc=x")
        uid = _mk(conn, "carol")
        conn.execute("INSERT INTO group_members(group_id, user_id) VALUES (?,?)",
                     (child, uid))
    permissions.set_subject_roles("group", str(parent), ["clerk"])
    permissions.invalidate_cache()
    exp = permissions.explain_effective_tools(uid)
    joined = " ".join(v for vs in exp["tools"].values() for v in vs)
    assert "巢狀繼承" in joined, f"沒有標出是繼承來的：{joined[:200]}"
    assert "RD部門" in joined


def test_admin_is_reported_as_full_access(env):
    conn = env
    with db.tx(conn):
        uid = _mk(conn, "root")
    permissions.set_subject_roles("user", str(uid), ["admin"])
    permissions.invalidate_cache()
    exp = permissions.explain_effective_tools(uid)
    assert exp["admin"] is True
    assert permissions.effective_tools(uid) == "ALL"


def test_auditor_is_a_hard_wall(env):
    """稽核員一律 0 個工具，即使同時有 admin 角色（職責分離）。"""
    conn = env
    with db.tx(conn):
        uid = _mk(conn, "auditor1")
    permissions.set_subject_roles("user", str(uid), ["auditor", "admin"])
    permissions.invalidate_cache()
    exp = permissions.explain_effective_tools(uid)
    assert exp["auditor"] is True
    assert exp["admin"] is False
    assert exp["tool_ids"] == []
    assert permissions.effective_tools(uid) == set()


def test_endpoint_requires_admin_and_returns_names(admin_session):
    client, admin_name, _ = admin_session
    from app.core import user_manager
    me = next(u for u in user_manager.list_users("all")
              if u["username"] == admin_name)
    r = client.get(f"/admin/users/{me['id']}/effective")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == admin_name
    assert "tool_names" in body and body["tool_names"], \
        "沒有附上工具顯示名稱，畫面還要自己對照一次"


def test_endpoint_404_for_unknown_user(admin_session):
    client, _, _ = admin_session
    assert client.get("/admin/users/999999/effective").status_code == 404


# ------------------------------------------------------ 接進編輯 UI

def _users_tpl() -> str:
    from pathlib import Path

    from app.core import permissions as _p
    return (Path(_p.__file__).resolve().parent.parent / "admin" / "templates" /
            "admin_users.html").read_text(encoding="utf-8")


def test_panel_is_wired_into_the_edit_modal():
    """端點做好卻沒有人呼叫的話，這個功能等於不存在（原本就是這樣）。"""
    tpl = _users_tpl()
    assert 'id="emEffBody"' in tpl, "編輯 modal 裡沒有面板"
    assert "/effective`" in tpl, "沒有呼叫有效權限端點"
    i = tpl.index("function openEdit(")
    body = tpl[i:i + 1400]
    assert "loadEffective(" in body, "打開編輯 modal 時沒有載入有效權限"


def test_panel_says_it_shows_the_saved_state():
    """面板顯示的是**存檔前**的現況 —— 沒寫清楚的話，管理員會以為勾選當下就生效。"""
    tpl = _users_tpl()
    i = tpl.index("function loadEffective")
    assert "重新開啟才會更新" in tpl[i:i + 2000]


def test_panel_builds_dom_not_innerhtml():
    """工具名稱與來源字串含使用者可控成分（群組名來自目錄）—— 一律 textContent。"""
    tpl = _users_tpl()
    i = tpl.index("async function loadEffective")
    body = tpl[i:tpl.index("function openEdit(")]
    assert "innerHTML" not in body
    assert "textContent" in body


def test_panel_lists_every_source_for_a_tool():
    """一個工具可能同時由好幾條規則給 —— 只顯示第一條，管理員會以為拿掉它就沒了。"""
    tpl = _users_tpl()
    i = tpl.index("async function loadEffective")
    body = tpl[i:tpl.index("function openEdit(")]
    assert ".join(" in body, "來源沒有全部列出"
