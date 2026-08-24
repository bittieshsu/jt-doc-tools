"""本專案宣告的授權必須處處一致（v1.14.48 起改為 AGPL-3.0-or-later）。

**為什麼要有這支**：授權宣告散在七個地方（LICENSE 全文、pyproject、README
徽章與授權段、介紹站三處、API 手冊頁尾、第三方聲明裡的那句對照）。這正是本
專案反覆出事的形狀 —— 同一件事寫在多處，改了其中幾處就上線，而**沒改到的那
一處就是對外的法律宣告**。

改授權的原因：核心 PDF 引擎 PyMuPDF 本身是 AGPL（Artifex 雙授權），本程式在
同一個行程內使用它，原本宣告 Apache-2.0 站不住。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GH = ROOT / "github"

#: 單一事實來源 —— 要換授權就改這裡，測試會指出所有沒跟上的檔案。
SPDX = "AGPL-3.0-or-later"
LICENSE_TITLE = "GNU AFFERO GENERAL PUBLIC LICENSE"
HUMAN_NAME = "GNU Affero General Public License v3.0"


def test_license_file_is_the_agpl_full_text():
    txt = (GH / "LICENSE").read_text(encoding="utf-8")
    assert LICENSE_TITLE in txt.split("\n", 3)[0] + txt.split("\n", 3)[1], \
        "LICENSE 開頭不是 AGPL 標題"
    # 第 13 條是 AGPL 與 GPL 的差別所在，必須在（截斷的授權文字是常見錯誤）
    assert "13. Remote Network Interaction" in txt
    assert len(txt.splitlines()) > 600, "LICENSE 看起來被截斷了"


def test_pyproject_declares_spdx():
    t = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(rf'^license\s*=\s*"{re.escape(SPDX)}"', t, re.M), \
        f"pyproject.toml 的 license 不是 {SPDX}"


def test_readme_badge_and_section():
    t = (GH / "README.md").read_text(encoding="utf-8")
    assert "License-AGPL" in t, "README 徽章還是舊授權"
    assert "AGPL-3.0-or-later" in t, "README 授權段沒有寫明 SPDX"
    assert "Apache License 2.0 — 詳見" not in t


def test_landing_site_has_no_stale_apache_claim():
    """介紹站上關於**本專案**的授權宣告不可殘留 Apache。

    第三方（LibreOffice / OxOffice…）的授權敘述不在此限，所以逐行判斷。
    """
    t = (GH / "docs" / "index.html").read_text(encoding="utf-8")
    bad = [ln.strip() for ln in t.splitlines()
           if "Apache" in ln
           and not re.search(r"OxOffice|LibreOffice|HAProxy|反向代理|nginx", ln)]
    assert not bad, f"介紹站仍宣告 Apache：{bad[:3]}"
    assert HUMAN_NAME in t, "介紹站沒有寫出新的授權名稱"


def test_api_page_footer_matches_generator():
    """API 手冊是生成的 —— 生成器與產出必須一致（改了沒重跑就會漂）。"""
    gen = (GH / "build-api-page.py").read_text(encoding="utf-8")
    page = (GH / "docs" / "api.html").read_text(encoding="utf-8")
    assert HUMAN_NAME in gen, "生成器裡的授權沒更新"
    assert HUMAN_NAME in page, "api.html 沒有重新生成（跑 build-api-page.py）"
    assert "Apache License 2.0" not in page


def test_third_party_notice_no_longer_claims_apache_for_us():
    t = (GH / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    assert "本專案以 Apache-2.0 釋出" not in t
    assert "本專案同樣以 AGPL-3.0-or-later 釋出" in t


def test_ui_offers_source_to_network_users():
    """AGPL 第 13 條：透過網路使用本程式的人要拿得到原始碼。

    慣例作法就是介面上放一個看得見的原始碼連結（AGPL 全文最後一段自己就是
    這樣建議的）。放在側欄 = 每一頁都有。
    """
    tpl = (ROOT / "app" / "web" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "brand-src" in tpl, "介面上沒有原始碼連結"
    assert "github.com/jasoncheng7115/jt-doc-tools" in tpl
    css = (ROOT / "static" / "css" / "platform.css").read_text(encoding="utf-8")
    assert ".sidebar .brand-src" in css, "原始碼連結沒有樣式（會是一坨沒排版的文字）"
    # 登入頁**不繼承側欄樣板**，要各自放一份 —— 未登入的訪客同樣是第 13 條
    # 講的「透過網路與軟體互動的使用者」（實機部署後才發現漏這一頁）。
    login = (ROOT / "app" / "web" / "templates" / "login.html").read_text(encoding="utf-8")
    assert "brand-src" in login, "登入頁沒有原始碼連結"
