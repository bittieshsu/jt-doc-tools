"""設定 → MCP 管理頁（v1.17.1）：存設定、逐張允許 Token、寫稽核、一般使用者不能改、客戶端設定範例。"""
from __future__ import annotations

import pytest


@pytest.fixture
def reset_mcp():
    from app.core import notify_settings
    from app.mcp import settings as ms
    yield
    ms.save({"enabled": False, "tools": None})
    notify_settings.save({"site_url": ""})


def test_page_shows_tools_tokens_and_snippets(client, auth_off, reset_mcp):
    from app.core import api_tokens as at
    from app.mcp import registry
    tok = at.api_tokens.create("ai-agent")
    try:
        html = client.get("/admin/mcp").text
        import re
        text = re.sub(r"<[^>]+>", "", html)       # 範例有語法顏色（span），比對去掉標記後的文字
        for t in registry.TOOLS:
            assert f'value="{t.name}"' in html
        assert "ai-agent" in html and tok.token not in html, "只顯示遮罩，不顯示 Token 本身"
        for client_name in ("claude mcp add --transport http", "bearer_token_env_var", '"type": "remote"',
                            "mcp-remote", "OpenCode"):
            assert client_name in text, client_name
        assert "/admin/mcp" in client.get("/").text, "設定選單要有 MCP"
    finally:
        at.api_tokens.revoke(tok.token)


def test_save_and_token_toggle_are_audited(client, auth_off, reset_mcp, monkeypatch):
    from app.core import api_tokens as at, audit_db, notify_settings
    from app.mcp import settings as ms
    seen = []
    monkeypatch.setattr(audit_db, "log_event", lambda ev, **kw: seen.append((ev, kw)))
    r = client.post("/admin/mcp/save", json={"enabled": True, "site_url": "https://docs.example.test",
                                             "tools": ["pdf_merge", "nope"], "rate_per_minute": 9999,
                                             "max_concurrent": 3})
    assert r.status_code == 200, r.text
    cfg = ms.get()
    assert cfg["enabled"] and cfg["tools"] == ["pdf_merge"] and cfg["rate_per_minute"] == 600
    assert notify_settings.get()["site_url"] == "https://docs.example.test"
    assert seen[-1][1]["target"] == "mcp"
    assert client.post("/admin/mcp/save", json={"site_url": "javascript:alert(1)"}).status_code == 400
    tok = at.api_tokens.create("agent")
    try:
        preview = next(t["token_preview"] for t in at.api_tokens.list_tokens() if t["label"] == "agent")
        assert client.post("/admin/mcp/token", json={"preview": preview, "allowed": True}).json()["mcp"]
        assert at.api_tokens.lookup(tok.token)["mcp"] is True
        assert seen[-1][1]["details"] == {"token": "agent", "mcp": True}
        assert tok.token not in str(seen), "稽核不記 Token"
        assert client.post("/admin/mcp/token", json={"preview": "nope", "allowed": True}).status_code == 404
    finally:
        at.api_tokens.revoke(tok.token)


def test_regular_users_cannot_change_it(admin_session):
    from tests.test_authz_boundaries import _user_client
    _, c = _user_client("alice_mcp")
    r = c.post("/admin/mcp/save", json={"enabled": True}, follow_redirects=False)
    assert r.status_code in (401, 403, 302, 303), r.status_code
    from app.mcp import settings as ms
    assert not ms.get()["enabled"]


@pytest.mark.parametrize("loc", ["zh-TW", "en", "ja"])
def test_client_product_names_are_not_translated(client, auth_off, reset_mcp, loc):
    """Claude Desktop、Claude Code、Codex CLI、OpenCode 是產品名稱，三種介面語言都照原文。"""
    import re
    client.cookies.set("jtdt_locale", loc)
    try:
        text = re.sub(r"<[^>]+>", "", client.get("/admin/mcp").text)
    finally:
        client.cookies.delete("jtdt_locale")
    for name in ("Claude Desktop", "Claude Code", "Codex CLI", "OpenCode"):
        assert name in text, (loc, name)
    for wrong in ("桌面版", "デスクトップ版", "Opencode", "opencode CLI"):
        assert wrong not in text, (loc, wrong)


def test_translations_keep_the_product_names():
    """原文寫了 Claude Desktop / Claude Code / Codex / OpenCode 的句子，英文、日文譯文也要原樣保留（不可以改寫成別的叫法）。"""
    import json
    from pathlib import Path
    names = ("Claude Desktop", "Claude Code", "Codex", "OpenCode")
    bad = []
    for loc in ("en", "ja"):
        cat = json.loads((Path(__file__).resolve().parent.parent / f"app/i18n/{loc}.json").read_text(encoding="utf-8"))
        for zh, tr_ in cat.items():
            for n in names:
                if n in zh and n not in tr_:
                    bad.append(f"{loc}: {zh!r} → {tr_!r}")
    assert not bad, "\n".join(bad)
