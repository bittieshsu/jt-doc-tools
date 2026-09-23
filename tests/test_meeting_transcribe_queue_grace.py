"""轉逐字稿的等待上限要留排隊的時間 —— 不可以把排在長會議後面的作業誤殺。

## 由來（2026-09-23，語音服務 v2.7 實測）

對方的 GPU 一次只跑一件、其餘排隊。**排隊時與辨識中都回 `running`、進度完全不動**
（連 `updated_at` 都不動），我們分不出「在排隊」與「卡住了」。前面排一場 3 小時的
中文會議約要等 18～30 分鐘 —— 超過我們原本 15 分鐘的下限，碰到就會被判逾時，
**而且我們會主動請對方取消**那一件本來會成功的作業。

所以在對方的 `progress.waiting` 上線之前，上限多給 60 分鐘排隊寬限。

## 判準

**真的跑一次輪詢迴圈**（假時鐘、假的對方一直回 `running` 且進度不動），不是只驗
那支算上限的函式 —— 只驗函式的話，證明得了「帳算得對」，證明不了「迴圈真的用了它」
（稽核 F06 那次的教訓）。
"""
from __future__ import annotations

import importlib

import pytest

R = importlib.import_module("app.tools.meeting_transcribe.router")


class _Job:
    def __init__(self):
        self.cancelled = False
        self.meta: dict = {}
        self.progress = 0.0
        self.message = ""


class _StuckButRunning:
    """對方的樣子：送件成功之後，永遠回 running、stage=asr、進度不動。"""

    def __init__(self, total_audio_ms: "int | None"):
        self.total = total_audio_ms
        self.cancelled: list[str] = []

    def submit(self, body, *, idempotency_key):
        return {"job_id": "remote-1"}

    def job(self, remote_id):
        prog = {"stage": "asr", "percent": 50.0, "processed_audio_ms": 0}
        if self.total is not None:
            prog["total_audio_ms"] = self.total
        return {"status": "running", "progress": prog}

    def cancel(self, remote_id):
        self.cancelled.append(remote_id)


