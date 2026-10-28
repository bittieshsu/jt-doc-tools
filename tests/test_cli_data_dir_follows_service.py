"""`jtdt` 指令找資料目錄時，跟著服務實際的設定走。

原本 `_data_dir()` 只看環境變數 `JTDT_DATA_DIR`，沒設就用平台預設值。管理員把資料目錄搬到別顆磁碟
（Linux 用 systemd 覆寫設定、Windows 改 `jtdt-svc.xml`、macOS 改 launcher）之後，
`sudo jtdt reset-password` / `db-check` / `db-restore` / `update` 都還是去動**舊的預設目錄**：
指令顯示成功，服務讀的資料卻完全沒變（2026-10-28 寫搬移流程時查到）。

另外 Windows 的 `jtdt bind` 重寫 `jtdt-svc.xml` 時只保留 host / port，資料目錄被蓋回預設值。
"""
from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest

from app import cli


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("JTDT_DATA_DIR", raising=False)
    monkeypatch.setattr(cli, "_SERVICE_DATA_DIR_CACHE", [])
    for f in ("_is_linux", "_is_windows", "_is_macos"):
        monkeypatch.setattr(cli, f, lambda: False)


def _linux(monkeypatch, stdout: str):
    monkeypatch.setattr(cli, "_is_linux", lambda: True)
    monkeypatch.setattr(cli.shutil, "which", lambda n: "/usr/bin/systemctl")
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    monkeypatch.setattr(cli.subprocess, "run", run)
    return calls


def test_linux_reads_the_service_environment(monkeypatch):
    calls = _linux(monkeypatch, 'JTDT_DATA_DIR=/var/lib/jt-doc-tools/data JTDT_HOST=0.0.0.0 '
                                'JTDT_DATA_DIR="/mnt/big disk/jtdt" LANG=C.UTF-8\n')
    assert cli._data_dir() == Path("/mnt/big disk/jtdt"), "覆寫設定（後面那個）優先，路徑可以有空白"
    assert calls[0][:3] == ["systemctl", "show", cli.SERVICE_NAME]
    cli._data_dir()
    assert len(calls) == 1, "一個行程只問一次"


def test_the_environment_variable_still_wins(monkeypatch):
    _linux(monkeypatch, "JTDT_DATA_DIR=/mnt/svc\n")
    monkeypatch.setenv("JTDT_DATA_DIR", "/from/env")
    assert cli._data_dir() == Path("/from/env")


def test_no_service_or_a_failure_falls_back_to_the_default(monkeypatch):
    _linux(monkeypatch, "")
    assert cli._data_dir() == Path("/var/lib/jt-doc-tools/data")
    monkeypatch.setattr(cli, "_SERVICE_DATA_DIR_CACHE", [])

    def boom(*a, **k):
        raise OSError("no systemd")
    monkeypatch.setattr(cli.subprocess, "run", boom)
    assert cli._data_dir() == Path("/var/lib/jt-doc-tools/data")


def test_windows_reads_what_write_winsw_xml_wrote(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_install_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_log_dir_windows", lambda: tmp_path / "logs")
    (tmp_path / "bin").mkdir()
    monkeypatch.setattr(cli, "_is_windows", lambda: True)
    target = Path("D:/jtdt data & more")
    cli._write_winsw_xml(host="127.0.0.1", port=8765, data_dir=target)
    assert (tmp_path / "bin" / "jtdt-svc.xml").exists()
    assert cli._service_data_dir() == target


def test_macos_reads_the_launcher(monkeypatch, tmp_path):
    app = tmp_path / "Jason Tools.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "launcher").write_text(
        '#!/bin/sh\nINSTALL_DIR=/usr/local/jt-doc-tools\nDATA_DIR="/Volumes/Data/jtdt"\nLOG_DIR="/x"\n',
        encoding="utf-8")
    monkeypatch.setattr(cli, "MACOS_APP_PATH", str(app))
    monkeypatch.setattr(cli, "_is_macos", lambda: True)
    assert cli._data_dir() == Path("/Volumes/Data/jtdt")


def test_windows_bind_keeps_the_data_dir():
    src = inspect.getsource(cli)
    i = src.index("final_host = new_host if new_host is not None else cur_host")
    block = src[i - 800:i + 400]
    assert 'name="JTDT_DATA_DIR"' in block and "data_dir=cur_data" in block, \
        "jtdt bind 重寫 jtdt-svc.xml 時要把原本的資料目錄帶回去"


def test_db_check_says_which_data_dir_it_checked():
    """搬過資料目錄的人要看得出 `jtdt db-check` 檢查的是哪一份（OPS.md 叫他們這樣確認）。"""
    src = inspect.getsource(cli.svc_db_check)
    assert "Data dir: %s" in src and "settings" in src
