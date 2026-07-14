"""目錄瀏覽「已選定」filter 端點整合測試（auth OFF = 單機 admin）。

filter 設定端點 backend-agnostic（不需 LDAP）；/directory/selected 需目錄後端，
這裡只驗「非目錄後端時回 400」與「頁面可渲染」。實際 LDAP 剪枝樹屬手動 / 部署驗收。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _seed_auth_db():
    # 正式部署由 app startup 建 auth DB / roles 表；TestClient 未進 lifespan 時
    # 需自行 seed，directory_page 才能呼叫 roles.list_roles()。
    from app.core import auth_db, roles
    auth_db.init()
    roles.seed_builtin_roles()


def test_directory_page_renders(client):
    r = client.get("/admin/directory")
    assert r.status_code == 200


def test_filter_get_default(client):
    r = client.get("/admin/directory/filter")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["default_mode"] == "all"
    assert isinstance(j["rules"], list)


def test_filter_save_and_roundtrip(client):
    payload = {
        "default_mode": "all",
        "rules": [
            {"name_contains": "sales", "types": ["group", "ou"], "base_dn": "OU=TW,DC=x"},
            {"name_contains": "", "types": [], "base_dn": ""},   # 空規則 → 應被清掉
            {"types": ["user"]},
        ],
    }
    r = client.post("/admin/directory/filter", json=payload)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["default_mode"] == "all"
    assert len(j["rules"]) == 2

    # 再讀一次確認持久化
    j2 = client.get("/admin/directory/filter").json()
    assert j2["default_mode"] == "all"
    assert len(j2["rules"]) == 2
    assert j2["rules"][0]["types"] == ["group", "ou"]

    # 還原預設，避免污染其他測試的共用 data dir
    client.post("/admin/directory/filter", json={"default_mode": "all", "rules": []})


def test_selected_requires_directory_backend(client):
    # auth OFF → backend 非 ldap/ad → /directory/selected 應回 400
    r = client.get("/admin/directory/selected")
    assert r.status_code == 400
