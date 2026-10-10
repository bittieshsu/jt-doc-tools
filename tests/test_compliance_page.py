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
    # 分隔列（`|---|---|`）一個表格一列；直接數 `|---|` 的話三欄的表會算成兩個
    assert page.count('<table class="cp-table">') == len(re.findall(r"^\|(?:\s*-+\s*\|)+\s*$", md, re.M))


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


# ---- 條文與控制項對照（2026-10-10 使用者：「合規應該要順便寫對應到 27001 42001 的那些條文跟那些控制項」）----
#
# 判準：①每一個編號都是那一份標準裡真的有的條文或控制項（打錯一碼，稽核員一查就知道）；
# ②各節開頭的標籤與對照表互相對得上：某一節標了 A.5.16，對照表 A.5.16 那一列的
# 「本文件章節」就要寫那一節，反過來也一樣；③網頁上每個標籤都連得到那一列。

#: ISO/IEC 27001:2022 附錄 A：5.1–5.37、6.1–6.8、7.1–7.14、8.1–8.34
A27 = ({f"A.5.{i}" for i in range(1, 38)} | {f"A.6.{i}" for i in range(1, 9)}
       | {f"A.7.{i}" for i in range(1, 15)} | {f"A.8.{i}" for i in range(1, 35)})
#: ISO/IEC 42001:2023 附錄 A（38 項）
A42 = {"A.2.2", "A.2.3", "A.2.4", "A.3.2", "A.3.3", "A.4.2", "A.4.3", "A.4.4", "A.4.5", "A.4.6",
       "A.5.2", "A.5.3", "A.5.4", "A.5.5", "A.6.1.2", "A.6.1.3", "A.6.2.2", "A.6.2.3", "A.6.2.4",
       "A.6.2.5", "A.6.2.6", "A.6.2.7", "A.6.2.8", "A.7.2", "A.7.3", "A.7.4", "A.7.5", "A.7.6",
       "A.8.2", "A.8.3", "A.8.4", "A.8.5", "A.9.2", "A.9.3", "A.9.4", "A.10.2", "A.10.3", "A.10.4"}
#: 兩份標準本文的條文（兩份都是 Annex SL 架構；6.1.4 只有 42001 有）
_CL = ({f"4.{i}" for i in range(1, 5)} | {f"5.{i}" for i in range(1, 4)}
       | {"6.1", "6.1.1", "6.1.2", "6.1.3", "6.2", "6.3"} | {f"7.{i}" for i in range(1, 6)}
       | {"7.5.1", "7.5.2", "7.5.3", "9.1", "9.2", "9.2.1", "9.2.2", "9.3", "9.3.1", "9.3.2", "9.3.3",
          "10.1", "10.2"})
C27 = _CL | {"8.1", "8.2", "8.3"}
C42 = _CL | {"6.1.4", "8.1", "8.2", "8.3", "8.4"}
VALID = {"27": A27 | C27, "42": A42 | C42}
_CODE = re.compile(r"(?:A\.)?\d+(?:\.\d+)+")


def _parse():
    """回 (chips, rows)：chips＝{(標準, 節名): {編號}}；rows＝{(標準, 編號): {節名}}。"""
    gen = _gen()
    md = (PUB / "COMPLIANCE.md").read_text(encoding="utf-8")
    chips, rows, std, sec, in_map = {}, {}, None, None, False
    for ln in md.split("\n"):
        if ln.startswith("## "):
            std, in_map = gen._std_of(ln[3:]), gen._head_key(ln[3:]) == "條文與控制項對照"
        elif ln.startswith("### "):
            sec = gen._head_key(ln[4:])
            if in_map:
                std = gen._std_of(ln[4:])
        elif (m := gen._MAP_LINE.match(ln)) and std:
            chips.setdefault((std, sec), set()).update(c.strip() for c in m.group(2).split("、"))
        elif in_map and std and ln.startswith("|") and not re.match(r"^\|(?:\s*-+\s*\|)+", ln):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            code = gen._CODE.match(cells[0])
            if code:
                rows[(std, code.group(1))] = {s.strip() for s in cells[-1].split("、")}
    return chips, rows


