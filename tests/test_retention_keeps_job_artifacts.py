"""作業還在保留期內，它放在暫存區的東西就不可以先被清掉。

## 由來（v1.16.6，使用者回報、正式機證實）

「我的作業」寫著「結果檔會在 24 小時後自動清除」，一件 4 小時 50 分前完成的
會議摘要按「開啟」卻得到 **410 檔案已過期**。正式機上看：作業紀錄還在、狀態
「完成」，但分析結果、逐字稿、中繼資料、歸屬紀錄**全部已經被清掉**。

原因是**兩個期限對不上**：全站的作業結果與「開啟」要讀的資料都放在
`data/temp/`（保留 2 小時），而作業保留期是 24 小時。v1.14.31 讓 `data/jobs/`
改用 `jobs_hours`，但**沒有任何一支工具把結果放進 `jobs/`**（當時 `jobs/`
裡 0 個檔案）—— 那次只修了一半。

## 判準

* 保留期內（或還沒結束）的作業：結果檔、檔名帶它編號或 `upload_id` 的暫存檔、
  歸屬紀錄 → **留著**
* 超過作業保留期的作業 → 照樣清
* **跟任何作業都無關的舊暫存檔 → 照樣清**（反向對照：把清理整段關掉的話，
  「留著」那幾條也會過）
"""
from __future__ import annotations

import json
import os
import pathlib
import time
import uuid

import pytest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from app.config import settings
    from app.core import job_store

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    for sub in ("temp", "jobs", "temp/.owners"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    job_store.init()
    return tmp_path


def _aged(path: pathlib.Path, hours: float) -> pathlib.Path:
    path.write_bytes(b"x")
    t = time.time() - hours * 3600
    os.utime(path, (t, t))
    return path


def _job(*, tool: str, status: str, finished_hours_ago: float | None,
         created_hours_ago: float, result_path: pathlib.Path | None = None,
         upload_id: str = "", job_id: str = "") -> str:
    from app.core import db, job_store

    jid = job_id or uuid.uuid4().hex
    now = time.time()
    fin = None if finished_hours_ago is None else now - finished_hours_ago * 3600
    meta = json.dumps({"upload_id": upload_id}) if upload_id else None
    conn = db.get_conn(job_store.db_path())
    with db.tx(conn):
        conn.execute(
            "INSERT INTO jobs (id, tool_id, status, result_path, meta, "
            "                  created_at, updated_at, finished_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (jid, tool, status, str(result_path) if result_path else None, meta,
             now - created_hours_ago * 3600, fin or now, fin))
    return jid


def _sweep():
    from app.core import retention
    retention._sweep_temp_dir(2 * 3600, 24 * 3600)


def test_a_finished_job_keeps_what_open_needs(data_dir):
    """會議摘要那一件的原樣：3 小時前完成，「開啟」要讀的四樣東西都要在。"""
    t = data_dir / "temp"
    uid = uuid.uuid4().hex
    result = _aged(t / f"ms_{uid}_result.json", 3)
    segs = _aged(t / f"ms_{uid}_segments.json", 3)
    meta = _aged(t / f"ms_{uid}_meta.json", 3)
    owner = _aged(t / ".owners" / f"{uid}.json", 3)
    _job(tool="meeting-summary", status="done", finished_hours_ago=3,
         created_hours_ago=3.1, result_path=result, upload_id=uid)

    _sweep()

    for p in (result, segs, meta, owner):
        assert p.exists(), f"{p.name} 在作業保留期內被清掉了 —— 按「開啟」會得到 410"


def test_a_job_named_by_its_own_id_is_kept(data_dir):
    """逐句翻譯的結果檔是用**作業編號**命名的（`trd_<job_id>.json`）。"""
    jid = uuid.uuid4().hex
    trd = _aged(data_dir / "temp" / f"trd_{jid}.json", 5)
    _job(tool="translate-doc", status="done", finished_hours_ago=5,
         created_hours_ago=5.5, job_id=jid)

    _sweep()

    assert trd.exists()


def test_a_result_without_any_id_in_its_name_is_kept_by_name(data_dir):
    """結果檔名裡不一定有識別碼 —— 那就認 `result_path` 本身。"""
    out = _aged(data_dir / "temp" / "compressed_output.pdf", 6)
    _job(tool="pdf-compress", status="done", finished_hours_ago=6,
         created_hours_ago=6.2, result_path=out)

    _sweep()

    assert out.exists(), "「我的作業」的下載鈕看的就是這個檔"


def test_a_running_job_is_never_swept_out_from_under_it(data_dir):
    """還在跑的作業不管多舊都不可以清（它還要讀自己的輸入）。"""
    uid = uuid.uuid4().hex
    src = _aged(data_dir / "temp" / f"mt_{uid}_audio.m4a", 30)
    _job(tool="meeting-transcribe", status="running", finished_hours_ago=None,
         created_hours_ago=30, upload_id=uid)

    _sweep()

    assert src.exists()


