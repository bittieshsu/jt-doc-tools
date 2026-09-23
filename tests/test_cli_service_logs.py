"""`jtdt update` 健康檢查失敗時，要讀得到服務**真正的**記錄檔（v1.16.11，客戶回報）。

客戶的 Windows 更新結尾印：

    Health check failed. ...
    Service state: RUNNING
    Last log lines:
      (no log at C:\\ProgramData\\jt-doc-tools\\Data\\logs\\jt-doc-tools.log)

那個目錄**從來不存在** —— 服務由 WinSW 包著，記錄寫在 WinSW 的 `<logpath>`
（`%ProgramData%\\jt-doc-tools\\Logs\\jtdt-svc.err.log`）。最需要的那幾行
（為什麼沒回應）就這樣拿不到。

**其他平台也查了**：
* macOS：launcher 把 stdout 導到 `jt-doc-tools.log`、**stderr 導到 `.err`**，
  原本只讀 `.log`。服務自己的記錄在 `.log`，但 uvicorn 的啟動訊息與沒被攔下的
  例外堆疊在 `.err` —— 起不來時最需要的正好是沒讀的那一個。同一類錯。
  （2026-09-23 在 Mac 實機上看過兩個檔的內容才確定是這樣分的。）
* Linux：走 `journalctl -u`，stdout / stderr 都在裡面 —— 對的，沒動。

判準一律落在**讀出來的內容**上：把一行只在正確那個檔裡才有的字寫進去，
看 `_print_log_tail` 印不印得出來。只驗「路徑字串長得像不像」的話，
換一個同樣錯的路徑也會過。
"""
from __future__ import annotations

import ast
import importlib
import re
import time
import warnings
from pathlib import Path

import pytest

cli = importlib.import_module("app.cli")
ROOT = Path(__file__).resolve().parents[1]

import sys as _sys
_sys.path.insert(0, str(ROOT))
from tools.repo_paths import public_root as _public_root  # noqa: E402

# 開發樹有 `github/` 那一層，clone 下來沒有 —— 不可以寫死
PUB = _public_root(ROOT)


def _fake_platform(monkeypatch, *, linux=False, macos=False, windows=False):
    monkeypatch.setattr(cli, "_is_linux", lambda: linux)
    monkeypatch.setattr(cli, "_is_macos", lambda: macos)
    monkeypatch.setattr(cli, "_is_windows", lambda: windows)


# ---------------------------------------------------------------- Windows

@pytest.fixture
def win_logs(monkeypatch, tmp_path):
    _fake_platform(monkeypatch, windows=True)
    logdir = tmp_path / "ProgramData" / "jt-doc-tools" / "Logs"
    logdir.mkdir(parents=True)
    data = tmp_path / "ProgramData" / "jt-doc-tools" / "Data"
    data.mkdir()
    monkeypatch.setattr(cli, "_log_dir_windows", lambda: logdir)
    monkeypatch.setattr(cli, "_data_dir", lambda: data)
    return logdir, data


def test_windows_tail_reads_the_winsw_error_log(win_logs, capsys):
    logdir, _ = win_logs
    (logdir / "jtdt-svc.err.log").write_text(
        "INFO:     Started server process [1]\n"
        "OSError: [WinError 10048] only one usage of each socket address\n",
        encoding="utf-8")
    cli._print_log_tail(20)
    err = capsys.readouterr().err
    assert "WinError 10048" in err, f"讀不到 WinSW 的錯誤記錄：\n{err}"
    assert "no service log" not in err


def test_windows_does_not_look_in_the_data_directory(win_logs, capsys):
    """原本的路徑：資料目錄底下的 logs —— 放一份只存在那裡的內容，不可以被讀到。"""
    _, data = win_logs
    (data / "logs").mkdir()
    (data / "logs" / "jt-doc-tools.log").write_text("WRONG-PLACE\n", encoding="utf-8")
    cli._print_log_tail(20)
    err = capsys.readouterr().err
    assert "WRONG-PLACE" not in err
    assert "jtdt-svc.err.log" in err, "找不到時要講出它找了哪裡（Logs 目錄）"


def test_windows_log_dir_is_the_one_the_service_is_configured_with(monkeypatch, tmp_path):
    """讀的目錄要跟 WinSW XML 裡寫的 `<logpath>` 是**同一個** ——
    判準從產生 XML 的那支函式取，不拿自己的常數來驗自己。"""
    _fake_platform(monkeypatch, windows=True)
    monkeypatch.setattr(cli, "_install_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_data_dir", lambda: tmp_path / "data")
    (tmp_path / "bin").mkdir()
    cli._write_winsw_xml()
    xml = (tmp_path / "bin" / "jtdt-svc.xml").read_text(encoding="utf-8")
    logpath = re.search(r"<logpath>([^<]+)</logpath>", xml).group(1)
    for f in cli._service_log_files():
        assert str(f.parent) == logpath, f"讀 {f.parent}，服務寫到 {logpath}"


def test_windows_log_names_follow_the_wrapper_exe_name(monkeypatch):
    """WinSW 用包裝程式的檔名命名記錄檔（`jtdt-svc.exe` → `jtdt-svc.err.log`）。
    兩支安裝程式與 cli 都要把它叫成同一個名字。"""
    _fake_platform(monkeypatch, windows=True)
    stem = cli._winsw_exe_path().stem
    names = [f.name for f in cli._service_log_files()]
    assert names[0] == f"{stem}.err.log", "錯誤記錄要排第一個（健康檢查最需要它）"
    for ps1 in ("install.ps1", "packaging/windows/install_core.ps1"):
        src = (PUB / ps1).read_text(encoding="utf-8-sig")
        assert f"'{stem}.exe'" in src, f"{ps1} 的包裝程式不叫 {stem}.exe"
        assert re.search(r"\$LogDir\s*=.*'jt-doc-tools'\)\s*'Logs'", src), (
            f"{ps1} 的記錄目錄不是 %ProgramData%\\jt-doc-tools\\Logs")


