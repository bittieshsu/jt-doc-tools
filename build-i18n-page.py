#!/usr/bin/env python3
"""從中文版介紹站產出英文版（`index.html` → `index-en.html`）。

**為什麼用生成而不是手工維護兩份**：同一份文件放兩個地方一定會漂 —— 這個專案
已經吃過好幾次虧（`github/TEST_PLAN.md` 停在 v1.8.55 少了 182 行、介紹站的工具
數字與卡片對不上）。中文版永遠是**唯一的來源**，英文版每次重新生成。

用法：
    python3 github/build-i18n-page.py --extract   # 抽出待翻字串到 i18n/index.en.json
    python3 github/build-i18n-page.py             # 產生 index-en.html
    python3 github/build-i18n-page.py --check     # 只檢查有沒有漏翻（給守門測試用）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent / "docs"
I18N = DOCS / "i18n"
#: 中文標點也要算 —— 只認漢字的話，`<b>A</b>、<b>B</b>` 中間那個頓號會留在
#: 英文頁上（實測就是這樣漏的）。
CJK = re.compile(r"[㐀-鿿、。：；！？（）「」《》…—]")

#: 這些區段不翻：程式碼、樣式、註解、以及安裝指令那一類原樣照抄的東西。
_SKIP_BLOCK = re.compile(
    r"<(script|style|code|pre)\b.*?</\1>|<!--.*?-->", re.S | re.I)
#: 會翻的屬性（使用者看得到的）。
_ATTRS = ("alt", "title", "placeholder", "aria-label", "content")


def _segments(html: str):
    """回傳 [(是不是屬性, 起, 迄, 原文)]，只收含中文的片段。"""
    holes = [(m.start(), m.end()) for m in _SKIP_BLOCK.finditer(html)]

    def inside(pos: int) -> bool:
        return any(a <= pos < b for a, b in holes)

    out = []
    for m in re.finditer(r">([^<>]+)<", html):
        # **判斷用文字本身的位置，不是 `>` 的位置** —— `</code>中文<` 的那個 `>`
        # 屬於 code 區塊，用它判斷會把後面那段正常的中文一起跳過（第一版就是
        # 這樣漏了 21 段，而且產出的英文頁裡看得到中文才發現）。
        if inside(m.start(1)) or not CJK.search(m.group(1)):
            continue
        out.append((False, m.start(1), m.end(1), m.group(1)))
    for m in re.finditer(r'(%s)="([^"]*)"' % "|".join(_ATTRS), html):
        if inside(m.start()) or not CJK.search(m.group(2)):
            continue
        out.append((True, m.start(2), m.end(2), m.group(2)))
    return sorted(out, key=lambda x: x[1])


def extract(src: Path, cat_path: Path) -> int:
    html = src.read_text(encoding="utf-8")
    cat = json.loads(cat_path.read_text(encoding="utf-8")) if cat_path.exists() else {}
    seen = []
    for _attr, _a, _b, text in _segments(html):
        key = text.strip()
        if key and key not in seen:
            seen.append(key)
    for k in seen:
        cat.setdefault(k, "")
    # 樣板裡已經沒有的條目留著會誤導，直接丟掉
    cat = {k: v for k, v in cat.items() if k in seen}
    cat_path.parent.mkdir(parents=True, exist_ok=True)
    cat_path.write_text(json.dumps(cat, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    todo = [k for k, v in cat.items() if not v]
    print(f"{src.name}: {len(seen)} 條，未翻 {len(todo)}")
    return len(todo)


def build(src: Path, cat_path: Path, dst: Path, lang: str = "en") -> int:
    html = src.read_text(encoding="utf-8")
    cat = json.loads(cat_path.read_text(encoding="utf-8"))
    missing = []
    parts, last = [], 0
    for _attr, a, b, text in _segments(html):
        key = text.strip()
        rep = cat.get(key) or ""
        if not rep:
            missing.append(key)
            continue                      # 沒翻的原樣留著中文（比空白好）
        parts.append(html[last:a])
        parts.append(text.replace(key, rep, 1))
        last = b
    parts.append(html[last:])
    out = "".join(parts)
    out = re.sub(r'<html lang="[^"]*"', f'<html lang="{lang}"', out, count=1)
    # 語言連結要指回中文版（中文頁指向 -en，英文頁指回原檔）
    out = re.sub(
        r'<a href="[^"]*" class="nav-link nav-lang" id="langSwitch"[^>]*>[^<]*</a>',
        f'<a href="{src.name}" class="nav-link nav-lang" id="langSwitch"'
        ' hreflang="zh-Hant" lang="zh-Hant">繁體中文</a>',
        out, count=1)
    dst.write_text(out, encoding="utf-8")
    print(f"{dst.name}: 產生完成（{len(missing)} 條還沒翻，暫時保留中文）")
    return len(missing)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    pages = [("index.html", "index.en.json", "index-en.html"),
             ("api.html", "api.en.json", "api-en.html")]
    rc = 0
    for src, cat, dst in pages:
        s = DOCS / src
        if not s.exists():
            continue
        if a.extract:
            extract(s, I18N / cat)
        elif a.check:
            missing = extract(s, I18N / cat)
            rc |= 1 if missing else 0
        else:
            rc |= 1 if build(s, I18N / cat, DOCS / dst) else 0
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
