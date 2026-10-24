"""install.sh 在 Apple Silicon 的 Mac 上要從頭裝到服務起來（issue #57）。

實機（macOS 26、arm64、沒裝 Rosetta、暫時移開 `/usr/local/bin`）依序撞到三件事：

1. 出廠沒有 `/usr/local/bin`（Homebrew 在 `/opt/homebrew`）。舊寫法直接 `> /usr/local/bin/jtdt`，
   `No such file or directory`，`set -e` 讓安裝停在「建立 /usr/local/bin/jtdt 指令」。
2. 健康檢查讀 `$SVC_FILE`，那個變數只有 Linux 會設；`set -u` 下 macOS 以 `unbound variable` 結束。
3. `.app` 的執行檔是 shell 腳本：LaunchServices 當成 Intel 程式要 Rosetta，沒裝時 `open` 回 -10669、
   服務起不來。Info.plist 要寫 `LSArchitecturePriority`，arm64 排第一（實機加上後 `open` 成功）。

判準落在實際執行：把函式抽出來用 bash 跑。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.repo_paths import public_root as _public_root  # noqa: E402

PUB = _public_root(ROOT)

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="要 bash")


def _run_install_cli(cli_link: Path, install_dir: Path) -> subprocess.CompletedProcess:
    src = (PUB / "install.sh").read_text(encoding="utf-8")
    fn = re.search(r"^install_cli\(\) \{.*?^\}", src, re.M | re.S).group(0)
    script = (
        "set -euo pipefail\n"
        'log() { printf "LOG %s\\n" "$*"; }\nok() { printf "OK %s\\n" "$*"; }\n'
        f'CLI_LINK="{cli_link}"\nINSTALL_DIR="{install_dir}"\n'
        f"{fn}\ninstall_cli\n"
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


def test_the_command_is_installed_when_its_folder_does_not_exist_yet(tmp_path):
    link = tmp_path / "usr" / "local" / "bin" / "jtdt"
    assert not link.parent.exists()
    out = _run_install_cli(link, tmp_path / "jt-doc-tools")
    assert out.returncode == 0, out.stderr
    assert link.is_file() and os.access(link, os.X_OK)
    body = link.read_text(encoding="utf-8")
    assert f'{tmp_path / "jt-doc-tools"}/.venv/bin/python" -m app.cli' in body


def test_an_existing_folder_and_command_are_fine(tmp_path):
    """反向對照：資料夾已經在、指令也已經在（重跑安裝 / 升級）照樣覆寫成功。"""
    link = tmp_path / "bin" / "jtdt"
    link.parent.mkdir()
    link.write_text("old", encoding="utf-8")
    out = _run_install_cli(link, tmp_path / "jt")
    assert out.returncode == 0, out.stderr
    assert "app.cli" in link.read_text(encoding="utf-8")


def _fn(name: str) -> str:
    src = (PUB / "install.sh").read_text(encoding="utf-8")
    return re.search(rf"^{name}\(\) \{{.*?^\}}", src, re.M | re.S).group(0)


def test_the_health_check_does_not_trip_over_the_linux_only_service_file():
    """macOS 沒有 SVC_FILE；`set -u` 下不可以因此失敗，要照常去打 /healthz。"""
    script = (
        "set -euo pipefail\n"
        'log() { :; }\nok() { echo "OK $*"; }\nwarn() { echo "WARN $*"; }\n'
        'curl() { return 0; }\nsleep() { :; }\n'
        'PLATFORM=macos\nBIND_PORT=8765\nADVERTISED_HOST=127.0.0.1\n'
        f"{_fn('health_check')}\nhealth_check\n"
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "OK 服務已上線" in out.stdout, "macOS 要真的做健康檢查，不是跳過"


def test_linux_without_the_service_file_still_skips_the_check(tmp_path):
    """反向對照：Linux 沒有 systemd 單元（容器）時照舊跳過。"""
    script = (
        "set -euo pipefail\n"
        'log() { :; }\nok() { echo "OK $*"; }\nwarn() { echo "WARN $*"; }\n'
        f'PLATFORM=linux\nSVC_FILE="{tmp_path}/missing.service"\n'
        f"{_fn('health_check')}\nhealth_check\n"
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "跳過健康檢查" in out.stdout


def test_the_app_asks_launchservices_for_arm64_first():
    src = (PUB / "install.sh").read_text(encoding="utf-8")
    plist = src[src.index('cat > "$APP_DIR/Contents/Info.plist"'):]
    plist = plist[:plist.index("\nEOF\n")]
    m = re.search(r"<key>LSArchitecturePriority</key>\s*<array>\s*<string>([^<]+)</string>", plist)
    assert m and m.group(1) == "arm64", "沒有 Rosetta 的 Apple Silicon 上 open 會回 -10669"
    after = src[src.index('cat > "$APP_DIR/Contents/Info.plist"'):]
    assert "lsregister" in after[:after.index("Launcher script")], "重跑安裝要重新登記，才不會沿用舊的 Info.plist"


def test_tesseract_from_homebrew_is_seen_under_sudo(tmp_path):
    """sudo 的 PATH 沒有 /opt/homebrew/bin。把函式裡的 /opt/homebrew/bin 換成假的 brew 目錄，
    裡面放一支會列出 chi_tra 的 tesseract：要直接判定「已就緒」，不可以再跑 brew install。"""
    brew_bin = tmp_path / "homebrew" / "bin"
    brew_bin.mkdir(parents=True)
    tess = brew_bin / "tesseract"
    tess.write_text('#!/bin/sh\nprintf "List of available languages (2):\\nchi_tra\\neng\\n"\n', encoding="utf-8")
    tess.chmod(0o755)
    # PATH 只放函式要用的幾個指令：開發機自己的 /usr/bin 可能就有 tesseract，會讓這個測試無聲通過。
    tools = tmp_path / "sysbin"
    tools.mkdir()
    for name in ("grep", "tr", "tail", "sh"):
        (tools / name).symlink_to(shutil.which(name))
    fn = _fn("install_tesseract").replace("/opt/homebrew/bin", str(brew_bin))
    script = (
        "set -euo pipefail\n"
        'log() { echo "LOG $*"; }\nok() { echo "OK $*"; }\nwarn() { echo "WARN $*"; }\n'
        f"PATH={tools}\nPLATFORM=macos\nARCH=arm64\n"
        f"{fn}\ninstall_tesseract\n"
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "繁中訓練檔已就緒" in out.stdout, out.stdout
    assert "WARN" not in out.stdout and "安裝 tesseract" not in out.stdout
