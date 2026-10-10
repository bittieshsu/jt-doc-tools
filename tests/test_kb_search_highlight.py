"""公文知識庫「檢索測試」把符合處標出來（2026-10-09 使用者：「下面有檢索符合的
請用高亮把符合處標出來」）。

判準是**標對的地方、不標到處都有的東西**：
* 比對跟關鍵字那一路同一套（正規化、拿掉空白比子字串），換行夾在中間照樣標得到；
  位置換算回**原文**的字元索引。
* 一段連在一起的符合處，要有一個「兩個字以上、知識庫裡不到一半的段落有」的詞才標
  —— 單獨的「機關」（法規裡 77% 的段落都有）或單一個字標出來只會滿版黃色。
* 英數詞要整個詞符合，邊界看**原文**（拿掉空白之後 `vmware esxi` 的 esxi 前面緊接 e）。
* 只有檢索測試頁要標記；給工具用的 `retrieval.search()` 不帶（公文撰擬用不到）。
"""
from __future__ import annotations

import pytest

from app.core.kb import retrieval, store
from tests._kb_support import HANDBOOK_TXT, add_doc, kb_isolated  # noqa: F401


def _marked(text: str, terms: dict[str, float]) -> list[str]:
    return [text[a:b] for a, b in retrieval.highlight_spans(text, terms)]


def test_a_common_word_alone_is_not_marked():
    assert _marked("各機關應依規定辦理。", {"機關": 0.77}) == []


def test_a_common_word_inside_a_rarer_run_is_marked_with_it():
    terms = {"上級": 0.10, "級機": 0.02, "機關": 0.77}
    assert _marked("報請上級機關核定，並副知各機關。", terms) == ["上級機關"]


def test_a_line_break_between_two_characters_does_not_stop_the_mark():
    terms = {"資訊": 0.13, "訊安": 0.02, "安全": 0.20}
    assert _marked("落實資訊\n安全管理", terms) == ["資訊\n安全"]


def test_single_characters_are_never_marked_alone():
    assert _marked("得以函送辦理。", {"函": 0.01}) == []


def test_ascii_words_must_match_whole_words_on_the_original_text():
    t = {"esxi": 0.01}
    assert _marked("升級 VMware ESXi 8 主機", t) == ["ESXi"]
    assert _marked("升級ＥＳＸｉ主機", t) == ["ＥＳＸｉ"]            # 全形照樣標
    assert _marked("型號 esxi8 與 noesxi", t) == []
    assert _marked("2026 年", {"2": 0.01}) == []


def test_spans_point_at_the_original_text_even_after_full_width_and_spaces():
    text = "（十二）　各 機 關辦理資訊安全稽核。"
    terms = {"資訊": 0.1, "訊安": 0.01, "安全": 0.2, "稽核": 0.05}
    spans = retrieval.highlight_spans(text, terms)
    assert [text[a:b] for a, b in spans] == ["資訊安全稽核"]
    assert all(0 <= a < b <= len(text) for a, b in spans)


def test_overlapping_and_adjacent_terms_merge_into_one_mark():
    terms = {"公文": 0.3, "文格": 0.05, "格式": 0.2}
    assert _marked("依公文格式辦理", terms) == ["公文格式"]


def test_empty_inputs():
    assert retrieval.highlight_spans("", {"x": 0.1}) == []
    assert retrieval.highlight_spans("內文", {}) == []


@pytest.fixture
def ds(kb_isolated):
    return store.create_dataset({"name": "文書規範", "category": "writing_rules"})


def test_term_ratios_come_from_the_knowledge_base(ds):
    add_doc(ds["id"], HANDBOOK_TXT.encode("utf-8"), ".txt")
    n = store.conn().execute("SELECT count(*) FROM kb_chunks").fetchone()[0]
    assert n >= 2
    r = retrieval.highlight_terms("機密文書封裝")
    assert r["封裝"] == pytest.approx(1 / n)
    assert "裝不" not in r and all(0 < v <= 1 for v in r.values())


def test_the_admin_search_returns_marks_and_the_tool_search_does_not(admin_session, ds):
    client, _, _ = admin_session
    add_doc(ds["id"], HANDBOOK_TXT.encode("utf-8"), ".txt")
    j = client.post("/admin/knowledge/api/search",
                    json={"query": "機密文書對外發文要怎麼封裝"}).json()
    assert j["results"], j
    top = j["results"][0]
    marks = [top["text"][a:b] for a, b in top["highlights"]]
    assert any("封裝" in m for m in marks), marks
    tool = retrieval.search("機密文書對外發文要怎麼封裝", user_id=None)
    assert tool and all("highlights" not in r for r in tool)
