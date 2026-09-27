"""轉逐字稿的輪詢與取結果，要撐過對方短暫連不上。

## 由來（2026-09-27，語音服務 v2.17 通知）

對方升級時要重啟服務，中斷約 5 秒。去看我們的輪詢才發現：`client.job()` 碰到
**一次**連不上就丟例外，整件作業判失敗 —— 而對方那邊其實還在跑。一場三小時的會議
白轉，我們也不會再回去取結果（沒 ACK，對方 72 小時後清掉）。網路閃一下也是一樣。

## 判準

**真的跑一次 `_run_job`**（假時鐘、假的對方在中途斷線），驗三個方向：

* 短暫斷線 → 作業照樣完成、逐字稿存得下來、有 ACK。
* 斷太久 → 要放棄，而且是在寬限時間到的時候（不是立刻、也不是永遠等下去）。
* **反向對照**：不是暫時性的錯誤（找不到作業）→ 照舊立刻失敗、不重試。
  只驗前兩條的話，「什麼錯誤都重試」也會過 —— 而那會讓真的壞掉晚 5 分鐘才看得到、
  訊息還變成「連不上」而不是真正的原因。
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

R = importlib.import_module("app.tools.meeting_transcribe.router")
C = importlib.import_module("app.core.jtlw_client")


class _Job:
    def __init__(self):
        self.cancelled = False
        self.meta: dict = {}
        self.progress = 0.0
        self.message = ""
        self.result_path = None
        self.result_filename = ""


def _down():
    return C.JtlwUnavailable("連不上 JTLW", details={"base": "https://jtlw.example"})


class _Flaky:
    """假的對方。`plan` 是每一次 `job()` 要做的事；`seg_plan` 是每一次 `segments()` 的。

    'run' 執行中／'done' 成功／'down' 連不上／'503' 伺服器暫時不能服務／'404' 找不到作業
    """

    def __init__(self, plan, seg_plan=()):
        self.plan = list(plan)
        self.seg_plan = list(seg_plan)
        self.job_calls = 0
        self.acked: list[str] = []
        self.cancelled: list[str] = []

    def profiles(self):
        return []

    def submit(self, body, *, idempotency_key):
        return {"job_id": "remote-1"}

    def job(self, remote_id):
        self.job_calls += 1
        step = self.plan.pop(0) if self.plan else "done"
        if step == "down":
            raise _down()
        if step == "503":
            raise C.JtlwError("JTLW 回了 HTTP 503", status=503)
        if step == "404":
            raise C.JtlwError("找不到這件作業", code="job_not_found", status=404)
        if step == "run":
            return {"status": "running", "progress": {"stage": "asr", "percent": 40.0}}
        return {"status": "succeeded", "progress": {"stage": "finalize", "percent": 100.0}}

    def segments(self, remote_id, layer, *, after_seq=0, limit=500):
        if self.seg_plan:
            step = self.seg_plan.pop(0)
            if step == "down":
                raise _down()
        if after_seq:
            return {"segments": [], "has_more": False}
        row = {"seq": 1, "text": "今天先確認時程", "start_ms": 0, "end_ms": 2000}
        if layer == "speakers":
            row = {"seq": 1, "speaker_id": "S1"}
        return {"segments": [row], "has_more": False}

    def result(self, remote_id):
        return {"duration_ms": 2000}

    def ack(self, remote_id):
        self.acked.append(remote_id)
        return {}

    def cancel(self, remote_id):
        self.cancelled.append(remote_id)
        raise _down()


@pytest.fixture
def harness(monkeypatch, tmp_path):
    clock = {"t": 1000.0}
    monkeypatch.setattr(R.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(R.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(R, "_build_body", lambda *a, **k: {})
    monkeypatch.setattr(R, "_tasks_for", lambda client, prof, tasks: (tasks, []))
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"filename": "會議.m4a", "size_bytes": 1234}), encoding="utf-8")
    monkeypatch.setattr(R, "_meta_path", lambda _u: meta)
    out = tmp_path / "out.json"
    monkeypatch.setattr(R, "_out_path", lambda _u: out)

    def run(fake):
        monkeypatch.setattr(R.jtlw_client, "JtlwClient", lambda *a, **k: fake)
        job = _Job()
        clock["t"] = 1000.0
        R._run_job(job, "0" * 32, "zh-Hant", None)
        return job, clock["t"] - 1000.0, out

    def run_failing(fake, exc_type):
        monkeypatch.setattr(R.jtlw_client, "JtlwClient", lambda *a, **k: fake)
        job = _Job()
        clock["t"] = 1000.0
        with pytest.raises(exc_type) as ei:
            R._run_job(job, "0" * 32, "zh-Hant", None)
        return job, clock["t"] - 1000.0, ei.value

    return run, run_failing


def test_a_brief_outage_while_polling_does_not_fail_the_job(harness):
    """對方重啟那幾秒剛好碰上輪詢：作業要照樣完成，逐字稿存下來，而且有 ACK。"""
    run, _ = harness
    fake = _Flaky(["run", "down", "down", "run", "done"])
    job, _, out = run(fake)
    assert out.exists(), "短暫連不上之後，作業沒有完成"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [s["text"] for s in data["segments"]] == ["今天先確認時程"]
    assert fake.acked == ["remote-1"], "逐字稿存好之後要 ACK"
    assert job.message == "完成"


def test_gateway_errors_count_as_temporary(harness):
    """重啟時若前面有代理，看到的是 502 / 503 / 504，不是連不上 —— 一樣要撐過去。"""
    run, _ = harness
    fake = _Flaky(["run", "503", "503", "done"])
    _, _, out = run(fake)
    assert out.exists()


def test_a_brief_outage_while_fetching_the_transcript_is_survived(harness):
    """作業做完、正在取三層逐字稿時斷線：一樣要撐過去，不可以把做好的結果丟掉。"""
    run, _ = harness
    fake = _Flaky(["done"], seg_plan=["down", "down"])
    _, _, out = run(fake)
    assert out.exists(), "取逐字稿時短暫連不上，結果被丟掉了"
    assert json.loads(out.read_text(encoding="utf-8"))["segments"][0]["speaker"] == "S1"


def test_the_screen_says_it_is_reconnecting_and_then_recovers(harness, monkeypatch):
    """斷線當下要講出「重試中」（不然看起來像卡住），恢復之後要回到原本的階段文字。"""
    run, _ = harness
    seen: list[str] = []

    class _Watch(_Flaky):
        def job(self, remote_id):
            seen.append(self._job_ref.message)
            return super().job(remote_id)

    fake = _Watch(["down", "run", "done"])
    orig = R._through_outage

    def spy(j, *a, **k):
        fake._job_ref = j
        return orig(j, *a, **k)

    monkeypatch.setattr(R, "_through_outage", spy)
    job, _, _ = run(fake)
    assert any("暫時連不上 JTLW" in m for m in seen), f"斷線時畫面沒有講出在重試：{seen}"
    assert job.message == "完成"


def test_a_long_outage_gives_up_at_the_grace_period(harness):
    """一直連不上：要在寬限時間到的時候放棄，訊息講得出斷了多久。"""
    _, run_failing = harness
    fake = _Flaky(["run"] + ["down"] * 500)
    _, waited, exc = run_failing(fake, C.JtlwUnavailable)
    assert waited >= R._OUTAGE_GRACE_S, f"只等了 {waited:.0f} 秒就放棄"
    assert waited < R._OUTAGE_GRACE_S + R._OUTAGE_RETRY_MAX + R._POLL_MAX + 5, (
        f"等了 {waited:.0f} 秒，寬限失控了")
    assert "連續" in str(exc) and "分鐘連不上" in str(exc)


def test_a_real_error_still_fails_at_once(harness):
    """反向對照：對方說「找不到這件作業」不是暫時的，重問幾次都一樣 —— 要立刻失敗。"""
    _, run_failing = harness
    fake = _Flaky(["run", "404", "run", "done"])
    _, _, exc = run_failing(fake, C.JtlwError)
    assert exc.status == 404, f"真正的原因被蓋掉了：{exc!r}"
    assert fake.job_calls == 2, "不是暫時性的錯誤卻重試了"


def test_cancelling_during_an_outage_stops_the_job(harness, monkeypatch):
    """斷線中按停止：要停下來（而且試著把取消傳過去），不可以等滿寬限時間。"""
    run, run_failing = harness

    class _CancelMidOutage(_Flaky):
        def job(self, remote_id):
            if self.job_calls >= 2:
                self._job_ref.cancelled = True
            return super().job(remote_id)

    fake = _CancelMidOutage(["run", "down", "down", "down", "done"])
    orig = R._through_outage

    def spy(j, *a, **k):
        fake._job_ref = j
        return orig(j, *a, **k)

    monkeypatch.setattr(R, "_through_outage", spy)
    _, waited, exc = run_failing(fake, RuntimeError)
    assert str(exc) == "已取消"
    assert fake.cancelled == ["remote-1"], "取消沒有試著傳給對方"
    assert waited < R._OUTAGE_GRACE_S


def test_the_new_messages_are_translated():
    """斷線與放棄的訊息是背景執行緒產生、送到前端翻譯的 —— 語系檔要有。"""
    for lang in ("en", "ja"):
        cat = json.loads((ROOT / "app" / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        for key in (R._MSG_RECONNECTING, R._MSG_OUTAGE):
            assert key in cat, f"{lang}.json 缺：{key}"
    assert R._MSG_RECONNECTING in R.PROGRESS_TEMPLATES
    assert R._MSG_OUTAGE in R.PROGRESS_TEMPLATES
