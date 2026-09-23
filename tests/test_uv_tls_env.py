"""uv 的「用 OS 信任庫」變數：**只設這支 uv 認得的那一個**（v1.16.11）。

企業 TLS 檢查設備會換掉 HTTPS 憑證，uv 內建的根憑證不認 → 裝不了東西。
v1.12.12 起讓 uv 改用 OS 信任庫，新舊兩個變數**都設**（`UV_SYSTEM_CERTS` /
`UV_NATIVE_TLS`），想說 uv 會忽略不認得的那個 —— 但新版 uv **認得舊的，
而且每次都印一行棄用警告**。2026-09-23 在 Mac 實機的 `jtdt update` 輸出裡看到：

    warning: The `UV_NATIVE_TLS` environment variable is deprecated ...

讀起來像升級出了錯（同一天才為了客戶截圖拿掉 `locale.getdefaultlocale` 那段警告）。

判準是**問 uv 自己**（`uv sync --help` 有沒有 `--system-certs`），不寫死版本號。
問不到就退回舊變數 —— 反過來的話舊版 uv 不認得新變數，企業 TLS 環境就裝不了東西。
這個功能從 v1.12.12 起**一條測試都沒有**。
"""
from __future__ import annotations

import importlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

cli = importlib.import_module("app.cli")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.repo_paths import public_root as _public_root  # noqa: E402

PUB = _public_root(ROOT)


class _Out:
    def __init__(self, stdout):
        self.stdout = stdout


def _fake_uv(monkeypatch, help_text=None, raises=None):
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        if raises:
            raise raises
        return _Out(help_text)

    monkeypatch.setattr(cli.subprocess, "run", run)
    return seen


def test_new_uv_gets_only_the_new_variable(monkeypatch):
    _fake_uv(monkeypatch, "      --system-certs\n          Whether to load TLS ...\n")
    env = cli._uv_tls_env("uv", {})
    assert env.get("UV_SYSTEM_CERTS") == "true"
    assert "UV_NATIVE_TLS" not in env, "新版 uv 看到舊變數會印棄用警告"


def test_old_uv_gets_the_old_variable(monkeypatch):
    _fake_uv(monkeypatch, "      --native-tls\n          Whether to load TLS ...\n")
    env = cli._uv_tls_env("uv", {})
    assert env.get("UV_NATIVE_TLS") == "true"
    assert "UV_SYSTEM_CERTS" not in env


def test_when_uv_cannot_be_asked_fall_back_to_the_variable_every_uv_knows(monkeypatch):
    """問不到時選舊的：新版 uv 只是多一行警告；選新的的話舊版 uv 會安靜地不用 OS 信任庫。"""
    _fake_uv(monkeypatch, raises=FileNotFoundError("uv"))
    env = cli._uv_tls_env("uv", {})
    assert env == {"UV_NATIVE_TLS": "true"}


@pytest.mark.parametrize("user", [{"UV_NATIVE_TLS": "false"}, {"UV_SYSTEM_CERTS": "false"}])
def test_a_user_setting_is_left_alone(monkeypatch, user):
    seen = _fake_uv(monkeypatch, "--system-certs")
    env = cli._uv_tls_env("uv", dict(user))
    assert env == user, "使用者自己設了（例如要關閉）就不可以再補"
    assert not seen, "使用者設了就不用問 uv"


def test_update_uses_the_helper():
    import inspect
    src = inspect.getsource(cli.svc_update)
    assert "_uv_tls_env(" in src
    assert not re.search(r'setdefault\("UV_(NATIVE_TLS|SYSTEM_CERTS)"', src), (
        "svc_update 又自己把兩個變數都設了")


@pytest.mark.skipif(not shutil.which("uv"), reason="這台沒有 uv")
def test_the_real_uv_prints_no_deprecation_warning(tmp_path):
    """拿這台真的 uv 跑一次：照 helper 給的環境，不可以出現那行棄用警告。
    反向對照：把兩個都設（原本的做法）時要看得到警告 —— 不然這條測不到任何東西。"""
    uv = shutil.which("uv")
    base = {k: v for k, v in os.environ.items()
            if k not in ("UV_NATIVE_TLS", "UV_SYSTEM_CERTS")}
    base["UV_CACHE_DIR"] = str(tmp_path / "cache")
    cmd = [uv, "pip", "list", "--python", sys.executable]

    both = dict(base, UV_NATIVE_TLS="true", UV_SYSTEM_CERTS="true")
    r_old = subprocess.run(cmd, env=both, capture_output=True, text=True, timeout=60)
    if "deprecated" not in r_old.stderr:
        pytest.skip("這支 uv 還不會對舊變數警告（舊版 uv）")

    env = cli._uv_tls_env(uv, dict(base))
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    assert "deprecated" not in r.stderr, r.stderr[:300]


# ---------------------------------------------------------------- 安裝腳本

def test_install_sh_asks_uv_after_it_is_installed():
    src = (PUB / "install.sh").read_text(encoding="utf-8")
    assert "set_uv_tls_env() {" in src
    # 函式裡面那兩行是「問過 uv 之後」才 export 的；其他地方一行都不可以有
    outside = re.sub(r"^set_uv_tls_env\(\) \{.*?^\}", "", src, flags=re.M | re.S)
    assert not re.search(r"^\s*export UV_(NATIVE_TLS|SYSTEM_CERTS)=", outside, re.M), (
        "又在問 uv 之前就設了變數（新版 uv 看到舊變數會印棄用警告）")
    main = src[src.rindex("    install_uv\n"):]
    assert main.startswith("    install_uv\n    set_uv_tls_env\n"), (
        "set_uv_tls_env 要緊接在 install_uv 之後（uv 裝好才問得到）")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="要 bash")
@pytest.mark.parametrize("help_text,want", [
    ("  --system-certs  Whether to load TLS", "SYSTEM=true NATIVE="),
    ("  --native-tls  Whether to load TLS", "SYSTEM= NATIVE=true"),
])
def test_install_sh_function_really_picks_one(tmp_path, help_text, want):
    src = (PUB / "install.sh").read_text(encoding="utf-8")
    fn = re.search(r"^set_uv_tls_env\(\) \{.*?^\}", src, re.M | re.S).group(0)
    (tmp_path / "bin").mkdir()
    fake = tmp_path / "bin" / "uv"
    fake.write_text(f"#!/bin/sh\necho '{help_text}'\n", encoding="utf-8")
    fake.chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if k not in ("UV_NATIVE_TLS", "UV_SYSTEM_CERTS")}
    out = subprocess.run(
        ["bash", "-c", f'INSTALL_DIR="{tmp_path}"\n{fn}\nset_uv_tls_env\n'
                       'echo "SYSTEM=${UV_SYSTEM_CERTS:-} NATIVE=${UV_NATIVE_TLS:-}"'],
        env=env, capture_output=True, text=True, timeout=30).stdout.strip()
    assert out == want


def test_setup_python_cmd_asks_uv_first():
    """Windows 那一支（安裝時從 main 抓下來跑，不在 exe 裡）。
    cmd.exe 的行為在 .154 實機驗過三種情況；這裡釘住寫法不要退回「兩個都設」。"""
    src = (PUB / "setup-python.cmd").read_text(encoding="utf-8")
    assert not re.search(r"^if not defined UV_NATIVE_TLS set UV_NATIVE_TLS=true",
                         src, re.M | re.I), "又無條件設了舊變數"
    assert 'findstr /c:"--system-certs"' in src
    assert "if not defined UV_NATIVE_TLS if not defined UV_SYSTEM_CERTS (" in src
