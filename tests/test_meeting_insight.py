"""會議分析的確定性部分（切視窗、解析、引用驗證、合併、發言者統計）。

> **這些測試驗得到我們自己的程式，驗不到「模型會不會照做」。**
> 「每一條都要附段號」是一個**要求模型照做**的設計，本專案在翻譯對照字典上
> 踩過：佔位符 `⟪1⟫` 所有測試都綠（假模型當然會原樣保留），拿真的模型實跑
> 五句全部失敗。品質要靠 `tools/meeting_eval/run.py` 對真模型量。
"""
from __future__ import annotations

import json

import pytest

from app.core import meeting_insight as M


def seg(seq: int, text: str, speaker: str = "speaker_1",
        start: int | None = None, end: int | None = None) -> dict:
    a = start if start is not None else seq * 1000
    return {"seq": seq, "text": text, "speaker": speaker,
            "start_ms": a, "end_ms": end if end is not None else a + 800}


# ------------------------------------------------------------------ 視窗

def test_windows_cover_every_segment():
    segs = [seg(i, "字" * 50) for i in range(1, 61)]
    wins = M.make_windows(segs, window_chars=400, overlap_chars=100)
    covered: set[int] = set()
    for w in wins:
        covered |= w.seqs
    assert covered == {s["seq"] for s in segs}
    assert len(wins) > 1, "這個大小應該要切成多個視窗，否則下面的重疊驗不到"


def test_windows_actually_overlap():
    """**重疊不是保險，是必要的** —— 跨視窗邊界的決議否則兩邊都看不出結論。"""
    segs = [seg(i, "字" * 50) for i in range(1, 61)]
    wins = M.make_windows(segs, window_chars=400, overlap_chars=200)
    pairs = [(a, b) for a, b in zip(wins, wins[1:])]
    assert pairs
    assert all(a.seqs & b.seqs for a, b in pairs), "相鄰視窗必須有重疊"


def test_windows_terminate_on_pathological_input():
    """一段就超過整個視窗大小時不可以無窮迴圈。"""
    segs = [seg(i, "字" * 5000) for i in range(1, 6)]
    wins = M.make_windows(segs, window_chars=100, overlap_chars=50)
    # **要終止、而且每個視窗都有界** —— 以前一段就是一個視窗（不切開），
    # 所以一段兩萬字的發言會做出一個兩萬字的提示，超過模型的上下文之後
    # 是**安靜截斷**不是報錯（2026-09-18 在真的委員會逐字稿上量到 62,423 字）。
    assert wins, "沒有產出視窗"
    assert len(wins) < 500, "切得太碎"
    biggest = max(sum(len(x["text"]) for x in w.segments) for w in wins)
    assert biggest <= 400, f"視窗沒有上界：最大 {biggest} 字"
    # 切開之後 `seq` 不可以變 —— 引用是靠它指回逐字稿的
    assert {x["seq"] for w in wins for x in w.segments} == {1, 2, 3, 4, 5}
    # 內容一個字都不可以掉
    joined = "".join(x["text"] for w in wins for x in w.segments)
    assert joined.count("字") >= 5 * 5000
    assert M.make_windows([]) == []


# ------------------------------------------------------------------ 解析

@pytest.mark.parametrize("raw", [
    '{"decisions":[{"text":"x","segment_ids":[1]}]}',
    '```json\n{"decisions":[{"text":"x","segment_ids":[1]}]}\n```',
    '好的，以下是整理結果：\n{"decisions":[{"text":"x","segment_ids":[1]}]}\n希望有幫助',
    '{"decisions":[{"text":"x","segment_ids":[1]}],"actions":[]}',
])
def test_parse_finds_the_json(raw):
    assert M.parse_reply(raw)["decisions"] == [{"text": "x", "segment_ids": [1]}]


def test_parse_strips_byte_token_literals():
    """tokenizer 把位元組 token 的**字面寫法**吐成文字是真的會發生的
    （本專案在 `⟪1⟫` 與 `⟦<0xC2>5⟧` 上各踩過一次）。"""
    raw = '{"decisions":[{"text":"<0xE2>好","segment_ids":[1]}]}'
    assert M.parse_reply(raw)["decisions"][0]["text"] == "好"


@pytest.mark.parametrize("raw", ["", "抱歉我無法處理", "{壞掉的 json", "[1,2,3]"])
def test_parse_never_raises(raw):
    """一個視窗解析失敗不該讓整場會議失敗。"""
    got = M.parse_reply(raw)
    assert set(got) == set(M.KINDS)
    assert all(v == [] for v in got.values())


# ------------------------------------------------------------ 引用驗證

@pytest.fixture
def by_seq():
    return {s["seq"]: s for s in [
        seg(1, "那改用立信，多八萬可以接受，時程比較重要"),
        seg(2, "中午要吃什麼"),
        seg(3, "API 的 rate limit 要調高"),
    ]}


def test_citation_accepts_a_real_reference(by_seq):
    item = {"text": "改用立信，價格多八萬可以接受", "segment_ids": [1]}
    assert M.check_citation(item, by_seq).ok


def test_citation_rejects_a_fabricated_reference(by_seq):
    """**這是這支模組存在的理由。** 內容跟引用的段落完全無關 → 丟掉。"""
    item = {"text": "決定把預算提高到三百萬並更換整個團隊", "segment_ids": [2]}
    chk = M.check_citation(item, by_seq)
    assert not chk.ok and "找不到" in chk.reason


@pytest.mark.parametrize("ids,expect", [
    ([], "沒有附段號"),
    ([99], "不存在"),
])
def test_citation_rejects_bad_ids(by_seq, ids, expect):
    chk = M.check_citation({"text": "隨便", "segment_ids": ids}, by_seq)
    assert not chk.ok and expect in chk.reason


def test_citation_rejects_ids_outside_the_window(by_seq):
    """模型只看得到這個視窗 —— 引用視窗外的段號一定是它自己編的。"""
    chk = M.check_citation({"text": "改用立信", "segment_ids": [1]},
                           by_seq, allowed={2, 3})
    assert not chk.ok and "視窗" in chk.reason


def test_citation_matches_latin_and_numbers(by_seq):
    item = {"text": "請對方調高 rate limit", "segment_ids": [3]}
    assert M.check_citation(item, by_seq).ok


# ------------------------------------------------------------------ 合併

