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

ROOT = pathlib.Path(__file__).resolve().parent.parent
PS1 = ROOT / "github" / "install.ps1"
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
