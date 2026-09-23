"""公開樹裡不可以出現**我們自己的**內網位址。

「推之前掃內網 IP」這條規矩在慣例裡寫了很久，但**沒有檢查** —— 於是
`tools/meeting_eval/` 四支在 v1.15.6x 把推論機的位址寫成 argparse 的預設值，
一路同步到公開樹上都沒有人發現（2026-09-21 推版前手動掃才抓到）。
記了規則沒有檢查，等於沒記。

**判準是「非測試檔裡不可以有 RFC1918 的字面位址」**，不是「不可以有我們那幾台」
—— 把我們的位址寫進檢查，等於把它留在公開樹上，那跟外洩沒兩樣
（同「規則可以公開，理由不行」那條）。

**測試檔排除在外**：fixture 需要具體的位址當輸入（假的用戶端 IP、假的信任代理），
那些是編出來的、也不會被任何程式連出去。真正會出事的是**預設值、設定範例與文件**
—— 那些是「照著跑就會連出去」的位址。

例外要寫理由，而且例外清單自己也要有檢查（留著沒必要的豁免比沒有豁免更糟）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.repo_paths import public_root

_RFC1918 = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)

_SUFFIXES = (".py", ".sh", ".ps1", ".cmd", ".html", ".md", ".yaml", ".yml", ".nsi")

# 掃描範圍刻意不含 tests/（理由見上面的 docstring）與 docs/screenshots/（圖檔）。
# **虛擬環境也不算公開樹**：CI 的「正式機相依版本」那個 job 在 repo 根目錄跑
# `uv sync`，`.venv`（含第三方套件的原始碼）就長在這裡 —— 掃進去就是一堆
# 別人的位址（2026-09-23 CI 排程測試紅，本機因為沒有 `.venv` 永遠是綠的）。
_SKIP_DIRS = {"tests", "__pycache__", ".git", "node_modules", "vendor", "screenshots",
              ".venv", "venv", "site-packages"}

# 位址 → 為什麼它留著沒關係。一律是**文件上的示意值**，不是任何一台真的機器。
_ALLOWED = {
    "10.0.0.0": "說明文字裡的示意網段（講「信任代理要填整個網段」時的例子）",
    "10.0.0.5": "遠端 OCR 伺服器設定的示意預設值，客戶一定要自己改成他的位址",
    "192.168.1.10": "管理頁表單的 placeholder，示意「這裡填一個位址」",
}


def _files() -> list[Path]:
    """公開樹的檔案。

    **是 git 工作區就只看被追蹤的檔案** —— 那才是真正公開出去的東西；
    建置過程長出來的（`.venv`、快取）不算。開發樹沒有 `.git`，退回列目錄。
    """
    import subprocess
    root = public_root()
    if (root / ".git").exists():
        r = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                           capture_output=True, text=True, check=False)
        if r.returncode == 0 and r.stdout:
            cand = [root / n for n in r.stdout.split("\0") if n]
        else:
            cand = list(root.rglob("*"))
    else:
        cand = list(root.rglob("*"))
    out = []
    for p in cand:
        if not p.is_file() or p.suffix.lower() not in _SUFFIXES:
            continue
        if _SKIP_DIRS & set(p.relative_to(root).parts):
            continue
        out.append(p)
    return out


def test_no_internal_addresses_outside_tests():
    bad = []
    for p in _files():
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in _RFC1918.finditer(text):
            if m.group(0) in _ALLOWED:
                continue
            line = text.count("\n", 0, m.start()) + 1
            bad.append(f"{p.relative_to(public_root()).as_posix()}:{line} → {m.group(0)}")
    assert not bad, (
        "公開樹的非測試檔裡出現內網位址：\n  " + "\n  ".join(bad)
        + "\n改用 localhost 或環境變數（例如 os.environ.get(\"JTDT_EVAL_LLM\", ...)）；"
        "真的是示意值就加進 _ALLOWED 並寫理由。"
    )


def test_the_allow_list_has_not_gone_stale():
    """豁免掉一個其實已經不存在的位址，會讓下一個人以為那裡還有東西要小心。"""
    blob = "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in _files()
    )
    unused = [a for a in _ALLOWED if a not in blob]
    assert not unused, f"_ALLOWED 裡這幾個已經沒人用了，請刪掉：{unused}"


def test_the_scan_actually_reaches_the_files_that_matter():
    """「掃 0 個檔」跟「掃過都乾淨」在 pytest 輸出裡長得一模一樣。

    門檻是量出來的：2026-09-21 實際收到 700 支以上，取一半當下限。
    而且三類各自要收得到 —— 少收一整類（例如把 tools/ 漏掉）正是這次出事的形狀。
    """
    files = _files()
    assert len(files) >= 350, f"只掃到 {len(files)} 個檔案，範圍可能壞了"
    root = public_root()
    for want in ("app", "tools", "docs"):
        got = [p for p in files if p.relative_to(root).parts[0] == want]
        assert got, f"{want}/ 一個檔案都沒收到"
