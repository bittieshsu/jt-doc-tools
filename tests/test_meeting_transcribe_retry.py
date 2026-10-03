"""延後 ACK ＋ 補專有名詞只重跑校正（v1.16.41）。

JTLW 的 `POST /jobs/{id}/retry` 帶新的 `glossary` 只重跑校正 —— 但 ACK 之後內容就清掉了
（409 `content_cleared`）。對方同意我們**延後最多 24 小時**再 ACK，條件是：

* **不可以超過 24 小時** —— 那是會議內容；
* 使用者表示不需要了（按「不用再改了」）就**立刻** ACK。

這裡驗的是那兩條條件真的守得住，以及重跑本身「只動校正」：
`seq`、時間、發言者、使用者改過的名字都不變，只有文字換成新的。

假的 JTLW 照對方文件的契約（v2.26.7）：
ACK 之後 retry 回 409 `invalid_request` ＋ `details.reason = content_cleared`；
送件時沒要 `correct` 的作業 retry 回 409；作業還在跑時 ACK 回 409。
"""
from __future__ import annotations

import json
import time

import pytest

from app.core import jtlw_ack, jtlw_client
from app.core import jtlw_settings as js
from tests.test_meeting_transcribe import (   # noqa: F401  （`unconfigured` 是 fixture）
    FakeJtlw, _configure, _result, _run, _upload, unconfigured,
)

BASE = "/tools/meeting-transcribe"
DAY = 24 * 3600


@pytest.fixture(autouse=True)
def _quick_polls(monkeypatch):
    """輪詢間隔縮短（預設 3 秒起跳、每次 ×1.5），這一組才不會每條等半分鐘。"""
    import importlib
    r = importlib.import_module("app.tools.meeting_transcribe.router")
    monkeypatch.setattr(r, "_POLL_FIRST", 0.1)
    monkeypatch.setattr(r, "_POLL_MAX", 0.3)


