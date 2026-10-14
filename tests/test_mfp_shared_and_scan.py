"""共用事務機（管理員建立、加成員）、排隊、從事務機掃描進工作區 —— 路由層。

使用者 2026-10-14：
* 「這頁除了使用者自己加，也可以讓管理員在設定中建好，然後再把使用者成員拉進來，它們就可以看到，不用自己建」
* 「要避免同時多人存取事務機 要排隊機制，以免塞爆事務機作業」
* 從事務機掃描，直接進工作區（小型機沒有信箱）

驗：
* 成員看得到（直接加的使用者、群組、巢狀群組的上層），非成員看不到（回「找不到」，不透露存在）；
  成員可以測試、掃描、下載報告，不能改名、移除；管理端點只有管理員。
* 成員清單只收存在的使用者與群組；刪帳號、刪群組時一起拿掉。
* 同一台設備的連線測試要排隊（正在掃描時，等不到就說「正忙」）；掃描排隊有上限。
* 掃描結果存進**送出者自己**的工作區；工作區停用時留在暫存區給本人下載；別人拿 id 查不到也下載不到。

真正的網路連線在 `test_mfp_client.py` / `test_mfp_escl.py`；這裡把 `probe`、`scan` 換成假的。
"""
from __future__ import annotations

import importlib
import threading
import time

import pytest

from app.core import mfp_client, mfp_escl, mfp_queue, mfp_scan, mfp_store
from tests.test_mfp_escl import caps_xml, pdf_bytes
from tests.test_mfp_fetch_tool import _detect, _fake_report, _user_client, fake_probe  # noqa: F401

R = importlib.import_module("app.tools.mfp_fetch.router")
API = "/tools/mfp-fetch/api"


def _report_with_escl(host="192.168.1.50"):
    rep = _fake_report(host)
    caps = mfp_escl.parse_capabilities(caps_xml())
    summ = mfp_escl.summarize(caps)
    rep["escl"] = {"scheme": "http", "port": 80, "version": caps["version"], "model": summ["model"],
                   "caps": caps, "sources": summ["sources"]}
    return rep


def _clear_shared():
    """共用事務機是全站共用的範圍（不像個人的有各自的擁有者）—— 每支測試前後清掉，免得互相干擾。"""
    c = mfp_store._conn()
    c.execute("DELETE FROM mfp_connections WHERE owner_scope=?", (mfp_store.SHARED,))
    c.execute("DELETE FROM mfp_members")
    c.execute("DELETE FROM mfp_scans")


@pytest.fixture
def escl_probe(monkeypatch, fake_probe):
    monkeypatch.setattr(mfp_client, "probe", lambda host, port=None, scheme=None: _report_with_escl(
        mfp_client.parse_host(host)))
    mfp_queue._reset_for_tests()
    mfp_scan._reset_for_tests()
    _clear_shared()
    yield
    _clear_shared()
    mfp_queue._reset_for_tests()


@pytest.fixture
def people(admin_session, escl_probe):
    from app.core import group_manager
    admin, _u, _p = admin_session
    a, ua = _user_client("clerk-sa", ["clerk"])
    b, ub = _user_client("clerk-sb", ["clerk"])
    c, uc = _user_client("clerk-sc", ["clerk"])
    g = group_manager.create_local("三樓行政")
    group_manager.set_members(g, [ub])
    return {"admin": admin, "a": a, "b": b, "c": c, "ua": ua, "ub": ub, "uc": uc, "g": g}


def _make_shared(admin, host="192.168.1.60", name="三樓共用"):
    tok = _detect(admin, host).json()["token"]
    r = admin.post(f"{API}/shared", json={"token": tok, "display_name": name})
    assert r.status_code == 200, r.text
    return r.json()["connection"]["id"]


def _ids(c):
    return [x["id"] for x in c.get(f"{API}/connections").json()["connections"]]


# --------------------------------------------------------------------- 共用事務機

def test_members_see_shared_devices_and_others_do_not(people):
    p = people
    cid = _make_shared(p["admin"])
    r = p["admin"].put(f"{API}/shared/{cid}/members", json={"members": [
        {"type": "user", "key": str(p["ua"])}, {"type": "group", "key": str(p["g"])}]})
    assert r.status_code == 200, r.text
    assert cid in _ids(p["a"]), "直接加的使用者看得到"
    assert cid in _ids(p["b"]), "群組成員看得到"
    assert cid not in _ids(p["c"]), "不是成員看不到"
    row = next(x for x in p["a"].get(f"{API}/connections").json()["connections"] if x["id"] == cid)
    assert row["shared"] is True and row["can_manage"] is False and row["scan"]["sources"]
    # 非成員：查詳細、測試、報告、掃描一律「找不到」
    for m, url in (("get", f"{API}/connections/{cid}"), ("post", f"{API}/connections/{cid}/test"),
                   ("get", f"{API}/connections/{cid}/report"), ("post", f"{API}/connections/{cid}/scan")):
        assert getattr(p["c"], m)(url).status_code == 404, url


