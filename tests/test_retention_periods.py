"""檔案保留期：**設定頁上的每一個數字都要真的生效**。

## 由來

v1.14.31 的對抗式驗證發現「作業結果檔保留時數」（`jobs_hours`）這個設定
**從來沒有被任何清理程式讀過**。清理函式只收一個秒數、對 `data/temp` 與
`data/jobs` 用同一個 cutoff，而傳進去的是暫存檔那個值。

後果是使用者與管理員看到的保留期是假的：管理頁顯示「作業結果檔 24 小時」、
「我的作業」頁的取件期限也顯示 24 小時，實際上 2 小時就被清掉了。

**沒有錯誤訊息、沒有任何徵兆** —— 只有使用者回頭找檔案時發現不見了，而他會
以為是自己記錯或系統壞了。這正是「壞掉了很難發現」的典型，所以要有測試守著。
"""
from __future__ import annotations

import os
import pathlib
import time

import pytest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    for sub in ("temp", "jobs"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _aged(path: pathlib.Path, hours: float) -> pathlib.Path:
    path.write_bytes(b"x")
    t = time.time() - hours * 3600
    os.utime(path, (t, t))
    return path


def test_jobs_hours_is_actually_used(data_dir):
    """`jobs_hours` 要管 `data/jobs`，不可以跟著暫存檔的期限走。"""
    from app.core import retention

    old_temp = _aged(data_dir / "temp" / "upload.pdf", 3)
    job_3h = _aged(data_dir / "jobs" / "result_3h.pdf", 3)
    job_47h = _aged(data_dir / "jobs" / "result_47h.pdf", 47)
    job_49h = _aged(data_dir / "jobs" / "result_49h.pdf", 49)

    # 暫存 1 小時、作業結果 48 小時
    retention._sweep_temp_dir(1 * 3600, 48 * 3600)

    assert not old_temp.exists(), "3 小時前的暫存檔應該被清掉（保留 1 小時）"
    assert job_3h.exists(), "3 小時前的作業結果不該被清（保留 48 小時）"
    assert job_47h.exists(), (
        "47 小時前的作業結果不該被清 —— jobs_hours 沒有生效，"
        "使用者看到的取件期限是假的")
    assert not job_49h.exists(), "49 小時前的作業結果應該被清掉"


def test_permanent_retention_means_permanent(data_dir):
    """0 或負數 = 永久保留（`-1` 是文件寫的寫法，0 也不該刪東西）。"""
    from app.core import retention

    a = _aged(data_dir / "temp" / "a.pdf", 10_000)
    b = _aged(data_dir / "jobs" / "b.pdf", 10_000)

    retention._sweep_temp_dir(0, -1)
    assert a.exists() and b.exists(), "設成永久保留時不可以刪任何東西"


def test_sweep_all_passes_both_periods(data_dir, monkeypatch):
    """從設定檔一路到清理，兩個期限都要各自傳下去。

    只測 `_sweep_temp_dir` 不夠 —— 原本的 bug 正是在**呼叫端**（只傳了一個
    值），函式本身沒有錯。
    """
    from app.core import retention

    seen = {}

    def spy(temp_seconds, jobs_seconds):
        seen["temp"] = temp_seconds
        seen["jobs"] = jobs_seconds
        return 0

    monkeypatch.setattr(retention, "_sweep_temp_dir", spy)
    s = retention.get()
    s["temp_hours"] = 2
    s["jobs_hours"] = 24
    monkeypatch.setattr(retention, "get", lambda: s)
    for name in ("_sweep_audit", "_sweep_history", "_sweep_job_rows"):
        if hasattr(retention, name):
            monkeypatch.setattr(retention, name, lambda *a, **k: 0)
    try:
        retention.sweep_all()
    except Exception:  # noqa: BLE001 — 其他 sweeper 需要資料庫，這裡不關心
        pass

    assert seen.get("temp") == 2 * 3600, seen
    assert seen.get("jobs") == 24 * 3600, (
        f"jobs_hours 沒有被傳下去（收到 {seen.get('jobs')}）")
