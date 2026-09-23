"""IIS 反向代理的安裝順序：**URL Rewrite 要先裝，ARR 後裝。**

由來（2026-09-10，客戶回報）：文件原本寫「先裝 ARR + URL Rewrite」。
**ARR 相依於 URL Rewrite** —— 反過來裝的話 ARR 裝不起來，或是裝完之後
「Server Proxy Settings」根本出不來。

以前用 Web Platform Installer 裝 ARR 會自動帶 URL Rewrite，所以順序看不出
差別；WebPI 退役之後大家手動下載 MSI，順序就變成會踩到的坑。

**這種錯誤沒有任何自動化抓得到** —— 程式完全正確，只有照著文件做的人會卡住。
所以用字面檢查釘死。
"""
from __future__ import annotations

from pathlib import Path

import pytest

# **不可以寫死 `github/`**：開發樹的公開檔在 `github/` 底下，標準 clone 下來
# 就在根目錄 —— 寫死的話在 clone 上永遠 FileNotFoundError，而開發機上一直
# 是綠的。這條 CLAUDE.md 記過好幾次，這支測試（v1.15.24 寫的）還是踩了，
# 而且是 **CI 在 main 上跑才現形**（2026-09-13）。
from tools.repo_paths import public_root  # noqa: E402

OPS = public_root(Path(__file__).resolve().parents[1]) / "OPS.md"


@pytest.fixture(scope="module")
def iis_section() -> str:
    text = OPS.read_text(encoding="utf-8")
    start = text.find("### IIS")
    assert start >= 0, "OPS.md 裡找不到 IIS 那一節"
    end = text.find("\n### ", start + 1)
    return text[start:end if end > 0 else len(text)]


def test_url_rewrite_is_listed_before_arr(iis_section: str):
    """**順序就是重點**：兩個都提到不夠，要照正確的先後出現。"""
    i_rewrite = iis_section.find("URL Rewrite")
    i_arr = iis_section.find("Application Request Routing")
    assert i_rewrite >= 0 and i_arr >= 0, "兩個元件都要提到"
    assert i_rewrite < i_arr, (
        "IIS 那一節把 ARR 寫在 URL Rewrite 前面了 —— ARR 相依於 URL Rewrite，"
        "反過來裝會裝不起來（客戶回報過）")


def test_the_dependency_is_stated_not_just_implied(iis_section: str):
    """光是換順序不夠 —— 讀的人要知道**為什麼**，才不會自己調回去。"""
    assert "相依" in iis_section or "需要" in iis_section, \
        "沒有說明 ARR 相依於 URL Rewrite"
    assert "順序" in iis_section, "沒有明講順序不能顛倒"


def test_the_section_heading_matches_the_order(iis_section: str):
    """標題也要照順序 —— 那是使用者第一眼看到的東西。"""
    heading = iis_section.splitlines()[0]
    assert heading.index("URL Rewrite") < heading.index("ARR"), heading
