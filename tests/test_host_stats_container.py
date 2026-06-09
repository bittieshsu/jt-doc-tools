"""系統狀態 CPU 在容器(LXC/Docker)內要顯示容器自己的用量，不抓宿主機。

實機 / VM：/proc 是自己的 → psutil（含 loadavg）。
LXC / Docker：/proc/stat、/proc/loadavg 沒 namespace 化會讀到宿主機 →
改從 cgroup 算容器 CPU%、不顯示宿主機 loadavg。
"""
from __future__ import annotations

from app.core import host_stats as hs


def test_count_cpuset():
    assert hs._count_cpuset("0-3,5") == 5
    assert hs._count_cpuset("0") == 1
    assert hs._count_cpuset("0-1") == 2
    assert hs._count_cpuset("") == 0
    assert hs._count_cpuset("2,4,6") == 3


def test_container_cpu_percent_from_cgroup_delta(monkeypatch):
    # 模擬：上一筆 usage=1.0s，1 wall-second 後 usage=1.5s（用了 0.5 核秒）
    hs._CG_CPU_PREV["usage_usec"] = 1_000_000
    hs._CG_CPU_PREV["ts"] = 1000.0
    monkeypatch.setattr(hs, "_read_cgroup_cpu_usage_usec", lambda: 1_500_000)
    monkeypatch.setattr(hs, "_cgroup_ncpu", lambda fb: 2)
    monkeypatch.setattr(hs.time, "time", lambda: 1001.0)
    pct = hs._container_cpu_percent(2)
    # 0.5 核 / 2 核 * 100 = 25%
    assert abs(pct - 25.0) < 0.5


def test_container_cpu_percent_first_call_zero(monkeypatch):
    hs._CG_CPU_PREV["usage_usec"] = None
    hs._CG_CPU_PREV["ts"] = None
    monkeypatch.setattr(hs, "_read_cgroup_cpu_usage_usec", lambda: 5_000_000)
    pct = hs._container_cpu_percent(4)
    assert pct == 0.0  # 首次無前一筆 → 0（與 psutil interval=None 一致）


def test_container_cpu_percent_none_when_no_cgroup(monkeypatch):
    monkeypatch.setattr(hs, "_read_cgroup_cpu_usage_usec", lambda: None)
    assert hs._container_cpu_percent(4) is None


def test_get_host_stats_container_path(monkeypatch):
    monkeypatch.setattr(hs, "_is_container", lambda: True)
    monkeypatch.setattr(hs, "_container_cpu_percent", lambda fb: 12.5)
    monkeypatch.setattr(hs, "_cgroup_ncpu", lambda fb: 4)
    s = hs.get_host_stats()
    cpu = s.get("cpu", {})
    assert cpu.get("in_container") is True
    assert cpu.get("percent") == 12.5
    assert cpu.get("count_logical") == 4
    assert cpu.get("loadavg") is None       # 不顯示宿主機 loadavg
    assert cpu.get("source") == "cgroup"


def test_get_host_stats_baremetal_path(monkeypatch):
    # 非容器（實機 / VM）→ psutil，in_container False，loadavg 可有值
    monkeypatch.setattr(hs, "_is_container", lambda: False)
    s = hs.get_host_stats()
    cpu = s.get("cpu", {})
    assert cpu.get("in_container") is False
    assert "percent" in cpu
    assert "loadavg" in cpu  # 鍵存在（值可能是 list 或 None，視平台）
