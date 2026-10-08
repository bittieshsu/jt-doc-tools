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
