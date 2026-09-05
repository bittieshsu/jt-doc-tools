"""作業的三個時間點：送出 / 開始 / 結束。

使用者要求作業清單加「啟動時間」與「結束時間」。原本只存了送出時間
（`created_at`）與結束時間（`finished_at`）—— **開始時間只活在記憶體裡**，
重啟或從資料庫讀回來就沒了。

為什麼三個都要：作業會排隊。「送出」跟「開始跑」中間可能隔很久，只看耗時
分不出「這件排了半小時」還是「這件跑了半小時」，而那兩件事的處理方式完全
不同（前者要加併行度或插隊，後者要看是不是檔案太大）。
"""
from __future__ import annotations

import time

import pytest

from app.core import job_store
from app.core.job_manager import job_manager


def _run_a_job(sleep_s: float = 0.3) -> str:
    def work(job):
        time.sleep(sleep_s)
        return None

    jid = job_manager.submit("pdf-compress", work, meta={"filename": "t.pdf"}).id
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(0.1)
        j = job_manager.get(jid)
        if j and j.status in ("done", "error"):
            return jid
    pytest.fail("作業沒有在時限內結束")


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """每個測試用自己的 jobs.sqlite。

    **不可以用 `monkeypatch.setenv("JTDT_DATA_DIR", ...)`** —— `settings` 在
    模組載入時就讀完環境變數了，後面再改對它沒有作用，於是所有測試共用同一個
    資料庫：**單跑全綠、合跑互相污染**（2026-08-27 完整測試抓到，這個雷
    CLAUDE.md 記過，還是踩了）。要改的是 `settings.data_dir` 這個屬性，
    跟隔壁的 `test_job_queue.py` 一致。
    """
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr("app.config.settings.data_dir", d)
    job_store.init()
    return job_store


def test_all_three_timestamps_are_persisted(store):
    jid = _run_a_job()
    row = next((r for r in store.list_jobs(limit=20) if r["id"] == jid), None)
    assert row is not None, "作業沒寫進資料庫"
    assert row["created_at"], "沒有送出時間"
    assert row["started_at"], "沒有開始時間 —— 重啟之後就分不出排隊多久了"
    assert row["finished_at"], "沒有結束時間"


def test_the_three_timestamps_are_in_order(store):
    jid = _run_a_job(sleep_s=0.4)
    row = next(r for r in store.list_jobs(limit=20) if r["id"] == jid)
    assert row["created_at"] <= row["started_at"] <= row["finished_at"]
    # 實際執行時間要跟工作內容對得上（不是把排隊也算進去）。
    #
    # **門檻刻意離 0.4 秒遠一點**：機器同時在跑別的東西時（本輪就撞到一次：
    # 完整測試 + 無頭瀏覽器抓圖同時跑），排程抖動會讓量到的時間略短於 sleep，
    # 這條就會偶爾紅一次 —— 而**偶爾紅的守門跟壞掉的守門一樣沒人信**。
    #
    # 0.15 仍然擋得住「根本沒記 started_at」（差值趨近 0）。
    #
    # **它擋不住「started_at 記在進佇列而不是開始跑」** —— 這支測試裡作業是
    # 立刻開始的，兩個時間點幾乎一樣（實測把 `started_at` 改成 `created_at`
    # 這條照樣綠）。要驗那個得先讓佇列塞住再送一件，成本比較高；
    # 先把限制寫在這裡，不要讓人以為它守得住。
    assert row["finished_at"] - row["started_at"] >= 0.15


def test_restoring_a_job_from_the_database_keeps_started_at(store):
    """從資料庫還原作業時要帶回開始時間。

    少了這一步，還原之後任何一次 upsert 都會把資料庫裡的值寫成 NULL ——
    **資料靜靜地不見**，畫面上那一欄變成「—」，沒有人會發現。
    （2026-08-27 寫變異驗證時發現這條路徑真的漏了。）
    """
    conn = job_store._db.get_conn(job_store.db_path())
    with job_store._db.tx(conn):
        conn.execute(
            "INSERT INTO jobs (id, tool_id, status, progress, message, "
            "created_at, updated_at, started_at, finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("restored", "pdf-compress", "done", 1.0, "",
             1000.0, 1005.0, 1001.0, 1005.0))
    restored = job_manager.get("restored")
    assert restored is not None, "還原不回來"
    assert restored.started_at == 1001.0, "還原時把開始時間弄丟了"

    # 再寫一次不可以把它洗掉
    job_store.upsert(restored)
    row = next(r for r in store.list_jobs(limit=50) if r["id"] == "restored")
    assert row["started_at"] == 1001.0, "重新寫入之後開始時間不見了"


def test_started_at_is_not_overwritten_by_later_updates(store):
    """作業跑到一半會 upsert 好幾次（進度、訊息）。

    每次都寫的話「開始時間」會一路跟著最後一次更新跑 —— 那個欄位就沒有意義了。
    """
    def work(job):
        for i in range(4):
            job.progress = (i + 1) / 4
            job.message = f"步驟 {i}"
            job_store.upsert(job)
            time.sleep(0.1)
        return None

    jid = job_manager.submit("pdf-compress", work, meta={"filename": "t.pdf"}).id
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(0.1)
        j = job_manager.get(jid)
        if j and j.status in ("done", "error"):
            break
    row = next(r for r in store.list_jobs(limit=20) if r["id"] == jid)
    live = job_manager.get(jid)
    assert abs(row["started_at"] - live.started_at) < 0.05, \
        "開始時間被後續的更新蓋掉了"


def test_old_rows_without_started_at_are_readable(store):
    """舊資料沒有這個欄位 → NULL。讀得到、不可以爆掉，畫面顯示「—」。

    **不要拿 created_at 硬湊** —— 那會讓每一筆看起來都是「送出即開始」，
    等於騙人。
    """
    conn = job_store._db.get_conn(job_store.db_path())
    with job_store._db.tx(conn):
        conn.execute(
            "INSERT INTO jobs (id, tool_id, status, progress, message, "
            "created_at, updated_at, finished_at) VALUES (?,?,?,?,?,?,?,?)",
            ("oldjob", "pdf-compress", "done", 1.0, "", 1000.0, 1002.0, 1002.0))
    row = next(r for r in store.list_jobs(limit=50) if r["id"] == "oldjob")
    assert row["started_at"] is None
    assert row["created_at"] == 1000.0


def test_live_snapshot_exposes_started_at():
    """正在跑的作業還沒寫進資料庫的 started_at —— 少了這個，
    「進行中」那幾筆在畫面上的開始時間會是空的。"""
    started = {}

    def work(job):
        started["at"] = job.started_at
        snap = job_manager.live_snapshot().get(job.id) or {}
        started["in_snapshot"] = snap.get("started_at")
        time.sleep(0.15)
        return None

    jid = job_manager.submit("pdf-compress", work, meta={}).id
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(0.1)
        j = job_manager.get(jid)
        if j and j.status in ("done", "error"):
            break
    assert started.get("at"), "作業執行時 started_at 還是空的"
    assert started.get("in_snapshot") == started["at"], \
        "即時快照沒有帶 started_at"


def test_admin_jobs_table_shows_both_times():
    """畫面要真的有那兩欄（後端有值但沒顯示，等於沒做）。"""
    from pathlib import Path
    html = Path("app/admin/templates/admin_jobs.html").read_text(encoding="utf-8")
    assert "啟動時間" in html and "結束時間" in html
    assert 'data-sort="started_at"' in html, "新欄位要能排序，跟其他欄一致"
    assert "j.started_at" in html and "j.finished_at" in html