def test_merge_dedupes_by_citation_not_by_text():
    """重疊視窗會把同一條抓兩次，而**兩次的措辭不會一樣**。"""
    got = M.merge_kind([
        {"text": "改用立信", "segment_ids": [10]},
        {"text": "決定改用立信，因為時程比較重要", "segment_ids": [10, 11]},
    ])
    assert len(got) == 1
    assert got[0]["segment_ids"] == [10, 11]
    assert "時程" in got[0]["text"], "應保留資訊比較完整的那一版"


def test_merge_keeps_unrelated_items_apart():
    got = M.merge_kind([{"text": "a", "segment_ids": [1]},
                        {"text": "b", "segment_ids": [2]}])
    assert len(got) == 2


def test_merge_fills_in_missing_owner_and_due():
    got = M.merge_kind([
        {"text": "報價單", "segment_ids": [5], "owner": None, "due_text": None},
        {"text": "提供報價單", "segment_ids": [5], "owner": "陳經理",
         "due_text": "下週三"},
    ])
    assert len(got) == 1
    assert got[0]["owner"] == "陳經理" and got[0]["due_text"] == "下週三"


# ------------------------------------------------------------ 發言者統計

def test_speaker_stats_takes_the_union_not_the_sum():
    """插話時區間會重疊 —— 直接相加會超過會議總長，那個數字一看就假。"""
    segs = [seg(1, "x", "a", 0, 10_000), seg(2, "y", "a", 5_000, 12_000)]
    st = M.speaker_stats(segs)
    assert st["a"]["speaking_ms"] == 12_000, "0~12 秒的聯集，不是 10+7=17 秒"
    assert st["a"]["turn_count"] == 2


def test_speaker_stats_percentages_add_up():
    segs = [seg(1, "x", "a", 0, 1000), seg(2, "y", "b", 1000, 3000)]
    st = M.speaker_stats(segs)
    assert round(sum(v["percentage"] for v in st.values())) == 100


def test_speaker_stats_ignores_broken_timestamps():
    segs = [seg(1, "x", "a", 0, 1000), {"seq": 2, "speaker": "b", "text": "y"}]
    # **壞掉的時間只讓「時間」那幾個欄位消失，人不會整個不見** ——
    # 發言次數與字數不需要時間（2026-09-18 起）。
    st = M.speaker_stats(segs)
    assert set(st) == {"a", "b"}
    assert "speaking_ms" in st["a"]
    assert "speaking_ms" not in st["b"], "時間壞掉的人不該有時間統計"
    assert st["b"]["turn_count"] >= 1, "但發言次數要照算"


# --------------------------------------------------------------- 端到端

def test_analyse_drops_the_item_that_cannot_be_supported():
    """端到端：模型同時吐出一條有依據、一條沒依據的，只有前者留下來。"""
    segs = [seg(1, "那就改用立信，時程比較重要"), seg(2, "中午吃什麼")]

    def ask(_prompt: str) -> str:
        return ('{"decisions":['
                '{"text":"改用立信","segment_ids":[1]},'
                '{"text":"決定裁撤整個部門並停止所有專案","segment_ids":[2]}]}')

    res = M.analyse(segs, ask)
    assert [d["text"] for d in res.items["decisions"]] == ["改用立信"]
    assert len(res.dropped) == 1 and res.dropped[0]["kind"] == "decisions"


