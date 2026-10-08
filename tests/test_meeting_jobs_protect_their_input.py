"""排隊中 / 執行中的會議摘要與轉逐字稿作業，輸入檔不可以被暫存清理掉。

暫存檔的清理只認得「保留期內或還沒結束的作業」meta 裡的編號（`job_store.keep_alive_keys`：
作業編號與 `meta.upload_id`）。原本兩支工具的 `upload_id` 是**做完才寫進 meta** —— 排隊加處理
超過暫存保留時數（預設 2 小時）的話，逐字稿 / 錄音檔資訊與歸屬紀錄（`.owners/<id>.json`）
會在作業還沒做完時就被清掉：作業跑到一半讀不到檔，或做完了「開啟」卻打不開。

所以 `upload_id` 要在**送出當下**就寫進 meta。這裡把派送暫停（作業停在排隊中），把輸入檔的
時間改成 3 小時前，再真的跑一次清理 —— 判準是**檔案還在**，不只是 meta 裡有那個欄位。
"""
from __future__ import annotations

import io
import os
import time

import pytest

from app.config import settings
from app.core import job_store, retention
from app.core.job_manager import job_manager
from tests.test_meeting_transcribe import (   # noqa: F401  （`unconfigured` 是 fixture）
    FakeJtlw, _configure, _upload, unconfigured,
)

VTT = """WEBVTT

00:00:01.000 --> 00:00:06.000
<v 王小明>各位早，今天只談一件事：第四季的預算。

00:00:06.500 --> 00:00:12.000
<v 李美華>我看過草案了，行銷那一塊超出去兩百萬。
"""


def _age(path, hours: float = 3.0):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    t = time.time() - hours * 3600
    os.utime(path, (t, t))
    return path


def _queued(client, url: str, body: dict) -> str:
    """派送暫停時送出 → 作業停在排隊中，回作業編號（呼叫端負責恢復派送與取消）。"""
    r = client.post(url, json=body)
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "pending", f"派送暫停了，作業卻不是排隊中：{j.get('status')}"
    return jid


def _assert_kept(files, jid: str, uid: str, client):
    meta = client.get(f"/api/jobs/{jid}").json().get("meta") or {}
    assert meta.get("upload_id") == uid, (
        f"排隊中的作業 meta 沒有 upload_id —— 清理認不出它的輸入檔：{meta}")
    ids, names = job_store.keep_alive_keys(time.time())
    for f in files:
        assert job_store.owns_file(f.name, ids, names), f"{f.name} 不算在排隊中那件作業名下"
    retention._sweep_temp_dir(2 * 3600, 24 * 3600)
    gone = [f.name for f in files if not f.exists()]
    assert not gone, f"作業還在排隊，輸入檔卻被清掉了：{gone}"


@pytest.fixture
def paused():
    # 作業資料庫要在送出之前就在（TestClient 不跑啟動流程）—— 清理讀的是資料庫裡的作業
    job_store.init()
    job_manager.set_paused(True)
    jobs: list[str] = []
    try:
        yield jobs
    finally:
        for jid in jobs:
            job_manager.cancel(jid)
        job_manager.set_paused(False)


def test_a_queued_meeting_summary_keeps_its_transcript(client, auth_off, paused, monkeypatch):
    from app.core import llm_settings as ls
    monkeypatch.setattr(ls.llm_settings, "is_enabled", lambda: True)
    r = client.post("/tools/meeting-summary/upload",
                    files={"file": ("m.vtt", io.BytesIO(VTT.encode()), "text/plain")})
    assert r.status_code == 200, r.text
    uid = r.json()["upload_id"]
    jid = _queued(client, "/tools/meeting-summary/start",
                  {"upload_id": uid, "with_impacts": "0"})
    paused.append(jid)
    t = settings.temp_dir
    files = [_age(t / f"ms_{uid}_segments.json"), _age(t / f"ms_{uid}_meta.json"),
             _age(t / ".owners" / f"{uid}.json")]
    _assert_kept(files, jid, uid, client)


def test_a_queued_transcription_keeps_its_recording_info(client, unconfigured, paused):
    with FakeJtlw() as fake:
        _configure(fake)
        uid = _upload(client)["upload_id"]
        jid = _queued(client, "/tools/meeting-transcribe/start", {"upload_id": uid})
        paused.append(jid)
        t = settings.temp_dir
        files = [_age(t / f"mt_{uid}_meta.json"), _age(t / ".owners" / f"{uid}.json")]
        _assert_kept(files, jid, uid, client)
        assert fake.seen.get("body") is None, "派送暫停了，作業卻送出去了"
