"""SignPath 的往來筆記不可以出現在公開版（v1.15.27）。

**踩過**：`SIGNPATH-APPLICATION.md` 有列進 `.gitignore`，但同一個資料夾的
`SIGNPATH-LICENSE-CHANGE.md` **沒有** —— 於是它從 v1.15.8 起被公開了四天，
而 CLAUDE.md 上寫的是「已 gitignore」。**逐檔列的清單一定會漏下一個檔。**

裡面有 SignPath 的 **Organization ID**，而我們在 CI 是把它當
`SIGNPATH_ORG_ID` **機密**存的 —— 一邊當機密、一邊放在公開 repo 裡，
本身就是矛盾。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tools.repo_paths import public_root

ROOT = Path(__file__).resolve().parents[1]
PUB = public_root(ROOT)

# SignPath 主控台的 Organization ID（GUID）。這個值同時是 CI 的機密。
_GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


def _tracked_files() -> list[str] | None:
    """公開 repo 實際追蹤的檔案；不是 git 工作區就回 None。"""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=PUB, capture_output=True,
                             text=True, timeout=30)
    except Exception:  # noqa: BLE001
        return None
    if out.returncode != 0:
        return None
    return out.stdout.splitlines()


def test_the_gitignore_uses_a_wildcard_not_a_per_file_list():
    """判準放在**規則**上，這樣新增一份筆記就自動被擋住。"""
    rules = (PUB / ".gitignore").read_text(encoding="utf-8")
    assert "packaging/windows/SIGNPATH-*.md" in rules, (
        "SignPath 筆記要用萬用字元擋，逐檔列會漏掉下一份")


def test_no_signpath_note_is_tracked_in_the_public_repo():
    tracked = _tracked_files()
    if tracked is None:
        pytest.skip("公開樹不是 git 工作區（開發樹的 github/ 沒有 .git）")
    bad = [f for f in tracked if "SIGNPATH-" in f.upper()]
    assert not bad, f"SignPath 筆記被追蹤了：{bad}"


def test_no_organization_guid_leaks_into_public_text_files():
    """整個公開樹掃一次 GUID —— 就算換個檔名放也擋得到。"""
    bad = []
    for p in PUB.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in {".md", ".html", ".yml", ".yaml", ".txt"}:
            continue
        if ".git/" in p.as_posix() or "/uv.lock" in p.as_posix():
            continue
        # 筆記本身留在開發者的工作目錄是對的（已 gitignore），
        # 「有沒有被發佈出去」由上面兩條負責。
        if p.name.upper().startswith("SIGNPATH-"):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        for m in _GUID.findall(text):
            # 例：文件裡示範 UUID 的地方要標明是範例
            line = next((ln for ln in text.splitlines() if m in ln), "")
            if "example" in line.lower() or "範例" in line:
                continue
            bad.append(f"{p.relative_to(PUB).as_posix()}: {m}")
    assert not bad, f"公開檔案裡出現 GUID（SignPath Organization ID 是機密）：{bad}"