def test_the_mapping_is_there_for_both_standards():
    chips, rows = _parse()
    for std in ("27", "42"):
        assert sum(1 for s, _c in rows if s == std) >= 10, f"{std}001 的對照表幾乎是空的"
        assert any(s == std for s, _sec in chips), f"{std}001 的各節沒有標對應"


def test_every_number_is_a_real_clause_or_control_of_that_standard():
    md = (PUB / "COMPLIANCE.md").read_text(encoding="utf-8")
    chips, rows = _parse()
    bad = [f"{std}001 {c}" for (std, _s), cs in chips.items() for c in cs if c not in VALID[std]]
    bad += [f"{std}001 {c}" for (std, c) in rows if c not in VALID[std]]
    # 「導入單位的做法」那幾句寫在文字裡的編號（42001 那一大節）
    part = md[md.index("## 二、"):md.index("## 三、")]
    bad += [f"42001 {c}（內文）" for c in _CODE.findall(part) if c.startswith("A.") and c not in A42]
    assert not bad, f"這些編號在標準裡不存在：{bad}"


def test_section_labels_and_mapping_rows_point_to_each_other():
    chips, rows = _parse()
    problems = []
    for (std, sec), codes in chips.items():
        for c in codes:
            if (std, c) not in rows:
                problems.append(f"「{sec}」標了 {c}，對照表沒有那一列")
            elif sec not in rows[(std, c)]:
                problems.append(f"「{sec}」標了 {c}，但對照表那一列的章節沒寫它")
    for (std, c), secs in rows.items():
        for sec in secs:
            if c not in chips.get((std, sec), set()):
                problems.append(f"對照表 {c} 寫了「{sec}」，那一節卻沒有標 {c}")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("name", ["compliance", "compliance-en", "compliance-ja"])
def test_every_label_links_to_its_row(name):
    page = (DOCS / f"{name}.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([^"]+)"', page))
    hrefs = re.findall(r'class="cp-ctl" href="#([^"]+)"', page)
    assert len(hrefs) >= 40, len(hrefs)
    assert not [h for h in hrefs if h not in ids], "有對應標籤連不到對照表"


def test_screenshots_reserve_their_space():
    """沒寫寬高的話，從頁首膠囊或對應標籤跳到下面某一節時，上方延遲載入的截圖載進來會把整頁往下推，
    停在錯的那一節（做這一版時截圖就是這樣跳掉的）。"""
    page = (DOCS / "compliance.html").read_text(encoding="utf-8")
    imgs = re.findall(r"<img [^>]*>", page[page.index("<main"):])
    assert imgs and all(re.search(r'width="\d+" height="\d+"', i) for i in imgs), imgs


def test_the_english_page_uses_the_official_control_names():
    page = (DOCS / "compliance-en.html").read_text(encoding="utf-8")
    for name in ("A.8.15 Logging", "A.5.15 Access control", "A.6.2.8 AI system recording of event logs",
                 "9.2 Internal audit"):
        assert name in page, name


# ---- 標準一律寫出版本（2026-10-10 使用者：「iso 有版本，例如 iso 27001:2022，你要標上」）----

_EDITION_FILES = ("COMPLIANCE.md", "README.md", "README_en.md", "README_ja.md", "AUTH.md")


def _edition_sources():
    out = [PUB / f for f in _EDITION_FILES if (PUB / f).exists()]
    out += sorted((PUB / "docs").glob("*.html"))
    return out


def test_the_edition_scan_reaches_the_pages():
    names = {p.name for p in _edition_sources()}
    assert {"COMPLIANCE.md", "README.md", "compliance.html", "compliance-en.html",
            "compliance-ja.html", "index.html"} <= names, names


@pytest.mark.parametrize("path", _edition_sources(), ids=lambda p: p.name)
def test_every_mention_of_the_standards_names_the_edition(path):
    """ISO/IEC 27001 與 42001 都有好幾版（27001:2013 / 27001:2022），條文與附錄 A 的編號在不同版本完全不同 ——
    不寫版本的話，讀的人拿去對另一版的控制項清單會對不起來。更新記錄是歷史，不在範圍內。"""
    text = path.read_text(encoding="utf-8")
    bad = [m.group(0) for m in re.finditer(r"\b(27001|42001)\b(?!:20\d\d)", text)]
    assert not bad, f"{path.name} 有 {len(bad)} 處沒寫版本：{bad[:5]}"
