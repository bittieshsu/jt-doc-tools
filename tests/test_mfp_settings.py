"""管理員設定：允許使用者自行新增事務機（使用者 2026-10-14：「管理員可以設定是否允許使用者自行新增事務機，預設允許」）。

驗：
* 預設允許：一般使用者可以連線測試、儲存。
* 管理員關掉之後：一般使用者連線測試、儲存都回 403（`USER_DEVICES_DISABLED`），**還沒連設備就擋**；
  清單 `can_add: false`；自己以前新增的標 `paused`、不能測試 / 掃描 / 取件，但可以改名、移除（自己的資料）；
  共用事務機照常。管理員自己不受限（建立共用事務機也走連線測試）。
* 重新打開就恢復。設定只有管理員能讀寫；值一定要是 true / false；改設定寫稽核；設定檔進設定備份。
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

from app.core import audit_db, mfp_settings
from tests.test_mfp_shared_and_scan import (_make_shared, _own_device, escl_probe, people,  # noqa: F401
                                            fake_probe)

API = "/tools/mfp-fetch/api"


def _set(admin, allow):
    r = admin.put(f"{API}/settings", json={"allow_user_devices": allow})
    assert r.status_code == 200, r.text
    return r.json()["settings"]


@pytest.fixture
def restore_setting():
    yield
    mfp_settings.save({"allow_user_devices": True})


def test_users_may_add_devices_by_default(people, restore_setting):
    assert mfp_settings.get()["allow_user_devices"] is True
    p = people
    assert p["admin"].get(f"{API}/settings").json()["settings"]["allow_user_devices"] is True
    _own_device(p["a"])
    assert p["a"].get(f"{API}/connections").json()["can_add"] is True


def test_turning_it_off_blocks_adding_before_touching_any_device(people, fake_probe, restore_setting):
    p = people
    cid = _own_device(p["a"])
    before = len(fake_probe)
    assert _set(p["admin"], False)["allow_user_devices"] is False
    r = p["a"].post(f"{API}/detect", json={"host": "192.168.1.77"})
    assert r.status_code == 403 and r.json()["code"] == "USER_DEVICES_DISABLED"
    assert len(fake_probe) == before, "被擋了還是去連了設備"
    r = p["a"].post(f"{API}/connections", json={"token": "x" * 32})
    assert r.status_code == 403 and r.json()["code"] == "USER_DEVICES_DISABLED"
    j = p["a"].get(f"{API}/connections").json()
    assert j["can_add"] is False
    row = next(x for x in j["connections"] if x["id"] == cid)
    assert row["paused"] is True
    for m, url in (("post", f"{API}/connections/{cid}/test"), ("post", f"{API}/connections/{cid}/scan"),
                   ("post", f"{API}/connections/{cid}/box/list"), ("post", f"{API}/connections/{cid}/box/fetch")):
        r = getattr(p["a"], m)(url, json={"box": 1, "documents": ["1"]})
        assert r.status_code == 403 and r.json()["code"] == "USER_DEVICES_DISABLED", (url, r.text)
    assert len(fake_probe) == before
    # 自己的資料：還能改名、移除
    assert p["a"].patch(f"{API}/connections/{cid}", json={"display_name": "舊的"}).status_code == 200
    assert p["a"].delete(f"{API}/connections/{cid}").status_code == 200


def test_shared_devices_and_admins_are_not_affected(people, fake_probe, restore_setting):
    from app.core import mfp_store
    p = people
    cid = _make_shared(p["admin"])
    mfp_store.set_members(cid, [("user", str(p["ua"]))])
    _set(p["admin"], False)
    row = next(x for x in p["a"].get(f"{API}/connections").json()["connections"] if x["id"] == cid)
    assert row["paused"] is False
    assert p["a"].post(f"{API}/connections/{cid}/test").status_code == 200, "共用事務機照常"
    assert p["admin"].post(f"{API}/detect", json={"host": "192.168.1.78"}).status_code == 200, "管理員不受限"
    _make_shared(p["admin"], host="192.168.1.79", name="二樓")


def test_turning_it_back_on_restores_them(people, restore_setting):
    p = people
    cid = _own_device(p["a"])
    _set(p["admin"], False)
    _set(p["admin"], True)
    row = next(x for x in p["a"].get(f"{API}/connections").json()["connections"] if x["id"] == cid)
    assert row["paused"] is False
    assert p["a"].post(f"{API}/connections/{cid}/test").status_code == 200


def test_only_admins_read_or_change_it_and_values_are_checked(people, restore_setting):
    p = people
    assert p["a"].get(f"{API}/settings").status_code == 403
    r = p["a"].put(f"{API}/settings", json={"allow_user_devices": False})
    assert r.status_code == 403 and mfp_settings.allow_user_devices() is True
    for bad in ("no", 0, 1, None, "false", [False]):
        r = p["admin"].put(f"{API}/settings", json={"allow_user_devices": bad})
        assert r.status_code == 400 and r.json()["code"] == "SETTINGS_INVALID", bad
    assert mfp_settings.allow_user_devices() is True


def test_changes_are_audited(people, restore_setting):
    p = people
    _set(p["admin"], False)
    end = time.monotonic() + 5
    rows = []
    while time.monotonic() < end and not rows:
        rows = audit_db.conn().execute("SELECT details_json FROM audit_events WHERE event_type="
                                       "'mfp_settings_update' ORDER BY id DESC LIMIT 1").fetchall()
        time.sleep(0.05)
    assert rows and json.loads(rows[0][0]) == {"allow_user_devices": False}


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX 權限")
def test_the_file_is_private_and_a_broken_file_falls_back_to_allow(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    mfp_settings.save({"allow_user_devices": False})
    p = tmp_path / "mfp_settings.json"
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert mfp_settings.allow_user_devices() is False
    p.write_text("{broken", encoding="utf-8")
    assert mfp_settings.allow_user_devices() is True, "檔案壞掉就回預設（允許）"
    p.write_text('{"allow_user_devices": "no"}', encoding="utf-8")
    assert mfp_settings.allow_user_devices() is True


def test_the_setting_is_in_the_settings_backup():
    from app.core.settings_export import CATEGORIES
    assert any("mfp_settings.json" in (c.get("items") or []) for c in CATEGORIES)
