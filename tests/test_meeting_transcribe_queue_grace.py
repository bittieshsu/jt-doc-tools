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
    with pytest.raises(RuntimeError) as ei:
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
