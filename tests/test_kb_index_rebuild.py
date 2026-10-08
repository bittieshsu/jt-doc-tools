"""知識庫：index fingerprint、重建索引（完成而且驗證過才切換、失敗保留舊的）、
只拿 fingerprint 相同的向量比。"""
from __future__ import annotations

import numpy as np
import pytest

from app.core.kb import chunker, embed, indexer, retrieval, store
from tests._kb_support import FakeEmbed, HANDBOOK_TXT, add_doc, kb_isolated, setup_embed  # noqa: F401


@pytest.fixture
def ds(kb_isolated):
    d = store.create_dataset({"name": "手冊", "category": "writing_rules"})
    add_doc(d["id"], HANDBOOK_TXT.encode("utf-8"), ".txt")
    return d


def _fps() -> set[str]:
    return {r[0] for r in store.conn().execute("SELECT DISTINCT fingerprint FROM kb_vectors")}


# ---------------------------------------------------------------- fingerprint
def test_fingerprint_depends_on_model_dim_and_chunker(monkeypatch):
    base = {"kind": "ollama", "model": "m1"}
    fp = embed.fingerprint(base, 768)
    assert embed.fingerprint({**base, "model": "m2"}, 768) != fp
    assert embed.fingerprint(base, 1024) != fp
    assert embed.fingerprint({**base, "kind": "openai"}, 768) != fp
    # 伺服器位址、金鑰、批次、沿用與否不算在內 —— 換一台跑同一個模型的伺服器不用重建
    assert embed.fingerprint({**base, "base_url": "http://other:1", "batch_size": 3,
                              "use_llm_server": True}, 768) == fp
    # 前綴拿掉了：舊快照裡留著的前綴不影響
    assert embed.fingerprint({**base, "query_prefix": "q: ", "document_prefix": "d: "}, 768) == fp
    monkeypatch.setattr(embed, "CHUNKER_VERSION", "999")
    assert embed.fingerprint(base, 768) != fp


def test_fingerprint_of_an_index_built_without_prefixes_did_not_change():
    """拿掉前綴之前建的索引（前綴本來就是空的）fingerprint 要一模一樣 —— 不然每一台
    升級之後都被判成「要重建」，而向量檢索在重建之前整個不能用。照舊公式另算一份比。"""
    import hashlib
    import json as _json
    old = _json.dumps({"kind": "ollama", "model": "m1", "qp": "", "dp": "",
                       "chunker": embed.CHUNKER_VERSION}, ensure_ascii=False, sort_keys=True)
    want = hashlib.sha256((old + "|dim=768").encode("utf-8")).hexdigest()[:24]
    assert embed.fingerprint({"kind": "ollama", "model": "m1"}, 768) == want


def test_rebuild_switches_only_after_success(ds):
    with FakeEmbed() as fe:
        setup_embed(fe.base, model="m1")
        assert embed.get_public()["needs_rebuild"]
        r1 = indexer.rebuild()
        act = embed.active_snapshot()
        assert act["fingerprint"] == r1["fingerprint"] and act["dim"] == r1["dim"]
        assert _fps() == {r1["fingerprint"]}
        assert not embed.get_public()["needs_rebuild"]
        n = store.conn().execute("SELECT COUNT(*) FROM kb_chunks").fetchone()[0]
        assert store.count_vectors(r1["fingerprint"]) == n
        # 換模型：使用中的索引照舊，直到重建完成
        setup_embed(fe.base, model="m2")
        assert embed.get_public()["needs_rebuild"]
        assert embed.active_snapshot()["fingerprint"] == r1["fingerprint"]
        r2 = indexer.rebuild()
        assert r2["fingerprint"] != r1["fingerprint"]
        assert embed.active_snapshot()["fingerprint"] == r2["fingerprint"]
        assert _fps() == {r2["fingerprint"]}               # 舊的向量清掉


def test_rebuild_failure_keeps_the_old_index(ds):
    with FakeEmbed() as good:
        setup_embed(good.base, model="m1")
        r1 = indexer.rebuild()
    old_count = store.count_vectors(r1["fingerprint"])
    # 新設定：嵌入服務第 2 次請求之後就掛掉（重建到一半）
    with FakeEmbed(fail_after=2) as bad:
        setup_embed(bad.base, model="m2")
        with pytest.raises(indexer.RebuildError):
            indexer.rebuild()
    act = embed.active_snapshot()
    assert act["fingerprint"] == r1["fingerprint"], "重建失敗卻切換了索引"
    assert store.count_vectors(r1["fingerprint"]) == old_count, "舊索引的向量被動到了"
    assert _fps() == {r1["fingerprint"]}, "失敗的那一份半套向量沒有清掉"
    st = indexer.rebuild_state()
    assert not st["running"] and st["error"]


