"""Scheduled AD/LDAP directory sync + the perf fixes it enables (v1.12.67).

Covers:
  - migration v11 group cache columns exist
  - permissions.list_roles_for_subjects (batch) == per-subject results
  - group_manager.list_groups exposes dir_member_count / parent_dn
  - directory_sync settings defaults + clamp
  - run_sync mirrors + caches member counts (auth_ldap mocked)
  - run_sync no-ops on a non-directory backend
"""
from __future__ import annotations

import pytest

from app.core import auth_db, audit_db, permissions, group_manager, directory_sync


@pytest.fixture(autouse=True)
def _init_db():
    """Startup hook runs lazily under TestClient; init schemas so DB-only
    tests don't hit "no such table"."""
    auth_db.init()
    audit_db.init()
    from app.core import roles
    roles.seed_builtin_roles()          # so 'clerk'/'finance' FK targets exist
    yield


# --------------------------------------------------------------------- helpers

def _mk_group(name: str, source: str = "ldap", dn: str = "") -> int:
    conn = auth_db.conn()
    cur = conn.execute(
        "INSERT INTO groups(name, source, external_dn, created_at) VALUES(?,?,?,?)",
        (name, source, dn, "2026-01-01T00:00:00"))
    conn.commit()
    return cur.lastrowid


def _rm_group(name: str) -> None:
    conn = auth_db.conn()
    conn.execute("DELETE FROM groups WHERE name=?", (name,))
    conn.commit()


# ------------------------------------------------------------------- migration

def test_migration_added_group_cache_columns():
    cols = {r["name"] for r in auth_db.conn().execute("PRAGMA table_info(groups)")}
    assert {"member_count", "member_count_synced_at", "parent_dn"} <= cols


# --------------------------------------------------------------- batch roles

def test_list_roles_for_subjects_matches_per_subject():
    gid = _mk_group("dsync_batch_grp")
    try:
        # assign two roles to the group subject
        conn = auth_db.conn()
        for rid in ("clerk", "finance"):
            conn.execute(
                "INSERT OR IGNORE INTO subject_roles(subject_type, subject_key, role_id) "
                "VALUES('group', ?, ?)", (str(gid), rid))
        conn.commit()
        batch = permissions.list_roles_for_subjects("group", [str(gid), "999999"])
        assert set(batch[str(gid)]) == {"clerk", "finance"}
        assert batch["999999"] == []          # unknown key → empty, still present
        # equivalence with the per-subject helper
        assert set(batch[str(gid)]) == set(
            permissions.list_roles_for_subject("group", str(gid)))
    finally:
        auth_db.conn().execute("DELETE FROM subject_roles WHERE subject_key=?", (str(gid),))
        _rm_group("dsync_batch_grp")


def test_list_roles_for_subjects_empty_input():
    assert permissions.list_roles_for_subjects("user", []) == {}


# --------------------------------------------------- list_groups cached fields

def test_list_groups_exposes_dir_cache_fields():
    gid = _mk_group("dsync_fields_grp", source="ldap", dn="cn=x,dc=t")
    try:
        conn = auth_db.conn()
        conn.execute("UPDATE groups SET member_count=?, member_count_synced_at=? "
                     "WHERE id=?", (55, 1700000000.0, gid))
        conn.commit()
        g = next(x for x in group_manager.list_groups() if x["id"] == gid)
        assert g["dir_member_count"] == 55
        assert g["dir_synced_at"] == 1700000000.0
        assert g["parent_dn"] == ""
    finally:
        _rm_group("dsync_fields_grp")


# ------------------------------------------------------------------ settings

def test_settings_defaults_and_clamp():
    s = directory_sync.save_settings(enabled=True, interval_hours=999, name_contains="  UG_  ")
    assert s["interval_hours"] == 168        # clamped to 7 days
    assert s["name_contains"] == "UG_"
    s2 = directory_sync.save_settings(interval_hours=0)
    assert s2["interval_hours"] == 1          # clamped up


# ------------------------------------------------------------------- run_sync

def test_run_sync_caches_member_counts(monkeypatch):
    g1 = _mk_group("dsync_run_g1", source="ldap", dn="cn=g1,dc=t")
    g2 = _mk_group("dsync_run_g2", source="ldap", dn="cn=g2,dc=t")
    try:
        monkeypatch.setattr(directory_sync, "is_directory_backend", lambda: True)
        import app.core.auth_ldap as al
        monkeypatch.setattr(
            al, "sync_all_groups",
            lambda name_contains="": {"synced": 0, "updated": 0, "total_seen": 2})
        counts = {"cn=g1,dc=t": 42, "cn=g2,dc=t": 7}
        monkeypatch.setattr(al, "count_group_members", lambda dn: counts[dn])
        monkeypatch.setattr(al, "sync_all_users",
                            lambda name_contains="": {"synced": 3, "updated": 1,
                                                      "total_seen": 4, "skipped_clash": 0})
        directory_sync.save_settings(sync_users=True)
        rep = directory_sync.run_sync()
        assert rep.get("counts_updated") == 2
        assert rep.get("counts_failed") == 0
        assert rep.get("users_synced", {}).get("synced") == 3
        conn = auth_db.conn()
        r1 = conn.execute("SELECT member_count, member_count_synced_at FROM groups WHERE id=?",
                          (g1,)).fetchone()
        assert r1["member_count"] == 42 and r1["member_count_synced_at"] is not None
        r2 = conn.execute("SELECT member_count FROM groups WHERE id=?", (g2,)).fetchone()
        assert r2["member_count"] == 7
    finally:
        _rm_group("dsync_run_g1")
        _rm_group("dsync_run_g2")


