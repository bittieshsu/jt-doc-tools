"""服務的記錄在「非 Unicode 程式語系」是英文的 Windows 上，中文訊息不可以消失（v1.16.56）。

## 由來（Windows 實機，2026-10-06）

那台的顯示語言是中文、但非 Unicode 程式的系統地區是 en-US（ANSI 字碼頁 1252）。
Windows 服務由 WinSW 包著，stdout 是導進檔案的管線，Python 用系統字碼頁（cp1252）
編碼 —— **每一行含中文的記錄一寫就 `UnicodeEncodeError`**，logging 只在 stderr 印
一段 ``--- Logging error ---`` 堆疊，那一行記錄本身就不見了。一次啟動 50 支工具的
「Registered tool：<中文名>」全掉，慢請求、啟動、保留期清理、資料庫備份…凡是中文
的記錄都寫不出來，出事時查不到原因。

字碼頁 950 的機器（中文版 Windows）不會發生，所以之前的 Windows 測試機一直看不到；
企業的英文版 Windows Server 會全中。Linux 那邊 `cli.py` 早就替 systemd 補了
``PYTHONIOENCODING=utf-8``，WinSW 從來沒有對應的設定。

## 判準

用子行程把 stdout / stderr 強制成 cp1252（``PYTHONIOENCODING=cp1252``、關掉 UTF-8 模式），
呼叫 ``setup_logging()`` 之後記一行中文：

* stderr 不可以有 ``Logging error``；
* stdout 的位元組要能以 UTF-8 解碼，而且原句完整出現。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

_SCRIPT = r"""
import logging, sys
sys.path.insert(0, %r)
from app.logging_setup import setup_logging
setup_logging("INFO")
logging.getLogger("t").info("已註冊工具：文件擺正（掃描修正）")
print("列印也要過：會議摘要", flush=True)
"""


def _run(env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("PYTHONUTF8", None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", _SCRIPT % str(ROOT)],
        capture_output=True, env=env, timeout=60)


def test_the_simulated_console_really_is_cp1252():
    """前提：不經過 setup_logging 時，cp1252 真的寫不出中文 —— 不然下面那條什麼都沒驗到。"""
    r = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c",
         "import logging,sys; h=logging.StreamHandler(sys.stdout); "
         "logging.getLogger().addHandler(h); logging.getLogger().warning('中文')"],
        capture_output=True, env={**os.environ, "PYTHONIOENCODING": "cp1252"}, timeout=60)
    assert b"Logging error" in r.stderr, "模擬的 cp1252 輸出沒有重現原本的錯誤，這支檢查失去意義"


def test_chinese_log_lines_survive_a_cp1252_stdout():
    r = _run({"PYTHONIOENCODING": "cp1252"})
    assert b"Logging error" not in r.stderr, r.stderr.decode("utf-8", "replace")[-1500:]
    out = r.stdout.decode("utf-8")
    assert "已註冊工具：文件擺正（掃描修正）" in out, out
    assert "列印也要過：會議摘要" in out, out


def test_utf8_consoles_are_left_alone():
    r = _run({"PYTHONIOENCODING": "utf-8"})
    assert r.returncode == 0 and b"Logging error" not in r.stderr
    assert "已註冊工具" in r.stdout.decode("utf-8")


# ---------------------------------------------------------------- 讀記錄的那一端
def test_reading_the_log_tail_does_not_fall_back_when_the_cut_splits_a_character(tmp_path):
    """從檔尾往回 64 KB 的位置剛好切在中文字中間時，不可以整段退回系統字碼頁變亂碼。"""
    from app import cli
    line = "2026-10-06 15:00:00 [INFO] app: 已註冊工具：文件擺正\n".encode("utf-8")
    body = line * 2000
    # 讓「檔尾往回 65536」那個位置落在一個 3 位元組的中文字中間
    # （切點是從檔尾往回算的，所以補字要補在檔尾才會移動它）
    for pad in range(0, 3):
        data = body + b"x" * pad + b"\n"
        cut = len(data) - 65536
        if cut > 0 and (data[cut] & 0xC0) == 0x80:
            break
    else:
        raise AssertionError("造不出切在字中間的素材")
    log = tmp_path / "jtdt-svc.out.log"
    log.write_bytes(data)
    lines = cli._read_log_lines(log)
    assert lines and all("�" not in x for x in lines), lines[:2]
    assert lines[-2].endswith("已註冊工具：文件擺正")


def test_jtdt_logs_on_windows_reads_utf8(monkeypatch, tmp_path):
    from app import cli
    log = tmp_path / "jtdt-svc.err.log"
    log.write_text("x", encoding="utf-8")
    seen = []
    monkeypatch.setattr(cli, "_is_linux", lambda: False)
    monkeypatch.setattr(cli, "_is_macos", lambda: False)
    monkeypatch.setattr(cli, "_is_windows", lambda: True)
    monkeypatch.setattr(cli, "_service_log_files", lambda: [log])
    monkeypatch.setattr(cli, "_run", lambda cmd: seen.append(cmd) or 0)
    for follow in (False, True):
        cli.svc_logs(follow)
    assert len(seen) == 2
    for cmd in seen:
        assert "-Encoding UTF8" in cmd[-1], cmd