def test_members_cannot_rename_or_remove_a_shared_device(people):
    p = people
    cid = _make_shared(p["admin"])
    p["admin"].put(f"{API}/shared/{cid}/members", json={"members": [{"type": "user", "key": str(p["ua"])}]})
    assert p["a"].patch(f"{API}/connections/{cid}", json={"display_name": "x"}).json()["code"] == "SHARED_READ_ONLY"
    assert p["a"].delete(f"{API}/connections/{cid}").status_code == 403
    assert p["a"].post(f"{API}/connections/{cid}/test").status_code == 200, "成員可以重新測試"
    assert cid in _ids(p["a"])


def test_shared_management_is_admin_only(people):
    p = people
    cid = _make_shared(p["admin"])
    a = p["a"]
    tok = _detect(a, "192.168.1.61").json()["token"]
    assert a.post(f"{API}/shared", json={"token": tok}).status_code == 403
    assert a.get(f"{API}/shared").status_code == 403
    assert a.patch(f"{API}/shared/{cid}", json={"display_name": "x"}).status_code == 403
    assert a.put(f"{API}/shared/{cid}/members", json={"members": []}).status_code == 403
    assert a.delete(f"{API}/shared/{cid}").status_code == 403
    assert a.get(f"{API}/shared-subjects").status_code == 403
    assert p["admin"].get(f"{API}/shared").json()["connections"][0]["id"] == cid


def test_member_list_only_accepts_real_users_and_groups(people):
    p = people
    cid = _make_shared(p["admin"])
    for bad in ([{"type": "user", "key": "999999"}], [{"type": "group", "key": "999999"}],
                [{"type": "role", "key": "admin"}], [{"type": "user", "key": "1 OR 1=1"}], "x", [1]):
        r = p["admin"].put(f"{API}/shared/{cid}/members", json={"members": bad})
        assert r.status_code == 400, bad
    assert mfp_store.get_members(cid) == []


def test_a_nested_group_member_sees_the_device(people, monkeypatch):
    """設備加給上層群組，子群組的成員也看得到 —— 跟權限系統用同一個解析（含巢狀上層，見
    `permissions._user_groups_local`），不另外寫一套。"""
    from app.core import permissions
    p = people
    cid = _make_shared(p["admin"])
    mfp_store.set_members(cid, [("group", "424242")])
    assert cid not in _ids(p["c"])
    real = permissions._user_groups_local
    monkeypatch.setattr(permissions, "_user_groups_local",
                        lambda conn, uid: real(conn, uid) + (["424242"] if uid == p["uc"] else []))
    assert cid in _ids(p["c"])


def test_deleting_a_user_or_group_removes_the_membership(people):
    from app.core import group_manager, user_manager
    p = people
    cid = _make_shared(p["admin"])
    mfp_store.set_members(cid, [("user", str(p["ua"])), ("group", str(p["g"]))])
    user_manager.delete(p["ua"])
    group_manager.delete(p["g"])
    assert mfp_store.get_members(cid) == [], "刪帳號 / 刪群組時成員資格要一起拿掉"
    assert mfp_store.get_for(mfp_store.SHARED, cid), "共用事務機本身留著"


def test_removing_a_shared_device(people):
    p = people
    cid = _make_shared(p["admin"])
    mfp_store.set_members(cid, [("user", str(p["ua"]))])
    assert p["admin"].delete(f"{API}/shared/{cid}").status_code == 200
    assert cid not in _ids(p["a"]) and mfp_store.get_members(cid) == []


# --------------------------------------------------------------------- 排隊

def test_a_connection_test_waits_for_a_device_that_is_scanning(people, monkeypatch):
    p = people
    monkeypatch.setattr(R, "PROBE_WAIT", 0.3)
    t = mfp_queue.enqueue("192.168.1.50", "u-someone-else", "scan")
    assert mfp_queue.wait_turn(t, timeout=1)
    r = _detect(p["a"], "192.168.1.50")
    assert r.status_code == 429 and r.json()["code"] == "DEVICE_BUSY"
    mfp_queue.done(t)
    assert _detect(p["a"], "192.168.1.50").status_code == 200


def test_the_list_shows_a_busy_device(people):
    p = people
    tok = _detect(p["a"], "192.168.1.50").json()["token"]
    cid = p["a"].post(f"{API}/connections", json={"token": tok}).json()["connection"]["id"]
    t1 = mfp_queue.enqueue("192.168.1.50", "x", "scan")
    mfp_queue.enqueue("192.168.1.50", "y", "scan")
    row = next(x for x in p["a"].get(f"{API}/connections").json()["connections"] if x["id"] == cid)
    assert row["queue"] == {"busy": True, "waiting": 1, "kind": "scan"}
    mfp_queue.done(t1)


