"""知識庫：匯入流程（狀態、重複、中斷）、逐字索引（台／臺）、檢索門檻與排序（向量優先）。"""
from __future__ import annotations

import hashlib

import pytest

from app.core import cjk_fts
from app.core.kb import indexer, retrieval, store
from tests._kb_support import FakeEmbed, HANDBOOK_TXT, add_doc, kb_isolated, setup_embed  # noqa: F401


@pytest.fixture
def ds(kb_isolated):
    return store.create_dataset({"name": "文書規範", "category": "writing_rules"})


# ---------------------------------------------------------------- 逐字索引
def test_tokenize_and_variants():
    assert cjk_fts.tokenize("臺北市ABC１２３") == "台 北 市 abc123"
    assert cjk_fts.query_terms("公文格式") == ["公文", "文格", "格式"]
    assert cjk_fts.query_terms("函") == ["函"]
    assert "python" in cjk_fts.query_terms("Python 的寫法")
    assert cjk_fts.compact("( 十二) 各機關") == cjk_fts.compact("（十二）各機關")


def test_tai_and_tai_variant_match_each_other(ds):
    add_doc(ds["id"], "一、臺北市政府文書應採橫行格式辦理。".encode("utf-8"), ".txt", "a")
    allowed = retrieval._allowed_versions(None, None)
    hits = retrieval.keyword_hits("台北市政府的文書格式", allowed)
    assert hits and hits[0]["strong"]


# ---------------------------------------------------------------- 狀態流轉
def test_status_flow_and_only_active_documents_are_searchable(ds):
    data = HANDBOOK_TXT.encode("utf-8")
    v = store.create_version(ds["id"], title="手冊", filename="h.txt", ext=".txt", data=data,
                             sha256=hashlib.sha256(data).hexdigest(), meta={})
    assert v["status"] == "uploaded"
    res = indexer.process_version(v["id"])
    assert res["ok"] and store.get_version(v["id"])["status"] == "ready"
    q = "下級機關對上級機關的稱謂"
    assert retrieval.search(q, user_id=None) == []                 # ready 還不能被查到
    with pytest.raises(store.KBError):                             # uploaded→active 不行
        store.transition(v["id"], allowed_from=("inactive",), to="active")
    store.transition(v["id"], allowed_from=("ready",), to="active", activated_by="admin")
    got = retrieval.search(q, user_id=None)
    assert got and "鈞" in got[0]["text"]
    assert got[0]["purpose"] == "format_reference"
    store.transition(v["id"], allowed_from=("active",), to="inactive")
    assert retrieval.search(q, user_id=None) == []                 # 停用之後不進檢索


def test_disabled_dataset_is_not_searched(ds):
    add_doc(ds["id"], HANDBOOK_TXT.encode("utf-8"), ".txt")
    q = "機密文書對外發文要怎麼封裝"
    assert retrieval.search(q, user_id=None)
    store.update_dataset(ds["id"], {**ds, "enabled": False})
    assert retrieval.search(q, user_id=None) == []


def test_duplicate_content_in_same_dataset_is_rejected(ds):
    data = HANDBOOK_TXT.encode("utf-8")
    add_doc(ds["id"], data, ".txt")
    with pytest.raises(store.KBError):
        add_doc(ds["id"], data, ".txt")
    other = store.create_dataset({"name": "另一個", "category": "examples"})
    add_doc(other["id"], data, ".txt")          # 不同資料集可以放同一份


def test_extract_failure_marks_failed_and_keeps_reason(ds):
    data = b"%PDF-1.4 broken"
    v = store.create_version(ds["id"], title="壞檔", filename="b.pdf", ext=".pdf", data=data,
                             sha256=hashlib.sha256(data).hexdigest(), meta={})
    res = indexer.process_version(v["id"])
    v = store.get_version(v["id"])
    assert not res["ok"] and v["status"] == "failed" and v["error"]


def test_interrupted_imports_are_marked_failed_on_next_start(ds):
    data = HANDBOOK_TXT.encode("utf-8")
    v = store.create_version(ds["id"], title="x", filename="x.txt", ext=".txt", data=data,
                             sha256=hashlib.sha256(data).hexdigest(), meta={})
    store.set_status(v["id"], "indexing")
    store._INITED.discard(str(store.db_path()))     # 模擬新的行程第一次打開資料庫
    store.conn()
    v = store.get_version(v["id"])
    assert v["status"] == "failed" and "重新啟動" in v["error"]


