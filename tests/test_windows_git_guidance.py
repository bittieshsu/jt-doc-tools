"""Windows 缺 git 時的指引不可以只講 winget。

## 由來（2026-08-23）

使用者問「winget 在 Windows Server 是不是沒有內建」——是。App Installer
（winget 的載體）**不在 Windows Server 2019 / 2022**，Server 2025 才開始
內建，Server Core 一律沒有。

而我們兩處指引都只給 winget：

* `install.ps1`：winget 不在就放棄裝 git，只印一行手動網址
* `jtdt update`：缺 git 時印「winget install --id Git.Git」

第二個是**循環死結** —— 叫使用者用 winget 裝 git，但那台機器正好也沒有
winget（沒 winget 的機器本來就最可能沒 git）。後果不小：沒 git 就走
tarball 模式，`jtdt update` 從此不能用（本專案已經為 Linux 修過同一件事，
見 `feedback_tarball_install_must_be_updatable`），客戶會卡在舊版。

## 判準

1. `install.ps1` 有直接下載 git 的退路，不是只依賴 winget
2. `jtdt update` 缺 git 的訊息要先給直接下載網址，winget 只是備選
"""
from __future__ import annotations

import pathlib
import re

import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
from tools.repo_paths import public_root as _public_root

ROOT = pathlib.Path(__file__).resolve().parent.parent
PS1 = _public_root(ROOT) / "install.ps1"
CLI = ROOT / "app" / "cli.py"


def _strip_comments(src: str) -> str:
    """去掉行註解再掃。

    **說明裡本來就會引用「不可以用的寫法」當反例** —— 連註解一起掃會
    誤報（本專案記過的雷，2026-08-23 又踩一次：我在註解裡寫「不可用
    Net.WebClient」，掃描就判我用了 Net.WebClient）。
    """
    return re.sub(r"(?m)^\s*#.*$", "", src)


def test_install_ps1_has_direct_git_fallback():
    src = PS1.read_text(encoding="utf-8-sig")
    assert "function Install-GitDirect" in src, (
        "install.ps1 沒有不依賴 winget 的 git 安裝退路 —— "
        "Windows Server 沒有內建 winget，這條路會直接放棄")
    assert "git-for-windows" in src, "應從 Git for Windows 官方 release 下載"


def test_install_ps1_tries_direct_when_winget_missing():
    """winget 不在的分支要**呼叫**退路，不是只印訊息。"""
    src = PS1.read_text(encoding="utf-8-sig")
    i = src.index("winget not available (typical on Windows Server)")
    # 該分支之後、return 之前要有 Install-GitDirect
    seg = src[i:i + 400]
    assert "Install-GitDirect" in seg, "winget 缺席的分支沒有走直接下載"


def test_install_ps1_download_has_timeout():
    """一律 Invoke-WebRequest + -TimeoutSec（Net.WebClient 無預設 timeout）。"""
    src = _strip_comments(PS1.read_text(encoding="utf-8-sig"))
    assert "Net.WebClient" not in src, (
        "不可用 Net.WebClient —— 沒有預設 timeout，網路不通會默默卡數分鐘")
    i = src.index("function Install-GitDirect")
    seg = src[i:i + 2000]
    assert "-TimeoutSec" in seg


def test_update_guidance_is_not_winget_only():
    """`jtdt update` 缺 git 的指引要先給直接下載網址。"""
    src = _strip_comments(CLI.read_text(encoding="utf-8"))
    i = src.index("is not a git repo and git is not installed")
    seg = src[i:i + 1400]
    assert "git-scm.com/download/win" in seg, (
        "Windows 缺 git 的指引沒有直接下載網址 —— 只講 winget 是循環死結"
        "（沒 winget 的機器正好最可能沒 git）")
    # winget 可以留著當備選，但不可以是唯一選項
    assert seg.index("git-scm.com/download/win") < seg.index("winget"), (
        "直接下載網址應排在 winget 之前（winget 只是備選）")


def test_install_ps1_keeps_utf8_bom():
    """含中文的 .ps1 少了 BOM，Win11 PowerShell 5.1 會用 CP950 解碼而炸掉。"""
    assert PS1.read_bytes().startswith(b"\xef\xbb\xbf"), "install.ps1 缺 UTF-8 BOM"


def test_cli_does_not_rely_on_path_for_git():
    """`jtdt update` 不可以只靠 PATH 找 git。

    Windows 剛裝完 Git 時 PATH 只更新在登錄檔，**已經開著的終端機
    （含 Windows Terminal 的新分頁）看不到** —— 使用者照著「重新開啟
    PowerShell」做仍然失敗，只好重開機（客戶 2026-08-23 回報）。
    所以要直接找檔案：登錄檔 + 標準安裝位置。
    """
    src = _strip_comments(CLI.read_text(encoding="utf-8"))
    assert "def _find_git(" in src, "cli.py 缺 _find_git()（不依賴 PATH 的 git 尋找）"
    assert "GitForWindows" in src, "沒有查登錄檔 HKLM\\SOFTWARE\\GitForWindows"
    # 所有 git 呼叫都要走解析出來的完整路徑
    assert '["git", "-C"' not in src, (
        "還有直接呼叫 \"git\" 的地方 —— 剛裝完 git 的 Windows 上會找不到")


def test_find_git_resolves_on_this_machine():
    """在本機（Linux/CI）至少要找得到 —— 確認函式本身能動。"""
    from app.cli import _find_git
    assert _find_git(), "_find_git() 在有 git 的機器上回 None"


def test_update_syncs_the_add_remove_programs_version():
    """`jtdt update` 要把「設定 → 應用程式」的版本更正成實際安裝的版本。

    NSIS installer 是瘦 bootstrapper：登錄檔寫的是**打包當天**的版本，程式碼卻是
    安裝當下的 main。2026-09-05 實測：檔名 1.12.82 的 installer 裝出 v1.15.6，
    登錄檔仍寫 1.12.82 —— 使用者與客服看到的版本跟實際完全對不上。

    修在 `install_core.ps1` 只對**重新打包過的** installer 有效（那支腳本是打包時
    就嵌進 exe 的），既有安裝要靠這裡。
    """
    import inspect
    from app import cli
    assert hasattr(cli, "_sync_windows_display_version"), (
        "少了 _sync_windows_display_version —— 既有 Windows 安裝的版本永遠是錯的")
    src = inspect.getsource(cli.svc_update)
    assert "_sync_windows_display_version" in src, (
        "svc_update 沒有呼叫 _sync_windows_display_version，等於沒接上")
    fn = inspect.getsource(cli._sync_windows_display_version)
    assert "DisplayVersion" in fn and "winreg" in fn
    # 非 Windows 上必須直接 return，不可以丟例外（會讓 Linux 的升級整個失敗）
    cli._sync_windows_display_version("9.9.9")
