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

import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
from tools.repo_paths import public_root as _public_root

DOCS = _public_root(Path(__file__).resolve().parent.parent) / "docs"


def _locales() -> "list[str]":
    """要驗哪些語言 —— **唯一來源是 `ui_locale.SUPPORTED`**。

    寫死 `en` 的話，加了第三種語言之後這整支守門會**安靜地只驗英文**
    ——「掃 0 個檔」跟「掃過都乾淨」在 pytest 輸出裡長得一模一樣。
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.core.ui_locale import DEFAULT_LOCALE, SUPPORTED
    return [c for c in SUPPORTED if c != DEFAULT_LOCALE]


PAGES = tuple((f"{n}.html", f"{n}.{lang}.json", f"{n}-{lang}.html")
              for lang in _locales() for n in ("index", "api"))
CJK = re.compile(r"[㐀-鿿]")
_STRIP = re.compile(
    r"<(script|style|code|pre)\b.*?</\1>|<!--.*?-->"
    # 語言切換那一組是**產生的**，裡面一定會有其他語言的自稱
    #（英文頁上的「日本語」、日文頁上的「繁體中文」）—— 那是對的，不是漏翻。
    # （v1.15.47 起它是一個 `<select>` 下拉 —— 三種語言之後並排連結會
    #   黏成一團「繁體中文English日本語」，使用者截圖回報過。）
    r"|<select id=\"langSwitch\".*?</select>", re.S | re.I)


@pytest.mark.parametrize("src,cat,dst", PAGES)
def test_english_page_exists_and_has_no_chinese_left(src: str, cat: str, dst: str):
    p = DOCS / dst
    assert p.exists(), f"缺 {dst}（跑 python3 github/build-i18n-page.py）"
    html = p.read_text(encoding="utf-8")
    lang = dst.rsplit("-", 1)[1].removesuffix(".html")
    assert re.search(rf'<html lang="{lang}"', html), f"{dst} 的 lang 要是 {lang}"
    body = _STRIP.sub(" ", html)
    left = {m.group(1).strip() for m in re.finditer(r">([^<>]+)<", body)
            if CJK.search(m.group(1))}
    if lang == "ja":
        return          # 日文譯文本來就是漢字，這條判準對它不成立
    assert left == set(), f"{dst} 還有中文沒翻：{sorted(left)[:5]}"


@pytest.mark.parametrize("src,cat,dst", PAGES)
def test_catalog_has_no_empty_translation(src: str, cat: str, dst: str):
    data = json.loads((DOCS / "i18n" / cat).read_text(encoding="utf-8"))
    todo = sorted(k for k, v in data.items() if not v)
    assert todo == [], f"{cat} 還有 {len(todo)} 條沒翻：{todo[:5]}"


@pytest.mark.parametrize("src,cat,dst", PAGES)
def test_language_link_points_both_ways(src: str, cat: str, dst: str):
    """**每一種語言都要連得到其他每一種語言。**

    兩語時這是一個「切換」；三語之後不是了 —— 原本的寫法會讓日文頁
    沒有出口到英文頁（實際踩到）。
    """
    zh = (DOCS / src).read_text(encoding="utf-8")
    other = (DOCS / dst).read_text(encoding="utf-8")
    # 語言選單是 `<select>`，每個語言是一個 `<option value="…">`
    # —— 判準看的是**去得了哪些頁**，不是它長成連結還是下拉。
    def targets(html: str) -> set:
        m = re.search(r'<select id="langSwitch".*?</select>', html, re.S)
        assert m, "找不到語言選單"
        return set(re.findall(r'value="([^"]+\.html)"', m.group(0)))

    assert dst in targets(zh), f"{src} 的語言選單少了 {dst}"
    assert src in targets(other), f"{dst} 的語言選單少了 {src}"
    page = src.removesuffix(".html")
    me = dst.rsplit("-", 1)[1].removesuffix(".html")
    for lang in _locales():
        if lang == me:
            continue
        assert f"{page}-{lang}.html" in targets(other), \
            f"{dst} 的語言選單少了 {page}-{lang}.html（三語要互相去得了）"
    # 目前這一頁自己也要在選單裡而且是選起來的 —— 下拉要先讓人看到「現在是哪一個」
    assert f'value="{dst}" lang="{me}" selected' in other, \
        f"{dst} 的語言選單沒有把自己標成 selected"


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
#: 行內程式碼（單反引號）也要拿掉 —— 英文版的更新記錄會**引用**中文字串當例子
#: （「產品名 `Jason Tools 文件工具箱` 是品牌，不翻」「欄位同義詞字典不可以翻」）。
#: 那是 mention 不是 use，跟「這一行忘了翻」是兩回事；不分開的話這條守門會在
#: 每次寫到中文例子時紅，然後被當成雜訊忽略。
_CODE = re.compile(r"`[^`]*`")


def _chinese_lines(md: str) -> list[str]:
    return [ln for ln in _CODE.sub(" ", _FENCE.sub(" ", md)).splitlines()
            if CJK.search(ln) and "繁體中文" not in ln]


@pytest.mark.parametrize("lang", _locales())
def test_readme_translated_version_is_generated_and_complete(lang: str):
    p = GH / f"README_{lang}.md"
    assert p.exists(), f"缺 README_{lang}.md（跑 python3 github/build-i18n-md.py）"
    if lang == "ja":
        return          # 日文譯文本來就是漢字
    left = _chinese_lines(p.read_text(encoding="utf-8"))
    assert left == [], f"README_{lang}.md 還有中文沒翻：{left[:3]}"


@pytest.mark.parametrize("lang", _locales())
def test_changelog_translated_version_exists(lang: str):
    p = GH / f"CHANGELOG_{lang}.md"
    assert p.exists(), f"缺 CHANGELOG_{lang}.md"
    if lang == "ja":
        return
    left = _chinese_lines(p.read_text(encoding="utf-8"))
    assert left == [], f"CHANGELOG_{lang}.md 還有中文沒翻：{left[:3]}"


@pytest.mark.parametrize("base", ["README", "CHANGELOG"])
def test_markdown_language_switch_covers_every_language(base: str):
    """第一行的語言列要**每一種語言都連得到其他每一種**。"""
    heads = {"zh-Hant": (GH / f"{base}.md").read_text(encoding="utf-8").splitlines()[0]}
    for lang in _locales():
        heads[lang] = (GH / f"{base}_{lang}.md").read_text(
            encoding="utf-8").splitlines()[0]
    names = {"zh-Hant": f"{base}.md",
             **{lang: f"{base}_{lang}.md" for lang in _locales()}}
    for me, head in heads.items():
        for other, fname in names.items():
            if other == me:
                continue
            assert f"({fname})" in head, \
                f"{names[me]} 第一行少了連到 {fname} 的語言切換"


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


#: **每一份文件語系檔、每一種語言都要驗**。原本只列了介紹站 / API 手冊 / README 的
#: **英文版** —— 疑難排解頁與全部日文版都不在範圍內，於是英文疑難排解頁的第一則
#: 從上線起標題就只剩「Upgrade stops at」、症狀那一行顯示成標題（譯文錯位貼到下一條鍵，
#: 行內標籤數對不上，這條本來一眼就抓得到）—— 2026-09-23 加 jtlw 疑難排解時才發現。
#: 範圍太窄跟沒有守門一樣；語言清單照慣例從 `_locales()` 讀，不寫死。
_ALL_CATALOGUES = [f"{n}.{lang}.json" for lang in _locales()
                   for n in ("index", "api", "readme", "troubleshooting")]


@pytest.mark.parametrize("cat", _ALL_CATALOGUES)
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


def _generator():
    """載入 `build-i18n-page.py`（檔名有連字號，只能用 importlib）。"""
    import importlib.util as ilu

    root = Path(__file__).resolve().parents[1]
    f = _public_root(root) / "build-i18n-page.py"
    spec = ilu.spec_from_file_location("_bip", f)
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", ["index", "api", "troubleshooting"])
@pytest.mark.parametrize("lang", _locales())
def test_no_block_starts_with_punctuation_unless_the_chinese_one_does(
        name: str, lang: str):
    """英文頁的每個區塊，開頭標點要跟中文頁一致。

    **判準放在產出的頁面上，不放在語系檔**：語系檔裡「以標點開頭」有時候是對的
    （原文本來就是被 `<code>` 切斷的句子中段），逐條猜會誤報。逐區塊比對中英兩份
    產出則是位置對位置，精確 —— 使用者截圖看到的正是這個症狀：
    「, including but not limited to…」整條以逗號開頭。
    """
    src, dst = f"{name}.html", f"{name}-{lang}.html"
    # **比對之前兩邊要是同一個形狀**：台灣專屬工具的截圖區塊在別的語言底下
    # 整塊拿掉了（使用者 2026-09-14 要求），中文那邊也要照同一條規則拿掉，
    # 不然區塊數對不上、這條就只能放寬成沒有牙齒。
    zh_html = _generator()._drop_hidden_tool_shots(
        (DOCS / src).read_text(encoding="utf-8"), lang)
    zh = _block_heads(zh_html)
    en = _block_heads((DOCS / dst).read_text(encoding="utf-8"))
    assert len(zh) == len(en), f"{dst} 的區塊數與中文版不同（{len(en)} vs {len(zh)}）"
    bad = [i for i, (a, b) in enumerate(zip(zh, en))
           if b in _LEAD_PUNCT and a not in _LEAD_PUNCT]
    assert not bad, (
        f"{dst} 有 {len(bad)} 個區塊以標點開頭、但中文版不是 —— "
        f"譯文貼錯鍵或句子被切碎了（第 {bad[:5]} 個區塊）")


@pytest.mark.parametrize("cat", _ALL_CATALOGUES)
def test_no_pure_punctuation_keys(cat: str):
    """整段只有標點的片段不可以進語系檔。

    表格裡孤零零一個 `—`、一個 `）` 不是句子 —— 收進去就變成一個到處都對得上
    的鍵，然後把別處的譯文貼到那個標點的位置（實際發生過：表格欄位變成
    「; JSON:」）。
    """
    data = json.loads((DOCS / "i18n" / cat).read_text(encoding="utf-8"))
    bad = [k for k in data if not _HAS_WORD.search(k)]
    assert not bad, f"純標點的鍵：{bad}"


@pytest.mark.parametrize("lang", _locales())
def test_translated_page_uses_that_language_screenshots(lang: str):
    """某語言版引用的截圖必須是**那個語言的介面**那一組。

    原本兩個版本共用同一批中文截圖 —— 讀者看到的畫面跟他實際會看到的不一樣，
    而截圖正是「有沒有真的支援那個語言」最直接的證據。

    產生方式：
    `python tools/capture_locale_screenshots.py --locale <語言> --base <拋棄式實例>`。
    """
    import re
    html = (DOCS / f"index-{lang}.html").read_text(encoding="utf-8")
    refs = sorted(set(re.findall(r'screenshots/[\w./-]+\.png', html)))
    assert refs, f"{lang} 版頁面一張截圖都沒有"
    zh = [r for r in refs if not r.startswith(f"screenshots/{lang}/")]
    assert not zh, f"{lang} 版還在用中文介面的截圖：{zh}"
    for r in refs:
        p = DOCS / r
        assert p.is_file(), f"{lang} 版引用了不存在的截圖：{r}"


# ---------------------------------------------------------------------------
# 英文版跟不跟得上中文版（2026-09-13 使用者問「CHANGELOG_en.md 為何很久沒更新了?」）
#
# 答案是：**沒有守門。** 上面那幾條只驗「檔案在不在、有沒有殘留中文、連結指不指
# 得回去」—— 一份停在 34 個版本以前的英文更新記錄，那三條**全部都是綠的**。
# 這個專案反覆出現同一句教訓：記了規則但沒有守門，等於沒記。
#
# 判準要能自己算，不可以寫死期望值：
#   * `CHANGELOG_en.md` **最新那一條的版本號**必須等於中文版最新那一條。
#   * `README_en.md` 標題的版本號必須等於 `README.md` 的（它是生成的，
#     所以只要有人忘記在最後一次編輯之後重跑產生器就會紅）。
# ---------------------------------------------------------------------------

_ENTRY = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.M)
_README_VER = re.compile(r"v(\d+\.\d+\.\d+)")


def _latest_entry(p: Path) -> str:
    m = _ENTRY.search(p.read_text(encoding="utf-8"))
    assert m, f"{p.name} 找不到任何 `## [x.y.z]` 條目"
    return m.group(1)


def test_english_changelog_keeps_up_with_the_chinese_one() -> None:
    zh = _latest_entry(GH / "CHANGELOG.md")
    en = _latest_entry(GH / "CHANGELOG_en.md")
    assert en == zh, (
        f"CHANGELOG_en.md 最新只到 {en}，中文版已經到 {zh} —— "
        "每次 bump 版本，中英兩份要一起寫（使用者 2026-09-13 指示）。\n"
        "英文版是**摘要**不是全譯，但最新那一版一定要有。"
    )


def test_english_readme_keeps_up_with_the_chinese_one() -> None:
    zh_m = _README_VER.search((GH / "README.md").read_text(encoding="utf-8"))
    en_m = _README_VER.search((GH / "README_en.md").read_text(encoding="utf-8"))
    assert zh_m and en_m, "README 的標題應該帶著版本號"
    assert en_m.group(1) == zh_m.group(1), (
        f"README_en.md 停在 v{en_m.group(1)}，中文版是 v{zh_m.group(1)} —— "
        "跑 `python3 github/build-i18n-md.py` 重新生成（要在最後一次編輯之後才跑）。"
    )


def _regenerated(script: str, targets: tuple[str, ...]) -> list[str]:
    """把產生器跑一次，比對輸出檔有沒有變 —— 變了就是公開樹上那份是舊的。

    **判準不能是「有沒有記得跑產生器」**（那是人的記憶，正是會漏的地方），
    要能自己算：把現在的產出留起來、重新生成、逐位元組比對。
    """
    import shutil
    import subprocess
    import sys

    root = GH.parent
    keep = {t: (GH / t).read_bytes() for t in targets}
    try:
        subprocess.run([sys.executable, str(GH / script)], cwd=root,
                       check=True, capture_output=True)
        return [t for t in targets if (GH / t).read_bytes() != keep[t]]
    finally:
        for t, data in keep.items():
            (GH / t).write_bytes(data)
        del shutil


def test_english_site_pages_are_regenerated_after_every_change() -> None:
    """介紹站與 API 手冊的英文版不可以落後中文版。

    使用者 2026-09-13：「以後更新版本 英文文件 pages 都要跟著」。
    """
    stale = _regenerated("build-i18n-page.py",
                         ("docs/index-en.html", "docs/api-en.html"))
    assert not stale, (
        f"英文版的介紹站 / API 手冊是舊的：{stale} —— "
        "跑 `python3 github/build-i18n-page.py`（要在最後一次編輯之後才跑）。"
    )


def test_english_readme_is_regenerated_after_every_change() -> None:
    stale = _regenerated("build-i18n-md.py", ("README_en.md",))
    assert not stale, (
        "README_en.md 是舊的 —— 跑 `python3 github/build-i18n-md.py`。"
    )


def test_japanese_pages_do_not_leave_a_stray_space_around_inline_tags():
    """日文頁：行內標記**兩側都是日文**時中間不可以有空白。

    中文原文在 `<b>` 旁邊常留一個空白（標籤裡面是拉丁字，例如
    `<b>不需</b> Office 引擎`）；翻成日文之後兩側都變日文字，那個空白就變成
    「不要 です」這種怪東西 —— **畫面上看得到，自動化測試原本一律抓不到**。

    產生器 `_tighten_cjk_spaces()` 負責收掉。這一條是它的守門：
    只驗「產物有沒有重跑生成器」的話，把那條規則拿掉再重跑一樣是綠的。
    """
    ja_ch = r"[\u3040-\u30ff\u3400-\u9fff\uff01-\uff60、。「」－]"
    tag = r"</?(?:b|strong|i|em|code|span|a|kbd|small|u|mark)\b[^>]*>"
    pats = [re.compile(rf"{ja_ch}(?:{tag})+ (?={ja_ch})"),
            re.compile(rf"{ja_ch} (?:{tag})+(?={ja_ch})")]
    bad: list[str] = []
    for name in ("index", "api", "troubleshooting"):
        f = DOCS / f"{name}-ja.html"
        if not f.is_file():
            continue
        html = f.read_text(encoding="utf-8")
        for pat in pats:
            bad += [f"{f.name}: …{m.group(0)}…" for m in pat.finditer(html)]
    assert not bad, "日文頁行內標記旁邊多了空白：\n" + "\n".join(bad[:10])


# ---------------------------------------------------------------------------
# 左上的品牌區：**第一行永遠是 `Jason Tools`**（2026-09-14 使用者截圖回報）
#
# 原本整串產品名放第一行，翻成英文 / 日文之後就折行了。量過的數字：
# 標題不折行時要 中文 166 / 英文 225 / **日文 285** px，加上導覽列之後
# 英文 1186、日文 1188 —— 而容器只有 1180，**就是差那幾個 px**。
#
# 說明搬到第二行（11px）之後，翻成多長都不會再影響第一行。
# 這條擋的是「有人又把整串產品名塞回第一行」。
# ---------------------------------------------------------------------------

_BRAND_NAME = re.compile(r'<div class="brand-name">(.*?)</div>', re.S)


@pytest.mark.parametrize("page", ["index", "api", "troubleshooting"])
def test_the_brand_first_line_is_the_same_in_every_language(page: str):
    langs = ["zh-Hant"] + _locales()
    seen = {}
    for lang in langs:
        f = DOCS / (f"{page}.html" if lang == "zh-Hant" else f"{page}-{lang}.html")
        m = _BRAND_NAME.search(f.read_text(encoding="utf-8"))
        assert m, f"{f.name} 找不到 .brand-name"
        seen[lang] = m.group(1).strip()
    assert set(seen.values()) == {"Jason Tools"}, (
        "品牌區第一行每個語言都要是 `Jason Tools` —— 把說明放回第一行的話，"
        f"英文 / 日文會折行（量過：日文要 285px，容器只有 1180px）：{seen}")


def test_the_brand_lines_are_pinned_to_one_line_each():
    """兩行都要 `white-space: nowrap` —— 少了它就是原本那個折行的樣子。"""
    css = (DOCS / "style.css").read_text(encoding="utf-8")
    for sel in (".brand-name", ".brand-sub"):
        i = css.index(sel + " ")
        block = css[i:css.index("}", i)]
        assert "nowrap" in block, f"{sel} 少了 white-space: nowrap"