def test_chunks_are_linked_to_neighbours(ds):
    v = add_doc(ds["id"], ("一、" + "內容很長的一段話。" * 200).encode("utf-8"), ".txt")
    chunks = store.list_chunks(v["id"], limit=50)
    assert len(chunks) >= 2
    for a, b in zip(chunks, chunks[1:]):
        assert a["next_id"] == b["id"] and b["prev_id"] == a["id"]
    assert chunks[0]["prev_id"] == "" and chunks[-1]["next_id"] == ""


def test_delete_version_removes_file_chunks_and_index(ds):
    v = add_doc(ds["id"], HANDBOOK_TXT.encode("utf-8"), ".txt")
    p = store.stored_path(v)
    assert p.is_file()
    store.delete_version(v["id"])
    assert not p.exists()
    assert store.conn().execute("SELECT COUNT(*) FROM kb_chunks").fetchone()[0] == 0
    assert store.conn().execute("SELECT COUNT(*) FROM kb_fts").fetchone()[0] == 0


def test_field_validation(ds):
    with pytest.raises(store.KBError):
        store.clean_url("javascript:alert(1)")
    with pytest.raises(store.KBError):
        store.clean_date("2026/1/1", "日期")
    assert store.clean_date("", "日期") == ""
    with pytest.raises(store.KBError):
        store.create_dataset({"name": "x", "category": "no-such"})
    with pytest.raises(store.KBError):
        store.create_dataset({"name": "y", "category": "examples", "access": "groups", "group_ids": []})


# ---------------------------------------------------------------- 門檻與排序
def test_irrelevant_questions_return_nothing_without_vectors(ds):
    add_doc(ds["id"], HANDBOOK_TXT.encode("utf-8"), ".txt")
    for q in ("如何烤出好吃的戚風蛋糕", "Python 的 list comprehension 怎麼寫"):
        d = retrieval.search_detail(q, user_id=None)
        assert d["results"] == [], (q, d["results"])
        assert d["mode"] == "keyword" and d["note"]          # 講出這次只用關鍵字


def _many_docs(ds_id: str) -> None:
    """12 份小文件，「關防」只出現在其中 3 份 —— 它的 IDF 介於「只出現一次」的一半
    與一倍之間，正好是「弱命中」（見 retrieval 的 KW_WEAK_MASS / KW_MIN_MASS）。"""
    words = ["會計", "採購", "人事", "總務", "研考", "資訊", "法制", "政風", "主計", "秘書", "文書", "檔案"]
    for n, w in enumerate(words, start=1):
        extra = "應加蓋關防。" if n in (2, 5, 9) else ""
        add_doc(ds_id, f"{n}、{w}業務的作業說明。{extra}".encode("utf-8"), ".txt", w)


def test_weak_keyword_hit_needs_vector_agreement(ds, monkeypatch):
    _many_docs(ds["id"])
    allowed = retrieval._allowed_versions(None, None)
    hits = retrieval.keyword_hits("關防", allowed)
    assert len(hits) == 3 and all(not h["strong"] for h in hits), hits   # 前提：真的是弱命中
    # 沒有向量時：弱命中不算
    assert retrieval.search("關防", user_id=None) == []
    # 有向量時：只有向量那一路也找到的那一筆算（向量那一路用替身，讓「找到哪一筆」確定）
    chosen = hits[1]["id"]
    monkeypatch.setattr(retrieval, "vector_hits",
                        lambda *a, **k: [{"id": chosen, "cos": 0.9}])
    with FakeEmbed() as fe:
        setup_embed(fe.base)
        indexer.rebuild()
        got = retrieval.search_detail("關防", user_id=None)
    ids = [r["chunk_id"] for r in got["results"]]
    assert ids == [chosen], got["results"]
    assert set(got["results"][0]["matched_by"]) == {"keyword", "vector"}


def test_hybrid_search_fuses_both_routes_and_reports_how(ds):
    add_doc(ds["id"], HANDBOOK_TXT.encode("utf-8"), ".txt")
    with FakeEmbed() as fe:
        setup_embed(fe.base)
        indexer.rebuild()
        d = retrieval.search_detail("下級機關對上級機關稱謂用語", user_id=None)
    assert d["mode"] == "hybrid"
    assert d["results"], d
    top = d["results"][0]
    assert "鈞" in top["text"]
    assert set(top["matched_by"]) <= {"keyword", "vector"} and top["matched_by"]
    assert d["score_note"] and "不是正確率" in d["score_note"]
    # 分數只拿來排序：由高到低
    scores = [r["score"] for r in d["results"]]
    assert scores == sorted(scores, reverse=True)


def _chunk_ids(contains: str) -> list[str]:
    rows = store.conn().execute("SELECT id, text FROM kb_chunks").fetchall()
    return [r["id"] for r in rows if contains in r["text"]]