def test_windows_log_in_the_system_codepage_is_readable(win_logs, capsys, monkeypatch):
    """服務在繁中 Windows 上用 cp950 寫記錄 —— 用 UTF-8 硬讀會整行亂碼。"""
    import locale
    logdir, _ = win_logs
    monkeypatch.setattr(locale, "getpreferredencoding", lambda do_setlocale=True: "cp950")
    (logdir / "jtdt-svc.err.log").write_bytes("備份略過：磁碟空間不足\n".encode("cp950"))
    cli._print_log_tail(5)
    assert "磁碟空間不足" in capsys.readouterr().err


def test_a_huge_log_only_reads_the_tail(win_logs, capsys):
    logdir, _ = win_logs
    body = "".join(f"line {i}\n" for i in range(200_000))    # 約 2 MB
    (logdir / "jtdt-svc.err.log").write_text(body + "THE-LAST-LINE\n", encoding="utf-8")
    cli._print_log_tail(3)
    err = capsys.readouterr().err
    assert "THE-LAST-LINE" in err and "line 0\n" not in err


# ---------------------------------------------------------------- macOS

def test_macos_tail_reads_stderr_first(monkeypatch, tmp_path, capsys):
    _fake_platform(monkeypatch, macos=True)
    logs = tmp_path / "Library" / "Logs"
    logs.mkdir(parents=True)
    monkeypatch.setattr(cli, "_real_home", lambda: tmp_path)
    (logs / "jt-doc-tools.log").write_text(
        "2026-09-23 [WARNING] app.core.saml: signature check failed\n", encoding="utf-8")
    (logs / "jt-doc-tools.err").write_text("Traceback: ModuleNotFoundError: xyz\n",
                                           encoding="utf-8")
    cli._print_log_tail(20)
    err = capsys.readouterr().err
    assert "ModuleNotFoundError" in err, "macOS 的錯誤在 .err（launcher 把 stderr 導到那裡）"
    assert err.index("jt-doc-tools.err") < err.index("jt-doc-tools.log"), "錯誤要先印"
    # 服務自己的記錄（logging_setup 寫 stdout）在 .log —— 那一份也要印
    assert "signature check failed" in err, "只印 .err 的話，服務自己寫的警告看不到"


def test_macos_names_match_the_launcher_redirects():
    src = (PUB / "install.sh").read_text(encoding="utf-8")
    assert '>> "\\$LOG_DIR/jt-doc-tools.log" 2>> "\\$LOG_DIR/jt-doc-tools.err"' in src, (
        "launcher 改了導向的檔名 —— cli 讀記錄檔那邊要跟著改")


def test_linux_still_uses_the_journal(monkeypatch):
    _fake_platform(monkeypatch, linux=True)
    seen = []
    monkeypatch.setattr(cli, "_run", lambda cmd: seen.append(cmd) or 0)
    cli._print_log_tail(20)
    assert seen and seen[0][:3] == ["journalctl", "-u", cli.SERVICE_NAME]


# ---------------------------------------------------------------- 等服務起來

def _fake_clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    return now


class _Ok:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_a_service_that_takes_40_seconds_to_start_is_not_a_failure(monkeypatch, capsys):
    """升級剛換過相依，第一次啟動要重新編譯 .pyc、防毒還會逐檔掃 ——
    原本只等約 15 秒，服務正在起來也被判成失敗。"""
    now = _fake_clock(monkeypatch)

    def urlopen(url, timeout):
        if now[0] < 40:
            raise ConnectionRefusedError
        return _Ok()

    monkeypatch.setattr(cli._safe_fetch, "urlopen_direct", urlopen)
    assert cli._wait_healthy(["http://127.0.0.1:8765/healthz"]) is True
    assert "still waiting" in capsys.readouterr().out, "等很久時要講還在等（不然看起來像卡住）"


def test_a_service_that_never_answers_still_fails(monkeypatch):
    """反向對照：等久一點不等於永遠等、也不等於一律成功。"""
    now = _fake_clock(monkeypatch)

    def urlopen(url, timeout):
        raise ConnectionRefusedError

    monkeypatch.setattr(cli._safe_fetch, "urlopen_direct", urlopen)
    assert cli._wait_healthy(["http://127.0.0.1:8765/healthz"]) is False
    assert now[0] <= cli.HEALTH_WAIT_S + 2


def test_update_uses_the_wait_helper():
    import inspect
    assert "_wait_healthy(" in inspect.getsource(cli.svc_update)
    assert cli.HEALTH_WAIT_S >= 60


# ---------------------------------------------------------------- 棄用警告

def test_no_deprecated_getdefaultlocale_call():
    """客戶截圖裡那一段 DeprecationWarning —— 讀起來像升級出了錯。"""
    tree = ast.parse((ROOT / "app" / "cli.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "getdefaultlocale"]
    assert not calls, "又呼叫了 locale.getdefaultlocale()"


def test_troubleshoot_url_raises_no_warning(monkeypatch):
    for k in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(k, raising=False)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        cli._troubleshoot_url()


@pytest.mark.parametrize("name,zh", [
    ("zh_TW", True), ("Chinese (Traditional)_Taiwan", True), ("en_US", False), ("", False),
])
def test_windows_ui_language_picks_the_right_page(monkeypatch, name, zh):
    for k in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cli, "_os_ui_language", lambda: name)
    url = cli._troubleshoot_url()
    assert (url == cli.TROUBLESHOOT_URL_ZH) is zh, url