# --------------------------------------------------------------------- 掃描

@pytest.fixture
def fake_scan(monkeypatch, tmp_path):
    gate = threading.Event()
    gate.set()
    calls = []

    def scan(host, ip, scheme, port, caps, opts, workdir, on_progress=None, is_cancelled=None):
        calls.append({"host": host, "ip": ip, "scheme": scheme, "port": port, "opts": dict(opts)})
        on_progress and on_progress({"phase": "scanning", "pages": 0})
        while not gate.wait(0.05):
            if is_cancelled and is_cancelled():
                raise mfp_escl.ScanError("SCAN_CANCELLED")
        from pathlib import Path
        Path(workdir).mkdir(parents=True, exist_ok=True)
        out = Path(workdir) / "scan.pdf"
        out.write_bytes(pdf_bytes(2))
        return {"path": out, "pages": 2, "bytes": out.stat().st_size, "settings": {"source": "platen"},
                "tls_verified": None}

    monkeypatch.setattr(mfp_escl, "scan", scan)
    return {"gate": gate, "calls": calls}


def _wait_scan(c, sid, until=("done", "error", "cancelled"), timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get(f"{API}/scans/{sid}").json()["scan"]
        if s["status"] in until:
            return s
        time.sleep(0.05)
    raise AssertionError(f"掃描沒有到 {until}：{s}")


def _own_device(c, host="192.168.1.50"):
    tok = _detect(c, host).json()["token"]
    return c.post(f"{API}/connections", json={"token": tok}).json()["connection"]["id"]


def test_scan_goes_into_my_workspace(people, fake_scan, monkeypatch):
    from app.core import workspace
    monkeypatch.setattr(workspace, "is_enabled", lambda: True)
    p = people
    cid = _own_device(p["a"])
    r = p["a"].post(f"{API}/connections/{cid}/scan", json={"source": "platen", "color": "gray",
                                                             "resolution": 300, "file_name": "../合約掃描"})
    assert r.status_code == 202, r.text
    sid = r.json()["scan"]["id"]
    s = _wait_scan(p["a"], sid)
    assert s["status"] == "done" and s["pages"] == 2 and s["workspace_file_id"], s
    assert s["file_name"] == "合約掃描.pdf", "檔名不可以帶路徑"
    assert fake_scan["calls"][0]["ip"] == "192.168.1.50" and fake_scan["calls"][0]["port"] == 80
    meta = workspace.find_for_key(f"u{p['ua']}", s["workspace_file_id"])
    assert meta and meta["name"].endswith(".pdf"), "要在送出者自己的工作區"
    assert workspace.find_for_key(f"u{p['ub']}", s["workspace_file_id"]) is None
    # 別人拿 id：查不到、取消不了、下載不到
    for m, url in (("get", f"{API}/scans/{sid}"), ("post", f"{API}/scans/{sid}/cancel"),
                   ("get", f"{API}/scans/{sid}/download")):
        assert getattr(p["b"], m)(url).status_code == 404, url


def test_scan_settings_outside_the_capabilities_are_refused_before_queueing(people, fake_scan):
    p = people
    cid = _own_device(p["a"])
    r = p["a"].post(f"{API}/connections/{cid}/scan", json={"source": "adf"})
    assert r.status_code == 400 and r.json()["code"] == "SCAN_OPTION_INVALID"
    assert not fake_scan["calls"] and mfp_queue.snapshot("192.168.1.50")["busy"] is False


def test_a_device_without_escl_cannot_scan(people, monkeypatch):
    p = people
    monkeypatch.setattr(mfp_client, "probe", lambda host, port=None, scheme=None: _fake_report(host))
    cid = _own_device(p["a"])
    r = p["a"].post(f"{API}/connections/{cid}/scan", json={})
    assert r.status_code == 400 and r.json()["code"] == "SCAN_NOT_SUPPORTED"


def test_scans_on_one_device_queue_up_and_can_be_cancelled(people, fake_scan, monkeypatch):
    from app.core import workspace
    monkeypatch.setattr(workspace, "is_enabled", lambda: True)
    p = people
    cid = _make_shared(p["admin"], host="192.168.1.70")
    mfp_store.set_members(cid, [("user", str(p["ua"])), ("user", str(p["ub"]))])
    fake_scan["gate"].clear()                               # 第一件卡在掃描中
    s1 = p["a"].post(f"{API}/connections/{cid}/scan", json={}).json()["scan"]["id"]
    _wait_scan(p["a"], s1, until=("running",))
    s2 = p["b"].post(f"{API}/connections/{cid}/scan", json={}).json()["scan"]
    st2 = p["b"].get(f"{API}/scans/{s2['id']}").json()["scan"]
    assert st2["status"] == "queued" and st2["ahead"] == 1, st2
    assert len(fake_scan["calls"]) == 1, "第二件不可以跟第一件同時去連設備"
    # 排隊中取消：不會碰到設備
    p["b"].post(f"{API}/scans/{s2['id']}/cancel")
    assert _wait_scan(p["b"], s2["id"])["status"] == "cancelled"
    fake_scan["gate"].set()
    assert _wait_scan(p["a"], s1)["status"] == "done"
    assert len(fake_scan["calls"]) == 1


def test_each_person_can_only_queue_a_few_on_one_device(people, fake_scan, monkeypatch):
    monkeypatch.setattr(mfp_queue, "MAX_PER_OWNER", 1)
    p = people
    cid = _own_device(p["a"])
    fake_scan["gate"].clear()
    assert p["a"].post(f"{API}/connections/{cid}/scan", json={}).status_code == 202
    r = p["a"].post(f"{API}/connections/{cid}/scan", json={})
    assert r.status_code == 429 and r.json()["code"] == "OWNER_QUEUE_FULL"
    fake_scan["gate"].set()


def test_without_a_workspace_the_scan_waits_for_download(people, fake_scan, monkeypatch):
    from app.core import workspace
    monkeypatch.setattr(workspace, "is_enabled", lambda: False)
    p = people
    cid = _own_device(p["a"])
    sid = p["a"].post(f"{API}/connections/{cid}/scan", json={"file_name": "x"}).json()["scan"]["id"]
    s = _wait_scan(p["a"], sid)
    assert s["status"] == "done" and not s["workspace_file_id"] and s["workspace_note"] == "WS_DISABLED"
    assert s["has_file"] is True
    r = p["a"].get(f"{API}/scans/{sid}/download")
    assert r.status_code == 200 and r.content.startswith(b"%PDF-")


def test_scan_failures_are_recorded_with_the_reason(people, monkeypatch):
    p = people

    def boom(*a, **k):
        raise mfp_escl.ScanError("SCAN_ADF_EMPTY")

    monkeypatch.setattr(mfp_escl, "scan", boom)
    cid = _own_device(p["a"])
    sid = p["a"].post(f"{API}/connections/{cid}/scan", json={}).json()["scan"]["id"]
    s = _wait_scan(p["a"], sid)
    assert s["status"] == "error" and s["error_code"] == "SCAN_ADF_EMPTY" and s["error_message"]
    assert mfp_queue.snapshot("192.168.1.50")["busy"] is False, "失敗也要叫下一位"


def test_scans_interrupted_by_a_restart_are_marked(people):
    p = people
    cid = _own_device(p["a"])
    mfp_store.create_scan("f" * 32, conn_id=cid, owner=f"u{p['ua']}", owner_user_id=p["ua"],
                          device="192.168.1.50", conn_name="x", settings={}, file_name="x.pdf")
    mfp_scan._reset_for_tests()
    mfp_scan._init_once()
    s = p["a"].get(f"{API}/scans/{'f' * 32}").json()["scan"]
    assert s["status"] == "error" and s["error_code"] == "SCAN_INTERRUPTED"


def test_deleting_a_user_removes_their_scans(people, fake_scan):
    from app.core import user_manager
    p = people
    cid = _own_device(p["a"])
    sid = p["a"].post(f"{API}/connections/{cid}/scan", json={}).json()["scan"]["id"]
    _wait_scan(p["a"], sid)
    user_manager.delete(p["ua"])
    assert mfp_store.get_scan_any(sid) is None


# --------------------------------------------------------------------- 資料庫遷移

def test_migration_from_v1_keeps_existing_connections(tmp_path):
    """`_m2_shared_and_scans` 只新增 `mfp_members`、`mfp_scans`，不動既有的 `mfp_connections` ——
    先建 v1 結構＋塞一台事務機，再升級：那台還在、新表建好（全新資料庫升級測不到「搬丟資料」）。"""
    import sqlite3
    from app.core import db
    p = tmp_path / "mfp.sqlite"
    c = sqlite3.connect(p)
    mfp_store._m1_initial(c)
    c.execute("INSERT INTO mfp_connections(owner_scope, owner_user_id, display_name, host, port, scheme,"
              " created_at, updated_at) VALUES ('u7', 7, '三樓', '192.168.1.50', NULL, NULL, 1, 1)")
    c.execute("PRAGMA user_version=1")
    c.commit()
    c.close()
    assert db.migrate(p, mfp_store._MIGRATIONS) == 2
    c = sqlite3.connect(p)
    assert c.execute("SELECT display_name, host FROM mfp_connections").fetchall() == [("三樓", "192.168.1.50")]
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"mfp_members", "mfp_scans"} <= names
    c.close()