def test_vector_order_wins_and_keyword_only_fills_the_gap(ds, monkeypatch):
    """有向量時照向量的名次排，關鍵字只在向量不到 k 筆時補在後面（2026-10-09 量過：
    等權合併 nDCG@6 0.787、只照向量 0.885，見 retrieval 的模組說明）。"""
    _many_docs(ds["id"])
    a, b = _chunk_ids("會計業務")[0], _chunk_ids("人事業務")[0]
    kw_target = _chunk_ids("採購業務")[0]
    allowed = retrieval._allowed_versions(None, None)
    kw = retrieval.keyword_hits("採購業務的作業說明", allowed)
    assert kw and kw[0]["id"] == kw_target and kw[0]["strong"], kw    # 前提：關鍵字第一名是「採購」
    # 向量那一路（替身）：會計、人事，沒有採購
    monkeypatch.setattr(retrieval, "vector_hits",
                        lambda *a_, **k_: [{"id": a, "cos": 0.9}, {"id": b, "cos": 0.8}])
    with FakeEmbed() as fe:
        setup_embed(fe.base)
        indexer.rebuild()
        got = retrieval.search_detail("採購業務的作業說明", user_id=None, k=8)
        two = retrieval.search_detail("採購業務的作業說明", user_id=None, k=2)
    ids = [r["chunk_id"] for r in got["results"]]
    assert ids[:2] == [a, b], ids                       # 關鍵字第一名不會插到向量前面
    assert kw_target in ids[2:], ids                    # 向量不夠 k 筆，關鍵字補在後面
    assert got["results"][2]["matched_by"] == ["keyword"]
    assert [r["chunk_id"] for r in two["results"]] == [a, b]   # 向量已經夠 k 筆：不補
    scores = [r["score"] for r in got["results"]]
    assert scores == sorted(scores, reverse=True) and len(set(scores)) == len(scores)


def test_a_vector_hit_also_found_by_keyword_says_both(ds, monkeypatch):
    _many_docs(ds["id"])
    target = _chunk_ids("採購業務")[0]
    monkeypatch.setattr(retrieval, "vector_hits", lambda *a_, **k_: [{"id": target, "cos": 0.9}])
    with FakeEmbed() as fe:
        setup_embed(fe.base)
        indexer.rebuild()
        got = retrieval.search_detail("採購業務的作業說明", user_id=None)
    top = got["results"][0]
    assert top["chunk_id"] == target and top["matched_by"] == ["vector", "keyword"], top


def _common_word_corpus(ds_id: str) -> None:
    """「機密」「文書」「解密」各自出現在很多段（每個詞權重都小），只有一段三個都有。
    匯入整批法規之後的知識庫就是這個樣子（2026-10-08 實測：「機密文書 解密」只用關鍵字時 0 筆）。"""
    for n in range(1, 31):
        # 「機密文書」（機密、密文、文書）與「解密」各出現在一半的段落，但不在同一段 ——
        # 每一個詞都常見，前提要先成立（不然只出現在目標那一段的詞自己就夠一倍，驗不到）
        w = "機密文書的保管" if n % 2 else "解密的程序"
        add_doc(ds_id, f"第 {n} 點　本點規定{w}，依各機關規定辦理。".encode("utf-8"), ".txt", f"規定{n}")
    add_doc(ds_id, "機密文書屆滿保密期限時，應由原核定機關辦理解密。".encode("utf-8"), ".txt", "解密作業")


def test_a_segment_with_every_query_word_counts_even_if_the_words_are_common(ds):
    _common_word_corpus(ds["id"])
    allowed = retrieval._allowed_versions(None, None)
    hits = retrieval.keyword_hits("機密文書 解密", allowed)
    target = [h for h in hits if h["coverage"] >= 0.999]
    assert target and all(h["mass"] < retrieval.KW_MIN_MASS for h in target), \
        ("前提：每個詞都常見，加起來不到一倍", target)
    assert all(h["strong"] for h in target), "詞都在的那一段應該算數"
    got = retrieval.search("機密文書 解密", user_id=None)
    assert got and "辦理解密" in got[0]["text"], [r["text"][:20] for r in got]


def test_two_common_words_in_an_unrelated_question_still_do_not_count(ds):
    """不相干的長問題剛好沾到兩個常見詞（「規定」「辦理」）不可以算數 —— 要問題裡大部分的詞都出現。"""
    _common_word_corpus(ds["id"])
    for q in ("週末想去哪裡露營，有什麼規定要辦理嗎", "如何烤出好吃的戚風蛋糕"):
        assert retrieval.search(q, user_id=None) == [], q
