#!/usr/bin/env python3
"""解析「公開樹」在哪裡 —— 開發樹與 clone 下來的樹**結構不一樣**。

* **開發樹**：`github/` 這個資料夾**本身就是公開 repo 的根**，README / CHANGELOG /
  docs / 介紹站都在裡面。
* **clone 下來的樹**：那些檔案**直接就在根目錄**，沒有 `github/` 這一層。

`sync-to-github.sh` 會把 `tests/` 與 `tools/` 一起同步出去，所以同一份程式在兩種
結構下都會被執行。寫死 `github/README.md` 的話，**在公開版永遠 file not found**
—— 外部評估（2026-09-05）跑標準 clone 時就這樣一次紅了 70 支測試，而開發機上
一直是綠的，因為開發樹剛好有那一層。

用法：

    from tools.repo_paths import public_root
    readme = public_root() / "README.md"

判準是**看檔案在不在**，不是看資料夾名字：`<repo>/github/README.md` 存在就代表
這是開發樹，否則 `<repo>` 自己就是公開樹。
"""
from __future__ import annotations

from pathlib import Path

#: 這支檔案在 `tools/` 底下，所以 repo 根在上一層。
REPO_ROOT = Path(__file__).resolve().parent.parent


def public_root(repo_root: Path | None = None) -> Path:
    """公開樹的根（README / CHANGELOG / docs / 介紹站所在的目錄）。"""
    root = repo_root or REPO_ROOT
    nested = root / "github"
    return nested if (nested / "README.md").is_file() else root


def is_development_tree(repo_root: Path | None = None) -> bool:
    """開發樹（有 `github/` 那一層）回 True；clone 下來的公開樹回 False。"""
    root = repo_root or REPO_ROOT
    return (root / "github" / "README.md").is_file()
