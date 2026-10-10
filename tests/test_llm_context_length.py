"""LLM 的上下文長度（2026-10-08 使用者：「我們不能幫客戶改嗎？或是 LLM AI 那邊設定要提示」）。

Ollama 出廠的上下文長度不大，**提示超過時安靜地截掉開頭** —— 被截掉的是指令，結果亂掉而沒有錯誤。
實測（Ollama 0.33.3）：`/api/ps` 的 `context_length` 是實際載入的大小；OpenAI 相容端點**不理**
`options.num_ctx`；`/api/create` 從原模型建一個帶 `num_ctx` 的新名字，經 OpenAI 相容端點呼叫時
就是那個大小，原模型不受影響。

* 設定頁「測試連線」講出實際的上下文長度，太小時提醒；
* 「建一個較大的版本」只對**已存檔**的伺服器做（不替任意主機建東西）、原模型不動、寫稽核。
"""
from __future__ import annotations

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse

from app.core import llm_client as lc
from tests.test_llm_thinking_off_any_backend import Gateway


class OllamaCtx(Gateway):
    """`ollama` 那一種，加上 `/api/ps`、`/api/show`、`/api/create`。"""

    def __init__(self, loaded_ctx=4096, kind="ollama"):
        self.loaded_ctx = loaded_ctx
        self.created: list[dict] = []
        super().__init__(kind)

    def _build(self):
        app = super()._build()
        me = self

        @app.get("/api/ps")
        async def ps():
            if me.kind != "ollama":
                return JSONResponse({"error": "not found"}, 404)
            return {"models": [{"name": "gemma4:26b", "model": "gemma4:26b",
                                "context_length": me.loaded_ctx}]}

        @app.post("/api/show")
        async def show(request: Request):
            body = await request.json()
            if body.get("model") == "gemma4:26b":
                return {"parameters": "temperature 1\ntop_k 64",
                        "model_info": {"gemma4.context_length": 262144}}
            return JSONResponse({"error": "model not found"}, 404)

        @app.post("/api/create")
        async def create(request: Request):
            body = await request.json()
            me.created.append(body)
            return {"status": "success"}

        return app


def _client(g):
    return lc.LLMClient(g.base, timeout=10)


def test_probe_reports_the_loaded_size_and_flags_a_small_one():
    with OllamaCtx(loaded_ctx=4096) as g:
        info = _client(g).context_probe("gemma4:26b")
    assert info["ollama"] and info["loaded"] == 4096 and info["max"] == 262144
    assert info["effective"] == 4096 and info["low"] is True
    with OllamaCtx(loaded_ctx=131072) as g:
        info = _client(g).context_probe("gemma4:26b")
    assert info["low"] is False


def test_probe_on_a_gateway_says_it_cannot_tell():
    with OllamaCtx(kind="litellm") as g:
        assert _client(g).context_probe("gemma4:26b") == {"ollama": False}


def test_variant_name():
    assert lc.LLMClient.context_variant_name("gemma4:26b", 32768) == "gemma4:26b-ctx32k"
    assert lc.LLMClient.context_variant_name("mymodel", 16384) == "mymodel:latest-ctx16k"


def test_make_variant_creates_a_new_name_from_the_original():
    with OllamaCtx() as g:
        new = _client(g).make_context_variant("gemma4:26b", 32768)
    assert new == "gemma4:26b-ctx32k"
    assert g.created == [{"model": "gemma4:26b-ctx32k", "from": "gemma4:26b",
                          "parameters": {"num_ctx": 32768}, "stream": False}]


@pytest.mark.parametrize("model,n", [("gemma4:26b", 5000), ("gemma4:26b", 0),
                                     ("bad name; rm", 32768), ("", 32768), ("x" * 250, 32768)])
def test_make_variant_rejects_bad_input(model, n):
    with OllamaCtx() as g:
        with pytest.raises(lc.LLMError):
            _client(g).make_context_variant(model, n)
        assert g.created == []


def test_make_variant_refuses_non_ollama():
    with OllamaCtx(kind="litellm") as g:
        with pytest.raises(lc.LLMError):
            _client(g).make_context_variant("gemma4:26b", 32768)


# ------------------------------------------------------------------ 管理頁

def test_test_connection_reports_the_context_length(client):
    with OllamaCtx(loaded_ctx=4096) as g:
        r = client.post("/admin/api/llm/test-connection",
                        json={"base_url": g.base, "probe_model": "gemma4:26b"})
    cc = r.json().get("context_check")
    assert cc and cc["effective"] == 4096 and cc["low"] is True and cc["recommended"] >= 16384, r.json()


def test_test_connection_without_a_model_does_not_probe(client):
    with OllamaCtx() as g:
        r = client.post("/admin/api/llm/test-connection", json={"base_url": g.base})
    assert r.json().get("context_check") is None


def _save_llm(base_url):
    from app.core.llm_settings import llm_settings
    llm_settings.update({"base_url": base_url, "enabled": True})


def _restore(saved):
    from app.core.llm_settings import llm_settings
    llm_settings.update({"base_url": saved.get("base_url") or "", "enabled": bool(saved.get("enabled"))})


