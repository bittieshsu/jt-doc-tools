"""CPU SIMD 指令集偵測 + sys-deps PyMuPDF 條目測試。

probe_cpu_simd 給 OCR 引擎頁判斷 EasyOCR(PyTorch) 能否安全執行 —— 缺 AVX2 的
CPU(如 PVE VM 的 x86-64-v2 model)會在辨識時 SIGILL 讓服務崩潰(v1.12.12)。
"""
import builtins
import io
import platform

import app.core.sys_deps as sd


def _run_probe(system, machine, flags=""):
    sd._cpu_simd_cache = None
    o_sys, o_mach, o_open = platform.system, platform.machine, builtins.open
    platform.system = lambda: system
    platform.machine = lambda: machine
    if system == "Linux":
        def fake_open(p, *a, **k):
            if str(p) == "/proc/cpuinfo":
                return io.StringIO(f"processor\t: 0\nflags\t\t: {flags}\n")
            return o_open(p, *a, **k)
        builtins.open = fake_open
    try:
        return sd.probe_cpu_simd()
    finally:
        platform.system, platform.machine, builtins.open = o_sys, o_mach, o_open
        sd._cpu_simd_cache = None


def test_avx2_cpu_is_ok():
    r = _run_probe("Linux", "x86_64", "fpu sse4_2 aes avx avx2 fma bmi2")
    assert r["ok"] is True
    assert r["avx2"] is True
    assert r["missing"] == []


def test_x86_64_v2_missing_avx2():
    # 客戶情境：x86-64-v2-AES（有 SSE4.2 / AES，沒 AVX/AVX2）
    r = _run_probe("Linux", "x86_64", "fpu sse4_1 sse4_2 popcnt aes")
    assert r["ok"] is False
    assert "AVX2" in r["missing"]
    assert "AVX" in r["missing"]


def test_arm_not_subject_to_avx2():
    r = _run_probe("Linux", "aarch64")
    assert r["ok"] is True
    assert "ARM" in r["note"] or "非 x86" in r["note"]


def test_undetermined_is_not_flagged_bad():
    # Linux 但讀不到 flags（空）→ 不誤判為壞
    r = _run_probe("Linux", "x86_64", "")
    assert r["undetermined"] is True
    assert r["ok"] is True


def test_sys_deps_includes_pymupdf():
    keys = [d["key"] for d in sd._DEPS]
    assert "pymupdf" in keys
    pm = next(d for d in sd._DEPS if d["key"] == "pymupdf")
    res = pm["probe"]()
    assert res["installed"] is True
    assert res["ok"] is True
    assert res["version"]  # 有版本字串
