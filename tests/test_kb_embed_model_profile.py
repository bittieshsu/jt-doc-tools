"""知識庫的嵌入：照固定前綴訓練的模型由程式自動套用官方寫法（2026-10-09 使用者：
「換nemotron，參數或前綴請調整好」）。

* Nemotron-3-Embed（官方模型卡：查詢 `query: `、文件 `passage: `）自動加前綴，而且載入時的
  上下文長度送 8192（推論機預設 131072 時一載入就佔 28.3 GB，8192 只要 10.7 GB）。
* **只比對這一族**：上一代 llama-nemotron-embed-1b-v2 加同一套前綴反而略差，不可以被套到。
* 其他模型照舊原樣送（`test_kb_embed_client.test_no_prefixes_title_goes_before_the_text`），
  fingerprint 跟拿掉前綴設定之前一樣（`test_kb_index_rebuild`）。
* 管理員仍然不能自己填前綴；設定檔或快照裡留著的舊前綴不影響。
"""
from __future__ import annotations

import pytest

from app.core.kb import embed
from tests._kb_support import FakeEmbed, kb_isolated  # noqa: F401

NEMO = "hf.co/Abiray/Nemotron-3-Embed-8B-GGUF:Q8_0"


def _snap(fe: FakeEmbed, model: str, **kw) -> dict:
    d = {"kind": fe.kind, "base_url": fe.base, "model": model, "batch_size": 4,
         "timeout_seconds": 10, "secret": ""}
    d.update(kw)
    return d


@pytest.mark.parametrize("name", [
    NEMO, "nemotron-3-embed-8b:q8_0", "nvidia/Nemotron-3-Embed-8B-BF16",
    "Nemotron_3_Embed_1B", "NEMOTRON-3-EMBEDDING-8B",
])
def test_the_nemotron_3_family_gets_the_official_prefixes(name):
    p = embed.model_profile(name)
    assert (p["query_prefix"], p["document_prefix"]) == ("query: ", "passage: ")
    assert p["num_ctx"] == 8192


@pytest.mark.parametrize("name", [
    # 上一代：同一套前綴在公文知識庫上反而略差（0.746 → 0.727）
    "llama-nemotron-embed-1b-v2", "nemotron-embed-1b-v2-meanpool-test:q8_0",
    "qwen3-embedding:8b", "embeddinggemma:300m", "granite-embedding:278m", "nomic-embed-text", "",
])
def test_other_models_are_sent_as_is(name):
    p = embed.model_profile(name)
    assert (p["query_prefix"], p["document_prefix"], p["num_ctx"]) == ("", "", 0)
    assert embed.usage_public(name) is None


def test_nemotron_queries_and_documents_carry_the_prefixes():
    with FakeEmbed() as fe:
        cli = embed.EmbedClient.from_snapshot(_snap(fe, NEMO, query_prefix="Q:", document_prefix="D:"))
        cli.embed([cli.query_text("問題"), cli.document_text("本文", "壹、總述"),
                   cli.document_text("本文", "")])
    # 前綴在最前面（標題之前）；快照裡留著的舊前綴不用
    assert fe.requests[0]["input"] == ["query: 問題", "passage: 壹、總述\n本文", "passage: 本文"]


def test_ollama_loads_nemotron_with_a_small_context_and_others_untouched():
    with FakeEmbed("ollama") as fe:
        embed.EmbedClient.from_snapshot(_snap(fe, NEMO)).embed(["x"])
        embed.EmbedClient.from_snapshot(_snap(fe, "qwen3-embedding:8b")).embed(["x"])
    assert fe.requests[0]["options"] == {"num_ctx": 8192}
    assert fe.requests[0]["truncate"] is False            # 放不下照樣明講失敗，不安靜截掉
    assert "options" not in fe.requests[1], "沒在表上的模型照伺服器的預設載入"


def test_openai_compatible_endpoint_gets_no_ollama_options():
    with FakeEmbed("openai") as fe:
        cli = embed.EmbedClient.from_snapshot(_snap(fe, NEMO))
        cli.embed([cli.query_text("問題")])
    assert "options" not in fe.requests[0]
    assert fe.requests[0]["input"] == ["query: 問題"]     # 前綴照樣加（模型要的是文字）


def test_the_prefixes_are_part_of_the_fingerprint():
    """前綴改變了向量：Nemotron 的索引跟「同一個模型、沒有前綴」的不可以互相比較。"""
    import hashlib
    import json

    from app.core.kb.chunker import CHUNKER_VERSION
    plain = json.dumps({"kind": "ollama", "model": NEMO, "qp": "", "dp": "",
                        "chunker": CHUNKER_VERSION}, ensure_ascii=False, sort_keys=True)
    plain_fp = hashlib.sha256((plain + "|dim=4096").encode("utf-8")).hexdigest()[:24]
    assert embed.fingerprint({"kind": "ollama", "model": NEMO}, 4096) != plain_fp
    # 設定檔裡留著的舊前綴不影響 fingerprint（只看模型自動套用的那一套）
    assert (embed.fingerprint({"kind": "ollama", "model": NEMO, "query_prefix": "x"}, 4096)
            == embed.fingerprint({"kind": "ollama", "model": NEMO}, 4096))


def test_switching_to_nemotron_asks_for_a_rebuild(kb_isolated):
    embed.save({"base_url": "http://h:1", "model": "qwen3-embedding:8b"})
    d = embed._read()
    act = {k: d["embed"][k] for k in embed.DEFAULT_EMBED}
    act.update(dim=4096, fingerprint=embed.fingerprint(d["embed"], 4096), chunks=3)
    embed.set_active(act, dim=4096, fp=act["fingerprint"], chunks=3)
    assert not embed.get_public()["needs_rebuild"]
    pub = embed.save({"model": NEMO})
    assert pub["needs_rebuild"]
    assert pub["usage"] == {"label": "NVIDIA Nemotron-3-Embed", "query_prefix": "query: ",
                            "document_prefix": "passage: ", "num_ctx": 8192}


def test_model_list_and_connection_test_say_what_is_added(admin_session, kb_isolated):
    client, _, _ = admin_session
    with FakeEmbed("ollama", models=[(NEMO, ["embedding"]), ("qwen3-embedding:8b", ["embedding"])]) as fe:
        j = client.post("/admin/knowledge/api/embedding/models",
                        json={"use_llm_server": False, "kind": "ollama", "base_url": fe.base}).json()
        usage = {m["id"]: m["usage"] for m in j["models"]}
        assert usage[NEMO]["query_prefix"] == "query: " and usage["qwen3-embedding:8b"] is None
        r = client.post("/admin/knowledge/api/embedding/test",
                        json={"use_llm_server": False, "kind": "ollama", "base_url": fe.base,
                              "model": NEMO}).json()
        assert r["ok"] and r["usage"]["document_prefix"] == "passage: "
        # 測試連線也照模型的用法送（不然測得過、真的建索引時卻是另一回事）
        sent = [x for q in fe.requests for x in q["input"]]
        assert any(s.startswith("query: ") for s in sent) and any(s.startswith("passage: ") for s in sent)


def test_admins_still_cannot_set_prefixes(kb_isolated):
    embed.save({"base_url": "http://h:1", "model": NEMO, "query_prefix": "Q:", "document_prefix": "D:"})
    snap = embed.configured_snapshot()
    with FakeEmbed() as fe:
        snap.update(base_url=fe.base)
        cli = embed.EmbedClient.from_snapshot(snap)
    assert (cli.query_prefix, cli.document_prefix) == ("query: ", "passage: ")