def test_run_sync_noop_on_non_directory_backend(monkeypatch):
    monkeypatch.setattr(directory_sync, "is_directory_backend", lambda: False)
    rep = directory_sync.run_sync()
    assert "skipped" in rep


# --------------------------- view filter + pagination (OOM fix) --------------

def test_list_users_view_filter_excludes_mirror_catalog():
    from app.core import user_manager
    a = _mk_user("vf_active", "ad", "cn=va,dc=t", last_login=1700000000.0, enabled=1)
    m = _mk_user("vf_mirror", "ad", "cn=vm,dc=t", last_login=None, enabled=0)
    loc = _mk_user("vf_local", "local", "", last_login=None, enabled=1)
    try:
        active = {u["username"] for u in user_manager.list_users(view="active")}
        directory = {u["username"] for u in user_manager.list_users(view="directory")}
        assert "vf_active" in active and "vf_local" in active
        assert "vf_mirror" not in active            # mirror catalog hidden
        assert directory == {"vf_mirror"}
        assert user_manager.count_users(view="directory") >= 1
    finally:
        for u in ("vf_active", "vf_mirror", "vf_local"):
            _rm_user(u)


def test_group_list_names_and_page():
    from app.core import group_manager
    ids = [_mk_group(f"pg_grp_{i}") for i in range(5)]
    try:
        names = group_manager.list_group_names(q="pg_grp_")
        assert len(names) >= 5 and all("member_ids" not in n for n in names)
        page = group_manager.list_groups_page(offset=0, limit=2, q="pg_grp_")
        assert page["total"] >= 5 and len(page["rows"]) == 2
        assert all(r.get("depth") == 0 for r in page["rows"])
    finally:
        for i in range(5):
            _rm_group(f"pg_grp_{i}")


# --------------------------- migration v12: unprovision mirrored users -------

def _mk_user(username, source="ldap", dn="", last_login=None, enabled=1):
    conn = auth_db.conn()
    cur = conn.execute(
        "INSERT INTO users(username, display_name, source, external_dn, enabled, "
        "is_admin_seed, created_at, last_login_at) VALUES(?,?,?,?,?,0,?,?)",
        (username, username, source, dn, enabled, "2026-01-01", last_login))
    conn.commit()
    return cur.lastrowid


def _rm_user(username):
    conn = auth_db.conn()
    conn.execute("DELETE FROM users WHERE username=?", (username,))
    conn.commit()


def test_m12_unprovisions_only_never_logged_in_mirrors():
    from app.core import auth_db as adb, permissions
    conn = auth_db.conn()
    # never-logged-in mirror (the v1.12.69 casualty) with the default role
    u_mirror = _mk_user("m12_mirror", "ldap", "cn=mirror,dc=t", last_login=None, enabled=1)
    permissions.set_subject_roles("user", str(u_mirror), ["default-user"])
    # genuinely logged-in ldap user with a role — must stay untouched
    u_active = _mk_user("m12_active", "ldap", "cn=active,dc=t", last_login=1700000000.0, enabled=1)
    permissions.set_subject_roles("user", str(u_active), ["default-user"])
    # local user — untouched
    u_local = _mk_user("m12_local", "local", "", last_login=None, enabled=1)
    # never-logged-in mirror that admin gave a CUSTOM role — keep custom, drop default
    u_custom = _mk_user("m12_custom", "ad", "cn=custom,dc=t", last_login=None, enabled=1)
    permissions.set_subject_roles("user", str(u_custom), ["clerk", "default-user"])
    try:
        adb._m12_unprovision_mirrored_users(conn)
        conn.commit()

        def _row(uid):
            return conn.execute("SELECT enabled FROM users WHERE id=?", (uid,)).fetchone()

        # mirror: de-activated + default role removed
        assert _row(u_mirror)["enabled"] == 0
        assert permissions.list_roles_for_subject("user", str(u_mirror)) == []
        # active: fully untouched
        assert _row(u_active)["enabled"] == 1
        assert "default-user" in permissions.list_roles_for_subject("user", str(u_active))
        # local: untouched
        assert _row(u_local)["enabled"] == 1
        # custom mirror: de-activated, default removed, custom kept
        assert _row(u_custom)["enabled"] == 0
        assert permissions.list_roles_for_subject("user", str(u_custom)) == ["clerk"]
    finally:
        for u in ("m12_mirror", "m12_active", "m12_local", "m12_custom"):
            _rm_user(u)


