"""事務機掃描取件：誰看得到哪台、換電腦後設定還在不在（v1.17.5，客戶回報「新增的事務機好像只有在該台機器有」）。

全盤檢查後改的幾件事：
* 信任的憑證、HTTP / 未加密的同意、上次的信箱編號與帳號：原本存在瀏覽器，換電腦就要重來 → 存伺服器、跟著帳號走；
  **不存密碼**。
* 共用事務機：管理員確認過的憑證與同意，成員直接沿用；成員不能自己改信任別的指紋，請求裡帶別的指紋也沒用。
* 未啟用登入時新增的事務機（記在「本機」名下），啟用登入後任何帳號都看不到 → 管理員可以轉成共用事務機。
* 用舊的辨識規則判斷、認不出來的事務機，清單標「可重新測試」。
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app.core import mfp_profiles, mfp_store
from tests.test_mfp_shared_and_scan import API, _make_shared, escl_probe, people  # noqa: F401
from tests.test_mfp_fetch_tool import _detect, fake_probe  # noqa: F401

R = importlib.import_module("app.tools.mfp_fetch.router")
FP_A = "a" * 64
FP_B = "b" * 64


def _save_own(client, host="192.168.1.70"):
    tok = _detect(client, host).json()["token"]
    r = client.post(f"{API}/connections", json={"token": tok, "display_name": "自己的"})
    assert r.status_code == 200, r.text
    return r.json()["connection"]["id"]


def _second_session(uid):
    """同一個帳號、另一台電腦（另一個瀏覽器工作階段）。"""
    from app.core import sessions
    token, _ = sessions.issue(uid, remember=False, ip="127.0.0.2", ua="pytest-other-pc")
    c = TestClient(app_main.app)
    c.cookies.set(sessions.COOKIE_NAME, token)
    return c


def test_prefs_follow_the_account_to_another_computer(people):
    p = people
    cid = _save_own(p["a"])
    r = p["a"].put(f"{API}/connections/{cid}/prefs", json={"kind": "box", "patch": {
        "trust": FP_A, "http": True, "box": "96", "user": "clerk", "password": "secret", "evil": 1}})
    assert r.status_code == 200, r.text
    other = _second_session(p["ua"])
    assert cid in [x["id"] for x in other.get(f"{API}/connections").json()["connections"]], "清單跟著帳號，不是跟著電腦"
    pr = other.get(f"{API}/connections/{cid}/prefs?kind=box").json()["prefs"]
    assert pr == {"trust": FP_A, "http": True, "box": "96", "user": "clerk"}, pr
    raw = mfp_store._conn().execute("SELECT data FROM mfp_prefs").fetchall()
    assert all("secret" not in r["data"] for r in raw), "密碼一律不存"


def test_prefs_are_private_to_each_account(people):
    p = people
    cid = _save_own(p["a"])
    assert p["b"].get(f"{API}/connections/{cid}/prefs?kind=box").status_code == 404
    assert p["b"].put(f"{API}/connections/{cid}/prefs", json={"kind": "box", "patch": {"trust": FP_B}}).status_code == 404


def test_invalid_values_are_dropped():
    assert mfp_store.clean_prefs({"trust": "xyz", "http": "yes", "box": "9a", "target": "ftp:21",
                                  "user": "x" * 300, "route": "kyocera_soap"}) == {"route": "kyocera_soap"}
    assert mfp_store.clean_prefs({"trust": "", "box": 12}) == {"trust": None, "box": "12"}


def _shared_with_member(p):
    cid = _make_shared(p["admin"])
    r = p["admin"].put(f"{API}/shared/{cid}/members", json={"members": [{"type": "user", "key": str(p["ua"])}]})
    assert r.status_code == 200, r.text
    return cid


def test_admin_trust_on_a_shared_device_is_used_by_members(people):
    p = people
    cid = _shared_with_member(p)
    assert p["admin"].put(f"{API}/connections/{cid}/prefs",
                          json={"kind": "share", "patch": {"trust": FP_A, "http": True}}).status_code == 200
    pr = p["a"].get(f"{API}/connections/{cid}/prefs?kind=share").json()["prefs"]
    assert pr["trust"] == FP_A and pr["http"] is True and set(pr["managed"]) == {"trust", "http"}
    r = p["a"].put(f"{API}/connections/{cid}/prefs", json={"kind": "share", "patch": {"trust": FP_B}})
    assert r.status_code == 409 and r.json()["code"] == "PREF_MANAGED", "成員不能改信任別的憑證"
    # 請求裡帶別的指紋也沒用：伺服器用管理員那一份
    c = mfp_store.get_visible(f"u{p['ua']}", [("user", str(p["ua"]))], cid)

    class W:
        owner = f"u{p['ua']}"
    assert R._effective(W(), c, "share", FP_B, False, False) == (FP_A, True, False)


def test_member_can_trust_for_themselves_when_admin_has_not(people):
    p = people
    cid = _shared_with_member(p)
    assert p["a"].put(f"{API}/connections/{cid}/prefs",
                      json={"kind": "box", "patch": {"trust": FP_B, "box": "3"}}).status_code == 200
    pr = p["a"].get(f"{API}/connections/{cid}/prefs?kind=box").json()["prefs"]
    assert pr["trust"] == FP_B and pr["box"] == "3" and pr["managed"] == []
    # 之後管理員確認了另一個：以管理員的為準
    p["admin"].put(f"{API}/connections/{cid}/prefs", json={"kind": "box", "patch": {"trust": FP_A}})
    assert p["a"].get(f"{API}/connections/{cid}/prefs?kind=box").json()["prefs"]["trust"] == FP_A


def test_trust_changes_are_audited(people, monkeypatch):
    from app.core import audit_db
    seen = []
    monkeypatch.setattr(audit_db, "log_event", lambda ev, **kw: seen.append((ev, kw)))
    p = people
    cid = _save_own(p["a"])
    p["a"].put(f"{API}/connections/{cid}/prefs", json={"kind": "box", "patch": {"trust": FP_A}})
    ev = [kw for e, kw in seen if e == "mfp_trust"]
    assert ev and ev[-1]["details"]["trust"] == FP_A[:16]
    p["a"].put(f"{API}/connections/{cid}/prefs", json={"kind": "box", "patch": {"box": "5"}})
    assert len([1 for e, _ in seen if e == "mfp_trust"]) == 1, "只有信任 / 同意才寫稽核"


def test_devices_added_without_sign_in_can_be_adopted_as_shared(people):
    p = people
    # 測試資料庫整個套件共用：先清掉別支測試留下的「本機」事務機，台數才算得準
    mfp_store._conn().execute("DELETE FROM mfp_connections WHERE owner_scope='local'")
    c = mfp_store.create("local", None, "192.168.1.81", 443, "https", "沒登入時加的", None)
    mfp_store.create("local", None, "192.168.1.60", None, None, "跟共用重複", None)
    _make_shared(p["admin"], host="192.168.1.60")
    try:
        assert p["admin"].get(f"{API}/shared/local").json()["count"] == 2
        assert p["a"].get(f"{API}/shared/local").status_code in (401, 403), "只有管理員"
        assert p["a"].post(f"{API}/shared/adopt-local").status_code in (401, 403)
        r = p["admin"].post(f"{API}/shared/adopt-local").json()
        assert r["moved"] == 1 and r["left"] == 1, "跟既有共用事務機重複的那台不轉、不覆蓋"
        assert c["id"] in [x["id"] for x in mfp_store.list_shared()]
    finally:
        mfp_store._conn().execute("DELETE FROM mfp_connections WHERE owner_scope='local'")


def test_deleting_an_account_removes_their_prefs(people):
    p = people
    cid = _save_own(p["a"])
    p["a"].put(f"{API}/connections/{cid}/prefs", json={"kind": "box", "patch": {"box": "1"}})
    mfp_store.purge_user(p["ua"])
    assert not mfp_store._conn().execute("SELECT 1 FROM mfp_prefs WHERE owner_scope=?", (f"u{p['ua']}",)).fetchone()


@pytest.mark.parametrize("status,rules,stale", [
    ("unknown", None, True), ("unsupported_vendor", "old-rules", True), ("login_required", None, True),
    ("unknown", "CURRENT", False), ("supported", None, False), ("experimental_vendor", None, False)])
def test_old_rules_with_an_unidentified_result_suggest_a_retest(status, rules, stale):
    rules = mfp_profiles.rules_signature() if rules == "CURRENT" else rules
    det = {"status": status, **({"rules": rules} if rules else {})}
    c = mfp_store.create("u987654", 987654, "192.168.1.90", None, None, "x", {"detection": det, "summary": {}})
    try:
        row = mfp_store.get_for("u987654", c["id"])
        assert row["rules_stale"] is stale, (status, rules)
    finally:
        mfp_store.delete("u987654", c["id"])


def test_rules_signature_changes_when_a_rule_changes(monkeypatch):
    import re
    before = mfp_profiles.rules_signature()
    mfp_profiles.rules_signature.cache_clear()
    monkeypatch.setattr(mfp_profiles, "_OTHER_VENDORS",
                        mfp_profiles._OTHER_VENDORS + (("Acme", re.compile("ACME")),))
    try:
        assert mfp_profiles.rules_signature() != before
    finally:
        mfp_profiles.rules_signature.cache_clear()


def test_new_detections_record_the_rules():
    import inspect
    from app.core import mfp_client
    assert "rules=mfp_profiles.rules_signature()" in inspect.getsource(mfp_client)


def test_migration_from_v3_keeps_connections_and_adds_prefs(tmp_path):
    """`_m4_prefs`（v1.17.5）只新增 `mfp_prefs`：v3 的事務機與作業紀錄都還在，新表可以寫、刪事務機時一起刪。"""
    import sqlite3
    from app.core import db
    p = tmp_path / "mfp.sqlite"
    c = sqlite3.connect(p)
    for m in mfp_store._MIGRATIONS[:3]:
        m(c)
    c.execute("INSERT INTO mfp_connections(owner_scope, owner_user_id, host, display_name, created_at, updated_at)"
              " VALUES ('u7', 7, '192.168.1.50', '三樓', 1, 1)")
    c.execute("PRAGMA user_version=3")
    c.commit()
    c.close()
    assert db.migrate(p, mfp_store._MIGRATIONS) == len(mfp_store._MIGRATIONS)
    c = sqlite3.connect(p)
    c.execute("PRAGMA foreign_keys=ON")
    assert c.execute("SELECT host, display_name FROM mfp_connections").fetchall() == [("192.168.1.50", "三樓")]
    cid = c.execute("SELECT id FROM mfp_connections").fetchone()[0]
    c.execute("INSERT INTO mfp_prefs(owner_scope, conn_id, kind, data, updated_at) VALUES ('u7', ?, 'box', '{}', 1)", (cid,))
    c.execute("DELETE FROM mfp_connections WHERE id=?", (cid,))
    assert c.execute("SELECT COUNT(*) FROM mfp_prefs").fetchone()[0] == 0
    c.close()
