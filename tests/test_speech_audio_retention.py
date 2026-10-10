"""會議錄音要有保留期限（v1.16.71）。

## 由來

轉逐字稿上傳的原始錄音存在 `data/speech_audio/<編號>.bin`，**原本沒有任何保留期，
永遠不刪** —— 合規盤點時查到。錄音是聲音本身，比逐字稿更敏感，一場會議又動輒
上百 MB。現在在「檔案保留 / 清理」頁跟其他資料一樣可以設定天數（預設 30 天）。

## 判準

* 超過保留期的錄音 → 刪掉；保留期內的 → 留著
* `-1`（與 0）＝ 永久保留，一個都不刪
* **還在排隊或辨識中的作業的錄音，再舊都不刪** —— 語音服務是排到才來拉檔，
  刪掉的話那一件會變成「來源連不上」
* 已經結束的作業不受保護（保留期是給錄音本身的，不是給作業的）
* 目錄裡不是 `<32 碼>.bin` 的東西不碰
* `sweep_all()` 真的有跑這一支；保留設定頁有這一列、看得到佔用量
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
    (tmp_path / "speech_audio").mkdir()
    job_store.init()
    return tmp_path


def _rec(root: pathlib.Path, days_old: float, *, name: str | None = None) -> pathlib.Path:
    p = root / "speech_audio" / (name or f"{uuid.uuid4().hex}.bin")
    p.write_bytes(b"RIFF....WAVE" + b"\0" * 64)
    t = time.time() - days_old * 86400
    os.utime(p, (t, t))
    return p


def _job(status: str, upload_id: str, *, finished_days_ago: float | None = None) -> None:
    from app.core import db, job_store

    now = time.time()
    fin = None if finished_days_ago is None else now - finished_days_ago * 86400
    conn = db.get_conn(job_store.db_path())
    with db.tx(conn):
        conn.execute(
            "INSERT INTO jobs (id, tool_id, status, result_path, meta, "
            "                  created_at, updated_at, finished_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, "meeting-transcribe", status, None,
             json.dumps({"upload_id": upload_id}), now - 40 * 86400, fin or now, fin))


def test_the_default_is_thirty_days_and_it_saves():
    from app.core import retention
    assert retention._DEFAULTS["speech_audio_days"] == 30


def test_old_recordings_go_and_recent_ones_stay(data_dir):
    from app.core import retention
    old = _rec(data_dir, 31)
    new = _rec(data_dir, 29)
    assert retention._sweep_speech_audio(30) == 1
    assert not old.exists(), "超過保留期的錄音沒有刪"
    assert new.exists(), "保留期內的錄音被刪了"


@pytest.mark.parametrize("days", [-1, 0])
def test_keep_forever_deletes_nothing(data_dir, days):
    from app.core import retention
    old = _rec(data_dir, 4000)
    assert retention._sweep_speech_audio(days) == 0
    assert old.exists()


def test_a_recording_still_waiting_to_be_transcribed_is_never_deleted(data_dir):
    from app.core import retention
    queued = _rec(data_dir, 90)
    running = _rec(data_dir, 90)
    _job("pending", queued.stem)
    _job("running", running.stem)
    retention._sweep_speech_audio(30)
    assert queued.exists() and running.exists(), (
        "還在排隊 / 辨識中的作業，錄音被刪了 —— 語音服務來拉檔時會變成「來源連不上」")


def test_a_finished_job_does_not_keep_its_recording_alive(data_dir):
    """反向對照：只驗「進行中的留著」的話，把整段清理關掉也會過。"""
    from app.core import retention
    done = _rec(data_dir, 90)
    _job("done", done.stem, finished_days_ago=89)
    retention._sweep_speech_audio(30)
    assert not done.exists()


def test_a_half_copied_file_is_cleaned_and_strangers_are_left_alone(data_dir):
    from app.core import retention
    part = _rec(data_dir, 60, name=f"{uuid.uuid4().hex}.bin.part")
    other = [_rec(data_dir, 60, name=n) for n in
             ("notes.txt", "abc.bin", f"{uuid.uuid4().hex}.json", f"{uuid.uuid4().hex.upper()}.bin")]
    retention._sweep_speech_audio(30)
    assert not part.exists(), "寫到一半留下的 .part 沒有清"
    for p in other:
        assert p.exists(), f"不是錄音檔的 {p.name} 被刪了"


def test_sweep_all_runs_it_with_the_saved_setting(data_dir, monkeypatch):
    from app.core import audit_db, auth_db, retention
    audit_db.init()
    auth_db.init()
    s = retention.get()
    s["speech_audio_days"] = 10
    monkeypatch.setattr(retention, "get", lambda: s)
    old = _rec(data_dir, 11)
    keep = _rec(data_dir, 9)
    report = retention.sweep_all()
    assert report["speech_audio"] == 1
    assert not old.exists() and keep.exists()


def test_stats_count_the_recordings(data_dir):
    from app.core import retention
    _rec(data_dir, 5)
    _rec(data_dir, 2)
    st = retention.collect_stats()["speech_audio"]
    assert st["files"] == 2
    assert st["size_mb"] > 0
    assert 4.5 < st["oldest_days"] < 5.5


def test_the_retention_page_has_the_row(admin_session):
    c, *_ = admin_session
    r = c.get("/admin/retention")
    assert r.status_code == 200, r.text[:500]
    assert 'data-key="speech_audio_days"' in r.text, "保留設定頁沒有會議錄音這一列 —— 管理員改不到"
    assert 'data-hint="speech_audio"' in r.text
