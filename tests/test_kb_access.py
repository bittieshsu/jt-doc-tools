"""知識庫的存取範圍（規格 S02）：別的群組查不到、取不到段落、下載不到原檔，
而且「看不到」跟「不存在」回一樣的結果；權限一撤，下一次查詢就生效。

管理端點：只有管理員；一般使用者打每一支都要被擋。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.kb import retrieval, store
from tests._kb_support import HANDBOOK_TXT, add_doc, kb_isolated  # noqa: F401

Q = "機密文書對外發文要怎麼封裝"


@pytest.fixture
def world(admin_session, kb_isolated):
    """認證開著：管理員、A 組的 alice、不在 A 組的 bob；一個全站資料集、一個只給 A 組的。"""
    from app.core import auth_db, group_manager, permissions, user_manager
    client, _, _ = admin_session
    admin_id = auth_db.conn().execute(
        "SELECT id FROM users WHERE username='jtdt-admin'").fetchone()["id"]
    alice = user_manager.create_local("alice", "Alice", "AlicePass1234", roles=["default-user"])
    bob = user_manager.create_local("bob", "Bob", "BobPass12345", roles=["default-user"])
    gid = group_manager.create_local("A組")
    group_manager.set_members(gid, [alice])
    permissions.invalidate_cache()
    pub = store.create_dataset({"name": "全站手冊", "category": "writing_rules"})
    priv = store.create_dataset({"name": "A組專用規定", "category": "agency_rules",
                                 "access": "groups", "group_ids": [gid]})
    vpub = add_doc(pub["id"], "一、公文用語：下級對上級稱「鈞」。".encode("utf-8"), ".txt", "公開")
    vpriv = add_doc(priv["id"], HANDBOOK_TXT.encode("utf-8"), ".txt", "私有")
    priv_chunk = store.list_chunks(vpriv["id"])[0]["id"]
    yield {"client": client, "admin": admin_id, "alice": alice, "bob": bob, "gid": gid,
           "pub": pub, "priv": priv, "vpub": vpub, "vpriv": vpriv, "priv_chunk": priv_chunk}
    with __import__("app.core.db", fromlist=["tx"]).tx(auth_db.conn()):
        auth_db.conn().execute("DELETE FROM group_members")
        auth_db.conn().execute("DELETE FROM groups")


def test_group_member_sees_private_dataset(world):
    got = retrieval.search(Q, user_id=world["alice"])
    assert got and got[0]["dataset_id"] == world["priv"]["id"]
    assert retrieval.get_chunk(world["priv_chunk"], user_id=world["alice"]) is not None
    assert retrieval.open_original(world["vpriv"]["id"], user_id=world["alice"]) is not None
    names = {d["name"] for d in retrieval.list_datasets(user_id=world["alice"])}
    assert names == {"全站手冊", "A組專用規定"}


def test_other_user_cannot_see_private_dataset(world):
    assert all(r["dataset_id"] != world["priv"]["id"]
               for r in retrieval.search(Q, user_id=world["bob"]))
    assert retrieval.get_chunk(world["priv_chunk"], user_id=world["bob"]) is None
    assert retrieval.open_original(world["vpriv"]["id"], user_id=world["bob"]) is None
    names = {d["name"] for d in retrieval.list_datasets(user_id=world["bob"])}
    assert names == {"全站手冊"}
    # 指定資料集也繞不過去
    assert retrieval.search(Q, user_id=world["bob"], dataset_ids=[world["priv"]["id"]]) == []
    # 看不到與不存在回一樣的結果
    assert retrieval.get_chunk("0" * 32, user_id=world["bob"]) is None
    from app.core import kb
    st = kb.status(user_id=world["bob"])
    assert {d["name"] for d in st["datasets"]} == {"全站手冊"}


def test_revoking_membership_takes_effect_on_next_query(world):
    from app.core import group_manager
    assert retrieval.get_chunk(world["priv_chunk"], user_id=world["alice"]) is not None
    group_manager.set_members(world["gid"], [])
    assert retrieval.get_chunk(world["priv_chunk"], user_id=world["alice"]) is None
    assert all(r["dataset_id"] != world["priv"]["id"]
               for r in retrieval.search(Q, user_id=world["alice"]))


def test_admin_sees_everything_and_anonymous_sees_only_public(world):
    assert retrieval.get_chunk(world["priv_chunk"], user_id=world["admin"]) is not None
    assert retrieval.get_chunk(world["priv_chunk"], user_id=None) is None


def test_inactive_version_is_not_searched_but_history_can_still_read_it(world):
    store.transition(world["vpriv"]["id"], allowed_from=("active",), to="inactive")
    assert all(r["version_id"] != world["vpriv"]["id"]
               for r in retrieval.search(Q, user_id=world["alice"]))
    ch = retrieval.get_chunk(world["priv_chunk"], user_id=world["alice"])
    assert ch is not None and ch["active"] is False


def test_deleted_version_cannot_be_read(world):
    store.delete_version(world["vpriv"]["id"])
    assert retrieval.get_chunk(world["priv_chunk"], user_id=world["admin"]) is None


# ---------------------------------------------------------------- 管理端點
ENDPOINTS = [
    ("GET", "/admin/knowledge"),
    ("GET", "/admin/knowledge/api/overview"),
    ("POST", "/admin/knowledge/api/datasets"),
    ("POST", "/admin/knowledge/api/datasets/{did}"),
    ("POST", "/admin/knowledge/api/datasets/{did}/delete"),
    ("GET", "/admin/knowledge/api/datasets/{did}/versions"),
    ("POST", "/admin/knowledge/api/datasets/{did}/upload"),
    ("POST", "/admin/knowledge/api/versions/{vid}/activate"),
    ("POST", "/admin/knowledge/api/versions/{vid}/deactivate"),
    ("POST", "/admin/knowledge/api/versions/{vid}/delete"),
    ("POST", "/admin/knowledge/api/versions/{vid}/reprocess"),
    ("POST", "/admin/knowledge/api/versions/{vid}/meta"),
    ("GET", "/admin/knowledge/api/versions/{vid}/preview"),
    ("GET", "/admin/knowledge/api/versions/{vid}/file"),
    ("POST", "/admin/knowledge/api/search"),
    ("GET", "/admin/knowledge/api/embedding"),
    ("POST", "/admin/knowledge/api/embedding"),
    ("POST", "/admin/knowledge/api/embedding/test"),
    ("POST", "/admin/knowledge/api/embedding/models"),
    ("POST", "/admin/knowledge/api/rebuild"),
    ("POST", "/admin/knowledge/api/vectors/disable"),
    ("GET", "/admin/knowledge/api/groups"),
    # 政府公開資料（`knowledge_gov_routes.py`）
    ("GET", "/admin/knowledge/gov"),
    ("GET", "/admin/knowledge/api/gov/status"),
    ("GET", "/admin/knowledge/api/gov/{gid}/search"),
    ("GET", "/admin/knowledge/api/gov/{gid}/selection"),
    ("POST", "/admin/knowledge/api/gov/{gid}/selection"),
    ("POST", "/admin/knowledge/api/gov/{gid}/selection/reset"),
    ("POST", "/admin/knowledge/api/gov/packages/{pid}"),
    ("POST", "/admin/knowledge/api/gov/packages/{pid}/reset"),
    ("POST", "/admin/knowledge/api/gov/{gid}/download"),
    ("POST", "/admin/knowledge/api/gov/{gid}/import"),
    ("POST", "/admin/knowledge/api/gov/{gid}/update"),
    ("POST", "/admin/knowledge/api/gov/packages/{pid}/upload"),
]


def test_endpoint_list_covers_every_knowledge_route():
    """上面那份清單要跟真的路由一樣 —— 新增端點忘了加進來，下面那條就沒驗到它。"""
    import app.main as m
    from tools.route_index import iter_routes
    real = set()
    for r in iter_routes(m.app):     # 新版 FastAPI 的 include_router 不再攤平在 app.routes
        p = getattr(r, "path", "")
        if p.startswith("/admin/knowledge"):
            for meth in getattr(r, "methods", set()) - {"HEAD"}:
                real.add((meth, p.replace("{dataset_id}", "{did}").replace("{version_id}", "{vid}")))
    assert real == set(ENDPOINTS)


def test_plain_user_is_blocked_from_every_admin_endpoint(world):
    import app.main as m
    from app.core import sessions
    tok, _ = sessions.issue(world["alice"], remember=False, ip="127.0.0.1", ua="pytest")
    c = TestClient(m.app)
    c.cookies.set(sessions.COOKIE_NAME, tok)
    for meth, path in ENDPOINTS:
        url = path.format(did=world["priv"]["id"], vid=world["vpriv"]["id"],
                          gid="moj", pid="moj-law")
        r = c.request(meth, url, json={}, follow_redirects=False)
        assert r.status_code in (302, 303, 401, 403), f"{meth} {url} → {r.status_code}"
    # 什麼都沒有被改到
    assert store.get_dataset(world["priv"]["id"])["name"] == "A組專用規定"
    assert store.get_version(world["vpriv"]["id"])["status"] == "active"


def test_admin_endpoints_work_and_write_audit(world):
    c = world["client"]
    r = c.get("/admin/knowledge")
    assert r.status_code == 200 and "kbStatus" in r.text
    ov = c.get("/admin/knowledge/api/overview").json()
    assert {d["name"] for d in ov["datasets"]} == {"全站手冊", "A組專用規定"}
    priv = next(d for d in ov["datasets"] if d["name"] == "A組專用規定")
    assert priv["groups"] == [{"id": world["gid"], "name": "A組"}]
    r = c.post("/admin/knowledge/api/search", json={"query": Q})
    assert r.status_code == 200 and r.json()["results"]
    r = c.post(f"/admin/knowledge/api/versions/{world['vpriv']['id']}/deactivate")
    assert r.status_code == 200 and r.json()["status"] == "inactive"
    r = c.get(f"/admin/knowledge/api/versions/{world['vpub']['id']}/file")
    assert r.status_code == 200 and "鈞".encode("utf-8") in r.content
    import time
    from app.core import audit_db
    for _ in range(50):
        rows = audit_db.conn().execute(
            "SELECT details_json FROM audit_events WHERE event_type='kb_change'").fetchall()
        if rows:
            break
        time.sleep(0.05)
    assert any("deactivate" in r_["details_json"] for r_ in rows)


def test_bad_ids_are_404_not_500(world):
    c = world["client"]
    for url in ("/admin/knowledge/api/versions/../../etc/preview",
                "/admin/knowledge/api/versions/" + "f" * 32 + "/preview",
                "/admin/knowledge/api/datasets/not-an-id/versions"):
        assert c.get(url).status_code in (404, 400)
