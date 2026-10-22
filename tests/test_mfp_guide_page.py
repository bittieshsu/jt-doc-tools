"""介紹站「事務機掃描取件」使用說明頁（`docs/mfp-fetch.html`，v1.17.8；使用者 2026-10-21 要求）。

驗的是「這一頁不會悄悄過期、不會破圖、不會洩漏內網資訊」：
* 產生器重跑一次要跟檔案一模一樣（改了產生器忘了重產會被抓到）。
* 寫在頁面上的「已支援型號」要跟程式裡的已驗證清單一致 —— 工具頁那句說明就是因為寫死而過期過（v1.17.8）。
* 每一張截圖都存在，英日文版指到各自語言那一組；圖都寫明寬高。
* 頁內導覽的每一個錨點都有對應的區塊；首頁第 17 項與頁尾連得到這一頁。
* 頁面與截圖檔名裡沒有內網位址、本機位址或測試埠。
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from tools.repo_paths import public_root

ROOT = Path(__file__).resolve().parents[1]
PUB = public_root(ROOT)
DOCS = PUB / "docs"


def _gen():
    spec = importlib.util.spec_from_file_location("mg", PUB / "build-mfp-guide-page.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_page_is_up_to_date_with_its_generator():
    assert _gen().build() == (DOCS / "mfp-fetch.html").read_text(encoding="utf-8"), \
        "改了 build-mfp-guide-page.py 要重跑它（再跑 build-i18n-page.py）"


def test_verified_models_on_the_page_match_the_code():
    from app.core import mfp_profiles
    html = (DOCS / "mfp-fetch.html").read_text(encoding="utf-8")
    models = [m for ms in mfp_profiles.VERIFIED_OTHER.values() for m in ms]
    models += [f"{fam} {m}" for fam, m in mfp_profiles.VERIFIED_PROFILES]
    missing = [m for m in models if m not in html]
    assert not missing, f"程式裡已驗證、頁面上沒寫：{missing}"


@pytest.mark.parametrize("lang", ["", "-en", "-ja"])
def test_every_screenshot_exists_and_is_localised(lang):
    html = (DOCS / f"mfp-fetch{lang}.html").read_text(encoding="utf-8")
    refs = sorted(set(re.findall(r'src="(screenshots/[\w./-]+\.png)"', html)))
    assert len(refs) >= 8, refs
    for r in refs:
        assert (DOCS / r).is_file(), r
        if lang:
            assert r.startswith(f"screenshots/{lang[1:]}/"), f"{lang} 版還在用中文介面的截圖：{r}"
    for tag in re.findall(r"<img [^>]*>", html):
        assert 'width="' in tag and 'height="' in tag, "圖要寫明寬高（不然跳到下面的章節時版面會被推動）"


def test_page_nav_anchors_exist_and_the_site_links_here():
    html = (DOCS / "mfp-fetch.html").read_text(encoding="utf-8")
    nav = re.search(r'<nav class="cp-pagenav[^"]*".*?</nav>', html, re.S).group(0)
    for sid in re.findall(r'href="#([\w-]+)"', nav):
        assert f'<section id="{sid}"' in html, sid
    index = (DOCS / "index.html").read_text(encoding="utf-8")
    assert 'href="mfp-fetch.html"' in index.split('<div class="ss-num">17</div>', 1)[1].split("</figure>", 1)[0], \
        "首頁第 17 項（事務機掃描取件）要連到使用說明"
    for page in ("index.html", "troubleshooting.html", "compliance.html"):
        assert 'href="mfp-fetch.html"' in (DOCS / page).read_text(encoding="utf-8"), f"{page} 頁尾要有連結"
    assert 'href="mfp-fetch-en.html"' in (DOCS / "index-en.html").read_text(encoding="utf-8"), "英文首頁要連到英文版"


@pytest.mark.parametrize("lang", ["", "-en", "-ja"])
def test_no_internal_addresses_on_the_page(lang):
    html = (DOCS / f"mfp-fetch{lang}.html").read_text(encoding="utf-8")
    hits = re.findall(r"\b(?:127\.0\.0\.\d+|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)\b", html)
    assert not hits, hits


def test_capture_tool_rewrites_local_addresses_before_shooting():
    """截圖是公開的：拍之前把本機位址、測試埠換成範例主機名稱與 443 / 80（換法寫在截圖工具裡）。"""
    src = (ROOT / "tools/capture_mfp_guide.py").read_text(encoding="utf-8")
    # 比對的是原始碼裡有沒有寫這幾段（不是檢查網址），用 re.search 寫
    assert re.search(r"example\.com", src) and "127\\\\.0\\\\.0\\\\." in src and "HTTPS?" in src
    assert "_shoot(" in src and src.count("t.js(rewrite)") == 1


def test_the_guide_link_on_the_dark_gallery_is_readable():
    """首頁截圖區是深色底：連結不可以用主色靛藍字（使用者 2026-10-22：字看不清楚）。"""
    css = (DOCS / "style.css").read_text(encoding="utf-8")
    rule = re.search(r"\.ss-more a\s*\{([^}]*)\}", css).group(1)
    color = re.search(r"(?<![-\w])color:\s*([^;]+);", rule).group(1).strip()
    assert "indigo" not in color and color.lower() in ("#e0e7ff", "#fff", "#ffffff", "#eef2ff", "#c7d2fe"), color
