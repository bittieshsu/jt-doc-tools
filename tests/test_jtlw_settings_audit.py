"""語音服務設定頁存檔要留稽核紀錄，而且**不可以把金鑰寫進去**（v1.16.28）。

## 由來

2026-09-29 語音服務通知「台語模式以前是壞的」，要查我們有沒有人切過那個模式 ——
**查不到**：其他設定頁存檔都會寫 `settings_change`，只有這一頁沒有。

兩個方向（缺一條就沒有牙齒）：
①存檔之後有一筆 `settings_change`、目標 `jtlw`，看得出改了哪個辨識模式
②**送進來的金鑰字串絕不出現在紀錄裡**（紀錄會被轉送到 SIEM，金鑰寫進去等於外洩）
"""
from __future__ import annotations

import json
import time

import pytest


def _latest_jtlw_events(after_id: int) -> list:
    from app.core import audit_db
    deadline = time.time() + 5
    rows = []
    while time.time() < deadline:
        rows = audit_db.conn().execute(
            "SELECT id, username, target, details_json FROM audit_events "
            "WHERE event_type='settings_change' AND target='jtlw' AND id > ? "
            "ORDER BY id DESC", (after_id,)).fetchall()
        if rows:
            return rows
        time.sleep(0.2)
    return rows


def _max_id() -> int:
    from app.core import audit_db
    r = audit_db.conn().execute("SELECT COALESCE(MAX(id), 0) AS m FROM audit_events").fetchone()
    return int(r["m"])


@pytest.fixture
def restore_jtlw_settings():
    from app.core import jtlw_settings
    before = dict(jtlw_settings.get())
    yield
    jtlw_settings.save(before)


def test_saving_the_page_writes_an_audit_event(admin_session, restore_jtlw_settings):
    client, admin, _ = admin_session
    start = _max_id()
    secret = "jtlw_test_SECRET_do_not_log_4f9a"
    r = client.post("/admin/api/jtlw/settings",
                    json={"profile_id": "transcribe.taiwanese", "api_key_enc": secret})
    assert r.status_code == 200, r.text
    rows = _latest_jtlw_events(start)
    assert rows, "存了語音服務設定卻沒有 settings_change 稽核紀錄"
    row = rows[0]
    assert row["username"] == admin
    detail = json.loads(row["details_json"])
    assert detail.get("profile_id") == "transcribe.taiwanese", detail
    assert detail.get("api_key_status") == "已更新", detail
    assert secret not in row["details_json"], "金鑰被寫進稽核紀錄了"


def test_an_untouched_key_is_recorded_as_unchanged(admin_session, restore_jtlw_settings):
    from app.core import jtlw_settings
    client, _, _ = admin_session
    start = _max_id()
    r = client.post("/admin/api/jtlw/settings",
                    json={"profile_id": "meeting.balanced",
                          "api_key_enc": jtlw_settings.SECRET_KEPT})
    assert r.status_code == 200, r.text
    rows = _latest_jtlw_events(start)
    assert rows
    detail = json.loads(rows[0]["details_json"])
    assert detail.get("api_key_status") == "不變", detail
