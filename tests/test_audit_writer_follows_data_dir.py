"""稽核寫入執行緒要跟著資料目錄走（v1.17.0）。

寫入執行緒原本在第一次啟動時就把資料庫路徑固定下來。資料目錄之後換了（測試切換暫存目錄），
它還一直寫到舊的那個 —— 讀的那邊永遠讀不到。實際現象：`test_notify.py` 先觸發了寫入執行緒，
之後 OWASP 的登入稽核測試一律讀不到事件（等 5 秒也一樣，所以不是時間差）。
"""
from __future__ import annotations

import time


def _wait(conn_fn, event: str, timeout: float = 5.0) -> int:
    end = time.time() + timeout
    while time.time() < end:
        n = conn_fn().execute("SELECT COUNT(*) FROM audit_events WHERE event_type=?", (event,)).fetchone()[0]
        if n:
            return n
        time.sleep(0.05)
    return 0


def test_events_follow_a_changed_data_dir(tmp_path, monkeypatch):
    from app.core import audit_db
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setattr("app.config.settings.data_dir", a)
    audit_db.init()
    audit_db.log_event("writer_probe_a")
    assert _wait(audit_db.conn, "writer_probe_a"), "第一個目錄要寫得進去"
    monkeypatch.setattr("app.config.settings.data_dir", b)
    audit_db.init()
    audit_db.log_event("writer_probe_b")
    assert _wait(audit_db.conn, "writer_probe_b"), "換了資料目錄之後，事件要寫進新的目錄"
    monkeypatch.setattr("app.config.settings.data_dir", a)
    assert audit_db.conn().execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type='writer_probe_b'").fetchone()[0] == 0, "不可以寫到舊目錄"