def _run(monkeypatch, tmp_path, total_audio_ms: "int | None"):
    """跑到輪詢迴圈放棄為止，回傳（放棄時經過了幾秒, 假的對方）。"""
    clock = {"t": 1000.0}
    fake = _StuckButRunning(total_audio_ms)
    monkeypatch.setattr(R.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(R.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(R.jtlw_client, "JtlwClient", lambda *a, **k: fake)
    monkeypatch.setattr(R, "_build_body", lambda *a, **k: {})
    uid = "0" * 32
    monkeypatch.setattr(R, "_meta_path", lambda _u: tmp_path / "meta.json")
    (tmp_path / "meta.json").write_text("{}", encoding="utf-8")
    # **逾時要是自己那一種例外** —— 同步 API 靠它回 504，跟「對方說失敗了」（502）分開
    with pytest.raises(R.TranscribeTimeout) as ei:
        R._run_job(_Job(), uid, "auto", None)
    assert "還沒有回報結果" in str(ei.value), f"放棄的原因不是逾時：{ei.value}"
    return clock["t"] - 1000.0, fake


def test_a_short_recording_waits_through_a_long_queue(monkeypatch, tmp_path):
    """7 分鐘的錄音：以前 15 分鐘就放棄；現在要撐過一場長會議的排隊。"""
    waited, fake = _run(monkeypatch, tmp_path, total_audio_ms=7 * 60 * 1000)
    assert waited >= (15 + 60) * 60, (
        f"只等了 {waited / 60:.1f} 分鐘就放棄 —— 排在一場 3 小時會議後面"
        "（約 18～30 分鐘）的作業會被誤殺")
    assert waited < (15 + 60) * 60 + 60, f"等了 {waited / 60:.1f} 分鐘，上限失控了"
    assert fake.cancelled == ["remote-1"], "放棄時要請對方取消（不然對方照樣算完、GPU 白燒）"


def test_a_long_recording_gets_its_own_budget_plus_the_grace(monkeypatch, tmp_path):
    """3 小時的錄音：音訊長度 × 0.5（90 分鐘）再加排隊寬限。"""
    waited, _ = _run(monkeypatch, tmp_path, total_audio_ms=3 * 3600 * 1000)
    assert waited >= (90 + 60) * 60, f"只等了 {waited / 60:.1f} 分鐘"
    assert waited < (90 + 60) * 60 + 60, f"等了 {waited / 60:.1f} 分鐘，上限失控了"


def test_the_grace_is_not_unbounded():
    """寬限是有上限的 —— 真的卡住的作業終究要放棄，不可以永遠等下去。"""
    assert 0 < R._QUEUE_GRACE_S <= 2 * 3600
    assert R._deadline_s(None) == R._POLL_FLOOR_S + R._QUEUE_GRACE_S


def test_when_they_do_not_report_the_length_the_grace_still_applies(monkeypatch, tmp_path):
    """對方排隊時**不一定回報錄音長度**（v2.7 實測那張表裡就沒有 `total_audio_ms`）。

    那時候用的是迴圈一開始設的上限 —— 第一版的變異驗證抓到：只要假的對方每次都回報
    長度，一開始那個值根本用不到，把它改回不含寬限的版本照樣全綠。
    """
    waited, _ = _run(monkeypatch, tmp_path, total_audio_ms=None)
    assert waited >= (15 + 60) * 60, (
        f"對方沒回報長度時只等了 {waited / 60:.1f} 分鐘就放棄")


# ---------- api_revision 2.2（2026-09-23 上線）：`progress.waiting` ----------
#
# 對方升級之後排隊看得出來了：GPU 伺服器排隊時 `progress.waiting` 有值
# （`reason` / `ahead` / `since`），其餘時候是 null。形狀照對方 schema 2.2 的
# `Progress`（`docs-share/…/jtlw-api-v1.schema.json`，不上 git）。

class _MessageLog(_Job):
    """記下每一句 `job.message`，驗畫面上真的講出「前面還有幾件」。"""

    def __init__(self):
        self.messages: list[str] = []
        super().__init__()

    @property
    def message(self):
        return self.messages[-1] if self.messages else ""

    @message.setter
    def message(self, v):
        self.messages.append(v)


class _Timeline:
    """依假時鐘回應：先排隊 `queue_s` 秒（前面 `ahead` 件），之後辨識中，
    `work_s` 秒後以 `end_status` 結束（None＝一直辨識下去）。"""

    def __init__(self, clock: dict, *, queue_s: float, work_s: "float | None",
                 ahead: int = 2, end_status: str = "failed"):
        self.clock, self.t0 = clock, clock["t"]
        self.queue_s, self.work_s = queue_s, work_s
        self.ahead, self.end_status = ahead, end_status
        self.cancelled: list[str] = []

    def submit(self, body, *, idempotency_key):
        return {"job_id": "remote-1"}

    def job(self, remote_id):
        t = self.clock["t"] - self.t0
        base = {"stage": "asr", "stage_index": 3, "stage_count": 6, "percent": 30.0,
                "total_audio_ms": 7 * 60 * 1000, "items_done": None, "items_total": None}
        if t < self.queue_s:
            return {"status": "running", "progress": {
                **base, "processed_audio_ms": 0,
                "waiting": {"reason": "gpu_queue", "ahead": self.ahead,
                            "since": "2026-09-23T09:00:00Z"}}}
        if self.work_s is not None and t >= self.queue_s + self.work_s:
            return {"status": self.end_status,
                    "errors": [{"code": "fake_end", "retryable": False}]}
        return {"status": "running", "progress": {
            **base, "processed_audio_ms": 60000, "waiting": None}}

    def cancel(self, remote_id):
        self.cancelled.append(remote_id)


def _run_timeline(monkeypatch, tmp_path, **kw):
    clock = {"t": 1000.0}
    fake = _Timeline(clock, **kw)
    monkeypatch.setattr(R.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(R.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(R.jtlw_client, "JtlwClient", lambda *a, **k: fake)
    monkeypatch.setattr(R, "_build_body", lambda *a, **k: {})
    monkeypatch.setattr(R, "_meta_path", lambda _u: tmp_path / "meta.json")
    (tmp_path / "meta.json").write_text("{}", encoding="utf-8")
    job = _MessageLog()
    with pytest.raises(RuntimeError) as ei:
        R._run_job(job, "0" * 32, "auto", None)
    return str(ei.value), clock["t"] - 1000.0, fake, job


def test_three_hours_in_the_gpu_queue_is_not_a_timeout(monkeypatch, tmp_path):
    """排在好幾場長會議後面三小時 —— 以前 75 分鐘就放棄，現在排隊不算上限。"""
    msg, waited, fake, job = _run_timeline(monkeypatch, tmp_path,
                                           queue_s=3 * 3600, work_s=5 * 60)
    assert "還沒有回報結果" not in msg and "排隊超過" not in msg, (
        f"排了 {waited / 3600:.1f} 小時就被判逾時：{msg}")
    assert "fake_end" in msg, f"應該是跑到對方回報結束才停：{msg}"
    assert fake.cancelled == [], "排隊中被我們主動取消了"
    assert "排隊中（前面還有 2 件）" in job.messages, (
        f"畫面上沒講出前面還有幾件：{sorted(set(job.messages))}")


def test_without_a_queue_the_old_grace_is_gone(monkeypatch, tmp_path):
    """對方回報 `waiting: null`（沒在排隊）卻一直不結束：上限回到處理本身的 15 分鐘，
    不再多給 60 分鐘 —— 寬限是給分不出排隊的舊版服務的。"""
    msg, waited, fake, _job = _run_timeline(monkeypatch, tmp_path, queue_s=0, work_s=None)
    assert "還沒有回報結果" in msg, msg
    assert R._POLL_FLOOR_S <= waited < R._POLL_FLOOR_S + 120, (
        f"等了 {waited / 60:.1f} 分鐘 —— 上限應該是 15 分鐘（不含排隊寬限）")
    assert fake.cancelled == ["remote-1"]


def test_the_queue_itself_has_a_cap(monkeypatch, tmp_path):
    """GPU 伺服器掛住時前面的作業永遠做不完 —— 排隊也要有總上限，不可以一直佔著名額。"""
    msg, waited, fake, _job = _run_timeline(monkeypatch, tmp_path,
                                            queue_s=10 * 3600, work_s=None)
    assert "排隊超過" in msg, msg
    assert R._QUEUE_CAP_S <= waited < R._QUEUE_CAP_S + 120, f"排了 {waited / 3600:.2f} 小時才放棄"
    assert fake.cancelled == ["remote-1"], "放棄時要請對方取消"


def test_queue_time_before_processing_still_leaves_the_full_budget(monkeypatch, tmp_path):
    """排隊 2 小時、之後卡住：要在**輪到之後**再給滿 15 分鐘才放棄，不是一輪到就放棄。"""
    msg, waited, _fake, _job = _run_timeline(monkeypatch, tmp_path,
                                             queue_s=2 * 3600, work_s=None)
    assert "還沒有回報結果" in msg, msg
    after_turn = waited - 2 * 3600
    assert R._POLL_FLOOR_S <= after_turn < R._POLL_FLOOR_S + 120, (
        f"輪到之後只給了 {after_turn / 60:.1f} 分鐘")


@pytest.mark.parametrize("info,label", [
    ({"status": "running", "progress": {"stage": "asr", "waiting":
      {"reason": "gpu_queue", "ahead": 3, "since": "2026-09-23T09:00:00Z"}}},
     "排隊中（前面還有 3 件）"),
    ({"status": "running", "progress": {"stage": "diarization", "waiting":
      {"reason": "gpu_queue", "ahead": 1, "since": "2026-09-23T09:00:00Z"}}},
     "排隊中（前面還有 1 件）"),
    ({"status": "running", "progress": {"stage": "correction", "waiting": None,
                                        "items_done": 1, "items_total": 3}},
     "校正中（已完成 1 / 3 批）"),
    ({"status": "running", "progress": {"stage": "asr", "waiting": None}}, "辨識中"),
    ({"status": "queued", "queue_position": 2}, "排隊中（前面還有 2 件）"),
])
def test_the_stage_label_says_what_it_is_waiting_for(info, label):
    assert R._stage_label(info) == label
