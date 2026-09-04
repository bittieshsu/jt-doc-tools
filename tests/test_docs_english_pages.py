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