def test_past_the_job_retention_it_is_released(data_dir):
    """作業過了保留期，它的東西照樣要清 —— 保護不是永久的。"""
    uid = uuid.uuid4().hex
    old = _aged(data_dir / "temp" / f"ms_{uid}_result.json", 25)
    owner = _aged(data_dir / "temp" / ".owners" / f"{uid}.json", 25)
    _job(tool="meeting-summary", status="done", finished_hours_ago=25,
         created_hours_ago=25.2, result_path=old, upload_id=uid)

    _sweep()

    assert not old.exists()
    assert not owner.exists()


def test_unrelated_old_temp_files_are_still_swept(data_dir):
    """**反向對照**：跟任何作業都無關的舊暫存檔照樣 2 小時就清。

    沒有這一條的話，把整段清理拿掉，上面每一條「留著」都會過。
    """
    t = data_dir / "temp"
    stray = _aged(t / f"up_{uuid.uuid4().hex}_x.pdf", 3)
    plain = _aged(t / "upload.pdf", 3)
    stray_owner = _aged(t / ".owners" / f"{uuid.uuid4().hex}.json", 3)
    fresh = _aged(t / "just_now.pdf", 0.1)
    # 旁邊有一件保留期內的作業 —— 它的存在不可以讓別人的檔案也被留下來
    uid = uuid.uuid4().hex
    kept = _aged(t / f"ms_{uid}_result.json", 3)
    _job(tool="meeting-summary", status="done", finished_hours_ago=3,
         created_hours_ago=3.1, result_path=kept, upload_id=uid)

    _sweep()

    assert not stray.exists()
    assert not plain.exists()
    assert not stray_owner.exists()
    assert fresh.exists()
    assert kept.exists()


def test_the_protection_is_what_keeps_them(data_dir, monkeypatch):
    """同一個場景把「認出保留期內的作業」拿掉 → 那些檔案必須被清掉。

    證明留下來的原因**是這個修正**，不是場景本身就不會清（例如 mtime 設錯）。
    """
    from app.core import job_store

    uid = uuid.uuid4().hex
    result = _aged(data_dir / "temp" / f"ms_{uid}_result.json", 3)
    _job(tool="meeting-summary", status="done", finished_hours_ago=3,
         created_hours_ago=3.1, result_path=result, upload_id=uid)
    monkeypatch.setattr(job_store, "keep_alive_keys", lambda since: (set(), set()))

    _sweep()

    assert not result.exists()


def test_no_job_database_means_the_old_behaviour(tmp_path, monkeypatch):
    """全新安裝還沒有 jobs.sqlite 時不可以出錯，也不可以因此什麼都不清。"""
    from app.config import settings
    from app.core import job_store, retention

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    (tmp_path / "temp").mkdir()
    old = _aged(tmp_path / "temp" / f"x_{uuid.uuid4().hex}.pdf", 3)
    assert not job_store.db_path().exists()

    retention._sweep_temp_dir(2 * 3600, 24 * 3600)

    assert not old.exists()
    assert not job_store.db_path().exists(), "清理不應該順手建出一個空的作業資料庫"


def test_the_30_minute_loop_in_main_keeps_them_too(data_dir, monkeypatch):
    """**第二條清理路徑**：`app/main.py` 的 `_sweep_temp_files_loop`（每 30 分鐘）。

    v1.16.6 第一版只改了 `retention` 那支，上面每一條都綠 —— 部署到正式機之後，
    一件 2.1 小時前完成的作業檔案**照樣被這支刪掉**（它自己寫了一份固定 2 小時的
    清理，連管理頁的保留期設定都不看）。是在正式機上真的去看檔案還在不在才抓到的。

    所以這一條**真的跑一輪那支迴圈**，不是看它的原始碼。
    """
    import asyncio
    import app.main as m

    t = data_dir / "temp"
    uid = uuid.uuid4().hex
    kept = _aged(t / f"mt_{uid}_transcript.json", 3)
    stray = _aged(t / f"up_{uuid.uuid4().hex}_x.m4a", 3)
    _job(tool="meeting-transcribe", status="done", finished_hours_ago=3,
         created_hours_ago=3.1, result_path=kept, upload_id=uid)

    class _Stop(Exception):
        pass

    async def _one_round(_s):
        raise _Stop

    monkeypatch.setattr(asyncio, "sleep", _one_round)
    with pytest.raises(_Stop):
        asyncio.run(m._sweep_temp_files_loop())

    assert kept.exists(), "30 分鐘那支迴圈把保留期內的作業檔案刪了"
    assert not stray.exists(), "反向對照：跟作業無關的舊暫存檔要照樣清"
