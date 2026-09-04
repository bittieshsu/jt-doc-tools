"""介紹站與 API 手冊的英文版（GitHub Pages）。

**為什麼是「生成」而不是手工維護兩份**：同一份文件放兩個地方一定會漂 ——
這個專案已經吃過好幾次虧（`github/TEST_PLAN.md` 停在 v1.8.55 少了 182 行、
介紹站的工具數字與卡片對不上）。中文版永遠是唯一的來源，英文版由
`github/build-i18n-page.py` 重新生成。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parent.parent / "github" / "docs"
PAGES = (("index.html", "index.en.json", "index-en.html"),
         ("api.html", "api.en.json", "api-en.html"))
CJK = re.compile(r"[㐀-鿿]")
_STRIP = re.compile(
    r"<(script|style|code|pre)\b.*?</\1>|<!--.*?-->", re.S | re.I)


@pytest.mark.parametrize("src,cat,dst", PAGES)
def test_english_page_exists_and_has_no_chinese_left(src: str, cat: str, dst: str):
    p = DOCS / dst
    assert p.exists(), f"缺英文版：{dst}（跑 python3 github/build-i18n-page.py）"
    html = p.read_text(encoding="utf-8")
    assert re.search(r'<html lang="en"', html), "英文版的 lang 要是 en"
    body = _STRIP.sub(" ", html)
    left = {m.group(1).strip() for m in re.finditer(r">([^<>]+)<", body)
            if CJK.search(m.group(1))}
    # 語言切換那顆按鈕本來就要寫中文（它是「切回中文」的入口）
    left.discard("繁體中文")
    assert left == set(), f"{dst} 還有中文沒翻：{sorted(left)[:5]}"


@pytest.mark.parametrize("src,cat,dst", PAGES)
def test_catalog_has_no_empty_translation(src: str, cat: str, dst: str):
    data = json.loads((DOCS / "i18n" / cat).read_text(encoding="utf-8"))
    todo = sorted(k for k, v in data.items() if not v)
    assert todo == [], f"{cat} 還有 {len(todo)} 條沒翻：{todo[:5]}"


@pytest.mark.parametrize("src,cat,dst", PAGES)
def test_language_link_points_both_ways(src: str, cat: str, dst: str):
    """兩邊都要有語言連結，而且方向相反 —— 只有單向的話英文使用者回不去。"""
    zh = (DOCS / src).read_text(encoding="utf-8")
    en = (DOCS / dst).read_text(encoding="utf-8")
    assert f'href="{dst}"' in zh, f"{src} 少了往英文版的連結"
    assert f'href="{src}"' in en, f"{dst} 少了回中文版的連結"


# ---------------------------------------------------------------------------
# README / CHANGELOG 的英文版
#
# 使用者定案（2026-09-04）：中文版維持原檔名不動，英文版另立
# `README_en.md` / `CHANGELOG_en.md`，兩邊最上面各放一條語言切換。
#
# README_en 同樣是**生成**的（`github/build-i18n-md.py` 逐行對照
# `docs/i18n/readme.en.json`）；CHANGELOG_en 則是**手寫的摘要版** ——
# 中文版有 724 個版本、6 千多行，全譯沒有意義也維護不起來，
# 它自己在開頭就寫明「完整歷史看中文版」。所以這裡只驗它存在、
# 沒有殘留中文、而且真的指回中文版。
# ---------------------------------------------------------------------------

GH = DOCS.parent
_FENCE = re.compile(r"```.*?```", re.S)


def _chinese_lines(md: str) -> list[str]:
    return [ln for ln in _FENCE.sub(" ", md).splitlines()
            if CJK.search(ln) and "繁體中文" not in ln]


def test_readme_english_version_is_generated_and_complete():
    p = GH / "README_en.md"
    assert p.exists(), "缺 README_en.md（跑 python3 github/build-i18n-md.py）"
    left = _chinese_lines(p.read_text(encoding="utf-8"))
    assert left == [], f"README_en.md 還有中文沒翻：{left[:3]}"


def test_changelog_english_version_exists_and_has_no_chinese_left():
    p = GH / "CHANGELOG_en.md"
    assert p.exists(), "缺 CHANGELOG_en.md"
    left = _chinese_lines(p.read_text(encoding="utf-8"))
    assert left == [], f"CHANGELOG_en.md 還有中文沒翻：{left[:3]}"


@pytest.mark.parametrize("zh,en", [("README.md", "README_en.md"),
                                   ("CHANGELOG.md", "CHANGELOG_en.md")])
def test_markdown_language_switch_points_both_ways(zh: str, en: str):
    zh_head = (GH / zh).read_text(encoding="utf-8").splitlines()[0]
    en_head = (GH / en).read_text(encoding="utf-8").splitlines()[0]
    assert f"({en})" in zh_head, f"{zh} 第一行要有連到 {en} 的語言切換"
    assert f"({zh})" in en_head, f"{en} 第一行要有連回 {zh} 的語言切換"
