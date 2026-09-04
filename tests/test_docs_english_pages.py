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


# ---------------------------------------------------------------------------
# 譯文對不對得上原文（2026-09-04 使用者從英文版介紹站截圖回報）
#
# 症狀是免責聲明每一條都以逗號開頭、標題變成別段的句子：
#
#     • , including but not limited to merchantability…
#     <div class="section-eyebrow">, with three common pitfalls marked.</div>
#
# 兩個根因：
#   1. **句子被行內標籤切開後逐段翻**。中文照原順序接起來剛好通順，英文語序
#      不同，接出來就是碎片。改成「含行內標籤的整塊一起翻」（標籤留在字串裡）。
#   2. **語系檔的譯文對錯了鍵**。早期用索引合併譯文，清單順序一變就整段錯位，
#      而既有守門只驗「有沒有殘留中文」—— 錯位之後一個中文字都沒有，全綠。
#
# 所以判準要驗「**這條譯文是不是這條原文的譯文**」，不能只驗「有沒有翻」。
# 下面三條都是**字面可判定**的，不猜語意。
# ---------------------------------------------------------------------------

_TAG = re.compile(r"<(/?)(\w+)")
_HREF = re.compile(r'href="([^"]+)"')
_LEAD_PUNCT = set(",;.:)]，。；：、）」")
_HAS_WORD = re.compile(r"[㐀-鿿A-Za-z0-9]")


def _shape(s: str):
    """行內標籤與連結目標 —— 譯文一定要跟原文一模一樣。

    `<br>` 例外：中文版有些地方是「英文原句 <br> 中文轉述」，英文版把重複的
    那半句拿掉是對的，換行也跟著少一個。
    """
    tags = sorted(m.group(0) for m in _TAG.finditer(s)
                  if m.group(2).lower() != "br")
    return (tags, sorted(_HREF.findall(s)))


@pytest.mark.parametrize("cat", ["index.en.json", "api.en.json", "readme.en.json"])
def test_translation_keeps_the_same_inline_tags_and_links(cat: str):
    data = json.loads((DOCS / "i18n" / cat).read_text(encoding="utf-8"))
    bad = [k for k, v in data.items() if v and _shape(k) != _shape(v)]
    assert not bad, (
        "這些譯文的行內標籤 / 連結跟原文對不上（多半是譯到一半被截斷，"
        "或整條貼錯鍵）：\n  " + "\n  ".join(k[:60] for k in bad[:5]))


_BLOCK_RE = re.compile(
    # div 也要收：`免責聲明` 那條錯位就發生在 `<div class="section-eyebrow">`，
    # 只看 p / li / h* 的話變異驗證是綠的（實測過）。巢狀 div 用非貪婪比對會
    # 切在奇怪的地方，但**中英兩邊用同一套規則**，位置仍然對得起來。
    r"<(p|li|h1|h2|h3|h4|h5|h6|td|th|div)(?:\s[^>]*)?>(.*?)</\1>", re.S | re.I)
_INLINE = re.compile(r"<[^>]+>")


def _block_heads(html: str) -> list[str]:
    """每個區塊的第一個可見字元（行內標籤先拿掉，兩邊用同一套規則）。"""
    out = []
    for m in _BLOCK_RE.finditer(html):
        t = _INLINE.sub(" ", m.group(2)).replace("&nbsp;", " ").strip()
        out.append(t[0] if t else "")
    return out


@pytest.mark.parametrize("src,dst", [("index.html", "index-en.html"),
                                     ("api.html", "api-en.html")])
def test_no_block_starts_with_punctuation_unless_the_chinese_one_does(src: str, dst: str):
    """英文頁的每個區塊，開頭標點要跟中文頁一致。

    **判準放在產出的頁面上，不放在語系檔**：語系檔裡「以標點開頭」有時候是對的
    （原文本來就是被 `<code>` 切斷的句子中段），逐條猜會誤報。逐區塊比對中英兩份
    產出則是位置對位置，精確 —— 使用者截圖看到的正是這個症狀：
    「, including but not limited to…」整條以逗號開頭。
    """
    zh = _block_heads((DOCS / src).read_text(encoding="utf-8"))
    en = _block_heads((DOCS / dst).read_text(encoding="utf-8"))
    assert len(zh) == len(en), f"{dst} 的區塊數與中文版不同（{len(en)} vs {len(zh)}）"
    bad = [i for i, (a, b) in enumerate(zip(zh, en))
           if b in _LEAD_PUNCT and a not in _LEAD_PUNCT]
    assert not bad, (
        f"{dst} 有 {len(bad)} 個區塊以標點開頭、但中文版不是 —— "
        f"譯文貼錯鍵或句子被切碎了（第 {bad[:5]} 個區塊）")


@pytest.mark.parametrize("cat", ["index.en.json", "api.en.json", "readme.en.json"])
def test_no_pure_punctuation_keys(cat: str):
    """整段只有標點的片段不可以進語系檔。

    表格裡孤零零一個 `—`、一個 `）` 不是句子 —— 收進去就變成一個到處都對得上
    的鍵，然後把別處的譯文貼到那個標點的位置（實際發生過：表格欄位變成
    「; JSON:」）。
    """
    data = json.loads((DOCS / "i18n" / cat).read_text(encoding="utf-8"))
    bad = [k for k in data if not _HAS_WORD.search(k)]
    assert not bad, f"純標點的鍵：{bad}"