def test_analyse_survives_a_window_that_blows_up():
    """一個視窗失敗不可以讓整場會議失敗。"""
    segs = [seg(i, "字" * 80) for i in range(1, 31)]
    calls = {"n": 0}

    def ask(_prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模型掛了")
        return '{"decisions":[{"text":"字字字","segment_ids":[20]}]}'

    res = M.analyse(segs, ask, window_chars=500, overlap_chars=100)
    assert calls["n"] > 1 and res.windows > 1


# ------------------------------------------------------- 第二階段：複審

def test_review_can_drop_and_recategorise_but_never_invent():
    """複審**只能判斷**。它不能新增，所以最壞情況是「什麼都沒改」。"""
    by_seq = {1: seg(1, "報表初稿出來了"), 2: seg(2, "那就決定改用立信")}
    items = {"actions": [{"text": "報表初稿出來了", "segment_ids": [1]}],
             "risks": [{"text": "改用立信", "segment_ids": [2]}],
             "decisions": [], "questions": []}

    def ask(prompt: str) -> str:
        assert "原文(1)" in prompt, "複審一定要看得到原文，否則判不出是不是進度回報"
        return ('{"verdicts":[{"id":1,"keep":"drop"},'
                '{"id":2,"keep":"decisions"}],'
                '"decisions":[{"text":"憑空多出來的","segment_ids":[9]}]}')

    kept, dropped = M.review(items, by_seq, ask)
    assert [d["text"] for d in kept["decisions"]] == ["改用立信"]
    assert kept["risks"] == [] and kept["actions"] == []
    assert len(dropped) == 1 and dropped[0]["text"] == "報表初稿出來了"
    total = sum(len(v) for v in kept.values())
    assert total == 1, "複審不可以無中生有 —— 回覆裡多塞的那一條必須被忽略"


def test_review_keeps_everything_when_the_model_says_nothing():
    """漏判一條不該讓它消失 —— **沒有裁決就維持原樣**。"""
    by_seq = {1: seg(1, "那就決定改用立信")}
    items = {k: [] for k in M.KINDS}
    items["decisions"] = [{"text": "改用立信", "segment_ids": [1]}]
    kept, dropped = M.review(items, by_seq, lambda _p: '{"verdicts":[]}')
    assert kept["decisions"] and not dropped


@pytest.mark.parametrize("reply", ["", "壞掉的回覆", '{"nope":1}'])
def test_review_failure_falls_back_to_stage_one(reply):
    by_seq = {1: seg(1, "那就決定改用立信")}
    items = {k: [] for k in M.KINDS}
    items["decisions"] = [{"text": "改用立信", "segment_ids": [1]}]
    kept, _ = M.review(items, by_seq, lambda _p: reply)
    assert kept["decisions"], "複審壞掉時要保留第一階段的結果，不可以清空"


def test_review_exception_falls_back_to_stage_one():
    by_seq = {1: seg(1, "那就決定改用立信")}
    items = {k: [] for k in M.KINDS}
    items["decisions"] = [{"text": "改用立信", "segment_ids": [1]}]

    def boom(_p: str) -> str:
        raise RuntimeError("模型掛了")

    kept, _ = M.review(items, by_seq, boom)
    assert kept["decisions"]


def test_cross_kind_duplicates_are_dropped():
    """同一句話不可以同時是決議又是風險 —— 那比漏掉更傷信任。"""
    res = M.Result()
    res.items["decisions"] = [{"text": "不跳過測試環境", "segment_ids": [5]}]
    res.items["risks"] = [{"text": "不跳過測試環境", "segment_ids": [5]}]
    M._drop_cross_kind_duplicates(res)
    assert res.items["decisions"] and not res.items["risks"]
    assert res.dropped and "decisions" in res.dropped[0]["reason"]


def test_cross_kind_dedupe_keeps_genuinely_different_items():
    """同一段話**確實可能**既做出決議、也點出風險 —— 那時候文字不一樣。"""
    res = M.Result()
    res.items["decisions"] = [{"text": "這一版先加索引不改結構", "segment_ids": [5]}]
    res.items["risks"] = [{"text": "改結構要停機，這個月有稽核趕不上",
                           "segment_ids": [5]}]
    M._drop_cross_kind_duplicates(res)
    assert res.items["decisions"] and res.items["risks"]


# ------------------------------------------------------------------ 章節

def _segs(n: int = 30) -> list[dict]:
    return [seg(i, "字" * 20, start=i * 1000, end=i * 1000 + 900)
            for i in range(1, n + 1)]


def test_chapters_cover_everything_with_no_gap_and_no_overlap():
    """**不可以有一段不屬於任何章節** —— 那一段的內容在畫面上會整個消失。"""
    got = M.tidy_chapters(
        [{"title": "乙", "start_seq": 15, "end_seq": 20},
         {"title": "甲", "start_seq": 3, "end_seq": 9}], _segs())
    assert [c["start_seq"] for c in got] == [1, 15]
    assert got[0]["end_seq"] == 14 and got[-1]["end_seq"] == 30
    for a, b in zip(got, got[1:]):
        assert b["start_seq"] == a["end_seq"] + 1


@pytest.mark.parametrize("chaps", [
    [{"title": "a", "start_seq": 1, "end_seq": 20},
     {"title": "b", "start_seq": 10, "end_seq": 25}],          # 重疊
    [{"title": "a", "start_seq": -5, "end_seq": 900}],          # 超出範圍
    [{"title": "a", "start_seq": 20, "end_seq": 5}],            # 倒序
    [],                                                          # 什麼都沒有
])
def test_chapters_survive_whatever_the_model_gives(chaps):
    got = M.tidy_chapters(chaps, _segs())
    assert got, "壞資料也要給得出章節，不可以回空的"
    assert got[0]["start_seq"] == 1 and got[-1]["end_seq"] == 30
    for a, b in zip(got, got[1:]):
        assert b["start_seq"] > a["end_seq"]


def test_chapter_percentages_are_computed_from_time_not_guessed():
    """規格明訂主題佔比要由 timestamp 算，不由 LLM 猜。"""
    chaps = M.chapter_times(
        M.tidy_chapters([{"title": "甲", "start_seq": 1, "end_seq": 10},
                         {"title": "乙", "start_seq": 11, "end_seq": 30}],
                        _segs()), _segs())
    assert round(sum(c["percentage"] for c in chaps)) == 100
    assert chaps[1]["duration_ms"] > chaps[0]["duration_ms"]


def test_parse_chapters_ignores_malformed_entries():
    raw = ('{"chapters":[{"title":"好","start_seq":1,"end_seq":5},'
           '{"title":"","start_seq":6,"end_seq":9},'
           '{"title":"倒序","start_seq":9,"end_seq":6},'
           '{"start_seq":1}]}')
    assert M.parse_chapters(raw) == [{"title": "好", "start_seq": 1, "end_seq": 5}]


# ---------------------------------------------------------------- 心智圖

def test_every_mindmap_node_can_be_traced_back():
    """**規格要求所有視覺節點都能回到逐字稿。**

    這裡是**由構造保證**的（心智圖是用已驗證的項目組裝的，不是生成的），
    但仍然釘一條 —— 有人改成「讓模型直接產心智圖」時它會先紅。
    """
    chaps = M.chapter_times(M.tidy_chapters(
        [{"title": "採購", "start_seq": 1, "end_seq": 15},
         {"title": "系統", "start_seq": 16, "end_seq": 30}], _segs()), _segs())
    items = {"decisions": [{"text": "改用立信", "segment_ids": [5]}],
             "actions": [{"text": "報價單", "segment_ids": [20]}],
             "risks": [], "questions": []}
    nodes = M.build_mindmap(chaps, items)
    assert nodes
    assert all(n["segment_ids"] for n in nodes), "每個節點都要指得回逐字稿"
    assert len({n["node_id"] for n in nodes}) == len(nodes), "node_id 不可重複"
    roots = [n for n in nodes if n["parent_id"] is None]
    assert len(roots) == 2
    kids = {n["parent_id"] for n in nodes if n["parent_id"]}
    assert kids == {r["node_id"] for r in roots}


def test_mindmap_puts_each_item_under_the_chapter_it_happened_in():
    chaps = M.chapter_times(M.tidy_chapters(
        [{"title": "甲", "start_seq": 1, "end_seq": 15},
         {"title": "乙", "start_seq": 16, "end_seq": 30}], _segs()), _segs())
    items = {k: [] for k in M.KINDS}
    items["decisions"] = [{"text": "後半段的決議", "segment_ids": [20]}]
    nodes = M.build_mindmap(chaps, items)
    child = [n for n in nodes if n["parent_id"]][0]
    parent = [n for n in nodes if n["node_id"] == child["parent_id"]][0]
    assert parent["label"] == "乙"


# ------------------------------------------------------------------ 選圖

def test_charts_are_chosen_from_the_data_not_by_the_model():
    """一個主題的會議畫佔比圓餅、一個人的會議畫發言者佔比 —— 都會讓人
    以為功能壞了。**沒有內容的圖不要畫。**"""
    one_ch = [{"title": "全部", "start_seq": 1, "end_seq": 30,
               "segment_ids": [1], "start_ms": 0, "end_ms": 1, "duration_ms": 1,
               "percentage": 100.0}]
    two_ch = one_ch + [dict(one_ch[0], title="乙")]
    solo = {"speaker_1": {"speaking_ms": 100}}
    duo = {**solo, "speaker_2": {"speaking_ms": 50}}
    items = {"decisions": [{"text": "a", "segment_ids": [1]},
                           {"text": "b", "segment_ids": [2]}],
             "actions": [], "risks": [], "questions": []}

    assert "topic_share" not in M.suitable_charts(one_ch, items, duo)
    assert "topic_share" in M.suitable_charts(two_ch, items, duo)
    assert "speaker_share" not in M.suitable_charts(two_ch, items, solo)
    assert "speaker_share" in M.suitable_charts(two_ch, items, duo)
    assert M.suitable_charts([], {k: [] for k in M.KINDS}, {}) == []


# -------------------------------------------------------------- 補引用

def test_repair_fixes_an_incomplete_citation():
    """實測遇到的：模型只引用了回答那一句，沒引用提問那一句。

    內容完全正確，直接丟掉的話我們會**安靜地弄丟一條真的未決問題**。
    """
    by = {22: seg(22, "那舊系統的資料要不要一起搬？"),
          23: seg(23, "這個我還沒有答案，要看法務那邊怎麼說")}
    item = {"text": "舊系統的資料要不要一起搬（需看法務意見）", "segment_ids": [23]}
    assert not M.check_citation(item, by).ok
    assert M.repair_citation(item, by)
    assert item["segment_ids"] == [22, 23]
    assert M.check_citation(item, by).ok


def test_repair_does_not_rescue_a_fabrication():
    """**這條是補引用能不能存在的前提。**

    補完之後仍然要過同一個門檻，所以憑空捏造的內容在鄰近段落裡一樣找不到。
    """
    by = {1: seg(1, "中午要吃什麼"), 2: seg(2, "我帶便當"), 3: seg(3, "厲害")}
    item = {"text": "決定裁撤整個部門並把預算提高到三百萬", "segment_ids": [2]}
    assert not M.repair_citation(item, by)
    assert item["segment_ids"] == [2], "沒補成功就不可以動它的引用"


def test_repair_stays_inside_the_window():
    """視窗外的段號模型看不到 —— 不可以拿來補。"""
    by = {5: seg(5, "那舊系統的資料要不要一起搬？"),
          6: seg(6, "這個我還沒有答案，要看法務那邊怎麼說")}
    item = {"text": "舊系統的資料要不要一起搬（需看法務意見）", "segment_ids": [6]}
    assert not M.repair_citation(item, by, allowed={6})


def test_repair_ignores_neighbours_that_add_nothing():
    by = {10: seg(10, "那就改用立信，時程比較重要"),
          11: seg(11, "嗯嗯"), 12: seg(12, "好")}
    item = {"text": "改用立信", "segment_ids": [10]}
    before = list(item["segment_ids"])
    M.repair_citation(item, by)
    assert item["segment_ids"] == before, "只補真的帶來內容的段落"


# ------------------------------------------------------------ 敘述摘要

def _mat():
    chaps = [{"title": "採購進度", "percentage": 60.0},
             {"title": "系統調整", "percentage": 40.0}]
    items = {"decisions": [{"text": "改用立信，維護費一年三十二萬"}],
             "actions": [{"text": "提供報價單", "owner": "陳經理",
                          "due_text": "下週三"}],
             "risks": [], "questions": []}
    return chaps, items


def test_summary_prompt_carries_the_material_not_the_transcript():
    """摘要**從已驗證的材料寫**，不重讀逐字稿 —— 這樣它不可能冒出新事實。"""
    chaps, items = _mat()
    p = M.build_summary_prompt(chaps, items)
    assert "採購進度" in p and "三十二萬" in p
    assert "陳經理" in p and "下週三" in p


def test_summary_grounding_catches_an_invented_number():
    """**中文數字一定要抓** —— 會議裡的金額是「三十二萬」不是「320000」，
    只查阿拉伯數字的話，最可能被捏造的那種數字剛好漏掉。"""
    chaps, items = _mat()
    ok, bad = M.summary_is_grounded(
        "會議決定改用立信，維護費一年四十五萬，並請陳經理下週三提供報價單。",
        chaps, items)
    assert not ok and "四十五萬" in bad
    ok2, bad2 = M.summary_is_grounded("預算提高到 3000000 元", chaps, items)
    assert not ok2 and "3000000" in bad2


def test_summary_grounding_does_not_trip_on_ordinary_chinese():
    """「一年」「一條」裡的「一」是單字，不可以被當成數字報出來。"""
    chaps, items = _mat()
    ok, bad = M.summary_is_grounded(
        "決定改用立信，維護費一年三十二萬，先簽一年。", chaps, items)
    assert ok, bad


def test_summary_grounding_catches_an_invented_product_name():
    chaps, items = _mat()
    ok, bad = M.summary_is_grounded("將導入 Salesforce 以改善流程", chaps, items)
    assert not ok and "Salesforce" in bad


def test_summary_grounding_allows_a_faithful_rewrite():
    """中文本來就要改寫成通順的句子 —— 改寫不可以被判成違規。"""
    chaps, items = _mat()
    ok, bad = M.summary_is_grounded(
        "本次會議聚焦採購進度與系統調整。採購方面決定改用立信，"
        "維護費一年三十二萬；後續由陳經理在下週三前提供報價單。", chaps, items)
    assert ok, bad


def test_summary_is_kept_even_when_it_fails_grounding():
    """**沉默地拿掉比標記出來更糟** —— 摘要是使用者最先看的東西。"""
    chaps, items = _mat()
    got = M.build_summary(chaps, items,
                          lambda _p: '{"summary":"預算 9999 萬全數通過"}')
    assert got["text"] and got["grounded"] is False
    assert "9999" in got["unsupported"]


def test_summary_survives_a_broken_reply():
    chaps, items = _mat()
    assert M.build_summary(chaps, items, lambda _p: "")["text"] == ""
    def boom(_p): raise RuntimeError("掛了")
    assert M.build_summary(chaps, items, boom)["text"] == ""


# -------------------------------------------------------------- 整條管線

def _fake_model(prompt: str) -> str:
    # **分支要認各自獨有的句子** —— 「章節」兩個字在摘要的提示裡也有
    # （素材清單的【章節】標題），拿它當判準會撞在一起。
    if "請把它切成" in prompt:
        return '{"chapters":[{"title":"採購","start_seq":1,"end_seq":2}]}'
    if "請逐條判斷" in prompt:
        return '{"verdicts":[{"id":1,"keep":"decisions"}]}'
    if "請寫一段" in prompt:
        return '{"summary":"會議決定改用立信。"}'
    return '{"decisions":[{"text":"那就改用立信","segment_ids":[1]}]}'


def test_full_analysis_produces_every_layer():
    segs = [seg(1, "那就改用立信，時程比較重要"), seg(2, "好，就這樣")]
    got = M.full_analysis(segs, _fake_model)
    assert got.items["decisions"] and got.chapters and got.mindmap
    assert got.summary["text"] and got.summary["grounded"]
    assert got.stats and got.calls >= 4
    pub = got.to_public()
    assert set(pub) >= {"summary", "items", "chapters", "mindmap", "charts"}


def test_a_failing_stage_does_not_wipe_out_the_rest():
    """章節切不出來仍然要有項目 —— **沒有一步可以讓整場分析歸零**。"""
    def flaky(prompt: str) -> str:
        if "章節" in prompt:
            raise RuntimeError("章節掛了")
        return _fake_model(prompt)

    got = M.full_analysis([seg(1, "那就改用立信，時程比較重要")], flaky)
    assert got.items["decisions"], "項目不可以因為章節失敗而消失"
    # **退化成整場一章，不是回空的** —— 空的話心智圖就沒有上層節點整個垮掉，
    # 而「整場是一個未分段的區塊」是真話不是編的。
    assert [c["title"] for c in got.chapters] == [M.FALLBACK_CHAPTER_TITLE]
    assert got.mindmap, "退化之後心智圖仍然要建得起來"
    assert "timeline" not in got.charts, "只有一章就不該畫時間軸"


def test_stage_names_are_reported_for_progress():
    """三小時的會議跑好幾分鐘 —— **進度要說得出在做什麼**，
    只有百分比的話使用者分不出「在跑」還是「卡住」。"""
    seen: list[str] = []
    M.full_analysis([seg(1, "那就改用立信")], _fake_model,
                    on_stage=lambda name, i, n: seen.append(name))
    assert set(seen) <= set(M.STAGES) and seen
    assert M.STAGES[2] in seen and M.STAGES[3] in seen


def test_review_sees_the_segments_around_the_citation():
    """**實測**：「教育訓練要辦幾場，這個我沒辦法決定」只引用了那一句，
    複審看不到下一句「等總經理回覆」，三次都把它當成隨口抱怨刪掉。
    少了後面那一句，它確實看起來不像未決問題。"""
    by = {10: seg(10, "前面無關的話"),
          11: seg(11, "教育訓練要辦幾場，這個我沒辦法決定"),
          12: seg(12, "那等總經理那邊回覆再說")}
    got = M.build_review_prompt([(1, "questions",
                                  {"text": "教育訓練要辦幾場",
                                   "segment_ids": [11]})], by)
    assert "原文(11)" in got
    assert "等總經理" in got, "後面那一句沒給複審看，它就判不出這是未決問題"
    assert "前後(12)" in got, "前後文要標示出來，不要跟被引用的混在一起"


def test_merge_collapses_a_later_reconfirmation():
    """會議後段又確認一次（「所以索引那件事就這一版做對吧？」「對」）——
    引用的是**完全不同**的段落，段號併不起來，要靠文字。
    不處理的話同一個決議會在畫面上出現兩次。
    """
    got = M.merge_kind([
        {"text": "這一版先加索引不改結構", "segment_ids": [5]},
        {"text": "這一版只加索引不改結構", "segment_ids": [480]},
    ])
    assert len(got) == 1
    assert got[0]["segment_ids"] == [5, 480], "兩處都要留著，點哪一個都跳得到"


def test_merge_keeps_two_similar_but_different_decisions_apart():
    """**這條是文字合併能不能存在的前提。**

    「測試環境升到 3.2」與「正式環境維持 3.0」看起來很像但是兩件事，
    併掉的話會有一個環境的決議憑空消失。
    """
    got = M.merge_kind([
        {"text": "測試環境的版本統一升到 3.2", "segment_ids": [10]},
        {"text": "正式環境這一波先不動，維持 3.0", "segment_ids": [11]},
    ])
    assert len(got) == 2, f"不可以併掉：{got}"


@pytest.mark.parametrize("a,b,should_merge", [
    ("改用立信", "決定改用立信", True),
    ("測試環境升到 3.2", "正式環境維持 3.0", False),
    ("不跳過測試環境", "不跳過驗收環境", False),
    ("請陳經理提供報價單", "中午要吃什麼", False),
])
def test_text_similarity_merge_boundaries(a, b, should_merge):
    got = M.merge_kind([{"text": a, "segment_ids": [1]},
                        {"text": b, "segment_ids": [2]}])
    assert (len(got) == 1) is should_merge, f"{a} / {b} → {got}"


# -------------------------------------------------------- 章節數量收斂

def test_condense_leaves_a_good_result_alone():
    """**上限是上限不是目標。** 19.7 分鐘切出 4 章是好結果，不可以被併壞。"""
    segs = [seg(i, "字" * 20, start=i * 9000, end=i * 9000 + 8000)
            for i in range(1, 133)]          # 約 19.8 分鐘
    chaps = M.tidy_chapters(
        [{"title": f"議題{k}", "start_seq": 1 + k * 33, "end_seq": 33 + k * 33}
         for k in range(4)], segs)
    assert M.condense_chapters(chaps, segs) == chaps


def test_condense_rescues_an_absurd_result():
    """實測 160 分鐘的語料模型切出 241 章 —— 那樣時間軸整個沒有用。"""
    segs = [seg(i, "字" * 20, start=i * 9000, end=i * 9000 + 8000)
            for i in range(1, 1001)]         # 約 150 分鐘
    chaps = M.tidy_chapters(
        [{"title": f"微主題{k}", "start_seq": 1 + k * 4, "end_seq": 4 + k * 4}
         for k in range(250)], segs)
    got = M.condense_chapters(chaps, segs)
    assert len(got) <= M._chapter_cap(segs[-1]["end_ms"] - segs[0]["start_ms"])
    assert got[0]["start_seq"] == 1 and got[-1]["end_seq"] == 1000
    for a, b in zip(got, got[1:]):
        assert b["start_seq"] == a["end_seq"] + 1, "併完仍然要連續不重疊"


def test_condense_keeps_the_longer_chapters_title():
    """短的那一段多半是過場，標題要取有內容的那一邊。"""
    segs = [seg(i, "字", start=i * 1000, end=i * 1000 + 900)
            for i in range(1, 21)]
    got = M.condense_chapters(
        [{"title": "主要討論", "start_seq": 1, "end_seq": 18},
         {"title": "過場", "start_seq": 19, "end_seq": 20}], segs, target=1)
    assert [c["title"] for c in got] == ["主要討論"]
    assert got[0]["start_seq"] == 1 and got[0]["end_seq"] == 20


def test_chapter_rules_survive_substitution():
    """提示裡有 JSON 的大括號 —— **不可以用 .format()**，會 KeyError。"""
    rules = (M._CHAPTER_RULES.replace("{minutes}", "20").replace("{want}", "5"))
    assert "{minutes}" not in rules and "{want}" not in rules
    assert '"chapters"' in rules, "JSON 範例要原封不動留著"


# ------------------------------------------------------------ 複審拆條

_SPLIT_SEG = {88: seg(88, "機房那邊請陳經理去確認電力，網路的部分林工程師接")}
_SPLIT_ITEM = {"text": "機房那邊請陳經理去確認電力，網路的部分林工程師接",
               "owner": "陳經理", "segment_ids": [88]}


def test_review_splits_one_sentence_into_two_tasks():
    """不拆的話，林工程師在任務清單上**永遠看不到自己的工作**。"""
    items = {k: [] for k in M.KINDS}
    items["actions"] = [dict(_SPLIT_ITEM)]
    kept, _ = M.review(items, _SPLIT_SEG, lambda _p: json.dumps({"verdicts": [
        {"id": 1, "keep": "actions", "split": [
            {"text": "確認機房電力", "owner": "陳經理", "segment_ids": [88]},
            {"text": "負責網路的部分", "owner": "林工程師", "segment_ids": [88]}]}]}))
    got = kept["actions"]
    assert len(got) == 2
    assert {g["owner"] for g in got} == {"陳經理", "林工程師"}


@pytest.mark.parametrize("split,why", [
    ([{"text": "確認機房電力", "segment_ids": [88]},
      {"text": "順便把預算提高到三百萬", "segment_ids": [88]}], "編出來的內容"),
    ([{"text": "確認機房電力", "segment_ids": [88]},
      {"text": "負責網路的部分", "segment_ids": [999]}], "引用原本沒用過的段號"),
    ([{"text": "", "segment_ids": [88]},
      {"text": "負責網路的部分", "segment_ids": [88]}], "空的內容"),
])
def test_split_that_breaks_the_rules_is_rejected_whole(split, why):
    """**任何一條不合格就整組不拆** —— 寧可粗一點，不要錯。"""
    items = {k: [] for k in M.KINDS}
    items["actions"] = [dict(_SPLIT_ITEM)]
    kept, _ = M.review(items, _SPLIT_SEG, lambda _p: json.dumps(
        {"verdicts": [{"id": 1, "keep": "actions", "split": split}]}))
    assert len(kept["actions"]) == 1, why
    assert kept["actions"][0]["text"] == _SPLIT_ITEM["text"]


def test_split_prompt_shows_the_worked_example():
    got = M.build_review_prompt([(1, "actions", _SPLIT_ITEM)], _SPLIT_SEG)
    assert "拆開" in got and "split" in got


def test_a_meeting_with_nothing_to_report_says_so_plainly():
    """**實測抓到的真缺陷**：一場純進度同步的會議，模型寫出
    「會議已完成相關項目的整理，並確認了後續的執行方向」—— **那是假的**，
    而依據檢查抓不到（它只查數字與拉丁詞，這句話兩者都沒有）。

    沒有項目時根本不要叫模型寫，誠實的一句話比通順的空話有用。
    """
    called = {"n": 0}

    def ask(_p: str) -> str:
        called["n"] += 1
        return '{"summary":"會議確認了後續的執行方向。"}'

    got = M.build_summary([{"title": "進度同步", "percentage": 100.0}],
                          {k: [] for k in M.KINDS}, ask)
    assert called["n"] == 0, "沒有項目就不該呼叫模型"
    assert got["empty"] is True
    assert "沒有做出決議" in got["text"]
    assert "確認了後續" not in got["text"]
    assert "進度同步" in got["text"], "談了什麼還是要講出來"


def test_empty_summary_omits_the_fallback_chapter_title():
    """退化成「會議內容」那一章時，不要寫「這場會議談了會議內容」。"""
    got = M.build_summary(
        [{"title": M.FALLBACK_CHAPTER_TITLE, "percentage": 100.0}],
        {k: [] for k in M.KINDS}, lambda _p: "")
    assert got["text"].startswith("沒有做出決議")


# ------------------------------------------------------------------ 沒有時間戳記

def _plain(n=12):
    """純文字逐字稿：**沒有 speaker、沒有時間**（.txt / .docx 本來就是這樣）。"""
    return [{"seq": i, "text": f"第 {i} 句，這裡講了一些事情。"} for i in range(1, n + 1)]


def test_chapters_still_work_without_timestamps():
    """**章節是內容的分段，不需要時間。**

    2026-09-18 拿真的語料跑才發現：以前 `chapter_times` 直接
    `int(seg["end_ms"])`，純文字逐字稿一進來就 `KeyError` →
    被 `build_chapters` 的 try 吞掉 → **摘要與決議照常，只有章節安靜消失**，
    而畫面上看起來只像「這場會議沒有分章節」。
    """
    segs = _plain()
    chaps = M.chapter_times([{"title": "開場", "start_seq": 1, "end_seq": 6},
                              {"title": "討論", "start_seq": 7, "end_seq": 12}], segs)
    assert [c["title"] for c in chaps] == ["開場", "討論"]
    assert chaps[0]["segment_ids"] == list(range(1, 7))
    # **沒有時間就不要編一個出來** —— 猜一個數字比不給更糟
    assert "start_ms" not in chaps[0] and "duration_ms" not in chaps[0]
    assert "percentage" not in chaps[0]


def test_condense_and_build_do_not_need_timestamps():
    segs = _plain(40)
    chaps = [{"title": f"章 {i}", "start_seq": i * 2 - 1, "end_seq": i * 2}
             for i in range(1, 21)]
    out = M.condense_chapters(chaps, segs)
    assert out and len(out) <= len(chaps), "沒有時間時也要算得出上限"

    calls = []

    def ask(p):
        calls.append(p)
        return json.dumps({"chapters": [{"title": "全部", "start_seq": 1,
                                         "end_seq": len(segs)}]}, ensure_ascii=False)

    built = M.build_chapters(segs, ask)
    assert built and built[0]["title"] == "全部"
    assert calls, "沒有時間時仍然要問模型，不是整段跳過"


def test_the_mindmap_omits_times_it_does_not_have():
    segs = _plain()
    chaps = M.chapter_times([{"title": "開場", "start_seq": 1, "end_seq": 12}], segs)
    nodes = M.build_mindmap(chaps, {k: [] for k in M.KINDS})
    assert nodes and nodes[0]["label"] == "開場"
    assert "start_ms" not in nodes[0], "沒有時間的節點不可以帶一個假的時間"


def test_timestamps_still_produce_times_when_they_are_there():
    """反向對照：只驗「沒有時間也不會爆」的話，把時間整段拿掉也會過。"""
    segs = [{"seq": i, "text": f"第 {i} 句", "start_ms": (i - 1) * 1000,
             "end_ms": i * 1000} for i in range(1, 13)]
    chaps = M.chapter_times([{"title": "開場", "start_seq": 1, "end_seq": 6},
                              {"title": "討論", "start_seq": 7, "end_seq": 12}], segs)
    assert chaps[0]["start_ms"] == 0 and chaps[0]["end_ms"] == 6000
    assert chaps[0]["duration_ms"] == 6000
    assert abs(chaps[0]["percentage"] - 50.0) < 0.1


def test_speaker_stats_work_with_no_timestamps_at_all():
    """**「誰講最多」不需要時間。**

    2026-09-18 使用者上傳沒有時間戳記的會議紀錄，整個發言者統計區塊消失 ——
    而那個問題本來就答得出來：發言次數與字數都是確定性的。
    """
    segs = [{"seq": 1, "speaker": "主席", "text": "各位早" * 10},
            {"seq": 2, "speaker": "李委員", "text": "我有意見"},
            {"seq": 3, "speaker": "主席", "text": "請說"}]
    st = M.speaker_stats(segs)
    assert set(st) == {"主席", "李委員"}
    assert st["主席"]["turn_count"] == 2 and st["李委員"]["turn_count"] == 1
    assert st["主席"]["chars"] > st["李委員"]["chars"]
    assert abs(sum(v["char_pct"] for v in st.values()) - 100.0) < 0.2
    # 沒有時間就**不要編一個出來**
    assert all("speaking_ms" not in v for v in st.values())
    # 而且圖要出得來（畫的是字數佔比）
    assert "speaker_share" in M.suitable_charts(
        [{"title": "a", "start_seq": 1, "end_seq": 2},
         {"title": "b", "start_seq": 3, "end_seq": 3}],
        {k: [] for k in M.KINDS}, st)


# ================================================================== 很長的會議

def test_the_extraction_prompt_stays_bounded_no_matter_how_long_a_turn_is():
    """**一個人連講三萬字也不可以做出三萬字的提示。**

    委員會逐字稿常常一個人連講好幾分鐘 ＝ 一段兩、三萬字。視窗是整段整段
    加的，不切開的話單一視窗會膨脹到模型的上下文外面 —— 而 Ollama 多半是
    **安靜地截斷**（只看到尾巴），不是報錯。也就是「看起來成功、其實漏了一半」。
    2026-09-18 在真的委員會逐字稿上量到最大 62,423 字，切開之後 6,715 字。
    """
    def biggest(repeat):
        segs = ([seg(1, "短的一句話。")]
                + [seg(2, "這是一段非常長的發言。" * repeat)]
                + [seg(3, "結尾一句。")])
        wins = M.make_windows(segs, window_chars=3000)
        return max(len(M.build_prompt(w)) for w in wins), wins

    # **判準是「把那段話加倍，提示不可以跟著變大」** ——
    # 寫一個魔術數字當上限的話，改一下提示文字它就紅，而它要守的東西沒變。
    small, _ = biggest(2000)      # 約兩萬字
    large, wins = biggest(8000)   # 約八萬字
    assert large <= small * 1.05, f"提示跟著發言長度成長：{small} → {large} 字"
    assert large < 3000 * 3, f"單一提示太大：{large} 字"
    # 內容不可以掉、`seq` 不可以變
    # **視窗是重疊的**（那是刻意的：決議常常橫跨幾個發言），所以同一段話會
    # 出現在不只一個視窗裡 —— 判準是「一個字都沒掉」，不是「剛好出現一次」。
    joined = "".join(x["text"] for w in wins for x in w.segments)
    assert joined.count("這是一段非常長的發言。") >= 8000, "切開時掉字了"
    assert {x["seq"] for w in wins for x in w.segments} == {1, 2, 3}


def test_the_review_is_batched_so_one_long_meeting_does_not_blow_the_context():
    """**複審不可以把所有候選一次送出去。**

    一場長會議的候選加上它們引用的原文可以到 20 萬字（2026-09-18 量的）。
    超過上下文時模型只看得到尾巴那幾條，前面的全部沒有裁決 ——
    而「沒有裁決＝維持原樣」是刻意的安全設計，於是**結果看起來完全正常，
    只是複審根本沒發生**。
    """
    segs = [seg(i, f"第 {i} 段的內容，長度大概是這樣。" * 12) for i in range(1, 61)]
    by_seq = {int(s["seq"]): s for s in segs}
    items = {"decisions": [{"text": f"決議 {i}", "segment_ids": [i]}
                           for i in range(1, 41)],
             "actions": [], "risks": [], "questions": []}
    sizes = []

    def ask(prompt):
        sizes.append(len(prompt))
        return json.dumps({"verdicts": []}, ensure_ascii=False)

    M.review(items, by_seq, ask)
    assert len(sizes) > 1, "沒有分批"
    assert max(sizes) <= M._REVIEW_BATCH_CHARS * 1.3, \
        f"有一批太大：{max(sizes)} 字"


def test_one_failed_review_batch_only_costs_that_batch():
    """**一批失敗只損失那一批** —— 其餘照常複審。"""
    segs = [seg(i, f"第 {i} 段。" * 40) for i in range(1, 61)]
    by_seq = {int(s["seq"]): s for s in segs}
    items = {"decisions": [{"text": f"決議 {i}", "segment_ids": [i]}
                           for i in range(1, 41)],
             "actions": [], "risks": [], "questions": []}
    calls = {"n": 0}

    def ask(prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("這一批炸了")
        return json.dumps({"verdicts": []}, ensure_ascii=False)

    out, dropped = M.review(items, by_seq, ask)
    assert calls["n"] > 1, "第一批失敗就整個放棄了"
    assert len(out["decisions"]) == 40, "沒有裁決的要維持原樣，不可以消失"


# ---------------------------------------------------------------------------
# 會議背景（使用者填的主題／與會者職稱／自訂說明）
#
# 使用者 2026-09-19 要求「拉入檔案或貼上內容的下面 要有一個地方貼入逐字稿以外
# 資訊的，例如 主題 參與者介紹/職稱 等等 或是使用者要填入 prompt」。
#
# **這個功能最大的風險是它變成項目的來源。** 這支工具的保證是「每一條都指得回
# 逐字稿」—— 背景資料裡寫的事情**沒有在會議上發生過**，如果它能生出決議，
# 保證就破了，而且讀的人完全看不出來（那一條長得跟真的一模一樣）。
#
# 提示裡講了、順序也排過（規則在前、背景在中、逐字稿在後），
# 但那兩道都要**模型願意配合**。真正不靠模型的是引用驗證：
# 項目必須指得回逐字稿的段落，而背景資料不在被比對的範圍裡。
# 下面這幾條驗的就是那一道。
# ---------------------------------------------------------------------------

def _ctx_segs(n=6):
    """⚠ 名字不可以叫 `_segs` —— 這個檔案上面已經有一個同名的 helper，
    章節那幾條測試在用。我第一版就撞名，於是**五條完全不相干的測試變紅**
    （`assert [1] == [1, 15]`，看起來像章節邏輯壞掉）。"""
    return [{"seq": i, "speaker": "甲",
             "text": f"第{i}句，我們把倉儲系統的驗收時間往後挪兩週。"}
            for i in range(1, n + 1)]


def test_the_context_reaches_the_prompt_but_the_rules_bracket_it():
    """順序：我們的規則 → 使用者的字 → 我們再重申一次 → 逐字稿。

    使用者填的內容有可能（有意或無意）寫得像指令，**最後說話的必須是我們**。
    """
    seen = []

    def ask(p):
        seen.append(p)
        return '{"decisions":[],"actions":[],"risks":[],"questions":[]}'

    M.analyse(_ctx_segs(), ask, context="趙明哲：營運副總經理，會議主席")
    p = seen[0]
    assert "營運副總經理" in p, "背景沒有進到提示裡"
    assert p.index("你是會議記錄整理員") < p.index("營運副總經理"), \
        "規則要在使用者填的文字前面"
    assert p.index("營運副總經理") < p.index("一律只能來自下面的逐字稿"), \
        "使用者的文字後面要再重申一次規則"
    assert p.index("一律只能來自下面的逐字稿") < p.index("第1句"), \
        "逐字稿要在最後"


def test_no_context_means_no_extra_text_in_the_prompt():
    """沒填就完全不動提示 —— 不要留一段「（無）」佔著窗口。"""
    seen = []

    def ask(p):
        seen.append(p)
        return '{"decisions":[],"actions":[],"risks":[],"questions":[]}'

    M.analyse(_ctx_segs(), ask)
    assert "背景資料" not in seen[0]
    assert M.build_context_block("") == ""
    assert M.build_context_block(None) == ""
    assert M.build_context_block("   \n  ") == ""


def test_an_item_that_comes_from_the_context_is_dropped():
    """**這是最重要的一條。**

    模型照著背景資料生出一條決議（背景裡寫「已決定改用立信」，
    但會議上完全沒談到），而且隨便指一個段號 —— 引用驗證要把它丟掉。
    """
    ctx = "背景：公司已經決定改用立信的倉儲系統，本次會議追認。"
    fabricated = json.dumps({
        "decisions": [{"text": "改用立信的倉儲系統", "segment_ids": [2]}],
        "actions": [], "risks": [], "questions": []}, ensure_ascii=False)

    got = M.analyse(_ctx_segs(), lambda p: fabricated, context=ctx)
    texts = [d["text"] for d in got.items["decisions"]]
    assert "改用立信的倉儲系統" not in texts, (
        "從背景資料編出來的決議沒有被丟掉 —— "
        "「每一條都指得回逐字稿」的保證破了，而且讀的人看不出來")


def test_an_item_that_really_is_in_the_transcript_still_survives():
    """反向釘住 —— 只驗上一條的話，把所有項目都丟掉也會過。"""
    real = json.dumps({
        "decisions": [{"text": "倉儲系統的驗收時間往後挪兩週",
                       "segment_ids": [2]}],
        "actions": [], "risks": [], "questions": []}, ensure_ascii=False)
    got = M.analyse(_ctx_segs(), lambda p: real, context="趙明哲：營運副總經理")
    texts = [d["text"] for d in got.items["decisions"]]
    assert any("挪兩週" in x for x in texts), \
        "真的講過的決議被丟掉了 —— 背景資料不該影響正常項目"


def test_the_context_is_capped():
    """**要有上限** —— 它跟著每一個視窗送出去，一份長會議會送幾十次。"""
    block = M.build_context_block("字" * (M.MAX_CONTEXT_CHARS * 3))
    assert len(block) < M.MAX_CONTEXT_CHARS + 600, \
        f"背景沒有被截短（{len(block)} 字），長會議會把它乘上幾十倍送出去"
    assert block.endswith("\n")


def test_a_real_decision_about_an_agenda_item_is_not_mistaken_for_a_copy():
    """**誤殺才是這道檢查最大的風險。**

    使用者填的背景多半就是議程 —— 議程上的題目**本來就是**會上要談的事。
    只看「跟背景像不像」的話，真的決定了議程上那件事的決議會被丟掉，
    而使用者完全看不出來為什麼少了一條。

    所以判準要兩個條件都成立：跟背景很像、**而且**明顯比跟逐字稿更像。
    """
    ctx = ("會議主題：倉儲系統驗收時程與大促容量整備\n"
           "與會者\n趙明哲：營運副總經理，會議主席\n林冠宇：基礎架構部經理")
    segs = [
        {"seq": 1, "speaker": "趙明哲", "text": "先講倉儲系統驗收，時程要不要動。"},
        {"seq": 2, "speaker": "林冠宇",
         "text": "我建議倉儲系統的驗收時間往後挪兩週，機房那邊還沒好。"},
        {"seq": 3, "speaker": "趙明哲",
         "text": "好，那就決定倉儲系統驗收往後挪兩週，十月三號前給我新的時程表。"}]
    by = {s["seq"]: s for s in segs}

    keep = [("倉儲系統驗收往後挪兩週", [3]),      # 真的決定了，而且議程上也有
            ("十月三號前提出新的時程表", [3])]    # 一般待辦
    drop = [("會議主題：倉儲系統驗收時程與大促容量整備", [1]),  # 抄主題
            ("趙明哲：營運副總經理，會議主席", [1])]            # 抄名單

    for text, ids in keep:
        assert not M.looks_copied_from_context({"text": text, "segment_ids": ids},
                                               ctx, by), \
            f"真的講過的內容被當成抄背景：{text!r}"
    for text, ids in drop:
        assert M.looks_copied_from_context({"text": text, "segment_ids": ids},
                                           ctx, by), \
            f"從背景抄的沒被擋下來：{text!r}"