def test_rebuild_verification_catches_a_drifting_service(ds):
    """同一段文字兩次嵌入差很多（中途換了模型）→ 不切換。"""
    n = store.conn().execute("SELECT COUNT(*) FROM kb_chunks").fetchone()[0]
    with FakeEmbed() as good:
        setup_embed(good.base, model="m1")
        r1 = indexer.rebuild()
    batches = 1 + (n + 3) // 4        # 測試一次 ＋ 每批 4 段
    with FakeEmbed(drift_after=batches) as fe:
        setup_embed(fe.base, model="m2")
        with pytest.raises(indexer.RebuildError) as e:
            indexer.rebuild()
    assert "差很多" in str(e.value) or "沒有切換" in str(e.value)
    assert embed.active_snapshot()["fingerprint"] == r1["fingerprint"]


def test_search_only_uses_vectors_of_the_active_fingerprint(ds):
    """舊 fingerprint 的向量就算還在資料庫裡，也不可以拿來比。"""
    with FakeEmbed() as fe:
        setup_embed(fe.base, model="m1")
        r1 = indexer.rebuild()
        allowed = retrieval._allowed_versions(None, None)
        target = store.conn().execute(
            "SELECT id FROM kb_chunks WHERE text LIKE '%雙封套%'").fetchone()["id"]
        other = store.conn().execute(
            "SELECT id FROM kb_chunks WHERE text NOT LIKE '%雙封套%' LIMIT 1").fetchone()["id"]
        # 在另一個 fingerprint 底下塞一筆「跟查詢一模一樣」的向量給不相干的段落
        cli = embed.EmbedClient.from_snapshot(embed.active_snapshot())
        q = "機密文書對外發文要怎麼封裝"
        qv = cli.embed_query(q)
        store.put_vectors("someone-elses-fingerprint", [(other, qv.astype(np.float32).tobytes())],
                          int(qv.shape[0]))
        hits = retrieval.vector_hits(qv, r1["fingerprint"], allowed)
        assert hits and hits[0]["id"] == target
        assert other not in [h["id"] for h in hits[:1]]
        res = retrieval.search(q, user_id=None)
        assert res and res[0]["chunk_id"] == target


def test_import_after_rebuild_embeds_new_chunks(ds):
    with FakeEmbed() as fe:
        setup_embed(fe.base)
        r = indexer.rebuild()
        v = add_doc(ds["id"], "一、新的規定：開會通知單要寫明會議地點。".encode("utf-8"), ".txt", "新")
        ids = [c["id"] for c in store.list_chunks(v["id"])]
        got = {r_[0] for r_ in store.conn().execute(
            "SELECT chunk_id FROM kb_vectors WHERE fingerprint=?", (r["fingerprint"],))}
        assert set(ids) <= got
        assert v["warning"] == ""


def test_import_when_embedding_is_down_keeps_keyword_and_says_so(ds):
    with FakeEmbed() as fe:
        setup_embed(fe.base)
        indexer.rebuild()
    # 服務關掉了（FakeEmbed 已經離開）
    v = add_doc(ds["id"], "一、會勘通知單的寫法。".encode("utf-8"), ".txt", "新")
    assert v["status"] == "active"
    assert "向量沒有建立" in v["warning"]
    from app.core import kb
    assert kb.status()["embedding"]["vectors_missing"] >= 1
    # 查詢時嵌入服務連不上 → 退回只用關鍵字，並講出來
    d = retrieval.search_detail("會勘通知單", user_id=None)
    assert d["mode"] == "keyword" and "沒有回應" in d["note"]
    assert d["results"]


def test_connection_only_change_does_not_need_rebuild(ds):
    with FakeEmbed() as a:
        setup_embed(a.base)
        r = indexer.rebuild()
    with FakeEmbed() as b:
        embed.save({"base_url": b.base})              # 只換伺服器
        pub = embed.get_public()
        assert not pub["needs_rebuild"]
        assert embed.active_snapshot()["base_url"] == b.base
        assert embed.active_snapshot()["fingerprint"] == r["fingerprint"]
        assert retrieval.search_detail("機密文書雙封套", user_id=None)["mode"] == "hybrid"


def test_chunker_version_change_requires_rebuild_and_rechunks(ds, monkeypatch):
    with FakeEmbed() as fe:
        setup_embed(fe.base)
        indexer.rebuild()
        monkeypatch.setattr(chunker, "CHUNKER_VERSION", "2")
        monkeypatch.setattr(embed, "CHUNKER_VERSION", "2")
        assert embed.get_public()["needs_rebuild"]
        indexer.rebuild()
        vers = store.conn().execute("SELECT DISTINCT chunker_version FROM kb_versions").fetchall()
        assert [r[0] for r in vers] == ["2"]
        assert not embed.get_public()["needs_rebuild"]


def test_disable_vectors_falls_back_to_keyword(ds):
    with FakeEmbed() as fe:
        setup_embed(fe.base)
        indexer.rebuild()
        embed.clear_active()
        d = retrieval.search_detail("機密文書雙封套", user_id=None)
    assert d["mode"] == "keyword" and d["note"]
