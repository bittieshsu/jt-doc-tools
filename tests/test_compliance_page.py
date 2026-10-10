"""合規支援頁（2026-10-09 使用者：ISO 27001 / 42001 的說明「寫進 .md 跟 pages 裡，pages 放獨立一頁，
主頁做連結」「做不到的不要寫」）。

* 內容只有 `COMPLIANCE.md` 一份；`docs/compliance.html` 由 `build-compliance-page.py` 產生，
  不可以手改（改了 md 沒重跑產生器的話，網頁會停在舊內容）。
* 主頁要有連結（稽核那一節的說明框），每一頁的頁尾也要有。
* 英文與日文版都要產生、沒有漏翻。
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))
from tools.repo_paths import public_root  # noqa: E402

PUB = public_root(ROOT)
DOCS = PUB / "docs"


def _gen():
    spec = importlib.util.spec_from_file_location("bcp", PUB / "build-compliance-page.py")
    if spec is None or not (PUB / "build-compliance-page.py").exists():
        pytest.skip("公開樹沒有產生器")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_page_is_generated_from_the_markdown():
    assert (DOCS / "compliance.html").read_text(encoding="utf-8") == _gen().build(), \
        "docs/compliance.html 跟 COMPLIANCE.md 對不上：改了 md 要重跑 build-compliance-page.py"


def test_every_section_and_table_of_the_markdown_is_on_the_page():
    md = (PUB / "COMPLIANCE.md").read_text(encoding="utf-8")
    page = (DOCS / "compliance.html").read_text(encoding="utf-8")
    heads = re.findall(r"^#{2,3} (.+)$", md, re.M)
    assert len(heads) >= 15, heads
    for h in heads:
        assert re.sub(r"`", "", h) in re.sub(r"<[^>]+>", "", page), h
    assert page.count('<table class="cp-table">') == md.count("|---|")


def test_the_main_page_and_every_footer_link_to_it():
    idx = (DOCS / "index.html").read_text(encoding="utf-8")
    assert 'class="cp-cta"' in idx and 'href="compliance.html"' in idx
    for name in ("index", "api", "troubleshooting", "compliance"):
        t = (DOCS / f"{name}.html").read_text(encoding="utf-8")
        foot = t[t.index('<footer class="footer">'):]
        assert 'href="compliance.html"' in foot, f"{name}.html 的頁尾沒有連到合規支援頁"
    assert "COMPLIANCE.md" in (PUB / "README.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("lang", ["en", "ja"])
def test_translated_pages_exist_and_link_to_their_own_language(lang):
    t = (DOCS / f"compliance-{lang}.html").read_text(encoding="utf-8")
    assert f'href="index-{lang}.html' in t, "導覽要連到同語言的首頁"
    body = re.sub(r"<[^>]+>", " ", t[t.index("<main"):t.index("</main>")])
    if lang == "en":
        assert not re.search(r"[㐀-鿿]", body), "英文頁還有中文"


def test_the_page_has_a_hero_picture_an_icon_per_heading_and_screenshots():
    """2026-10-09 使用者：「別忘了 pages 要有 icon 跟配圖」。"""
    gen = _gen()
    md = (PUB / "COMPLIANCE.md").read_text(encoding="utf-8")
    page = (DOCS / "compliance.html").read_text(encoding="utf-8")
    assert 'class="cp-hero-art"' in page, "頁首沒有插圖"
    heads = re.findall(r"^#{2,3} (.+)$", md, re.M)
    assert page.count('class="cp-h-ic"') == len(heads), "每一個大節、小節標題都要有圖示"
    figs = re.findall(r'<img src="screenshots/([^"]+)"', page)
    assert len(figs) >= 3, figs
    for lang in ("", "en/", "ja/"):
        for f in figs:
            assert (DOCS / "screenshots" / f"{lang}{f}").is_file(), f"screenshots/{lang}{f} 不見了"
    assert set(gen.FIGS) <= {gen._head_key(h) for h in heads}, "截圖對應的小節標題改名了"
