"""安裝畫面上不可以出現亂碼（2026-09-15 客戶回報，Win11 25H2）。

`winget` 送出的是 **UTF-8**，而 NSIS 的 `nsExec::ExecToLog` 用**系統的 ANSI
字碼頁**去解（繁中 Windows 是 CP950）—— 於是安裝進度那一格出現一整片亂碼，
中間夾著 winget 的授權條款與進度動畫。

**修法不是去轉編碼**：那一片內容對使用者本來就沒有意義，能讀也只是雜訊。
一律導進 `installer.log`（要查的時候還在），畫面上只留我們自己的一行英文狀態。

這條擋的是「有人又直接 `Start-Process winget ... -NoNewWindow`」。

**同一條路對日文 Windows 一模一樣**（ANSI 是 CP932），對英文是 CP1252 ——
所以判準不可以只盯 winget：**任何會印到那一格的非 ASCII 文字都會亂碼**，
包含我們自己的 `Write-Host`。安裝程式本身的字串沒事，因為 `Unicode true`
讓語言表走 UTF-16；出事的只有「子行程的輸出」這條路。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.repo_paths import public_root  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PS1 = public_root(ROOT) / "packaging" / "windows" / "install_core.ps1"


def _code_lines() -> list[tuple[int, str]]:
    r"""(行號, 整條指令)。

    兩件事一定要做，少一件這條檢查就是假的：

    * **去掉 `#` 註解** —— 說明裡會**引用**那個錯誤寫法當反例。
    * **把接續行接起來**（PowerShell 用反引號結尾）—— `Start-Process` 與
      `-RedirectStandardOutput` 常常不在同一行，逐行看會把正確的寫法
      判成違規（本專案在 NSIS 的 `\` 接續行上踩過同一個錯，第一版就是這樣紅的）。
    """
    raw = PS1.read_text(encoding="utf-8-sig").splitlines()
    out: list[tuple[int, str]] = []
    buf, start = "", 0
    for i, line in enumerate(raw, 1):
        if line.lstrip().startswith("#"):
            continue
        s = line.rstrip()
        if not buf:
            start = i
        buf += (" " if buf else "") + s.rstrip("`").strip()
        if s.endswith("`"):
            continue                      # 還沒結束
        out.append((start, buf))
        buf = ""
    if buf:
        out.append((start, buf))
    return out


def _code() -> str:
    return "\n".join(c for _i, c in _code_lines())


def test_the_helper_exists():
    assert "function Invoke-Winget" in _code(), \
        "少了統一處理 winget 輸出的 Invoke-Winget"


def test_no_winget_call_streams_its_output_to_the_installer_window():
    bad = []
    for i, line in _code_lines():
        if "Start-Process" not in line or "winget" not in line:
            continue
        if "RedirectStandardOutput" not in line:
            bad.append(f"{i}: {line.strip()[:100]}")
    assert not bad, (
        "這幾行讓 winget 的 UTF-8 輸出直接流進 NSIS 的安裝畫面（會變亂碼）：\n"
        + "\n".join(bad) + "\n改走 Invoke-Winget。")


def test_the_captured_output_is_read_as_utf8():
    """存進記錄檔時也要**照 UTF-8 讀** —— 不然記錄檔裡也是亂碼，
    而那正是出事時唯一能查的東西。
    """
    code = _code()
    assert "System.Text.Encoding]::UTF8" in code, \
        "讀 winget 的輸出時沒有指定 UTF-8"


def test_the_scan_actually_reaches_the_file():
    """**先證明掃得到東西** —— 檔案改名 / 搬家時這條會先紅。"""
    assert PS1.is_file(), f"找不到 {PS1}"
    assert len(_code()) > 2000, "install_core.ps1 看起來不對"


#: 兩支都會被 `nsExec::ExecToLog` 叫起來，輸出走同一條 ANSI 解碼路徑。
_SCRIPTS = ("install_core.ps1", "uninstall_core.ps1")


#: 兩支腳本都把狀態文字包成 `Log` / `Ok` / `Warn` / `Die` 這幾支小 helper，
#: 底層才是 `Write-Host`。**只認 `Write-Host` 的話這條掃不到任何一句**
#: —— 而「掃 0 行」跟「掃過都乾淨」在 pytest 輸出裡長得一模一樣。
_SAYS = r"(Write-Host|Write-Output|echo|Log|Ok|Warn|Die|_w)"


def _prints_to_the_pane(cmd: str) -> bool:
    """這條指令的輸出會不會出現在安裝畫面上。"""
    s = cmd.strip()
    if s.startswith("function "):
        return False                       # helper 自己的定義不算
    return bool(re.match(_SAYS + r"\b", s))


def test_nothing_non_ascii_is_printed_to_the_installer_window():
    """**畫面上那一格只能是 ASCII。**

    `nsExec::ExecToLog` 用系統 ANSI 字碼頁解子行程的輸出：繁中 CP950、
    日文 CP932、英文 CP1252。我們送 UTF-8 出去，三種都會亂 —— 差別只在
    亂成什麼樣子。**寫死一句中文（或日文）在 Log 裡，就是下一張客戶截圖。**

    要講中文請寫進 `installer.log`（那個檔我們自己用 UTF-8 寫、自己讀），
    或走 NSIS 的 LangString（`Unicode true`，不經這條路）。
    """
    bad = []
    for name in _SCRIPTS:
        f = public_root(ROOT) / "packaging" / "windows" / name
        assert f.is_file(), f"找不到 {f}"
        raw = f.read_text(encoding="utf-8-sig").splitlines()
        buf, start = "", 0
        for i, line in enumerate(raw, 1):
            if line.lstrip().startswith("#"):
                continue
            s = line.rstrip()
            if not buf:
                start = i
            buf += (" " if buf else "") + s.rstrip("`").strip()
            if s.endswith("`"):
                continue
            if _prints_to_the_pane(buf) and any(ord(c) > 127 for c in buf):
                bad.append(f"{name}:{start}: {buf[:80]}")
            buf = ""
    assert not bad, (
        "這幾行會把非 ASCII 印進安裝畫面（繁中 / 日文 / 英文 Windows 都會亂碼）：\n"
        + "\n".join(bad))


def test_the_ascii_scan_actually_sees_the_print_statements():
    """**先證明掃得到東西。**

    「一行都沒掃到」跟「掃過都乾淨」在 pytest 輸出裡長得一模一樣 ——
    這條釘住上面那支真的有認出 `Write-Host` 這類指令。
    """
    seen = {}
    for name in _SCRIPTS:
        f = public_root(ROOT) / "packaging" / "windows" / name
        seen[name] = sum(1 for ln in f.read_text(encoding="utf-8-sig").splitlines()
                         if _prints_to_the_pane(ln))
    # **逐檔數，不要加總** —— 只看總數的話，其中一支整批改名（掃 0 行）
    # 仍然過得去，因為另一支撐著。
    thin = {k: v for k, v in seen.items() if v < 5}
    assert not thin, f"這幾支掃不到會印到畫面的指令，掃描器大概壞了：{thin}"