def _wait(client, job_id: str, timeout: float = 60.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j.get("status") in ("done", "error", "cancelled"):
            return j
        time.sleep(0.2)
    raise AssertionError(f"作業沒有結束：{j}")


def _transcribe(client, fake, **cfg) -> str:
    _configure(fake, **cfg)
    up = _upload(client)
    j = _run(client, up["upload_id"])
    assert j["status"] == "done", j.get("error")
    return up["upload_id"]


class _Stub:
    """只收 ACK 的假用戶端（巡檢那幾條不需要整台假服務）。"""

    def __init__(self, fail: Exception | None = None):
        self.acked: list[str] = []
        self.fail = fail

    def ack(self, rid):
        if self.fail:
            raise self.fail
        self.acked.append(rid)
        return {}


# ------------------------------------------------------------ ACK 延後多久

def test_the_ack_waits_for_the_retry_window(client, unconfigured):
    """存好之後先不 ACK；最晚 24 小時、巡檢一到就送。"""
    with FakeJtlw() as fake:
        t0 = time.time()
        uid = _transcribe(client, fake)
        assert fake.acked == [], "一存好就 ACK 了 —— 使用者就不能補專有名詞重跑"
        due = jtlw_ack.due_at("job_fake_1")
        assert due is not None and abs(due - (t0 + DAY)) < 120, due
        st = _result(client, uid)["retry_state"]
        assert st["possible"] is True and abs(st["due_at"] - due) < 1
        assert jtlw_ack.sweep(now=due) == 1
        assert fake.acked == ["job_fake_1"]
        assert jtlw_ack.due_at("job_fake_1") is None
        assert _result(client, uid)["retry_state"]["reason"] == "cleared"


def test_the_sweep_sends_a_little_early_never_late(unconfigured):
    """每 10 分鐘巡一次 —— 等「到期」才送的話，最多會晚 10 分鐘，那就超過 24 小時了。"""
    t0 = 1_000_000.0
    js.save({"retry_window_hours": 24})
    js.invalidate_cache()
    jtlw_ack.defer("r1", "a" * 32, now=t0)
    due = t0 + DAY
    stub = _Stub()
    assert jtlw_ack.sweep(now=due - jtlw_ack.SWEEP_S - 60, client=stub) == 0
    assert stub.acked == []
    assert jtlw_ack.sweep(now=due - jtlw_ack.SWEEP_S + 1, client=stub) == 1
    assert stub.acked == ["r1"], "下一次巡檢已經會超過 24 小時，這一輪就要送"


def test_the_window_is_capped_at_24_hours(unconfigured):
    """設定寫 48 也只留 24 小時（JTLW 的條件）。"""
    js.save({"retry_window_hours": 48})
    js.invalidate_cache()
    assert jtlw_ack.window_hours() == 24
    due = jtlw_ack.defer("r1", "a" * 32, now=0.0)
    assert due == DAY


def test_shortening_the_window_applies_to_transcripts_already_waiting(unconfigured):
    """管理員把保留時間改短（例如改成 0），已經在等的那幾件下一輪就要送 ——
    只對之後的作業生效的話，改設定的人會以為已經生效了。"""
    js.save({"retry_window_hours": 24})
    js.invalidate_cache()
    t0 = 1_000_000.0
    jtlw_ack.defer("r1", "a" * 32, now=t0)
    js.save({"retry_window_hours": 1})
    js.invalidate_cache()
    stub = _Stub()
    assert jtlw_ack.sweep(now=t0 + 3600, client=stub) == 1, "改成 1 小時之後，1 小時到了卻沒送"
    jtlw_ack.defer("r2", "b" * 32, now=t0)
    js.save({"retry_window_hours": 0})
    js.invalidate_cache()
    assert jtlw_ack.sweep(now=t0 + 1, client=stub) == 1, "改成 0 之後，下一輪沒有全部送出"


def test_zero_hours_acks_right_away(client, unconfigured):
    """保留時間 0 ＝ 照舊：存好就 ACK，不能重跑。"""
    with FakeJtlw() as fake:
        uid = _transcribe(client, fake, retry_window_hours=0)
        assert fake.acked == ["job_fake_1"]
        assert jtlw_ack.due_at("job_fake_1") is None
    assert _result(client, uid)["retry_state"]["reason"] == "cleared"


def test_without_correction_there_is_nothing_to_rerun_so_ack_right_away(client, unconfigured):
    """送件時沒要 `correct` 的作業，JTLW 的 retry 一律 409 —— 那就沒有理由多留內容。"""
    with FakeJtlw() as fake:
        uid = _transcribe(client, fake, tasks=["transcribe", "diarize"])
        assert fake.acked == ["job_fake_1"], "沒有校正可以重跑，卻還留在對方那邊"
    assert _result(client, uid)["retry_state"]["reason"] == "no_correct"


# ------------------------------------------------------------ 重跑校正

def test_a_retry_reruns_only_the_correction(client, unconfigured):
    """新的專有名詞送過去、只換校正後的文字；時間、發言者、`seq`、改過的名字都不變。"""
    with FakeJtlw() as fake:
        uid = _transcribe(client, fake)
        before = _result(client, uid)
        due = jtlw_ack.due_at("job_fake_1")
        r = client.post(f"{BASE}/speakers/{uid}", json={"map": {"S1": "王小明"}})
        assert r.status_code == 200
        r = client.post(f"{BASE}/retry", json={"upload_id": uid, "terms": "聯發科\n台灣範例"})
        assert r.status_code == 200, r.text
        j = _wait(client, r.json()["job_id"])
        assert j["status"] == "done", j.get("error")
        assert fake.retries == [{"glossary": {"entries": [
            {"source": "聯發科", "mode": "keep"}, {"source": "台灣範例", "mode": "keep"}]}}]
        assert fake.acked == [], "重跑完就 ACK 了 —— 使用者還可以再補"
        assert jtlw_ack.due_at("job_fake_1") == due, "重跑不可以把保留時間往後延"
    after = _result(client, uid)
    keep = ("seq", "start_ms", "end_ms", "speaker")
    assert [{k: s.get(k) for k in keep} for s in after["segments"]] == \
           [{k: s.get(k) for k in keep} for s in before["segments"]]
    assert all("聯發科" in s["text"] for s in after["segments"]), "文字沒有換成新的校正結果"
    assert after["speaker_names"] == {"S1": "王小明"}, "重跑把使用者改好的名字洗掉了"
    assert after["terms"] == ["聯發科", "台灣範例"]
    assert after["retry"]["count"] == 1


def test_a_name_changed_while_the_retry_runs_is_kept(client, unconfigured):
    """重跑要好一陣子，使用者在這段時間改了發言者的名字 —— 寫回去時不可以蓋掉。

    （重跑開始時讀一次檔、做完照那一份寫回去的話，這段時間改的名字就不見了，
    而畫面上要重新整理才看得出來。）"""
    with FakeJtlw(retry_polls=4) as fake:
        uid = _transcribe(client, fake)
        r = client.post(f"{BASE}/retry", json={"upload_id": uid, "terms": "聯發科"})
        assert r.status_code == 200, r.text
        r2 = client.post(f"{BASE}/speakers/{uid}", json={"map": {"S2": "陳經理"}})
        assert r2.status_code == 200
        assert _wait(client, r.json()["job_id"])["status"] == "done"
        assert fake.retries, "重跑根本沒送出去"
    out = _result(client, uid)
    assert out["speaker_names"] == {"S2": "陳經理"}, "重跑期間改的名字被蓋掉了"
    assert all("聯發科" in s["text"] for s in out["segments"])


def test_a_retry_needs_at_least_one_term(client, unconfigured):
    with FakeJtlw() as fake:
        uid = _transcribe(client, fake)
        r = client.post(f"{BASE}/retry", json={"upload_id": uid, "terms": "  \n "})
        assert r.status_code == 400
        assert fake.retries == []


def test_after_done_the_retry_is_refused_before_asking_jtlw(client, unconfigured):
    """按了「不用再改了」→ 立刻 ACK；之後再重跑要講「請重新送件」，而且不必再去問對方。"""
    with FakeJtlw() as fake:
        uid = _transcribe(client, fake)
        r = client.post(f"{BASE}/done", json={"upload_id": uid})
        assert r.status_code == 200 and r.json()["acked"] is True
        assert fake.acked == ["job_fake_1"]
        r = client.post(f"{BASE}/retry", json={"upload_id": uid, "terms": "聯發科"})
        assert r.status_code == 409
        assert "重新送件" in r.json()["detail"]
        assert fake.retries == []


def test_content_cleared_on_their_side_says_to_resubmit(client, unconfigured):
    """我們還以為在保留期內，對方卻已經清掉了（例如對方 7 天到期、或別處送過 ACK）：
    409 `content_cleared` → 講「請重新送件」，並從我們的清單拿掉。"""
    with FakeJtlw() as fake:
        uid = _transcribe(client, fake)
        fake.cleared = True
        r = client.post(f"{BASE}/retry", json={"upload_id": uid, "terms": "聯發科"})
        assert r.status_code == 200, r.text
        j = _wait(client, r.json()["job_id"])
        assert j["status"] == "error"
        assert j["error"] == jtlw_client.MESSAGES["content_cleared"], j["error"]
        assert jtlw_ack.due_at("job_fake_1") is None
    assert _result(client, uid)["retry_state"]["reason"] == "cleared"


def test_no_retry_in_the_last_15_minutes(client, unconfigured):
    """離刪除不到 15 分鐘時重跑，做完之前就到了該刪的時間 —— 那就會超過 24 小時。"""
    with FakeJtlw() as fake:
        uid = _transcribe(client, fake)
        d = json.loads(jtlw_ack._path().read_text(encoding="utf-8"))
        d["job_fake_1"]["due_at"] = time.time() + 5 * 60
        jtlw_ack._path().write_text(json.dumps(d), encoding="utf-8")
        st = _result(client, uid)["retry_state"]
        assert st["possible"] is False and st["reason"] == "too_late"
        r = client.post(f"{BASE}/retry", json={"upload_id": uid, "terms": "聯發科"})
        assert r.status_code == 409
        assert fake.retries == []


def test_one_retry_at_a_time(client, unconfigured):
    with FakeJtlw(retry_polls=3) as fake:
        uid = _transcribe(client, fake)
        r1 = client.post(f"{BASE}/retry", json={"upload_id": uid, "terms": "聯發科"})
        assert r1.status_code == 200
        st = _result(client, uid)["retry_state"]
        assert st["reason"] == "running" and st["job_id"] == r1.json()["job_id"]
        r2 = client.post(f"{BASE}/retry", json={"upload_id": uid, "terms": "台灣範例"})
        assert r2.status_code == 409
        assert client.post(f"{BASE}/done", json={"upload_id": uid}).status_code == 409, \
            "重跑還在跑就 ACK —— 對方會回 409，而畫面以為刪掉了"
        assert _wait(client, r1.json()["job_id"])["status"] == "done"
    assert _result(client, uid)["retry_state"]["possible"] is True


def test_two_clicks_at_once_start_only_one_retry(client, unconfigured, monkeypatch):
    """兩個請求同時通過「能不能重跑」的判斷（按兩下、兩個分頁）—— 只能有一件真的開始。

    判斷與登記之間有空檔，所以登記那一步要自己再擋一次；這裡把判斷固定成「可以」，
    模擬兩個請求都已經通過判斷的那一刻。"""
    import importlib
    r = importlib.import_module("app.tools.meeting_transcribe.router")
    with FakeJtlw(retry_polls=3) as fake:
        uid = _transcribe(client, fake)
        monkeypatch.setattr(r, "_retry_state",
                            lambda *_a: {"possible": True, "due_at": time.time() + DAY})
        r1 = client.post(f"{BASE}/retry", json={"upload_id": uid, "terms": "聯發科"})
        r2 = client.post(f"{BASE}/retry", json={"upload_id": uid, "terms": "台灣範例"})
        assert r1.status_code == 200 and r2.status_code == 409, (r1.text, r2.text)
        assert _wait(client, r1.json()["job_id"])["status"] == "done"
        assert len(fake.retries) == 1


# ------------------------------------------------------------ 送不出去的時候

def test_done_while_jtlw_is_down_is_kept_for_the_next_sweep(client, unconfigured):
    with FakeJtlw() as fake:
        uid = _transcribe(client, fake)
        fake.ack_status = 503
        r = client.post(f"{BASE}/done", json={"upload_id": uid})
        assert r.status_code == 200 and r.json()["acked"] is False
        due = jtlw_ack.due_at("job_fake_1")
        assert due is not None and due <= time.time() + 1, "送不出去的要在下一輪就再試"
        assert _result(client, uid)["retry_state"]["possible"] is False, \
            "使用者已經說不用了 —— 不可以因為這次沒送出去又讓他重跑"
        fake.ack_status = None
        assert jtlw_ack.sweep() == 1
        assert fake.acked == ["job_fake_1"]


def test_a_job_they_no_longer_have_is_dropped(unconfigured):
    jtlw_ack.defer("r1", "a" * 32, now=0.0)
    stub = _Stub(fail=jtlw_client.JtlwError("找不到", code="not_found", status=404))
    assert jtlw_ack.sweep(now=DAY, client=stub) == 1
    assert jtlw_ack.due_at("r1") is None


def test_one_that_never_gets_through_is_given_up_after_their_own_expiry(unconfigured):
    """對方沒 ACK 的作業 7 天後自己清掉 —— 一直連不上的話，8 天後不再重試。"""
    jtlw_ack.defer("r1", "a" * 32, now=0.0)
    stub = _Stub(fail=jtlw_client.JtlwUnavailable("連不上"))
    assert jtlw_ack.sweep(now=DAY, client=stub) == 0
    assert jtlw_ack.due_at("r1") is not None
    jtlw_ack.sweep(now=jtlw_ack.GIVE_UP_S + 1, client=stub)
    assert jtlw_ack.due_at("r1") is None


def test_the_pending_list_survives_a_restart(unconfigured):
    """清單存在資料目錄（不是暫存區、不是記憶體）—— 重啟之後要記得哪幾件還沒送。"""
    from app.config import settings
    jtlw_ack.defer("r1", "a" * 32, now=0.0)
    assert jtlw_ack._path().parent == settings.data_dir
    assert "r1" in json.loads(jtlw_ack._path().read_text(encoding="utf-8"))


# ------------------------------------------------------------ API 與設定頁

def test_the_sync_api_says_how_long_a_retry_is_possible(client, unconfigured):
    with FakeJtlw() as fake:
        _configure(fake)
        data = b"\x00\x00\x00\x20ftypM4A " + b"x" * 5000
        r = client.post(f"{BASE}/api/meeting-transcribe",
                        files={"file": ("會議.m4a", data, "audio/mp4")})
        assert r.status_code == 200, r.text
        out = r.json()
        assert len(out["upload_id"]) == 32
        assert out["retry_until"] == pytest.approx(time.time() + DAY, abs=120)
        assert fake.acked == []


def test_the_settings_page_clamps_the_window(client, unconfigured):
    for given, saved in ((30, 24), (-5, 0), (6, 6)):
        r = client.post("/admin/api/jtlw/settings", json={"retry_window_hours": given})
        assert r.status_code == 200, r.text
        js.invalidate_cache()
        assert js.get()["retry_window_hours"] == saved, (given, js.get()["retry_window_hours"])