def test_create_variant_only_targets_the_saved_server(client, monkeypatch):
    """頁面送來的位址一律不理 —— 這支會在對方伺服器上建東西。"""
    from app.core.llm_settings import llm_settings
    saved = llm_settings.get()
    with OllamaCtx() as good, OllamaCtx() as other:
        try:
            _save_llm(good.base)
            r = client.post("/admin/api/llm/context-variant",
                            json={"model": "gemma4:26b", "num_ctx": 32768, "base_url": other.base})
            assert r.status_code == 200 and r.json() == {"ok": True, "model": "gemma4:26b-ctx32k"}, r.text
            assert len(good.created) == 1 and other.created == []
        finally:
            _restore(saved)


def test_create_variant_is_audited(client):
    import time
    from app.core import audit_db
    from app.core.llm_settings import llm_settings
    audit_db.init()
    before = int(audit_db.conn().execute("SELECT COALESCE(MAX(id), 0) AS m FROM audit_events").fetchone()["m"])
    saved = llm_settings.get()
    with OllamaCtx() as g:
        try:
            _save_llm(g.base)
            client.post("/admin/api/llm/context-variant", json={"model": "gemma4:26b", "num_ctx": 65536})
        finally:
            _restore(saved)
    rows = []
    for _ in range(50):
        rows = audit_db.conn().execute(
            "SELECT details_json FROM audit_events WHERE event_type='settings_change' "
            "AND target='llm_context_variant' AND id > ?", (before,)).fetchall()
        if rows:
            break
        time.sleep(0.1)
    assert rows and "gemma4:26b-ctx64k" in rows[0]["details_json"], rows


def test_create_variant_bad_input_is_400_and_creates_nothing(client):
    from app.core.llm_settings import llm_settings
    saved = llm_settings.get()
    with OllamaCtx() as g:
        try:
            _save_llm(g.base)
            r = client.post("/admin/api/llm/context-variant", json={"model": "gemma4:26b", "num_ctx": 1234})
            assert r.status_code == 400 and "16K" in r.json()["error"], r.text
            assert g.created == []
        finally:
            _restore(saved)


# ------------------------------------------------------------------ 真的瀏覽器：設定頁

from tools.browser_probe import browser as _browser  # noqa: E402
from tools.browser_probe import profile_arg as _profile_arg  # noqa: E402

_needs_browser = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets")


@_needs_browser
def test_settings_page_warns_and_creates_a_bigger_version():
    """選模型按「測試連線」→ 上下文 4096 的紅字提醒 → 按「建立 32K 版本並改用」→ 預設模型換成新名字。"""
    import json as _json
    import os
    import shutil
    import subprocess
    import sys
    import tempfile
    import time
    import urllib.request
    from pathlib import Path
    from tests.test_kb_embedding_settings_location import _free_port, _Tab
    root = Path(__file__).resolve().parent.parent
    data = tempfile.mkdtemp(prefix="llmctx-")
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    with OllamaCtx(loaded_ctx=4096) as g:
        srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                                "--port", str(port), "--log-level", "warning"], cwd=str(root), env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        br = subprocess.Popen([_browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
                               _profile_arg(), f"--remote-debugging-port={cdp}", "--remote-allow-origins=*", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t = None
        try:
            for _ in range(120):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                    urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=1)
                    break
                except Exception:
                    time.sleep(0.5)
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/admin/api/llm/settings", method="POST",
                data=_json.dumps({"enabled": True, "base_url": g.base, "model": "gemma4:26b"}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=30).read()
            t = _Tab(cdp)
            t.go(f"http://127.0.0.1:{port}/admin/llm-settings")
            assert t.wait_for("!!document.getElementById('model')")
            t.js("(function(){var s=document.getElementById('model');"
                 "if(![...s.options].some(o=>o.value==='gemma4:26b')){var o=document.createElement('option');"
                 "o.value='gemma4:26b';o.textContent='gemma4:26b';s.appendChild(o);} s.value='gemma4:26b';"
                 "return testConnection(true);})()")
            assert t.wait_for("!document.getElementById('ctx-status').hidden && "
                              "document.getElementById('ctx-status').className==='conn-fail'", 30), \
                t.js("document.getElementById('ctx-status').outerHTML")
            txt = t.js("document.getElementById('ctx-text').textContent")
            assert "4096" in txt and "16384" in txt, txt
            assert t.js("!document.getElementById('ctx-make').hidden")
            t.js("document.getElementById('ctx-make').click(), 1")
            assert t.wait_for("document.getElementById('model').value === 'gemma4:26b-ctx32k'", 30), \
                t.js("document.getElementById('ctx-text').textContent")
            assert g.created and g.created[0]["from"] == "gemma4:26b"
            t.drain()
            assert not t.errs, t.errs
        finally:
            if t:
                t.close()
            br.terminate()
            srv.terminate()
            for p in (br, srv):
                try:
                    p.wait(timeout=5)
                except Exception:
                    p.kill()
            shutil.rmtree(data, ignore_errors=True)
