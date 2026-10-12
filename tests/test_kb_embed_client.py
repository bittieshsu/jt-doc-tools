"""知識庫的 embedding 用戶端：Ollama 原生與 OpenAI 相容兩種 API、不加前綴、金鑰、
不安靜截斷、SSRF 擋雲端中繼資料位址、金鑰不送到沒存檔的位址。"""
from __future__ import annotations

import numpy as np
import pytest

from app.core.kb import embed
from tests._kb_support import FakeEmbed, kb_isolated, read_json  # noqa: F401


def _snap(fe: FakeEmbed, **kw) -> dict:
    d = {"kind": fe.kind, "base_url": fe.base, "model": "m", "batch_size": 2, "timeout_seconds": 10,
         "secret": ""}
    d.update(kw)
    return d


@pytest.mark.parametrize("kind", ["ollama", "openai"])
def test_both_apis_return_normalised_vectors_in_order(kind):
    with FakeEmbed(kind) as fe:
        cli = embed.EmbedClient.from_snapshot(_snap(fe))
        m = cli.embed(["甲乙丙", "丁戊己", "庚辛壬"])
    assert m.shape == (3, 256)
    assert np.allclose(np.linalg.norm(m, axis=1), 1.0)
    # 批次大小 2 → 兩個請求
    assert len(fe.requests) == 2
    path = "/api/embed" if kind == "ollama" else "/v1/embeddings"
    assert set(fe.paths) == {path}


def test_ollama_is_told_not_to_truncate():
    with FakeEmbed("ollama") as fe:
        embed.EmbedClient.from_snapshot(_snap(fe)).embed(["x"])
    assert fe.requests[0]["truncate"] is False


def test_context_too_long_is_an_explicit_error_not_a_silent_cut():
    with FakeEmbed("ollama", context_limit=10) as fe:
        cli = embed.EmbedClient.from_snapshot(_snap(fe))
        with pytest.raises(embed.EmbedError) as e:
            cli.embed(["很長" * 20])
    assert "長度" in str(e.value)


def test_no_prefixes_title_goes_before_the_text():
    """不加前綴（2026-10-08 使用者：「有講過不要用前綴」）：查詢原樣送，文件只把上層標題接在前面。

    舊設定檔、舊快照裡還留著的前綴也不可以用上 —— 那會讓同一份設定在不同的機器上嵌入出不同的東西。
    """
    with FakeEmbed() as fe:
        cli = embed.EmbedClient.from_snapshot(_snap(fe, query_prefix="Q:",
                                                    document_prefix="title: {title} | text: "))
        cli.embed([cli.query_text("問題"), cli.document_text("本文", "壹、總述")])
        cli.embed([cli.document_text("本文", "")])
    sent = fe.requests[0]["input"] + fe.requests[1]["input"]
    assert sent == ["問題", "壹、總述\n本文", "本文"]


def test_api_key_is_sent_as_bearer():
    with FakeEmbed("openai") as fe:
        embed.EmbedClient.from_snapshot(_snap(fe, secret="k123")).embed(["x"])
        embed.EmbedClient.from_snapshot(_snap(fe, secret="")).embed(["x"])
    assert fe.auth == ["Bearer k123", ""]


@pytest.mark.parametrize("status,words", [(404, "模型"), (401, "金鑰"), (500, "HTTP 500")])
def test_errors_are_explained(status, words):
    with FakeEmbed(status=status) as fe:
        with pytest.raises(embed.EmbedError) as e:
            embed.EmbedClient.from_snapshot(_snap(fe)).embed(["x"])
    assert words in str(e.value)


def test_unreachable_service():
    with pytest.raises(embed.EmbedError):
        embed.EmbedClient.from_snapshot(
            {"kind": "ollama", "base_url": "http://127.0.0.1:1", "model": "m"}).embed(["x"])


@pytest.mark.parametrize("url", ["http://169.254.169.254", "http://metadata.google.internal:80",
                                 "file:///etc/passwd", "ftp://x", "http://user:pw@host:1"])
def test_ssrf_targets_are_rejected(kb_isolated, url):
    with pytest.raises(ValueError):
        embed.save({"base_url": url, "model": "m"})
    with pytest.raises(embed.EmbedError):
        embed.EmbedClient.from_snapshot({"kind": "ollama", "base_url": url, "model": "m"})


def test_path_in_url_is_never_used(kb_isolated):
    """路徑一律由我們接 —— 管理員填的路徑不會被拿去組請求。"""
    cli = embed.EmbedClient.from_snapshot(
        {"kind": "ollama", "base_url": "http://h:11434/evil/../path?x=1#f", "model": "m"})
    assert cli.base == "http://h:11434"


def test_key_is_encrypted_and_never_returned(kb_isolated):
    embed.save({"base_url": "http://h:1", "model": "m", "key_input": "Bearer sekrit-123"})
    raw = read_json(embed.settings_path())
    assert "sekrit" not in str(raw)
    assert raw["embed"]["api_key_enc"]
    assert embed.get_public()["embed"]["api_key"] == embed.SECRET_KEPT
    assert embed.configured_snapshot()["secret"] == "sekrit-123"     # 「Bearer 」剝掉
    # 替身字串 ＝ 不動
    embed.save({"model": "m2", "key_input": embed.SECRET_KEPT})
    assert embed.configured_snapshot()["secret"] == "sekrit-123"
    # 空字串 ＝ 清掉
    embed.save({"key_input": ""})
    assert embed.configured_snapshot()["secret"] == ""


