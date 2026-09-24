"""VC++ 執行階段被別的安裝程式換成舊版時，要看得出來、而且修得回來。

Win10 實機踩到：OxOffice 11.0.5 的 MSI 內含 14.29 的執行階段，安裝模式是
`REINSTALLMODE=dmus`（版本「不同」就覆蓋，連舊版也蓋上去）。裝完之後：

* System32 的 `msvcp140.dll` / `vcruntime140.dll` / `vcruntime140_1.dll` 變成 14.29
* 登錄檔卻還寫著 14.44 —— 原本的檢查只看登錄檔，於是判定「已是最新」
* PyTorch 的 `c10.dll` 初始化失敗（WinError 1114），EasyOCR 整個不能用，OCR 一律退回 Tesseract
* 已經在跑的服務不受影響（DLL 早就載入了），**下一次重啟才壞**

而且 `vc_redist /install` 在「同版本已登記」時回 0 卻什麼都不做 —— 要用 `/repair`
（實機：回 3010、檔案回到 14.44、新開的行程不必重開機就載得起來）。

三條路都要做：圖形安裝程式內嵌的 `install_core.ps1`、一行安裝的 `install.ps1`、`jtdt update`。
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app import cli
from app.core import sys_deps
from tools.repo_paths import public_root

ROOT = pathlib.Path(__file__).resolve().parent.parent
PUB = public_root(ROOT)
_SCRIPTS = ["install.ps1", "packaging/windows/install_core.ps1"]

NEW = (14, 44, 35211, 0)
OLD = (14, 29, 30157, 0)


# ------------------------------------------------------------------ jtdt update（Python）
@pytest.fixture
def fake_windows(monkeypatch):
    state = {"files": OLD, "reg": ("v14.44.35211.00", (14, 44)), "calls": [],
             "fixes": {"msi": True, "/repair": True, "/install": False}, "downloads": 0}
    monkeypatch.setattr(cli, "_is_windows", lambda: True)
    monkeypatch.setattr(cli, "_vc_redist_registry_version", lambda: state["reg"])
    monkeypatch.setattr(sys_deps, "vc_runtime_file_version", lambda: state["files"])

    def _fetch(url, dst):
        state["downloads"] += 1
    monkeypatch.setattr(cli._safe_fetch, "urlretrieve", _fetch)

    def _msi():
        state["calls"].append("msi")
        if state["fixes"]["msi"]:
            state["files"] = NEW
        return 2
    monkeypatch.setattr(cli, "_repair_vc_runtime_msi", _msi)

    import subprocess

    def _call(args, *a, **kw):
        action = args[1]
        state["calls"].append(action)
        if state["fixes"].get(action):
            state["files"] = NEW
        return 3010 if action == "/repair" else 0
    monkeypatch.setattr(subprocess, "call", _call)
    return state


def test_current_registry_and_current_files_do_nothing(fake_windows, capsys):
    fake_windows["files"] = NEW
    cli._ensure_vc_redist_windows()
    assert fake_windows["calls"] == [] and fake_windows["downloads"] == 0
    assert "already current" in capsys.readouterr().out


def test_downgraded_files_are_repaired_through_the_msi_first(fake_windows, capsys):
    """登錄檔寫 14.44、檔案卻是 14.29 → 直接修已安裝的 MSI，不必下載 vc_redist。"""
    cli._ensure_vc_redist_windows()
    assert fake_windows["calls"] == ["msi"], fake_windows["calls"]
    assert fake_windows["downloads"] == 0
    out = capsys.readouterr()
    assert "repairing" in out.out and "ready" in out.out and "WARNING" not in out.err


def test_when_the_msi_repair_is_not_enough_vc_redist_repairs(fake_windows):
    fake_windows["fixes"]["msi"] = False
    cli._ensure_vc_redist_windows()
    assert fake_windows["calls"][:2] == ["msi", "/repair"], (
        "已登記同版本時 vc_redist 要用 /repair（/install 會回 0 卻什麼都不做）")


def test_burn_refusing_is_followed_by_another_msi_repair(fake_windows):
    """Burn 在同一次開機回過 3010 後不肯再動 —— 之後還要再直接修一次 MSI。"""
    fake_windows["fixes"].update({"msi": False, "/repair": False})
    cli._ensure_vc_redist_windows()
    assert fake_windows["calls"] == ["msi", "/repair", "msi"]


def test_install_that_leaves_old_files_is_followed_by_an_msi_repair(fake_windows):
    fake_windows["reg"] = ("", ())
    cli._ensure_vc_redist_windows()
    assert fake_windows["calls"] == ["/install", "msi"]


def test_a_clean_install_does_not_repair_for_nothing(fake_windows):
    fake_windows["reg"] = ("", ())
    fake_windows["fixes"]["/install"] = True
    cli._ensure_vc_redist_windows()
    assert fake_windows["calls"] == ["/install"]


def test_a_repair_that_does_not_help_is_reported(fake_windows, capsys):
    fake_windows["fixes"].update({"msi": False, "/repair": False})
    cli._ensure_vc_redist_windows()
    err = capsys.readouterr().err
    assert "WARNING" in err and "14.29" in err, "修不好要講出來，不可以印 OK"


def test_a_pending_restart_is_named_as_the_way_out(fake_windows, capsys):
    """vc_redist 回 3010 而檔案還是舊的（被占用）：要講出下一步。"""
    fake_windows["fixes"].update({"msi": False, "/repair": False})
    cli._ensure_vc_redist_windows()
    err = capsys.readouterr().err
    assert "restart Windows" in err and "jtdt update" in err


# ------------------------------------------------------------------ 相依套件頁
def test_the_dependency_page_flags_a_downgraded_runtime(monkeypatch):
    monkeypatch.setattr(sys_deps, "_probe_python_pkg",
                        lambda *a, **k: {"installed": True, "version": "1.7.2", "extra": "", "ok": True})
    monkeypatch.setattr(sys_deps, "vc_runtime_file_version", lambda: OLD)
    r = sys_deps._probe_easyocr()
    assert r["ok"] is False and "14.29" in r["extra"] and "jtdt update" in r["extra"]

    monkeypatch.setattr(sys_deps, "vc_runtime_file_version", lambda: NEW)
    assert sys_deps._probe_easyocr()["ok"] is True

    monkeypatch.setattr(sys_deps, "vc_runtime_file_version", lambda: None)   # 非 Windows
    assert sys_deps._probe_easyocr()["ok"] is True


def test_the_easyocr_entry_uses_the_runtime_aware_probe():
    entry = next(d for d in sys_deps._DEPS if d.get("key") == "easyocr")
    assert entry["probe"] is sys_deps._probe_easyocr, "EasyOCR 那一列要走會檢查執行階段的 probe"


# ------------------------------------------------------------------ 兩支安裝腳本（PowerShell）
def _strip_comments(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def _function(rel: str, name: str) -> str:
    text = _strip_comments((PUB / rel).read_text(encoding="utf-8-sig"))
    m = re.search(rf"^function {re.escape(name)}\b.*?^}}", text, re.M | re.S)
    assert m, f"{rel}：找不到 function {name}"
    return m.group(0)


@pytest.mark.parametrize("rel", _SCRIPTS)
def test_the_file_version_reader_looks_at_the_real_dlls(rel):
    body = _function(rel, "Get-VCRuntimeFileVersion")
    for dll in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        assert dll in body, f"{rel}：沒有檢查 {dll}"
    assert "ProductVersion" in body
    assert "Sysnative" in body, f"{rel}：32 位元的 PowerShell 看到的 System32 是 SysWOW64"


@pytest.mark.parametrize("rel", _SCRIPTS)
def test_already_current_needs_both_registry_and_files(rel):
    body = _function(rel, "Ensure-VCRedist")
    assert "Get-VCRuntimeFileVersion" in body, f"{rel}：只看登錄檔 —— 檔案被換成舊版時會判成「已是最新」"
    m = re.search(r"if \(([^\n]*)\) \{ Ok \"Visual C\+\+ Redistributable already current", body)
    assert m and "$regOk" in m.group(1) and "$files" in m.group(1), (
        f"{rel}：「已是最新」要同時看登錄檔與 System32 的檔案版本")


@pytest.mark.parametrize("rel", _SCRIPTS)
def test_a_registered_runtime_is_repaired_not_reinstalled(rel):
    body = _function(rel, "Ensure-VCRedist")
    assert re.search(r"if \(\$regOk\) \{[^}]*Repair-VCRuntimeMsi", body, re.S), (
        f"{rel}：登錄檔 OK 但檔案舊時，先直接修已安裝的 MSI（不必下載、不受 Burn 限制）")
    assert re.search(r"\$action = if \(\$regOk\) \{ '/repair' \} else \{ '/install' \}", body), (
        f"{rel}：已登記同版本時 vc_redist 要用 /repair（/install 會回 0 卻什麼都不做）")
    after_vc = body[body.index("$action = if"):]
    assert "Repair-VCRuntimeMsi" in after_vc, (
        f"{rel}：vc_redist 之後檔案還是舊的要再直接修 MSI（Burn 在同一次開機回過 3010 後不肯再動）")
    assert re.search(r"\$after -ge \$min", body), f"{rel}：成功與否要看修完之後的檔案版本，不是只看離開碼"


@pytest.mark.parametrize("rel", _SCRIPTS)
def test_the_msi_repair_targets_the_x64_runtime_packages(rel):
    body = _function(rel, "Repair-VCRuntimeMsi")
    assert "/fomus" in body and "msiexec" in body
    assert re.search(r"X64 \(Minimum\|Additional\) Runtime", body), f"{rel}：要對準 X64 的 Minimum / Additional Runtime"
    assert "WOW6432Node" in body and "Uninstall" in body


@pytest.mark.parametrize("rel", _SCRIPTS)
def test_the_runtime_check_runs_after_office_is_installed(rel):
    """Office 的 MSI 會把執行階段換掉 —— 檢查要排在它後面，不然剛修好又被蓋掉。"""
    text = _strip_comments((PUB / rel).read_text(encoding="utf-8-sig"))
    calls = [m.start() for m in re.finditer(r"^\s*(if \(\$\w+\)\s*\{\s*)?Ensure-Office\b", text, re.M)]
    vc = [m.start() for m in re.finditer(r"^\s*(if \(\$\w+\)\s*\{\s*)?Ensure-VCRedist\b", text, re.M)]
    assert calls and vc, f"{rel}：找不到 Ensure-Office / Ensure-VCRedist 的呼叫"
    assert max(calls) < min(vc), f"{rel}：Ensure-VCRedist 要在 Ensure-Office 之後"
