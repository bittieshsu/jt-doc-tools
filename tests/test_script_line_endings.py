"""Windows 批次檔一律 CRLF、Unix 腳本一律 LF。

## 由來

**LF 行尾的 `.cmd` 在 cmd.exe 會逐 token 噴錯**（v1.12.8 / v1.12.10 的客戶
慘案）：走 tarball 安裝（機器上沒有 git）的人拿到 LF 版的
`setup-python.cmd`，整個 Windows 安裝就失敗了。用 git clone 的人不會踩到
—— `.gitattributes` 的 `*.cmd text eol=crlf` 會在 checkout 時轉回來，
**所以開發機上怎麼測都是對的**。

當時的修法是三件：①`install.ps1` / `install_core.ps1` 執行前正規化
②把檔案改成 CRLF ③加 `.gitattributes`。**但沒有守門** ——
於是 2026-09-19 我為了加一個 import 又把它改回 LF（用 Python 改檔案時
`write_text` 預設就是 LF）。前兩道保險讓它沒有真的壞掉，
但「記了規則沒有守門就會復發」在這個專案已經是常態。

## 判準

`.gitattributes` 宣告的那兩條規則，要在**檔案本身**上成立：

* `*.cmd` / `*.bat` —— 每一行都要以 CRLF 結尾
* `*.sh` —— 一個 CR 都不可以有

**兩種樹都成立**：clone 下來的是 git 依 `.gitattributes` 轉出來的（本來就對），
開發樹的 `github/` 不在版控裡，靠這條守門盯著。

**另外釘住 `.gitattributes` 本身** —— 規則被拿掉的話，clone 那一側就沒有
保護了，而那正是客戶走的路。
"""
from __future__ import annotations

import pathlib

import pytest

import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
from tools.repo_paths import public_root as _public_root

ROOT = _public_root(pathlib.Path(__file__).resolve().parent.parent)

#: 目前公開樹裡這兩類各只有一支。**逐支點名**，改名或搬走時這條會先紅
#: —— 只靠副檔名掃的話，檔案不見了會變成「掃 0 個檔」而照樣全綠。
_MUST_EXIST_CRLF = ["setup-python.cmd"]
_MUST_EXIST_LF = ["install.sh"]


def _crlf_files() -> list[pathlib.Path]:
    out = []
    for pat in ("*.cmd", "*.bat"):
        out += [p for p in ROOT.rglob(pat) if ".git" not in p.parts]
    return sorted(out)


def _lf_files() -> list[pathlib.Path]:
    return sorted(p for p in ROOT.rglob("*.sh") if ".git" not in p.parts)


def test_the_scan_actually_reaches_those_files():
    """先證明掃得到東西 —— 「掃 0 個檔」跟「掃過都乾淨」在輸出裡長得一樣。"""
    found_cmd = {p.name for p in _crlf_files()}
    found_sh = {p.name for p in _lf_files()}
    missing = [n for n in _MUST_EXIST_CRLF if n not in found_cmd]
    missing += [n for n in _MUST_EXIST_LF if n not in found_sh]
    assert not missing, (
        f"公開樹（{ROOT}）裡找不到這幾支腳本：{missing}。"
        "檔案改名或搬走了就把清單一起改，不要讓守門變成空迴圈。"
    )


@pytest.mark.parametrize("name", _MUST_EXIST_CRLF)
def test_windows_batch_files_use_crlf(name: str):
    path = ROOT / name
    data = path.read_bytes()
    lone_lf = data.replace(b"\r\n", b"").count(b"\n")
    assert lone_lf == 0, (
        f"{path} 有 {lone_lf} 行是 LF 結尾。cmd.exe 執行 LF-only 批次檔會"
        "逐 token 噴錯，走 tarball 安裝（沒有 git）的客戶整個安裝會失敗；"
        "用 git clone 的人因為 .gitattributes 會轉回來，所以開發機上測不出來。"
        "用 Python 改這個檔時記得 newline='\\r\\n'（write_text 預設是 LF）。"
    )
    assert data.count(b"\r\n") > 0, f"{path} 是空的或沒有任何行"


@pytest.mark.parametrize("name", _MUST_EXIST_LF)
def test_unix_shell_scripts_use_lf(name: str):
    path = ROOT / name
    data = path.read_bytes()
    assert b"\r" not in data, (
        f"{path} 含有 CR。shell 會把行尾的 \\r 當成指令的一部分"
        "（`command\\r: not found`），而訊息完全看不出跟行尾有關。"
    )


def test_gitattributes_still_declares_the_rules():
    """clone 那一側靠的是這個檔案 —— 規則被拿掉就沒有保護了。"""
    ga = ROOT / ".gitattributes"
    assert ga.is_file(), f"{ga} 不見了"
    text = ga.read_text(encoding="utf-8")
    lines = [ln.split("#", 1)[0].strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]

    def _declares(pattern: str, eol: str) -> bool:
        for ln in lines:
            parts = ln.split()
            if parts and parts[0] == pattern and f"eol={eol}" in parts[1:]:
                return True
        return False

    missing = [
        f"{pat} eol={eol}"
        for pat, eol in (("*.cmd", "crlf"), ("*.bat", "crlf"), ("*.sh", "lf"))
        if not _declares(pat, eol)
    ]
    assert not missing, (
        f"{ga} 少了這幾條行尾規則：{missing}。"
        "少了的話，clone 下來的檔案就會照 repo 裡的位元組（LF）給客戶。"
    )
