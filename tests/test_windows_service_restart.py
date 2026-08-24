"""Windows 的 `jtdt restart` 必須真的把服務啟起來（2026-08-24 實機重現）。

## 症狀

`jtdt restart` 之後服務停在 **STOPPED**，而**回傳碼是 0、畫面上沒有任何訊息**。
單獨再下一次 `jtdt start` 就會起來。更新流程走同一組函式，所以客戶
`jtdt update` 完也可能是「服務沒起來卻查不到原因」。

## 根因（實機抓到的原始輸出）

    [stop 已送出]  STATE : 3  STOP_PENDING
    [SC] StartService 失敗 1056:
    [start 回傳 0]

`sc.exe stop` 是**非同步**的 —— 送出要求就回來，服務還在 `STOP_PENDING`。
緊接著的 `sc.exe start` 被 SCM 以 **1056（ERROR_SERVICE_ALREADY_RUNNING）**
擋掉，而原本的程式把 1056 一律當成「已經在跑，別嚇使用者」直接回 0。
**錯誤被自己吞掉**，這是它無聲的原因。

測試清單：
  1. `svc_stop` 要等到真的 STOPPED 才回來
  2. 停止逾時要出聲（不可安靜地繼續）
  3. `svc_start` 遇到 1056：服務真的 RUNNING → 成功
  4. `svc_start` 遇到 1056：其實在 STOP_PENDING → **不可當成功**，要等停完重試
  5. 重試後仍失敗 → 回非 0（不可再吞）
  6. `svc_restart` 整條走完之後，服務要是 RUNNING
"""
from __future__ import annotations

import pytest

from app import cli


class _FakeSCM:
    """假的 Windows 服務控制器：stop 之後會先停在 STOP_PENDING 幾拍。"""

    def __init__(self, pending_ticks: int = 2, start_works: bool = True):
        self.state = "RUNNING"
        self.pending = 0
        self.pending_ticks = pending_ticks
        self.start_works = start_works
        self.start_calls = 0
        self.starts_while_pending = 0

    # --- 被測程式會呼叫的兩個入口 ---
    def run(self, cmd):
        if cmd[:2] == ["sc.exe", "stop"]:
            self.state = "STOP_PENDING"
            self.pending = self.pending_ticks
            return 0
        return 0

    def run_capture(self, cmd):
        if cmd[:2] == ["sc.exe", "query"]:
            if self.state == "STOP_PENDING":
                self.pending -= 1
                if self.pending <= 0:
                    self.state = "STOPPED"
            return 0, f"        STATE              : 4  {self.state}"
        if cmd[:2] == ["sc.exe", "start"]:
            self.start_calls += 1
            if self.state == "STOP_PENDING":
                # 記下「在停止中就嘗試啟動」—— 那是註定被 1056 擋掉的一次
                self.starts_while_pending += 1
            if self.state in ("STOP_PENDING", "RUNNING"):
                # SCM 對「停止中」與「已在跑」都回 1056
                return 1056, "[SC] StartService 失敗 1056:"
            if not self.start_works:
                return 1053, "[SC] StartService 失敗 1053:"
            self.state = "RUNNING"
            return 0, ""
        return 0, ""


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(cli, "_is_windows", lambda: True)
    monkeypatch.setattr(cli, "_is_linux", lambda: False)
    monkeypatch.setattr(cli, "_is_macos", lambda: False)
    scm = _FakeSCM()
    monkeypatch.setattr(cli, "_run", scm.run)
    monkeypatch.setattr(cli, "_run_capture", scm.run_capture)
    # 讓等待迴圈不要真的睡
    monkeypatch.setattr(cli.time if hasattr(cli, "time") else __import__("time"),
                        "sleep", lambda *_a: None, raising=False)
    return scm


# ------------------------------------------------------------------ 1
def test_stop_waits_until_really_stopped(win, monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda *_a: None)
    cli.svc_stop()
    assert win.state == "STOPPED", "stop 回來時服務還在停止中"


# ------------------------------------------------------------------ 2
def test_stop_complains_when_it_never_stops(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_is_windows", lambda: True)
    monkeypatch.setattr(cli, "_is_linux", lambda: False)
    monkeypatch.setattr(cli, "_is_macos", lambda: False)
    monkeypatch.setattr(cli, "_run", lambda cmd: 0)
    monkeypatch.setattr(cli, "_run_capture",
                        lambda cmd: (0, "STATE : 3 STOP_PENDING"))
    import time
    monkeypatch.setattr(time, "sleep", lambda *_a: None)
    monkeypatch.setattr(time, "time", (lambda c=iter([0, 1, 999, 999, 999]):
                                       next(c)), raising=False)
    cli.svc_stop()
    err = capsys.readouterr().err
    assert "沒有停止" in err, "停止逾時竟然沒有出聲（這正是無聲失敗的來源）"


# ---------------------------------------------------------------- 3, 4, 5
def test_1056_counts_as_success_only_when_really_running(win, monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda *_a: None)
    win.state = "RUNNING"
    assert cli.svc_start() == 0


def test_1056_while_stopping_is_not_success(win, monkeypatch):
    """**這條就是那個 bug**：停止中也會回 1056，早期版本一律當成功。"""
    import time
    monkeypatch.setattr(time, "sleep", lambda *_a: None)
    win.state = "STOP_PENDING"
    win.pending = 2
    rc = cli.svc_start()
    assert rc == 0
    assert win.state == "RUNNING", "服務沒有被啟動，卻回報成功"
    # 判準是**結果**（服務真的起來了），不是「重試了幾次」——
    # 修法是先等停完再啟動，所以一次就夠；寫死次數等於把實作細節當規格。
    assert win.starts_while_pending == 0, (
        "在服務還停止中就嘗試啟動 —— 那一次一定被 SCM 以 1056 擋掉")


def test_real_failure_is_reported(win, monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda *_a: None)
    win.state = "STOPPED"
    win.start_works = False
    assert cli.svc_start() != 0, "啟動真的失敗時不可回 0"


# ------------------------------------------------------------------ 6
def test_restart_ends_with_the_service_running(win, monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda *_a: None)
    assert cli.svc_restart() == 0
    assert win.state == "RUNNING", "restart 之後服務沒有在跑"
