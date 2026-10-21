"""API Token 綁定使用者（v1.17.6）。

使用者 2026-10-21：建了 MCP 用的 Token，MCP 回「This API token has no owner; assign one in Settings > API Token」，
可是 API Token 頁上根本沒有地方可以綁 —— v1.17.1 起只有測試呼叫過 `assign_owner`。
這裡驗：頁面有「綁定使用者」欄、新 Token 預設綁給建立的人、可以改綁與解除、輸入檢查、只有管理員、
稽核記名稱不記 Token，而且綁完 MCP 真的連得上。
"""
from __future__ import annotations

import json

import pytest

from tests.test_mcp_server import SITE, _rpc
from tests.test_mfp_fetch_tool import _user_client

A = "/admin/api/tokens"


@pytest.fixture
def admin(admin_session, monkeypatch):
    from app.core import audit_db
    seen = []
    real = audit_db.log_event
    monkeypatch.setattr(audit_db, "log_event", lambda ev, **kw: (seen.append((ev, kw)), real(ev, **kw))[1])
    c, _u, _p = admin_session
    return c, seen


def _admin_uid():
    from app.core import auth_db
    return auth_db.conn().execute("SELECT id FROM users WHERE username='jtdt-admin'").fetchone()[0]


def _owner_of(label):
    from app.core import api_tokens as at
    return next(t for t in at.api_tokens.list_tokens() if t["label"] == label)["owner_user_id"]


def test_new_tokens_are_bound_to_the_creator(admin):
    c, seen = admin
    r = c.post(f"{A}/create", json={"label": "for-ai"})
    assert r.status_code == 200
    j = r.json()
    assert j["owner_user_id"] == _admin_uid() == _owner_of("for-ai")
    ev = [kw for e, kw in seen if e == "api_token_change"]
    assert ev and ev[-1]["details"]["action"] == "create" and ev[-1]["target"] == "for-ai"
    assert j["token"] not in json.dumps([kw for _, kw in seen], ensure_ascii=False, default=str), "稽核不記 Token"


def test_bind_change_and_unbind(admin):
    from app.core import api_tokens as at, user_manager
    c, seen = admin
    tok = at.api_tokens.create("bind-me").token
    uid = user_manager.create_local("tok-owner", "Token 擁有者", "UserPass1234")
    r = c.post(f"{A}/owner", json={"token": tok, "user_id": uid})
    assert r.status_code == 200, r.text
    assert r.json()["owner"] == "tok-owner@local" and _owner_of("bind-me") == uid
    # 畫面上用遮罩也認得
    preview = next(t for t in at.api_tokens.list_tokens() if t["label"] == "bind-me")["token_preview"]
    assert c.post(f"{A}/owner", json={"token": preview, "user_id": _admin_uid()}).status_code == 200
    assert _owner_of("bind-me") == _admin_uid()
    assert c.post(f"{A}/owner", json={"token": tok, "user_id": None}).status_code == 200
    assert _owner_of("bind-me") is None
    ev = [kw for e, kw in seen if e == "api_token_change" and kw["details"]["action"] == "owner"]
    assert [x["details"]["owner"] for x in ev] == ["tok-owner@local", "jtdt-admin@local", None]
    assert all(x["target"] == "bind-me" for x in ev)
    assert tok not in json.dumps([kw for _, kw in seen], ensure_ascii=False, default=str)


@pytest.mark.parametrize("uid,code", [(999999, 400), ("3", 400), (True, 400), (1.5, 400)])
def test_bad_user_ids_are_rejected(admin, uid, code):
    from app.core import api_tokens as at
    c, _ = admin
    tok = at.api_tokens.create("bad-uid").token
    assert c.post(f"{A}/owner", json={"token": tok, "user_id": uid}).status_code == code
    assert _owner_of("bad-uid") is None


def test_disabled_users_and_unknown_tokens(admin):
    from app.core import api_tokens as at, auth_db, db, user_manager
    c, _ = admin
    uid = user_manager.create_local("tok-off", "停用", "UserPass1234")
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("UPDATE users SET enabled=0 WHERE id=?", (uid,))
    tok = at.api_tokens.create("to-disabled").token
    assert c.post(f"{A}/owner", json={"token": tok, "user_id": uid}).status_code == 400
    assert c.post(f"{A}/owner", json={"token": "no-such-token", "user_id": _admin_uid()}).status_code == 404


def test_candidates_search_and_label(admin):
    from app.core import user_manager
    c, _ = admin
    user_manager.create_local("finder-x", "找得到的人", "UserPass1234")
    users = c.get(f"{A}/owner-candidates", params={"q": "找得到"}).json()["users"]
    assert [u["label"] for u in users] == ["finder-x@local"] and users[0]["name"] == "找得到的人"
    assert len(c.get(f"{A}/owner-candidates").json()["users"]) <= 20


def test_only_admins(admin_session):
    from app.core import api_tokens as at
    tok = at.api_tokens.create("not-yours").token
    u, uid = _user_client("tok-clerk", ["clerk"])
    for m, url, kw in (("post", f"{A}/owner", {"json": {"token": tok, "user_id": uid}}),
                       ("get", f"{A}/owner-candidates", {}),
                       ("post", f"{A}/create", {"json": {"label": "x"}})):
        r = getattr(u, m)(url, **kw)
        assert r.status_code in (302, 303, 401, 403), (url, r.status_code)
    assert _owner_of("not-yours") is None


def test_page_shows_the_owner_column(admin):
    from app.core import api_tokens as at
    c, _ = admin
    at.api_tokens.create("page-unbound")
    c.post(f"{A}/create", json={"label": "page-bound"})
    html = c.get("/admin/api-tokens").text
    assert "綁定使用者" in html and "jtdt-admin@local" in html and "未綁定" in html
    assert 'class="btn btn-small btn-owner"' in html and 'id="ownerDlg"' in html


def test_auth_off_has_no_owner_binding(auth_off):
    from fastapi.testclient import TestClient
    from app import main as app_main
    from app.core import api_tokens as at
    c = TestClient(app_main.app)
    tok = at.api_tokens.create("solo").token
    assert c.post(f"{A}/owner", json={"token": tok, "user_id": 1}).status_code == 400
    assert c.get(f"{A}/owner-candidates").json()["users"] == []
    html = c.get("/admin/api-tokens").text
    assert 'class="btn btn-small btn-owner"' not in html and "綁定使用者</th>" not in html


def test_binding_on_the_page_lets_mcp_connect(admin):
    """端到端：沒綁 → MCP 403（訊息講得出去哪裡綁）；在 API Token 頁綁給自己 → 連得上。"""
    from app.core import api_tokens as at, notify_settings
    from app.mcp import settings as ms
    c, _ = admin
    ms.save({"enabled": True, "tools": None})
    notify_settings.save({"site_url": SITE})
    tok = at.api_tokens.create("mcp").token
    at.api_tokens.set_mcp(tok, True)
    try:
        r = _rpc(c, tok, "ping")
        assert r.status_code == 403 and "Bind user" in r.json()["error"]["message"]
        assert c.post(f"{A}/owner", json={"token": tok, "user_id": _admin_uid()}).status_code == 200
        r = _rpc(c, tok, "ping")
        assert r.status_code == 200, r.text
    finally:
        at.api_tokens.revoke(tok)
        ms.save({"enabled": False})
        notify_settings.save({"site_url": ""})
