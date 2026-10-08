"""Embedding 預設沿用「LLM 設定」裡的伺服器（2026-10-08 使用者：「預設 繼承上面有設的 llm server設定」）。

要守住的事：

* **預設沿用**：全新安裝、或舊設定檔還沒填位址 → 沿用；舊設定檔**已經填了位址** → 維持另外指定
  （不可以安靜地改掉管理員填好的那一台）。
* 沿用的是**公文撰擬用的那一台**：公文撰擬指定了另一台 LLM 伺服器（資料只能送那裡）時，
  embedding 也送那一台；那台不見了 → **不退回全站那台**，講出原因、算成「還沒設定」。
* 位址與金鑰**每次照 LLM 設定取**（LLM 那邊改了，這裡跟著改，使用中的索引也一樣），不另外存一份。
* API 種類在存檔時問一次（Ollama 才有 `/api/version`）；問不到不猜。
* 只給位址（API、舊腳本）＝ 要另外指定那一台。
"""
from __future__ import annotations

import json

import pytest

from app.core.kb import embed
from tests._kb_support import FakeEmbed, kb_isolated, read_json  # noqa: F401


@pytest.fixture()
def llm(tmp_path, monkeypatch):
    """一份獨立的 LLM 設定（`llm_settings` 的路徑是 import 當下綁的，要換掉）。"""
    from app.core.llm_settings import llm_settings
    monkeypatch.setattr(llm_settings, "_path", tmp_path / "llm_settings.json")
    return llm_settings


def _server(llm, name: str, base: str, key: str = "") -> str:
    sid = "a1b2c3d4e5f6"
    llm.update({"servers": [{"id": sid, "name": name, "base_url": base, "api_key": key}]})
    return sid


def test_default_is_to_use_the_llm_server(kb_isolated, llm):
    llm.update({"base_url": "http://127.0.0.1:11434/v1", "api_key": "sk-llm-1"})
    pub = embed.get_public()
    assert pub["embed"]["use_llm_server"] is True
    assert pub["llm_server"]["base_url"] == "http://127.0.0.1:11434/v1"
    assert pub["llm_server"]["has_key"] is True and pub["llm_server"]["pinned"] is False
    assert "sk-llm-1" not in json.dumps(pub), "金鑰不可以出現在給畫面的資料裡"
    assert pub["configured"] is False, "還沒填模型"
    embed.save({"model": "granite-embedding:278m"})
    snap = embed.configured_snapshot()
    assert snap["base_url"] == "http://127.0.0.1:11434/v1" and snap["secret"] == "sk-llm-1"
    raw = read_json(embed.settings_path())["embed"]
    assert raw["base_url"] == "" and raw["api_key_enc"] == "", "沿用時不另外存一份位址與金鑰"


@pytest.mark.parametrize("old_base, want", [("http://emb.example:11434", False), ("", True)])
def test_old_settings_files_keep_what_the_admin_chose(kb_isolated, llm, old_base, want):
    embed.settings_path().parent.mkdir(parents=True, exist_ok=True)
    embed.settings_path().write_text(json.dumps({"embed": {"kind": "ollama", "base_url": old_base,
                                                           "model": "m"}}), encoding="utf-8")
    assert embed.get_public()["embed"]["use_llm_server"] is want


def test_follows_the_server_pinned_for_official_doc(kb_isolated, llm):
    """公文撰擬指定了機關自己的伺服器 → embedding 也送那裡（同一批公文資料）。"""
    llm.update({"base_url": "http://127.0.0.1:11434/v1", "api_key": "sk-global"})
    sid = _server(llm, "機關內部", "http://127.0.0.2:8000/v1", key="sk-agency")
    llm.update({"server_per_tool": {"official-doc": sid}})
    embed.save({"model": "m", "use_llm_server": True})
    pub = embed.get_public()
    assert pub["llm_server"]["pinned"] is True and pub["llm_server"]["name"] == "機關內部"
    snap = embed.configured_snapshot()
    assert snap["base_url"] == "http://127.0.0.2:8000/v1" and snap["secret"] == "sk-agency"


