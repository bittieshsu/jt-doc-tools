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
#: **純標點的片段不收**。中文標點也算「要翻」是為了 `<b>A</b>、<b>B</b>` 中間那個
#: 頓號，但整段只有標點時（表格裡孤零零一個 `—`、一個 `）`）那不是句子 ——
#: 收進去只會變成一個誰都對得上的鍵，然後把**別處的譯文**貼到那個標點的位置。
#: 2026-09-04 使用者從英文版介紹站截圖回報：表格欄位變成「; JSON:」、
#: 免責聲明每一條都以逗號開頭，根因就是這個。
_HAS_WORD = re.compile(r"[㐀-鿿A-Za-z0-9]")

#: 這些區段不翻：程式碼、樣式、註解、以及安裝指令那一類原樣照抄的東西。
_SKIP_BLOCK = re.compile(
    r"<(script|style|code|pre)\b.*?</\1>|<!--.*?-->", re.S | re.I)
#: 會翻的屬性（使用者看得到的）。
_ATTRS = ("alt", "title", "placeholder", "aria-label", "content")


#: 整塊一起翻的區塊標籤。**句子被行內標籤切開時，逐段翻一定會壞** ——
#: 中文照原順序接起來剛好通順，英文語序不同，接出來就是
#: 「, including but not limited to…」這種以逗號開頭的碎片
#: （2026-09-04 使用者從英文版介紹站截圖回報）。
_BLOCK = re.compile(
    r"<(p|li|h1|h2|h3|h4|h5|h6|td|th|summary|figcaption|button|label|blockquote)"
    r"(\s[^>]*)?>(.*?)</\1>", re.S | re.I)
#: 區塊裡只有這些行內標籤時才整塊收；出現別的區塊標籤就退回逐段。
_INLINE_ONLY = re.compile(
    r"</?(b|strong|i|em|u|s|a|code|span|br|small|kbd|sup|sub|abbr|mark)\b[^>]*>",
    re.I)
_HAS_TAG = re.compile(r"<[^>]+>")


def _block_segments(html: str, inside) -> list:
    """含行內標籤的整塊 —— 標籤留在字串裡，讓譯者自己擺到英文該在的位置。"""
    out = []
    for m in _BLOCK.finditer(html):
        inner = m.group(3)
        a, b = m.start(3), m.end(3)
        if inside(a) or not CJK.search(inner) or not _HAS_WORD.search(inner):
            continue
        if not _HAS_TAG.search(inner):
            continue                      # 純文字的走原本的逐段路徑就好
        if _INLINE_ONLY.sub("", inner).find("<") >= 0:
            continue                      # 裡面還有別的區塊，整塊收會太大
        out.append((False, a, b, inner))
    return out


#: 中文不用空格，英文要。`…的<b>公司內部文件</b>，…` 翻成英文之後會變成
#: `is<b>downloaded from GitHub</b>, so` —— 字黏在一起（2026-09-05 使用者回報）。
#: 這是**語言層級**的規則，所以放在產生英文頁的最後一步，而不是逐條去改譯文。
_INLINE_TAGS = "b|strong|i|em|code|span|a|kbd|small|u|mark"


def _space_around_inline_tags(html: str) -> str:
    """英文頁：行內標記與前後英數字之間補一個空白。"""
    # 「字母 + <b>」→ 「字母 + 空白 + <b>」
    html = re.sub(r"(?<=[A-Za-z0-9,.)])(<(?:" + _INLINE_TAGS + r")[ >])",
                  r" \1", html)
    # 「</b> + 字母」→ 「</b> + 空白 + 字母」
    html = re.sub(r"(</(?:" + _INLINE_TAGS + r")>)(?=[A-Za-z0-9(])",
                  r"\1 ", html)
    return html


#: 安裝指令是**程式碼**，抽字串那條路不會碰它 —— 但裡面的錯誤訊息是給人看的，
#: 英文版留著中文很怪（2026-09-05 使用者截圖回報）。這裡逐字換掉那幾句。
_CMD_ZH_TO_EN = {
    "[X] 下載安裝腳本失敗：": "[X] Could not download the install script: ",
    "請檢查網路（VPN？防火牆？DNS？）後重試。":
        "Check the network (VPN? firewall? DNS?) and try again.",
    "按 Enter 關閉": "Press Enter to close",
}


def _english_install_command(html: str) -> str:
    """把安裝指令裡給人看的中文訊息換成英文。"""
    for zh, en in _CMD_ZH_TO_EN.items():
        html = html.replace(zh, en)
    return html


def _english_screenshots(html: str, docs_dir) -> str:
    """英文版指到英文介面的截圖（`screenshots/en/…`）。

    讀者看到的畫面要跟他實際會看到的一樣 —— 截圖正是「有沒有真的支援英文」
    最直接的證據。**只有在英文版那張真的存在時才換**，所以還沒補英文截圖的
    工具會繼續沿用中文版那張，不會變成破圖。
    """
    def rep(m):
        name = m.group(1)
        return (f"screenshots/en/{name}"
                if (docs_dir / "screenshots" / "en" / name).is_file()
                else m.group(0))
    return re.sub(r"screenshots/([\w.-]+\.png)", rep, html)


def _segments(html: str):
    """回傳 [(是不是屬性, 起, 迄, 原文)]，只收含中文的片段。

    先收「含行內標籤的整塊」，再收剩下的純文字節點與屬性 ——
    已經被整塊收走的範圍不再重複收。
    """
    holes = [(m.start(), m.end()) for m in _SKIP_BLOCK.finditer(html)]

    def inside(pos: int) -> bool:
        return any(a <= pos < b for a, b in holes)

    blocks = _block_segments(html, inside)
    taken = [(a, b) for _f, a, b, _t in blocks]

    def covered(pos: int) -> bool:
        return any(a <= pos < b for a, b in taken)

    out = list(blocks)
    for m in re.finditer(r">([^<>]+)<", html):
        # **判斷用文字本身的位置，不是 `>` 的位置** —— `</code>中文<` 的那個 `>`
        # 屬於 code 區塊，用它判斷會把後面那段正常的中文一起跳過（第一版就是
        # 這樣漏了 21 段，而且產出的英文頁裡看得到中文才發現）。
        if (inside(m.start(1)) or covered(m.start(1))
                or not CJK.search(m.group(1)) or not _HAS_WORD.search(m.group(1))):
            continue
        out.append((False, m.start(1), m.end(1), m.group(1)))
    for m in re.finditer(r'(%s)="([^"]*)"' % "|".join(_ATTRS), html):
        if (inside(m.start()) or not CJK.search(m.group(2))
                or not _HAS_WORD.search(m.group(2))):
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
    out = _space_around_inline_tags(out)
    out = _english_install_command(out)
    out = _english_screenshots(out, dst.parent)
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