def test_test_connection_does_not_send_saved_key_to_another_host(admin_session, kb_isolated):
    client, _, _ = admin_session
    with FakeEmbed() as saved_host, FakeEmbed() as other_host:
        embed.save({"base_url": saved_host.base, "model": "m", "key_input": "the-real-key"})
        r = client.post("/admin/knowledge/api/embedding/test",
                        json={"kind": "ollama", "base_url": other_host.base, "model": "m",
                              "api_key": embed.SECRET_KEPT})
        assert r.json()["ok"]
        assert other_host.auth == [""], "存著的金鑰被送到另一個位址"
        r = client.post("/admin/knowledge/api/embedding/test",
                        json={"kind": "ollama", "base_url": saved_host.base, "model": "m",
                              "api_key": embed.SECRET_KEPT})
        assert r.json()["ok"] and r.json()["dim"] == 256
        assert saved_host.auth == ["Bearer the-real-key"]


def test_settings_api_never_echoes_the_key(admin_session, kb_isolated):
    client, _, _ = admin_session
    r = client.post("/admin/knowledge/api/embedding",
                    json={"base_url": "http://h:1", "model": "m", "api_key": "abc-secret"})
    assert r.status_code == 200
    assert "abc-secret" not in r.text
    assert "abc-secret" not in client.get("/admin/knowledge/api/overview").text
    r = client.post("/admin/knowledge/api/embedding", json={"base_url": "http://169.254.169.254"})
    assert r.status_code == 400


def test_prefix_settings_are_gone(kb_isolated):
    """前綴的設定、建議前綴都拿掉了：存檔時送來的前綴不存、舊檔裡的讀進來就丟掉。"""
    assert not hasattr(embed, "PRESETS") and not hasattr(embed, "preset_for")
    embed.save({"base_url": "http://h:1", "model": "m", "query_prefix": "Q:", "document_prefix": "D:"})
    raw = read_json(embed.settings_path())["embed"]
    assert "query_prefix" not in raw and "document_prefix" not in raw
    # 舊版寫進去的
    p = embed.settings_path()
    d = read_json(p)
    d["embed"].update(query_prefix="Q:", document_prefix="D:")
    p.write_text(__import__("json").dumps(d), encoding="utf-8")
    pub = embed.get_public()["embed"]
    assert "query_prefix" not in pub and "document_prefix" not in pub


# ---------------------------------------------------------------- 選到聊天模型（v1.16.82）
#
# 實際案例：經過 LiteLLM 的伺服器上沒有嵌入模型，管理員挑了 `gemma4:26b`、`nemotron-3-nano:latest`
# （都是聊天模型），畫面只寫「嵌入服務回報錯誤（HTTP 400 / 501）」，看不出是選錯模型。
# Ollama 不會先檢查模型能不能做嵌入（會先把那個聊天模型載入），「不支援」的狀態碼依引擎而不同
# （llama.cpp 回 501）。訊息仍然是**固定句子**：對方回的內容只進服務紀錄檔，不回給瀏覽器
# （見 `test_admin_exception_leak.py` 的 OWN_EXCEPTIONS 說明）。

def _fail_with(monkeypatch, status: int, body: dict):
    import httpx

    def fake_post(url, **kw):
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", fake_post)


def _err(model: str) -> str:
    with pytest.raises(embed.EmbedError) as e:
        embed.EmbedClient.from_snapshot(
            {"kind": "openai", "base_url": "http://127.0.0.1:9", "model": model}).embed(["x"])
    return str(e.value)


@pytest.mark.parametrize("model, status, body", [
    ("nemotron-3-nano:latest", 501, {"error": {"message": "This server does not support embeddings."}}),
    ("gemma4:26b", 400, {"error": {"message": "litellm.BadRequestError: model does not support embeddings"}}),
    ("some-model", 501, {"error": "not implemented"}),
])
def test_a_model_that_cannot_embed_is_named_as_the_problem(monkeypatch, model, status, body):
    _fail_with(monkeypatch, status, body)
    msg = _err(model)
    assert "不能做嵌入" in msg and "嵌入模型" in msg and f"HTTP {status}" in msg, msg
    assert "litellm" not in msg.lower() and "server" not in msg.lower(), "對方回的內容不可以回給瀏覽器"


def test_a_chat_looking_name_gets_a_hint_on_other_errors(monkeypatch):
    _fail_with(monkeypatch, 400, {"error": "something else went wrong"})
    msg = _err("gemma4:26b")
    assert "HTTP 400" in msg and "不像嵌入模型" in msg and "something" not in msg, msg


def test_an_embedding_model_error_does_not_guess(monkeypatch):
    _fail_with(monkeypatch, 500, {"error": "out of memory"})
    msg = _err("nomic-embed-text:latest")
    assert "HTTP 500" in msg and "聊天" not in msg and "不像" not in msg and "out of memory" not in msg, msg
    assert "紀錄檔" in msg, "要講詳細原因去哪裡看"


@pytest.mark.parametrize("model, status, body", [
    ("gemma4:26b", 501, {"error": "x"}),                       # 不能做嵌入
    ("gemma4:26b", 400, {"error": "something else"}),          # 名稱不像嵌入模型
    ("nomic-embed-text:latest", 500, {"error": "boom"}),        # 一般錯誤
])
def test_the_new_sentences_are_translated(monkeypatch, model, status, body):
    """畫面上的 `tr()` 把連續數字換成 `{0}` 再查語系檔（`static/js/i18n.js`）——
    拿**實際丟出來的訊息**照同樣的規則換，語系檔裡要查得到（英文、日文）。"""
    import json
    import re
    from pathlib import Path
    _fail_with(monkeypatch, status, body)
    msg = _err(model)
    n = iter(range(10))
    key = re.sub(r"\d+", lambda _m: "{%d}" % next(n), msg)
    root = Path(__file__).resolve().parent.parent
    for lang in ("en", "ja"):
        cat = json.loads((root / "app" / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        assert key in cat, (lang, key)
