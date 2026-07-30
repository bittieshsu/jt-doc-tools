"""CPU 限制（轉檔不影響網頁回應）的測試。

核心要守住的事實只有一句：**預設就要留至少一顆核心給網頁**。這件事一旦被誰
「順手改成預設不限制」，正式機就會回到 2026-07-30 那天整站空轉的狀態，而且
症狀（CPU 沒滿卻沒反應）非常難連回這個設定。
"""
from __future__ import annotations

import os
import sys

import pytest

from app.core import concurrency_settings as cs
from app.core import cpu_limit


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """設定寫到 tmp_path —— 絕不能碰到使用者真正的 concurrency.json。"""
    from app import config
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    cs.invalidate_cache()
    yield
    cs.invalidate_cache()


# ---------- 配額計算 ----------

def test_default_reserves_one_core_for_web():
    """預設（0 = 自動）必須留一顆核心。這是「網頁回應優先」的底線。"""
    total = cpu_limit.cpu_count()
    if total > 1:
        assert cpu_limit.effective_cores(0) == total - 1
        assert cpu_limit.reserved_cores(0) >= 1


def test_single_core_machine_does_not_end_up_with_zero():
    """單核機器不能算出 0 顆（那會變成綁不到任何核心 → soffice 跑不動）。"""
    for pct in (0, 10, 25, 50, 100):
        assert cpu_limit.effective_cores(pct) >= 1


def test_explicit_percent_scales_with_core_count(monkeypatch):
    monkeypatch.setattr(cpu_limit, "cpu_count", lambda: 8)
    assert cpu_limit.effective_cores(25) == 2
    assert cpu_limit.effective_cores(50) == 4
    # 75% 會算出 6，仍在 total-1 之內
    assert cpu_limit.effective_cores(75) == 6


def test_even_high_percent_keeps_one_core_unless_exactly_100(monkeypatch):
    """填 90% 在 8 核上會算出 7.2 → 夾到 7（留 1 顆）；只有 100 才真的不限制。"""
    monkeypatch.setattr(cpu_limit, "cpu_count", lambda: 8)
    assert cpu_limit.effective_cores(90) == 7
    assert cpu_limit.effective_cores(100) == 8
    assert cpu_limit.reserved_cores(100) == 0


def test_core_set_leaves_cpu0_alone(monkeypatch):
    """挑核心要從編號大的往前取，CPU 0 留給網頁。"""
    monkeypatch.setattr(cpu_limit, "cpu_count", lambda: 8)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(8)),
                        raising=False)
    cores = cpu_limit._core_set(2)
    assert cores == [6, 7]
    assert 0 not in cores


def test_core_set_respects_existing_affinity(monkeypatch):
    """已經被 cgroup cpuset 限制過的機器，不可以挑到遮罩外的核心。"""
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {2, 3, 5},
                        raising=False)
    monkeypatch.setattr(cpu_limit, "cpu_count", lambda: 3)
    assert set(cpu_limit._core_set(2)) <= {2, 3, 5}


# ---------- 設定值 ----------

def test_setting_defaults_to_auto():
    assert cs.get()["soffice_cpu_percent"] == 0


def test_setting_clamped_and_zero_allowed():
    assert cs.save({"soffice_cpu_percent": 0})["soffice_cpu_percent"] == 0
    assert cs.save({"soffice_cpu_percent": -5})["soffice_cpu_percent"] == 0
    assert cs.save({"soffice_cpu_percent": 3})["soffice_cpu_percent"] == 10
    assert cs.save({"soffice_cpu_percent": 500})["soffice_cpu_percent"] == 100
    assert cs.save({"soffice_cpu_percent": "abc"})["soffice_cpu_percent"] == 0


def test_setting_survives_reload():
    cs.save({"soffice_cpu_percent": 50})
    cs.invalidate_cache()
    assert cs.get()["soffice_cpu_percent"] == 50


def test_describe_exposes_what_ui_needs():
    d = cs.describe()["cpu_limit"]
    for key in ("percent", "cpu_count", "effective_cores", "reserved_cores",
                "affinity_supported", "thread_nice_supported", "auto",
                "unlimited", "note"):
        assert key in d, key


