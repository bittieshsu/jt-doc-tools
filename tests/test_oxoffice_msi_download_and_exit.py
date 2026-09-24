"""Windows 安裝 OxOffice：下載要驗完整、msiexec 回 3010 要算成功。

兩個都是 Win10 實機踩到的（第一次真的走到「自動裝 OxOffice」這條路 ——
Win11 測試機早就裝過 Office，安裝程式一直直接跳過這一段）：

1. **下載不完整不會報錯。** MSI 約 400 MB、放在 GitHub 的發行檔案上，慢網路上
   Invoke-WebRequest 交回一份不完整的檔案，msiexec 回 **1625**「系統原則禁止這項安裝」
   （事件記錄 1008：「物件無法被信任」）—— 看起來像權限問題。同一份 MSI 完整下載後
   在同一台、同一種連線方式下裝得起來。
2. **3010 是「裝好了、建議重新開機」。** 原本 `ExitCode -ne 0` 一律當失敗 —— 裝好的
   OxOffice 被判成失敗，接著又用 winget 多裝一套 LibreOffice。

兩支安裝腳本（圖形安裝程式內嵌的 `install_core.ps1` 與一行安裝的 `install.ps1`）都要做到。
"""
from __future__ import annotations

import pathlib
import re

import pytest

from tools.repo_paths import public_root

ROOT = pathlib.Path(__file__).resolve().parent.parent
PUB = public_root(ROOT)
_SCRIPTS = ["install.ps1", "packaging/windows/install_core.ps1"]


def _strip_comments(text: str) -> str:
    out = []
    for ln in text.splitlines():
        s = ln.lstrip()
        if s.startswith("#"):
            continue
        out.append(ln)
    return "\n".join(out)


def _function(rel: str, name: str) -> str:
    text = _strip_comments((PUB / rel).read_text(encoding="utf-8-sig"))
    m = re.search(rf"^function {re.escape(name)}\b.*?^}}", text, re.M | re.S)
    assert m, f"{rel}：找不到 function {name}"
    return m.group(0)


@pytest.mark.parametrize("rel", _SCRIPTS)
def test_the_msi_is_not_downloaded_without_verification(rel):
    body = _function(rel, "Install-OxOffice")
    assert "Save-VerifiedMsi" in body, f"{rel}：OxOffice 的 MSI 沒有經過完整性檢查就交給 msiexec"
    assert "Invoke-WebRequest" not in body, (
        f"{rel}：Install-OxOffice 裡還有直接下載的 Invoke-WebRequest —— 要走 Save-VerifiedMsi")
    assert re.search(r"Save-VerifiedMsi\s+\$asset\.browser_download_url\s+\$asset\.size", body), (
        f"{rel}：要把 GitHub 回報的檔案大小（$asset.size）交給檢查")


@pytest.mark.parametrize("rel", _SCRIPTS)
def test_the_verifier_checks_size_and_signature_and_retries(rel):
    body = _function(rel, "Save-VerifiedMsi")
    assert re.search(r"-ErrorAction Stop", body), f"{rel}：下載失敗要丟得出例外，不然會拿半份檔案往下走"
    assert re.search(r"\$got\s+-ne\s+\$size", body), f"{rel}：沒有比對下載大小"
    assert "Get-AuthenticodeSignature" in body and "HashMismatch" in body, (
        f"{rel}：沒有檢查簽章的雜湊（同樣大小但內容壞掉的檔案）")
    loop = re.search(r"for \(\$i = 1; \$i -le (\d+);", body)
    assert loop and 2 <= int(loop.group(1)) <= 5, f"{rel}：要重試幾次（目前 {loop and loop.group(1)}）"
    assert body.rstrip().splitlines()[-2].strip() == "return $false", (
        f"{rel}：重試用完要回 $false（讓安裝退到 LibreOffice），不可以回 $true")


@pytest.mark.parametrize("rel", _SCRIPTS)
def test_exit_3010_counts_as_installed(rel):
    body = _function(rel, "Install-OxOffice")
    i3010 = body.find("-eq 3010")
    ifail = body.find("ExitCode -ne 0")
    assert i3010 != -1, f"{rel}：msiexec 回 3010（裝好了、建議重開機）被當成失敗"
    assert ifail != -1 and i3010 < ifail, (
        f"{rel}：要先認 3010 再判斷非 0 —— 反過來的話 3010 一樣被當失敗")
    assert re.search(r"elseif \(\$proc\.ExitCode -ne 0\)", body), (
        f"{rel}：3010 與「非 0 就失敗」要是同一組 if / elseif")


@pytest.mark.parametrize("rel", _SCRIPTS)
def test_msiexec_leaves_a_log(rel):
    body = _function(rel, "Install-OxOffice")
    assert "/l*v" in body and "$msiLog" in body, (
        f"{rel}：msiexec 沒留記錄 —— 失敗時只看得到一個數字")
    assert re.search(r"details: \$msiLog", body), f"{rel}：失敗訊息要指出記錄檔在哪裡"
