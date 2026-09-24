"""Windows 安裝時要真的挑得到 OxOffice 的 **64 位元** MSI。

原本的比對是 `win|Windows|x64`，而 OxOffice 的檔名長這樣：

    OxOffice_x86-11.0.5.msi       ← 32 位元
    OxOffice_x86_64-11.0.5.msi    ← 64 位元

**兩個都比不到**（`x86_64` 裡沒有連續的 `x64`），於是安裝程式一律退到 winget 裝
LibreOffice —— OxOffice 從來沒有被自動裝上過，而安裝記錄只寫一行
「No Windows MSI asset found」，看起來像對方沒出 Windows 版。

判準照 PowerShell 的 `-match`（不分大小寫）套在**真的出現過的檔名**上：
一定要挑到 64 位元那一支、一定不可以挑到 32 位元那一支。
"""
from __future__ import annotations

import re

import pytest

from tools.repo_paths import public_root

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PUB = public_root(ROOT)

#: 2026-09-24 查 GitHub 上 OxOffice 近六版的 Windows 資產（檔名形狀一直沒變）
_REAL_ASSETS = [
    ["OxOffice-11.0.5-deb.zip", "OxOffice-11.0.5-rpm.zip",
     "OxOffice_11.0.5.1_MacOS_aarch64.dmg", "OxOffice_11.0.5.1_MacOS_x86-64.dmg",
     "OxOffice_x86-11.0.5.msi", "OxOffice_x86_64-11.0.5.msi"],
    ["OxOffice_x86-11.0.4.msi", "OxOffice_x86_64-11.0.4.msi"],
    ["OxOffice_x86-11.0.2.1.msi", "OxOffice_x86_64-11.0.2.1.msi"],
    ["OxOffice_x86-11.0.14.msi", "OxOffice_x86_64-11.0.1.4.msi"],
]

_SCRIPTS = ["install.ps1", "packaging/windows/install_core.ps1"]


def _filters(rel: str) -> list[str]:
    text = (PUB / rel).read_text(encoding="utf-8-sig")
    # install.ps1 另有一行挑 Git 安裝檔的，只取 MSI 那一行
    lines = [ln for ln in text.splitlines()
             if "$asset = $rel.assets" in ln and "msi" in ln]
    assert len(lines) == 1, f"{rel}：找不到（或不只一行）挑 OxOffice 資產的那一行"
    pats = re.findall(r"-match '([^']+)'", lines[0])
    assert len(pats) >= 2, f"{rel}：那一行應該有副檔名與架構兩個條件，只找到 {pats}"
    return pats


def _pick(pats: list[str], names: list[str]) -> str | None:
    for n in names:                      # 跟 `Select-Object -First 1` 一樣照原順序
        if all(re.search(p, n, re.I) for p in pats):
            return n
    return None


@pytest.mark.parametrize("rel", _SCRIPTS)
@pytest.mark.parametrize("names", _REAL_ASSETS)
def test_the_64bit_msi_is_picked(rel, names):
    got = _pick(_filters(rel), names)
    assert got and "x86_64" in got, (
        f"{rel} 從 {names} 挑到 {got!r} —— 應該是 64 位元那一支 MSI。"
        "挑不到的話安裝程式會安靜地改裝 LibreOffice")


@pytest.mark.parametrize("rel", _SCRIPTS)
def test_the_32bit_msi_is_never_picked(rel):
    """反向對照：只驗「挑得到」的話，把條件放寬到任何 .msi 也會過 ——
    而清單上 32 位元那一支排在前面。"""
    got = _pick(_filters(rel), ["OxOffice_x86-11.0.5.msi"])
    assert got is None, f"{rel} 挑到 32 位元的 {got}"
