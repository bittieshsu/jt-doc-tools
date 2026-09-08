"""翻譯對照字典：單位內部的專有名詞怎麼翻（或不要翻）。

**做法不是把對照表寫進 prompt。** 那只是拜託模型照做，它可能不照做而且
沒照做你不會發現；而且翻成繁中的指令部分已經 1,179 字元、每批內容上限
才 1,200 —— 字典再塞進去只會讓批次變小、漏段重試變多。

改成送出前把命中的詞換成佔位符、收回來再填指定的譯法：模型根本看不到那些
詞，不可能翻錯，而 prompt 一個字都不加。
"""
from __future__ import annotations

import importlib
import re

import pytest

from app.core import translation_glossary as G


def _t(source, target, **kw):
    kw.setdefault("src_lang", "en")
    kw.setdefault("tgt_lang", "zh-TW")
    return G.Term(source=source, target=target, **kw)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每條測試自己的字典檔（`settings.data_dir` 是 property，改屬性不改環境變數）。"""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr("app.config.settings.data_dir", d)
    monkeypatch.setattr(G, "_CACHE", None)
    return d


# ---- 比對 ---------------------------------------------------------------

@pytest.mark.parametrize("text,hit,why", [
    ("Acerbic remarks.", False, "後邊界：Acer 不可以命中 Acerbic"),
    ("Buy a MyAcer box.", False, "前邊界：Acer 不可以命中 MyAcer"),
    ("Buy an Acer box.", True, "獨立的字要命中"),
    ("(Acer)", True, "標點旁邊也算邊界"),
])
def test_latin_terms_need_a_word_boundary_on_both_sides(text, hit, why):
    """**兩邊都要驗。**

    只驗一邊的話，把另一邊的邊界拿掉這條守門照樣全綠 —— 第一次做變異
    驗證時就是這樣：拿掉前邊界，測試沒紅。
    """
    m = G.Matcher([_t("Acer", "宏碁")])
    assert bool(G.protect(text, m)[1]) is hit, why


def test_cjk_terms_match_as_substrings():
    """中日韓沒有詞邊界，硬加 `\\b` 會讓中文詞整個匹配不到。"""
    m = G.Matcher([_t("宏碁", "Acer", src_lang="zh-TW", tgt_lang="en")])
    masked, mp = G.protect("我們買了宏碁的伺服器。", m)
    assert masked == "我們買了[[T1]]的伺服器。"


def test_the_longest_term_wins():
    """`Acer Chromebook` 要贏過 `Acer`，否則長詞永遠用不到。"""
    m = G.Matcher([_t("Acer", "宏碁"), _t("Acer Chromebook", "宏碁筆電")])
    masked, mp = G.protect("Buy an Acer Chromebook.", m)
    assert mp == {1: "宏碁筆電"}
    assert masked == "Buy an [[T1]]."


def test_case_sensitivity_is_per_term():
    m = G.Matcher([_t("IT", "資訊", case_sensitive=True)])
    assert G.protect("IT dept", m)[1] == {1: "資訊"}
    assert G.protect("it is here", m)[1] == {}      # 大小寫不同 → 不命中
    assert G.protect("ITEM", m)[1] == {}            # 詞邊界 → 不命中


def test_keep_mode_puts_the_original_back():
    """企業最常見的需求其實是「不要翻」：產品名、專案代號、系統名。"""
    m = G.Matcher([_t("jt-doc-tools", "", mode="keep")])
    masked, mp = G.protect("Install jt-doc-tools now.", m)
    assert mp == {1: "jt-doc-tools"}
    out, ok = G.restore(masked.replace("Install", "安裝").replace("now.", "。"), mp)
    assert ok and "jt-doc-tools" in out


def test_a_disabled_term_does_nothing():
    m = G.matcher_for("en", "zh-TW", [_t("Acer", "宏碁", enabled=False)])
    assert G.protect("Acer", m)[1] == {}


def test_terms_from_another_language_pair_are_not_used():
    """字典是**有方向**的：翻成日文時不可以套用中文的譯法。"""
    m = G.matcher_for("en", "ja", [_t("Acer", "宏碁")])
    assert G.protect("Acer", m)[1] == {}


def test_no_match_leaves_the_text_untouched():
    """沒設字典 / 沒命中的安裝，這條路徑上一個位元組都不會變。"""
    m = G.Matcher([_t("Acer", "宏碁")])
    src = "完全沒有命中的一段文字。"
    assert G.protect(src, m) == (src, {})


# ---- 還原（安全性的關鍵）-------------------------------------------------

def test_restore_fills_in_the_required_wording():
    m = G.Matcher([_t("Acer", "宏碁"), _t("Foxconn", "富士康")])
    masked, mp = G.protect("The Acer server runs Foxconn firmware.", m)
    out, ok = G.restore(masked.replace("The ", "").replace(" server runs ", " 伺服器執行 "), mp)
    assert ok and "宏碁" in out and "富士康" in out


def test_extra_whitespace_inside_the_marker_is_tolerated():
    """實測模型會在標記裡多塞空白。"""
    out, ok = G.restore("[[ T1 ]] X", {1: "Acer"})
    assert ok and out == "Acer X"


def test_byte_token_noise_does_not_break_the_restore():
    """模型偶爾把不成字的位元組吐成 `<0xE2>` 這種字面寫法。

    吐一個就整段退回的話，字典的命中率會被雜訊拖垮。批次解析早就在做
    同樣的清理。
    """
    out, ok = G.restore("<0xE2>[[T1]] 的伺服器", {1: "宏碁"})
    assert ok and "宏碁" in out and "<0x" not in out


@pytest.mark.parametrize("reply,mapping,want,why", [
    ("[[T1]] 伺服器執行 [[T2]] 韌體。", {1: "宏碁", 2: "富士康"},
     "宏碁伺服器執行富士康韌體。",
     "中文詞兩邊的空白要收掉 —— 中文詞之間不放空白"),
    ("在每一台 [[T1]] 上", {1: "宏碁"}, "在每一台宏碁上",
     "前後都是中文 → 兩邊都收"),
    ("[[T1]] 與 [[T2]] 簽約", {1: "宏碁", 2: "富士康"}, "宏碁與富士康簽約",
     "句首的詞沒有前文，只收後面那個"),
])
def test_spaces_around_a_cjk_term_are_collapsed(reply, mapping, want, why):
    """英文原文是 `The [[T1]] server`，模型很自然譯成 `[[T1]] 伺服器` ——
    填回中文詞就變成「宏碁 伺服器」，那個空格很刺眼。
    """
    out, ok = G.restore(reply, mapping)
    assert ok and out == want, why


def test_spaces_around_a_latin_term_are_kept():
    """反過來：中文句子裡的英文詞**要**留空白。"""
    out, ok = G.restore("請先安裝 [[T1]] 再重開機", {1: "jt-doc-tools"})
    assert ok and out == "請先安裝 jt-doc-tools 再重開機"


def test_the_placeholder_is_ascii():
    """**這條是拿真的模型換來的。**

    第一版用 `⟪1⟫`，在 gemma4:26b 上 **5 句全滅**：tokenizer 遇到不在詞彙表
    裡的字元會把**位元組 token 的字面寫法**吐成文字 ——
    `<0xE2><0x9F><0xAA>1⟫`（`E2 9F AA` 正是 `⟪` 的 UTF-8 位元組）。
    於是每句都還原失敗、退回不保護重翻：**功能等於沒有，還白花一倍的
    LLM 請求**，而假模型完全測不出來。

    實測七種寫法：`⟪1⟫` 與 `#1#` 失敗（後者被讀成「第 1 號」），
    `[[T1]]` / `⟦T1⟧` / `[[1]]` / `<<1>>` / `{{1}}` 逐句與批次都全過。
    """
    assert G._PH_OPEN.isascii() and G._PH_CLOSE.isascii()
    sample = f"{G._PH_OPEN}1{G._PH_CLOSE}"
    assert sample.isascii(), sample
    # 也不可以撞到批次協定的 `⟦編號⟧`
    import importlib
    DT = importlib.import_module("app.tools.doc_translate.router")
    assert not DT._SEG_RE.match(sample)


def test_text_that_already_looks_like_a_placeholder_is_left_alone():
    """原文自己就含 `[[T1]]` 時不保護 —— 否則還原會把它換成別的詞。"""
    m = G.Matcher([_t("Acer", "宏碁")])
    src = "See [[T1]] and Acer."
    assert G.protect(src, m) == (src, {})


@pytest.mark.parametrize("reply,why", [
    ("伺服器", "模型把標記整個弄丟了"),
    ("<1> 伺服器", "模型換成別的括號"),
    ("[[T1]] 伺服器 [[T2]]", "模型自己多生一個編號"),
    ("[[T1]] 伺服器 [[T1]]", "模型把同一個標記吐了兩次"),
])
def test_a_damaged_marker_is_rejected(reply: str, why: str):
    """**還原不完整就要退回不保護重翻。**

    硬把剩下的填回去會產出少了字（或多了字）的句子，而且沒有人看得出來；
    產出裡更不可以殘留 `[[T1]]` —— 那比翻錯還明顯。
    """
    out, ok = G.restore(reply, {1: "宏碁"})
    assert not ok, why
    assert out == reply, "退回時不可以動到原本的回覆"


def test_nothing_ever_ships_with_a_placeholder_left_in():
    assert G.has_placeholder("[[T1]] 伺服器")
    assert not G.has_placeholder("宏碁 伺服器")


# ---- 詞條的清理與驗證 ----------------------------------------------------

@pytest.mark.parametrize("bad", ["⟦", "⟧", "\n", "\x00"])
def test_protocol_characters_are_stripped_from_terms(bad: str):
    """`⟦⟧` 是批次協定的標記，換行會破壞逐行的批次格式。

    管理員是可信的，但**打錯字不該把翻譯功能弄壞**。
    """
    t = G.normalise({"source": f"Ac{bad}er", "target": "宏碁",
                     "src_lang": "en", "tgt_lang": "zh-TW"})
    assert t.source == "Acer"


@pytest.mark.parametrize("raw,msg", [
    ({"source": "", "target": "x", "src_lang": "en", "tgt_lang": "zh-TW"}, "原文"),
    ({"source": "x", "target": "", "src_lang": "en", "tgt_lang": "zh-TW"}, "譯文"),
    ({"source": "x", "target": "y", "src_lang": "", "tgt_lang": "zh-TW"}, "語言"),
    ({"source": "x", "target": "y", "src_lang": "en", "tgt_lang": "en"}, "相同"),
    ({"source": "x", "target": "y", "src_lang": "en", "tgt_lang": "zh-TW",
      "mode": "nope"}, "模式"),
])
def test_invalid_entries_are_rejected_with_a_reason(raw: dict, msg: str):
    with pytest.raises(ValueError, match=msg):
        G.normalise(raw)


def test_keep_mode_does_not_need_a_target():
    t = G.normalise({"source": "Acer", "src_lang": "en", "tgt_lang": "zh-TW",
                     "mode": "keep"})
    assert t.replacement() == "Acer"


# ---- 儲存 ---------------------------------------------------------------

def test_saved_terms_come_back():
    G.save([_t("Acer", "宏碁")])
    assert [t.source for t in G.load()] == ["Acer"]


def test_a_broken_entry_does_not_take_the_whole_dictionary_down():
    p = G._path()
    p.write_text('{"terms": [{"source": "Acer", "target": "宏碁", '
                 '"src_lang": "en", "tgt_lang": "zh-TW"}, {"source": ""}]}',
                 encoding="utf-8")
    assert [t.source for t in G.load()] == ["Acer"]


def test_the_dictionary_is_not_read_on_every_call():
    """這份資料每段文字都要用 —— 沒有快取等於每段讀一次檔。"""
    G.save([_t("Acer", "宏碁")])
    G.load()
    real = G._path().read_text
    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    import pathlib
    orig = pathlib.Path.read_text
    pathlib.Path.read_text = lambda self, *a, **k: (
        counting(*a, **k) if self == G._path() else orig(self, *a, **k))
    try:
        for _ in range(20):
            G.load()
    finally:
        pathlib.Path.read_text = orig
    assert calls["n"] == 0, f"讀了 {calls['n']} 次檔"


def test_changes_take_effect_without_a_restart():
    """快取依 mtime 失效 —— 管理員存完要立刻生效。"""
    G.save([_t("Acer", "宏碁")])
    assert G.count_for("en", "zh-TW") == 1
    G.save([_t("Acer", "宏碁"), _t("Foxconn", "富士康")])
    assert G.count_for("en", "zh-TW") == 2


def test_the_cap_is_enforced():
    with pytest.raises(ValueError, match="最多"):
        G.save([_t(f"w{i}", "x") for i in range(G.MAX_TERMS + 1)])


def test_pair_counts_only_counts_enabled_terms():
    G.save([_t("Acer", "宏碁"), _t("Off", "關", enabled=False),
            _t("Acer", "エイサー", tgt_lang="ja")])
    assert G.pair_counts() == {"en|zh-TW": 1, "en|ja": 1}


def test_the_language_list_is_shared_with_the_translation_tools():
    """同一份清單放兩個地方一定會漂 —— 這個專案為此吃過好幾次虧。"""
    R = importlib.import_module("app.tools.translate_doc.router")
    assert {c["code"] for c in G.lang_choices()} == set(R._LANG_NAMES)


# ---- 不可以偷偷變成 prompt 的一部分 --------------------------------------

def test_the_prompt_never_carries_the_dictionary():
    """整份設計的前提：**指令部分不因為字典而變長**。

    翻成繁中的指令已經 1,179 字元、每批內容上限 1,200 —— 字典寫進 prompt
    就會把批次擠掉一半。這條守門釘死那個前提。
    """
    R = importlib.import_module("app.tools.translate_doc.router")
    G.save([_t("Acer", "宏碁"), _t("Foxconn", "富士康")])
    before = R._build_prompt_prefix("en", "zh-TW")
    assert "宏碁" not in before and "Acer" not in before
    assert "富士康" not in before
