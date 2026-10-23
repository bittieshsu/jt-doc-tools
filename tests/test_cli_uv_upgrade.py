"""`jtdt update` 把安裝目錄裡太舊的 uv 換新（2026-10-22：eMudhra / emSign 中間憑證金鑰外洩，
uv 0.13.4 起內建的根憑證拿掉了它，GHSA-hq85-5cpj-mr62）。

驗：
* 夠新就不動；問不到版本也不動（不要因為探測失敗去跑安裝腳本）。
* 太舊先 `uv self update`，換成功就不跑安裝腳本；不成功才跑官方安裝腳本，`UV_INSTALL_DIR` 指到 `<安裝目錄>/bin`。
* 系統 PATH 上的 uv（不在安裝目錄裡）只提醒、不動它。
* 換不成功不中止升級（印出手動指令）。
* 升級流程在 `uv sync` 之前呼叫它。
"""
from __future__ import annotations

import inspect
import subprocess

import pytest

from app import cli


class _FakeUv:
    """假的 subprocess.run：`--version` 回目前版本，`self update` / 安裝腳本照設定把版本換掉。"""

    def __init__(self, version, self_update_to=None, installer_to=None):
        self.version, self.self_update_to, self.installer_to = version, self_update_to, installer_to
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw.get("env") or {}))
        out = ""
        if cmd[-1] == "--version":
            out = f"uv {self.version} (x86_64-unknown-linux-gnu)\n"
        elif cmd[1:3] == ["self", "update"] and self.self_update_to:
            self.version = self.self_update_to
        elif ("install.sh" in " ".join(cmd) or "install.ps1" in " ".join(cmd)) and self.installer_to:
            self.version = self.installer_to
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


@pytest.fixture
def bundled(tmp_path):
    (tmp_path / "bin").mkdir()
    uv = tmp_path / "bin" / "uv"
    uv.write_text("")
    return tmp_path, str(uv)


def _kinds(fake):
    out = []
    for cmd, _env in fake.calls:
        s = " ".join(cmd)
        out.append("version" if cmd[-1] == "--version" else "self" if "self update" in s
                   else "installer" if "install." in s else s)
    return out


def test_a_new_enough_uv_is_left_alone(bundled, monkeypatch):
    root, uv = bundled
    fake = _FakeUv("0.13.5")
    monkeypatch.setattr(cli.subprocess, "run", fake)
    cli._upgrade_uv_if_old(uv, root, {})
    assert _kinds(fake) == ["version"]


def test_an_unknown_version_is_left_alone(bundled, monkeypatch):
    root, uv = bundled
    fake = _FakeUv("garbage")
    monkeypatch.setattr(cli.subprocess, "run", fake)
    cli._upgrade_uv_if_old(uv, root, {})
    assert _kinds(fake) == ["version"], "問不到版本就不要去跑安裝腳本"


def test_an_old_uv_is_self_updated_first(bundled, monkeypatch, capsys):
    root, uv = bundled
    fake = _FakeUv("0.11.21", self_update_to="0.13.5")
    monkeypatch.setattr(cli.subprocess, "run", fake)
    cli._upgrade_uv_if_old(uv, root, {})
    assert _kinds(fake) == ["version", "self", "version"], "self update 成功就不跑安裝腳本"
    out = capsys.readouterr().out
    assert "0.11.21" in out and "uv is now 0.13.5" in out and "emSign" in out
    assert out.isascii(), "CLI 訊息一律英文 ASCII"


def test_the_installer_is_the_fallback_and_targets_the_install_dir(bundled, monkeypatch):
    root, uv = bundled
    fake = _FakeUv("0.11.21", installer_to="0.13.5")
    monkeypatch.setattr(cli.subprocess, "run", fake)
    monkeypatch.setattr(cli, "_is_windows", lambda: False)
    cli._upgrade_uv_if_old(uv, root, {"PATH": "/usr/bin"})
    assert _kinds(fake) == ["version", "self", "version", "installer", "version"]
    cmd, env = fake.calls[3]
    assert "https://astral.sh/uv/install.sh" in " ".join(cmd)
    assert env["UV_INSTALL_DIR"] == str(root / "bin") and env["UV_NO_MODIFY_PATH"] == "1"
    assert env["PATH"] == "/usr/bin", "原本的環境變數保留（企業代理等）"


def test_windows_uses_the_powershell_installer(bundled, monkeypatch):
    root, uv = bundled
    fake = _FakeUv("0.11.21", installer_to="0.13.5")
    monkeypatch.setattr(cli.subprocess, "run", fake)
    monkeypatch.setattr(cli, "_is_windows", lambda: True)
    cli._upgrade_uv_if_old(uv, root, {})
    cmd, env = fake.calls[3]
    assert cmd[0] == "powershell" and "https://astral.sh/uv/install.ps1" in cmd[-1]
    assert env["UV_INSTALL_DIR"] == str(root / "bin")


def test_failure_does_not_stop_the_update(bundled, monkeypatch, capsys):
    root, uv = bundled
    fake = _FakeUv("0.11.21")
    monkeypatch.setattr(cli.subprocess, "run", fake)
    cli._upgrade_uv_if_old(uv, root, {})     # 不丟例外
    err = capsys.readouterr().err
    assert "Could not update uv" in err and "self update" in err


def test_a_uv_outside_the_install_dir_is_not_touched(tmp_path, monkeypatch, capsys):
    (tmp_path / "bin").mkdir()
    other = tmp_path / "elsewhere" / "uv"
    other.parent.mkdir()
    other.write_text("")
    fake = _FakeUv("0.11.21", self_update_to="0.13.5")
    monkeypatch.setattr(cli.subprocess, "run", fake)
    cli._upgrade_uv_if_old(str(other), tmp_path, {})
    assert _kinds(fake) == ["version"], "系統的 uv 不是我們裝的，只提醒"
    assert "older than 0.13.4" in capsys.readouterr().out


def test_update_checks_uv_before_syncing():
    src = inspect.getsource(cli.svc_update)
    assert "_upgrade_uv_if_old(uv, root" in src
    assert src.index("_upgrade_uv_if_old(uv, root") < src.index('"sync"'), "要在 uv sync 之前換"
    assert cli.MIN_SAFE_UV == (0, 13, 4)


def test_the_lockfile_stays_readable_by_the_old_uv_customers_still_have():
    """開發機的 uv 0.13.x 重產 uv.lock 會把格式版本改成 `revision = 5`；客戶第一次 `jtdt update` 跑的是
    **他機器上的舊 uv**（例如 0.11.21），讀不懂的話同步相依失敗、整個升級被還原，連換新 uv 的那一步都到不了。
    （2026-10-23 實測：`revision = 3` 的內容一字不差，uv 0.11.21 `uv lock --check` 通過。）"""
    from pathlib import Path
    head = (Path(__file__).resolve().parents[1] / "uv.lock").read_text(encoding="utf-8").splitlines()[:3]
    rev = next(int(l.split("=")[1]) for l in head if l.startswith("revision"))
    assert rev <= 3, f"uv.lock 的 revision 是 {rev}：改回 3（內容不變），舊 uv 才讀得懂"
