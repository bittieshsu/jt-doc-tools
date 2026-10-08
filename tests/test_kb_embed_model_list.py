"""「嵌入模型」從伺服器的清單挑（2026-10-08 使用者：「應該是列出 llm server 上符合的 model 不是自己填」）。

要守住的事：

* Ollama 會說每個模型能做什麼（`capabilities`）：**只列能做嵌入的**，聊天用的不列；附維度與上下文長度。
* OpenAI 相容（`/v1/models`）與舊版 Ollama 不說：全部列出，名稱像嵌入模型的排前面
  （`capability_known=False`，畫面分兩組）。
* 沿用 LLM 伺服器時問的是那一台、帶那一台的金鑰；另外指定時金鑰的規則跟測試連線同一份 ——
  畫面送替身字串時**只有位址跟存檔的一樣才帶存著的金鑰**。
* 連不上不丟 500：回 `ok: False` 和一句話（畫面據此給「手動輸入…」）。
"""
from __future__ import annotations

import pytest

from app.core.kb import embed
from tests._kb_support import FakeEmbed, kb_isolated  # noqa: F401
from tests.test_kb_embed_inherits_llm import llm  # noqa: F401

API = "/admin/knowledge/api/embedding/models"


def test_ollama_lists_only_embedding_models(kb_isolated, llm, admin_session):
    c, _, _ = admin_session
    with FakeEmbed("ollama", dim=768) as fe:
        llm.update({"base_url": fe.base + "/v1", "api_key": "sk-llm-9"})
        j = c.post(API, json={"use_llm_server": True}).json()
    assert j["ok"] and j["capability_known"] is True
    assert [m["id"] for m in j["models"]] == ["embeddinggemma:300m", "granite-embedding:278m"]
    assert all(m["dim"] == 768 and m["ctx"] == 2048 for m in j["models"])
    assert "GET /api/tags" in fe.paths
    assert "Bearer sk-llm-9" in fe.get_auth, "沿用時要帶 LLM 設定裡的金鑰"


def test_openai_compatible_lists_everything_with_likely_ones_first(kb_isolated, llm, admin_session):
    c, _, _ = admin_session
    models = [("gpt-x", []), ("text-embedding-3-small", []), ("bge-m-test", []), ("llama-chat", [])]
    with FakeEmbed("openai", models=models) as fe:
        llm.update({"base_url": fe.base + "/v1"})
        j = c.post(API, json={"use_llm_server": True}).json()
    assert j["ok"] and j["capability_known"] is False and j["kind"] == "openai"
    ids = [m["id"] for m in j["models"]]
    assert ids == ["bge-m-test", "text-embedding-3-small", "gpt-x", "llama-chat"]
    assert [m["embedding"] for m in j["models"]] == [True, True, False, False]


def test_old_ollama_without_capabilities_falls_back_to_names(kb_isolated, monkeypatch):
    """舊版 Ollama 的 /api/tags 沒有 `capabilities`：全部列出、名稱像嵌入模型的排前面。"""
    import httpx

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"models": [{"name": "gemma4:26b"}, {"name": "nomic-embed-text:latest"}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    out = embed.list_models({"kind": "ollama", "base_url": "http://127.0.0.1:11434"})
    assert out["capability_known"] is False
    assert [(m["id"], m["embedding"]) for m in out["models"]] == \
        [("nomic-embed-text:latest", True), ("gemma4:26b", False)]


def test_own_server_key_only_goes_to_the_saved_address(kb_isolated, llm, admin_session):
    c, _, _ = admin_session
    with FakeEmbed("ollama") as fe, FakeEmbed("ollama") as other:
        c.post("/admin/knowledge/api/embedding",
               json={"use_llm_server": False, "kind": "ollama", "base_url": fe.base,
                     "model": "granite-embedding:278m", "api_key": "sk-own-1"})
        j = c.post(API, json={"use_llm_server": False, "kind": "ollama", "base_url": fe.base,
                              "api_key": "__KEPT__"}).json()
        assert j["ok"] and "Bearer sk-own-1" in fe.get_auth
        j2 = c.post(API, json={"use_llm_server": False, "kind": "ollama", "base_url": other.base,
                               "api_key": "__KEPT__"}).json()
        assert j2["ok"]
        assert not any("sk-own-1" in a for a in other.get_auth), "存著的金鑰不可以送到畫面上改過的位址"


def test_unreachable_server_is_a_message_not_an_error(kb_isolated, llm, admin_session):
    c, _, _ = admin_session
    llm.update({"base_url": "http://127.0.0.1:9/v1"})
    r = c.post(API, json={"use_llm_server": True, "kind": "ollama"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is False and j["error"] and j["models"] == []
    r = c.post(API, json={"use_llm_server": False, "kind": "ollama", "base_url": ""})
    assert r.json()["ok"] is False and "位址" in r.json()["error"]


@pytest.mark.parametrize("bad", ["http://169.254.169.254", "file:///etc/passwd", "ftp://x.example"])
def test_bad_addresses_are_refused_before_connecting(kb_isolated, llm, admin_session, bad):
    c, _, _ = admin_session
    j = c.post(API, json={"use_llm_server": False, "kind": "ollama", "base_url": bad}).json()
    assert j["ok"] is False and "位址不合格" in j["error"]


def test_the_page_offers_a_select_not_a_text_box(kb_isolated, admin_session):
    c, _, _ = admin_session
    html = c.get("/admin/llm-settings").text
    assert '<select id="kbEmbModel"' in html, "嵌入模型要從清單挑"
    assert 'id="kbEmbModels"' in html, "要有「重新整理清單」"