def test_m12_idempotent():
    from app.core import auth_db as adb
    conn = auth_db.conn()
    u = _mk_user("m12_idem", "ldap", "cn=idem,dc=t", last_login=None, enabled=1)
    try:
        adb._m12_unprovision_mirrored_users(conn); conn.commit()
        adb._m12_unprovision_mirrored_users(conn); conn.commit()   # 2nd run = no-op
        assert conn.execute("SELECT enabled FROM users WHERE id=?", (u,)).fetchone()["enabled"] == 0
    finally:
        _rm_user("m12_idem")


# --------------------------------------------------- group hierarchy (tree)

def _g(gid, name, dn="", parent=""):
    return {"id": gid, "name": name, "source": "ldap",
            "external_dn": dn, "parent_dn": parent}


def test_order_groups_as_tree_nesting_and_depth():
    groups = [
        _g(1, "資訊處", "cn=it,dc=t"),
        _g(2, "技術服務部", "cn=svc,dc=t", "cn=it,dc=t"),
        _g(3, "網路組", "cn=net,dc=t", "cn=svc,dc=t"),
        _g(4, "人資處", "cn=hr,dc=t"),
    ]
    out = group_manager.order_groups_as_tree(groups)
    depth = {g["name"]: g["depth"] for g in out}
    assert depth == {"資訊處": 0, "技術服務部": 1, "網路組": 2, "人資處": 0}
    # parent always appears before its child
    order = [g["name"] for g in out]
    assert order.index("資訊處") < order.index("技術服務部") < order.index("網路組")
    assert len(out) == 4                       # every group emitted exactly once


def test_order_groups_as_tree_cycle_safe():
    groups = [
        _g(1, "A", "cn=a,dc=t", "cn=b,dc=t"),
        _g(2, "B", "cn=b,dc=t", "cn=a,dc=t"),
    ]
    out = group_manager.order_groups_as_tree(groups)
    assert {g["name"] for g in out} == {"A", "B"}   # no infinite loop, both once


def test_order_groups_as_tree_local_and_unknown_parent_are_roots():
    groups = [
        _g(1, "本機群組", ""),                      # local → root
        _g(2, "孤兒", "cn=x,dc=t", "cn=missing,dc=t"),  # parent not in set → root
    ]
    out = group_manager.order_groups_as_tree(groups)
    assert all(g["depth"] == 0 for g in out)
    assert len(out) == 2


def test_run_sync_counts_failures(monkeypatch):
    gid = _mk_group("dsync_fail_g", source="ldap", dn="cn=bad,dc=t")
    try:
        monkeypatch.setattr(directory_sync, "is_directory_backend", lambda: True)
        import app.core.auth_ldap as al
        monkeypatch.setattr(al, "sync_all_groups",
                            lambda name_contains="": {"total_seen": 1})

        def _boom(dn):
            raise RuntimeError("ldap down")
        monkeypatch.setattr(al, "count_group_members", _boom)
        monkeypatch.setattr(al, "sync_all_users",
                            lambda name_contains="": {"synced": 0, "updated": 0,
                                                      "total_seen": 0, "skipped_clash": 0})
        rep = directory_sync.run_sync()
        assert rep.get("counts_failed", 0) >= 1
        assert rep.get("counts_updated") == 0
    finally:
        _rm_group("dsync_fail_g")


def test_run_sync_user_sync_failure_keeps_group_results(monkeypatch):
    """A user-sync exception must not lose the already-committed group counts."""
    gid = _mk_group("dsync_uf_g", source="ldap", dn="cn=ok,dc=t")
    try:
        monkeypatch.setattr(directory_sync, "is_directory_backend", lambda: True)
        import app.core.auth_ldap as al
        monkeypatch.setattr(al, "sync_all_groups",
                            lambda name_contains="": {"total_seen": 1})
        monkeypatch.setattr(al, "count_group_members", lambda dn: 5)

        def _boom(name_contains=""):
            raise RuntimeError("user enumerate failed")
        monkeypatch.setattr(al, "sync_all_users", _boom)
        directory_sync.save_settings(sync_users=True)
        rep = directory_sync.run_sync()
        assert rep.get("counts_updated") == 1                 # group result kept
        assert "error" in (rep.get("users_synced") or {})     # user error captured
    finally:
        _rm_group("dsync_uf_g")


def test_run_sync_skips_users_when_disabled(monkeypatch):
    gid = _mk_group("dsync_nou_g", source="ldap", dn="cn=nou,dc=t")
    try:
        monkeypatch.setattr(directory_sync, "is_directory_backend", lambda: True)
        import app.core.auth_ldap as al
        monkeypatch.setattr(al, "sync_all_groups",
                            lambda name_contains="": {"total_seen": 1})
        monkeypatch.setattr(al, "count_group_members", lambda dn: 5)
        called = {"users": False}

        def _mark(name_contains=""):
            called["users"] = True
            return {}
        monkeypatch.setattr(al, "sync_all_users", _mark)
        directory_sync.save_settings(sync_users=False)
        rep = directory_sync.run_sync()
        assert called["users"] is False
        assert rep.get("users_synced") is None
    finally:
        directory_sync.save_settings(sync_users=True)   # restore default
        _rm_group("dsync_nou_g")
