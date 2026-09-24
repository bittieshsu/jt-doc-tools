"""PowerShell 的雙引號字串裡不可以寫 `"$變數:"`。

`$after:` 會被當成「作用域 / 磁碟機限定的變數」（像 `$env:TEMP`、`$script:x`），
冒號後面接空白或引號就是**剖析錯誤** —— 整支腳本一行都不會執行。
v1.16.23 寫 VC++ 執行階段的警告訊息時就寫成 `"… still $after: restart …"`，
Linux 上的測試全綠（沒有 PowerShell），是丟到 Windows 用 `Parser::ParseFile` 才抓到。
對安裝程式來說這是**整個安裝失敗**，而且只有在 Windows 上才看得到。

正確寫法：`$($after):` 或 `${after}:`。
`$env:TEMP` 這種冒號後面緊接名稱的是合法的作用域寫法，不在這條的範圍。
"""
from __future__ import annotations

import pathlib
import re

import pytest

from tools.repo_paths import public_root

ROOT = pathlib.Path(__file__).resolve().parent.parent
PUB = public_root(ROOT)

_SCRIPTS = sorted(p for p in PUB.rglob("*.ps1") if ".git" not in p.parts)

# 雙引號字串（不跨行，允許 `" 跳脫）
_DQ = re.compile(r'"((?:`.|[^"`\n])*)"')
# `$名稱:` 後面不是名稱字元、也不是另一個冒號（`$env:TEMP`、`$script:x` 合法）
_BAD = re.compile(r"(?<![`$])\$[A-Za-z_][A-Za-z0-9_]*:(?![A-Za-z0-9_:])")


def test_the_scan_reaches_the_installers():
    names = {p.name for p in _SCRIPTS}
    assert {"install.ps1", "install_core.ps1", "uninstall_core.ps1"} <= names, names


@pytest.mark.parametrize("path", _SCRIPTS, ids=lambda p: p.name)
def test_no_scope_colon_right_after_a_variable_in_a_string(path):
    bad = []
    for no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for m in _DQ.finditer(line):
            if _BAD.search(m.group(1)):
                bad.append(f"{no}: {line.strip()[:120]}")
    assert not bad, f"{path.name}：雙引號字串裡的 `$變數:` 會讓整支腳本剖析失敗，改用 `$($x):`：\n" + "\n".join(bad)


def test_the_check_catches_the_real_mistake():
    line = 'Warn "System32 runtime is still $after: restart Windows"'
    assert any(_BAD.search(m.group(1)) for m in _DQ.finditer(line))
    ok = 'Log "temp is $env:TEMP and $($after): fine, ${after}: fine"'
    assert not any(_BAD.search(m.group(1)) for m in _DQ.finditer(ok))