def test_a_missing_pinned_server_never_falls_back_to_the_global_one(kb_isolated, llm):
    llm.update({"base_url": "http://127.0.0.1:11434/v1"})
    sid = _server(llm, "機關內部", "http://127.0.0.2:8000/v1")
    llm.update({"server_per_tool": {"official-doc": sid}})
    embed.save({"model": "m", "use_llm_server": True})
    # 那台不見了、指定還留著（設定頁會擋「刪掉還在用的伺服器」，直接改檔模擬手改 / 還原壞掉的備份）
    llm._path.write_text(json.dumps({**json.loads(llm._path.read_text(encoding="utf-8")),
                                     "servers": []}), encoding="utf-8")
    pub = embed.get_public()
    assert pub["llm_server"]["problem"], "那台不見了要講出來"
    assert pub["configured"] is False, "不可以改送全站那台"
    assert embed.configured_snapshot() is None


def test_the_active_index_follows_a_changed_llm_address(kb_isolated, llm):
    """LLM 那邊換了位址（同一個模型搬到另一台）→ 使用中的索引跟著用新位址，不必重建。"""
    llm.update({"base_url": "http://127.0.0.1:11434/v1"})
    embed.save({"model": "m", "use_llm_server": True})
    snap = embed.configured_snapshot()
    embed.set_active(snap, dim=8, fp=embed.fingerprint(snap, 8), chunks=1)
    llm.update({"base_url": "http://127.0.0.3:11434/v1"})
    assert embed.active_snapshot()["base_url"] == "http://127.0.0.3:11434/v1"
    assert embed.needs_rebuild() is False


def test_giving_an_address_means_a_separate_server(kb_isolated, llm):
    embed.save({"base_url": "http://emb.example:11434", "model": "m"})
    assert embed.get_public()["embed"]["use_llm_server"] is False
    assert embed.configured_snapshot()["base_url"] == "http://emb.example:11434"
    # 明講要沿用時，就算一起送了位址也沿用（畫面上藏起來的欄位照樣會送）
    llm.update({"base_url": "http://127.0.0.1:11434/v1"})
    embed.save({"base_url": "http://emb.example:11434", "model": "m", "use_llm_server": True})
    assert embed.configured_snapshot()["base_url"] == "http://127.0.0.1:11434/v1"


@pytest.mark.parametrize("kind", ["ollama", "openai"])
def test_saving_detects_the_api_kind_and_the_test_reaches_the_llm_server(
        kb_isolated, llm, admin_session, kind):
    c, _, _ = admin_session
    with FakeEmbed(kind) as fe:
        llm.update({"base_url": fe.base + "/v1", "api_key": "sk-llm-77"})
        r = c.post("/admin/knowledge/api/embedding",
                   json={"use_llm_server": True, "kind": "openai" if kind == "ollama" else "ollama",
                         "model": "m", "base_url": "", "api_key": ""})
        assert r.status_code == 200, r.text
        assert r.json()["embed"]["kind"] == kind, "沿用時要照對方是不是 Ollama 決定"
        t = c.post("/admin/knowledge/api/embedding/test", json={"use_llm_server": True, "model": "m"})
        assert t.json().get("ok") is True, t.text
    want = "/api/embed" if kind == "ollama" else "/v1/embeddings"
    assert want in fe.paths
    assert "Bearer sk-llm-77" in fe.auth, "沿用時金鑰也要沿用"


def test_detection_failure_keeps_the_kind(kb_isolated, llm, admin_session):
    """問不到（對方暫時連不上）→ 不改種類：改了的話索引會被判成要重建。"""
    c, _, _ = admin_session
    llm.update({"base_url": "http://127.0.0.1:9/v1"})        # 沒有人在聽
    r = c.post("/admin/knowledge/api/embedding", json={"use_llm_server": True, "kind": "ollama", "model": "m"})
    assert r.json()["embed"]["kind"] == "ollama"
    r = c.post("/admin/knowledge/api/embedding", json={"use_llm_server": True, "model": "m"})
    assert r.json()["embed"]["kind"] == "ollama"


def test_the_saved_own_key_is_not_carried_when_inheriting(kb_isolated, llm, admin_session):
    """另外指定時存過的金鑰，切成沿用之後不可以被帶去 LLM 伺服器（送的是 LLM 那把）。"""
    c, _, _ = admin_session
    with FakeEmbed("openai") as fe:
        c.post("/admin/knowledge/api/embedding",
               json={"use_llm_server": False, "kind": "openai", "base_url": "http://emb.example:1",
                     "model": "m", "api_key": "sk-own-SECRET"})
        llm.update({"base_url": fe.base + "/v1", "api_key": ""})
        t = c.post("/admin/knowledge/api/embedding/test",
                   json={"use_llm_server": True, "model": "m", "api_key": "__KEPT__"})
        assert t.json().get("ok") is True, t.text
    assert not any("sk-own-SECRET" in a for a in fe.auth)