def test_macos_reports_unsupported_honestly(monkeypatch):
    """macOS 沒有 affinity API —— UI 要照實說，不能假裝有限制到。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    assert cpu_limit.affinity_supported() is False
    assert cpu_limit.describe()["note"]


# ---------- 實際套用 ----------

@pytest.mark.skipif(not hasattr(os, "sched_setaffinity"),
                    reason="此平台沒有 sched_setaffinity")
def test_apply_to_pid_actually_narrows_the_mask():
    """對自己的行程套用，然後讀回遮罩驗證 —— 不是只驗「函式沒丟例外」。"""
    total = cpu_limit.cpu_count()
    if total < 2:
        pytest.skip("需要至少 2 顆核心")
    original = os.sched_getaffinity(0)
    try:
        cs.save({"soffice_cpu_percent": 0})
        cores = cpu_limit.apply_to_pid(os.getpid())
        assert cores is not None
        assert len(cores) == total - 1
        assert os.sched_getaffinity(0) == set(cores)
    finally:
        os.sched_setaffinity(0, original)


@pytest.mark.skipif(not hasattr(os, "sched_setaffinity"),
                    reason="此平台沒有 sched_setaffinity")
def test_unlimited_setting_does_not_touch_the_mask():
    original = os.sched_getaffinity(0)
    cs.save({"soffice_cpu_percent": 100})
    assert cpu_limit.apply_to_pid(os.getpid()) is None
    assert os.sched_getaffinity(0) == original


def test_apply_to_dead_pid_is_silent():
    """行程可能在我們套用之前就結束了 —— 不能因此讓轉檔流程炸掉。"""
    assert cpu_limit.apply_to_pid(2 ** 22) is None


def test_lower_current_thread_only_affects_this_thread():
    """降的必須是「這一個執行緒」，不能把整個行程（含處理 HTTP 的執行緒）拖下去。"""
    if not cpu_limit.thread_nice_supported():
        assert cpu_limit.lower_current_thread() is False
        return
    import threading
    main_tid = threading.get_native_id()
    before_main = os.getpriority(os.PRIO_PROCESS, main_tid)
    result = {}

    def worker():
        cpu_limit.lower_current_thread()
        result["nice"] = os.getpriority(os.PRIO_PROCESS,
                                        threading.get_native_id())

    t = threading.Thread(target=worker)
    t.start(); t.join()
    assert result["nice"] == cpu_limit.JOB_THREAD_NICE
    assert os.getpriority(os.PRIO_PROCESS, main_tid) == before_main


def test_lower_current_thread_is_idempotent():
    """執行緒池會重用執行緒 —— 第二次呼叫不可以再往下降（會越降越低）。"""
    if not cpu_limit.thread_nice_supported():
        pytest.skip("此平台沒有 per-thread nice")
    import threading
    seen = []

    def worker():
        for _ in range(3):
            cpu_limit.lower_current_thread()
            seen.append(os.getpriority(os.PRIO_PROCESS,
                                       threading.get_native_id()))

    t = threading.Thread(target=worker); t.start(); t.join()
    assert seen == [cpu_limit.JOB_THREAD_NICE] * 3


# ---------- 接線（真的有被呼叫嗎） ----------

def test_office_convert_applies_limit_on_spawn(monkeypatch):
    """`_track` 是所有 soffice 啟動的共同出口 —— 限制必須掛在那裡。

    漏掉就是「設定畫面有、實際沒作用」，這種 bug 從 UI 完全看不出來。
    """
    from app.core import office_convert
    called = []
    monkeypatch.setattr(cpu_limit, "apply_to_pid",
                        lambda pid: called.append(pid))

    class P:
        pid = 4242
    office_convert._track(P())
    assert called == [4242]


def test_job_thread_lowers_priority():
    """作業執行緒進場要降權 —— 這是純 Python 運算不卡網頁的關鍵。"""
    import inspect
    from app.core import job_manager
    src = inspect.getsource(job_manager.JobManager._run)
    assert "lower_current_thread" in src


def test_settings_export_covers_cpu_setting():
    """新設定必須被設定備份收錄（concurrency.json 已在 CATEGORIES 內）。"""
    from app.core import settings_export
    items = [i for cat in settings_export.CATEGORIES
             for i in (cat.get("items") or [])]
    assert "concurrency.json" in items
