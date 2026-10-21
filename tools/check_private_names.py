#!/usr/bin/env python3
"""發布前檢查：名單上的字不可以出現在要公開的檔案裡。

名單是開發樹裡的一份文字檔（一行一個正規表示式，`#` 開頭是註解），**不在公開樹裡**，
所以這支程式本身不含任何名字。公開的 clone 上沒有名單 → 什麼都不做、回 0。

    python tools/check_private_names.py                 # 掃開發樹裡會被同步出去的部分
    python tools/check_private_names.py github/         # 掃指定的目錄（同步腳本推送前用）

有命中就印出「檔案:行號」並回 1。**不印命中的字本身**——
這支的輸出可能被貼進 issue 或 CI 記錄。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NAMES_FILE = REPO / "docs-share" / "private-names.txt"
#: 內部主機代號（IP 最後一段、機器名稱）。跟名單分開放：名單 JTLW 的推送前檢查也讀，
#: 他們的文件會寫他們自己機器的代號。這一份只有我們讀。
HOSTS_FILE = REPO / "docs-share" / "private-hosts.txt"

#: 存取權杖的格式（不需要名單，公開的 clone 上也照樣檢查）。只寫格式、不寫任何真的權杖。
#: GitHub：`github_pat_` 細粒度、`ghp_` / `gho_` / `ghu_` / `ghs_` / `ghr_` 傳統與 App 權杖。
LEAK_FORMATS = [
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
]

#: 開發樹裡會被 `sync-to-github.sh` 同步出去的部分（跟它的 ITEMS 對齊）＋ 公開樹自己的檔案
DEFAULT_ROOTS = ("app", "static", "tests", "tools", "scripts",
                 "TEST_PLAN.md", "TEST_PLAN_SECURITY.md", "github")
TEXT_SUFFIXES = {".md", ".html", ".py", ".js", ".css", ".txt", ".sh", ".ps1", ".json",
                 ".yml", ".yaml", ".toml", ".cmd", ".nsi", ".cfg", ".ini", ".svg", ".xml"}
#: 第三方原始碼（不是我們寫的）、版控與快取
SKIP_PARTS = {"vendor", ".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}


def load_patterns(path: Path = NAMES_FILE) -> list[re.Pattern]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(re.compile(s))
    return out


def load_host_patterns(path: Path = HOSTS_FILE) -> list[re.Pattern]:
    """內部主機代號的式子（格式跟名單一樣）。"""
    return load_patterns(path)


def host_samples(path: Path = HOSTS_FILE) -> list[str]:
    """主機代號那份檔案裡的測試樣本（`#sample: ` 開頭的註解行）。樣本放在私有檔案裡，
    公開的測試才不必寫出任何代號。"""
    if not path.is_file():
        return []
    return [line.strip()[len("#sample:"):].strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("#sample:")]


def iter_files(roots):
    for r in roots:
        p = Path(r)
        if not p.is_absolute():
            p = REPO / p
        if p.is_file():
            yield p
            continue
        if not p.is_dir():
            continue
        for f in p.rglob("*"):
            if not f.is_file() or f.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if SKIP_PARTS & set(f.relative_to(p).parts):
                continue
            yield f


def scan(roots, patterns) -> list[tuple[Path, int]]:
    """命中的「檔案、行號」（一行命中幾個字都只算一次）。"""
    hits = []
    for f in iter_files(roots):
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for no, line in enumerate(text.splitlines(), 1):
            if any(pt.search(line) for pt in patterns):
                hits.append((f, no))
    return hits


def _shown(f: Path) -> str:
    try:
        return f.relative_to(REPO).as_posix()
    except ValueError:
        return f.as_posix()


def main(argv: list[str]) -> int:
    roots = argv or list(DEFAULT_ROOTS)
    leaked_lines = scan(roots, LEAK_FORMATS)
    for f, no in leaked_lines:
        print(f"{_shown(f)}:{no}")
    if leaked_lines:
        print(f"✗ {len(leaked_lines)} 行有存取權杖（GitHub token 等），不可以公開 —— 移掉之後也要到 GitHub 撤銷那一張",
              file=sys.stderr)
        return 1
    patterns = load_patterns() + load_host_patterns()
    if not patterns:
        print("（沒有名單，只檢查了存取權杖）")
        return 0
    hits = scan(roots, patterns)
    for f, no in hits:
        print(f"{_shown(f)}:{no}")
    if hits:
        print(f"✗ {len(hits)} 行有名單上的字（客戶名稱、人名、真實會議裡的詞或內部主機代號），不可以公開",
              file=sys.stderr)
        return 1
    print(f"✓ 名單上 {len(patterns)} 個字，要公開的檔案裡都沒有")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
