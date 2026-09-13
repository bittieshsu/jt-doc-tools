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
    # **而且不可以只綁在那一個路徑上。** 綁路徑的規則只要有人把筆記放到別的
    # 資料夾就又公開一次 —— 跟當初「只列了 APPLICATION 那一檔」是同一個病，
    # 只是換一個維度（那次漏的是檔名，這次會漏的是目錄）。
    assert any(ln.strip() == "SIGNPATH-*.md" for ln in rules.splitlines()), (
        "還要有一條不綁目錄的 `SIGNPATH-*.md`，任何位置的筆記都擋得住")


def test_the_rule_lives_where_it_actually_survives():
    """規則要寫在 **`sync-to-github.sh`** 裡，不是改公開樹那份產出。

    `github/.gitignore` 是**每次同步都用 heredoc 重寫的**。直接改那個檔，
    下一次 `sync-to-github.sh` 就把它蓋回去 —— 而且完全無聲：測試在同步之前
    是綠的，同步之後才紅（2026-09-13 就是這樣，我加的規則活了不到一輪）。

    這跟本專案反覆出現的那條是同一件事：**同一份清單放兩個地方一定會漂**，
    所以判準要落在**唯一來源**上。
    """
    script = (ROOT / "sync-to-github.sh")
    if not script.exists():
        pytest.skip("公開樹沒有 sync-to-github.sh（它只在開發樹）")
    body = script.read_text(encoding="utf-8")
    for rule in ("packaging/windows/SIGNPATH-*.md", "SIGNPATH-*.md"):
        assert any(ln.strip() == rule for ln in body.splitlines()), (
            f"`sync-to-github.sh` 產生 .gitignore 的那段少了 `{rule}` —— "
            "改 github/.gitignore 沒有用，下一次同步就被蓋掉")


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


def test_no_signpath_note_sits_in_the_public_tree_at_all():
    """公開樹的**檔案系統**上不該出現 SignPath 筆記以外的位置。

    `git ls-files` 只看得到「已經被追蹤的」。一份剛放進去、還沒 commit 的
    筆記在那條檢查眼裡是乾淨的 —— 而 `rsync -a --delete github/ <clone>/`
    會把它一起帶過去，接著 `git add -A` 就公開了。所以這裡直接掃檔案系統，
    並確認每一份都真的落在被 gitignore 擋住的位置。
    """
    notes = sorted(p.relative_to(PUB).as_posix()
                   for p in PUB.rglob("SIGNPATH-*.md"))
    allowed = {"packaging/windows/SIGNPATH-APPLICATION.md",
               "packaging/windows/SIGNPATH-LICENSE-CHANGE.md"}
    unexpected = [n for n in notes if n not in allowed]
    assert not unexpected, (
        f"公開樹裡出現沒預期的 SignPath 筆記：{unexpected} —— "
        "內部 SOP 放開發樹（例如 docs-share/），不要放進 github/")
