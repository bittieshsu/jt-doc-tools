"""第三方服務的 API 金鑰不可以出現在會公開或會部署出去的地方。

接外部服務時，金鑰應該進**使用者設定**（跟 LLM 設定、SMTP 密碼同一類），
不是程式碼。真正的風險不在「有人故意提交金鑰」，而在**接線的時候**：
「先寫死在程式裡試通，之後再搬」是最順手、也最容易留下來的做法。

**判準比對的是「形狀」不是任何一把金鑰本身**：
* 把金鑰寫進測試等於在公開樹上放一份（`tests/` 會同步出去）。
* 形狀比對換發新金鑰、換服務商都一樣擋得到。
"""
from __future__ import annotations

import pathlib
import re

import pytest

import sys as _sys
_sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from tools.repo_paths import public_root as _public_root

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 這一類金鑰的形狀：`<發行方>_<用途>_<一長串隨機字元>`。
#:
#: **式子裡不可以寫出發行方的名字，也不可以寫出金鑰本身。**
#: 這個檔案會被同步到公開樹 —— 寫了名字等於在公開 repo 上宣告
#: 「我們跟某某有整合」，而那件事在出貨前不該公開（同 SignPath 筆記那次：
#: **規則可以公開，理由不行**）。寫了金鑰本身更是直接外洩。
#:
#: 純形狀比對還有一個好處：**換發新金鑰、換服務商都一樣擋得到**。
#: **尾段要同時有大寫與數字** —— 少了這一段，`test_js_set_attributes_go_through_tr`
#: 這種函式名全部會中（實測整棵樹 1887 處誤報），而誤報一多這份檢查就會被
#: 當雜訊忽略。金鑰的尾段是高熵的 base64url，識別字是全小寫加底線。
_KEY_SHAPE = re.compile(
    r"\b[a-z][a-z0-9]{2,15}_[a-z0-9]{2,15}_"
    r"(?=[A-Za-z0-9_-]{24,})(?=[A-Za-z0-9_-]*[A-Z])"
    r"(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{24,}\b")

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "vendor",
              "docs-share", "temp", "temp_pdfs", "data", "releases"}
_TEXT_SUFFIXES = {".py", ".js", ".html", ".md", ".json", ".yml", ".yaml",
                  ".toml", ".sh", ".ps1", ".cmd", ".txt", ".nsi", ".cfg"}


def _scan(root: pathlib.Path) -> list[str]:
    hits = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _KEY_SHAPE.search(body):
            hits.append(p.relative_to(root).as_posix())
    return hits


def test_the_public_tree_has_no_partner_key():
    bad = _scan(_public_root(ROOT))
    assert not bad, (
        "公開樹裡有長得像 API 金鑰的字串：" + ", ".join(bad) +
        "。金鑰要放使用者設定（跟 LLM 設定同一類），不可以寫進程式碼。"
    )


def test_the_working_tree_has_no_partner_key_outside_the_private_notes():
    """開發樹也要擋 —— 公開樹是同步出去的，寫進 `app/` 下一次同步就公開了。"""
    bad = _scan(ROOT)
    assert not bad, (
        "開發樹裡有長得像 API 金鑰的字串：" + ", ".join(bad) +
        "。往來文件請放 `docs-share/`（不同步、不部署）。"
    )


def test_the_shape_really_matches_a_key_like_string():
    """**先證明這個式子抓得到東西。** 抓不到任何形狀的檢查等於沒有檢查。"""
    # 自己造一把長得像金鑰的字串（**不是任何真的金鑰**）
    # 自己造一把長得像金鑰的字串（**不是任何真的金鑰**）
    sample = "acme" + "_svc_" + "-bQx_pR3zK9TmW2aeVu-cJ7HdN4gYsZ1tL"
    assert _KEY_SHAPE.search(sample), "式子連自己造的樣本都抓不到"
    assert not _KEY_SHAPE.search("這家的 API 已經上線了"), "一般文字被誤判"
    assert not _KEY_SHAPE.search("acme_short_abc"), "太短的不該中"
    # **這幾個是真的會出現在這棵樹裡的識別字** —— 第一版的式子全部誤報
    for ident in ("some_var_name",
                  "test_js_set_attributes_go_through_tr",
                  "test_the_uninstall_handoff_reports_success"):
        assert not _KEY_SHAPE.search(ident), f"一般識別字被誤報：{ident}"


def test_the_scan_actually_reaches_files():
    """掃 0 個檔跟「掃過都乾淨」在 pytest 輸出裡長得一模一樣。"""
    n = sum(1 for p in _public_root(ROOT).rglob("*")
            if p.is_file() and p.suffix.lower() in _TEXT_SUFFIXES
            and not any(part in _SKIP_DIRS for part in p.parts))
    assert n >= 300, f"公開樹只掃到 {n} 個檔，掃描範圍可能壞了"
