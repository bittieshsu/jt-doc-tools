"""接「不是 Ollama」的 LLM 服務（v1.16.17）。

另一個專案的客戶接 OpenAI 相容服務時踩到一串問題，對照我們自己的程式碼之後有四件：

1. **Ollama 專屬欄位每一次都送**（`think`、`reasoning_effort: "none"`、`options`、
   `chat_template_kwargs`）。檢查嚴格的服務對不認得的參數回 400 → 每個 LLM 工具每次都失敗。
   原本的註解寫「OpenAI / LiteLLM 忽略不認的欄位」—— 沒驗證過的假設。
2. **表單填寫的逐欄校驗只打 Ollama 原生的 `/api/chat`**，而且把「沒回答」當成「沒問題」
   —— 接別家服務時每一欄都 404，報告卻是「全部填對了」。
3. SSE 只認 `data: `（規格允許沒有空白）；串流中途的錯誤被當成空白略過。
4. **LLM 金鑰明文存放**、原樣填回設定頁（其他三組祕密早就加密）。

這裡的假服務刻意做成**檢查嚴格的那一種**：不認得的參數一律 400。
對方寬鬆的話，第 1 條永遠測不到（同「假的對方太好心」那一族）。
"""
from __future__ import annotations

import ast
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.core import llm_client as lc
from app.core import llm_review_per_field as pf
from app.core import llm_settings as ls_mod
from app.core.llm_review import FilledField
from app.core.llm_settings import llm_settings

ROOT = Path(__file__).resolve().parent.parent

#: OpenAI Chat Completions 的標準欄位（我們會用到的那幾個 ＋ 常見的）
_STANDARD = {"model", "messages", "temperature", "stream", "max_tokens", "top_p",
             "frequency_penalty", "presence_penalty", "stop", "n", "response_format"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeLLM:
    """`kind="strict"`：檢查嚴格的 OpenAI 相容服務（不是 Ollama）。
    `kind="ollama"`：有 `/api/version` 與原生 `/api/chat`，什麼欄位都收。"""

    def __init__(self, kind: str = "strict", *, answer: str = "YES",
                 sse_space: bool = True, stream_error: bool = False,
                 fail_all: bool = False):
        self.kind = kind
        self.answer = answer
        self.sse_space = sse_space
        self.stream_error = stream_error
        self.fail_all = fail_all
        self.bodies: list[dict] = []
        self.auth: list[str] = []
        self.native_calls = 0
        self.port = _free_port()
        self.app = self._build()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def _build(self) -> FastAPI:
        app = FastAPI()
        me = self

        @app.get("/api/version")
        async def version():
            if me.kind == "ollama":
                return {"version": "0.33.3"}
            return JSONResponse({"error": "not found"}, status_code=404)

        @app.get("/v1/models")
        async def models(request: Request):
            me.auth.append(request.headers.get("authorization", ""))
            return {"data": [{"id": "gemma4:26b", "owned_by": "x"}]}

        @app.post("/api/chat")
        async def native(request: Request):
            me.native_calls += 1
            if me.kind != "ollama" or me.fail_all:
                return JSONResponse({"error": "not found"}, status_code=404)
            me.auth.append(request.headers.get("authorization", ""))
            return {"message": {"content": me.answer}}

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            body = await request.json()
            me.bodies.append(body)
            me.auth.append(request.headers.get("authorization", ""))
            if me.fail_all:
                return JSONResponse({"error": {"message": "no"}}, status_code=404)
            if me.kind == "strict":
                bad = sorted(set(body) - _STANDARD)
                if bad:
                    return JSONResponse({"error": {
                        "message": f"Unrecognized request argument supplied: {bad[0]}",
                        "type": "invalid_request_error"}}, status_code=400)
            if not body.get("stream"):
                return {"choices": [{"message": {"content": me.answer}}]}
            sp = " " if me.sse_space else ""

            def gen():
                yield f"data:{sp}" + json.dumps(
                    {"choices": [{"delta": {"content": me.answer}}]}) + "\n\n"
                if me.stream_error:
                    yield f"data:{sp}" + json.dumps(
                        {"error": {"message": "upstream failed at 10.9.8.7"}}) + "\n\n"
                yield f"data:{sp}[DONE]\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream")

        return app

    def __enter__(self):
        import uvicorn
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        for _ in range(100):
            if getattr(self._server, "started", False):
                break
            time.sleep(0.05)
        lc._BACKEND_CACHE.clear()
        return self

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(timeout=5)
        lc._BACKEND_CACHE.clear()


@pytest.fixture
def saved_settings():
    """這些測試會寫 LLM 設定檔 —— 跑完原樣還回去。"""
    p = llm_settings._path
    before = p.read_bytes() if p.exists() else None
    yield
    if before is None:
        p.unlink(missing_ok=True)
    else:
        p.write_bytes(before)
    lc._BACKEND_CACHE.clear()


# ------------------------------------------------ 1. Ollama 專屬欄位只給 Ollama

def test_a_strict_service_gets_no_ollama_only_fields():
    with FakeLLM("strict") as f:
        out = lc.LLMClient(f.base, timeout=10).text_query("hi", "gemma4:26b")
    assert out == "YES"
    body = f.bodies[-1]
    assert "think" not in body and "reasoning_effort" not in body, body


def test_ollama_still_gets_its_thinking_switches():
    """反向對照：Ollama 上的 gemma4 靠 `reasoning_effort: "none"` 才關得掉思考
    （不關的話一段翻譯寫兩萬多字的思考）。只驗嚴格那條的話，一律不送也會過。"""
    with FakeLLM("ollama") as f:
        lc.LLMClient(f.base, timeout=10).text_query("hi", "gemma4:26b")
    body = f.bodies[-1]
    assert body.get("think") is False and body.get("reasoning_effort") == "none", body


def test_vision_query_keeps_ollama_fields_away_from_a_strict_service():
    with FakeLLM("strict", answer="hello") as f:
        out = lc.LLMClient(f.base, timeout=10).vision_query(
            b"PNG", "read it", "gemma4:26b", parse_json=False, repeat_penalty=1.1)
    assert out.strip() == "hello"
    body = f.bodies[-1]
    for k in ("options", "think", "chat_template_kwargs", "reasoning_effort"):
        assert k not in body, f"送了 Ollama 專屬的 {k}"
    assert "frequency_penalty" in body, "標準的 frequency_penalty 照樣要送"
    assert f.native_calls == 0, "不是 Ollama 就不該去打 /api/chat"


# ------------------------------------------------ 3. SSE 解析

def test_sse_without_a_space_after_the_colon_is_read():
    with FakeLLM("strict", answer="ok", sse_space=False) as f:
        out = lc.LLMClient(f.base, timeout=10).text_query("hi", "m")
    assert out == "ok", "`data:{…}`（沒有空白）每一行都被略過了"


def test_an_error_in_the_middle_of_the_stream_is_raised_not_swallowed():
    with FakeLLM("strict", stream_error=True) as f:
        with pytest.raises(lc.LLMError) as ei:
            lc.LLMClient(f.base, timeout=10).text_query("hi", "m")
    assert "10.9.8.7" not in str(ei.value), "對方的訊息可能帶內部位址，不送到畫面上"


# ------------------------------------------------ 2. 表單填寫的逐欄校驗

def _fields(n=3):
    return [FilledField(page=0, label_text=f"欄位{i}", profile_key=f"k{i}",
                        value=f"值{i}", slot_pt=(50, 50 + i * 40, 300, 80 + i * 40))
            for i in range(n)]


def _review(monkeypatch, fake: FakeLLM, key: str | None = None):
    monkeypatch.setattr(pf.llm_settings, "get", lambda: {
        "enabled": True, "base_url": fake.base, "consecutive_required": 2})
    monkeypatch.setattr(pf.llm_settings, "get_model_for", lambda tool: "gemma4:26b")
    monkeypatch.setattr(pf.llm_settings, "make_client",
                        lambda: lc.LLMClient(fake.base, api_key=key, timeout=10))
    monkeypatch.setattr(pf, "_render_page", lambda p, i: None)
    monkeypatch.setattr(pf, "_crop_tile", lambda img, slot: b"TILE")
    return pf.per_field_review(Path("x.pdf"), _fields(), page_index=0)


def test_field_review_works_on_a_non_ollama_service(monkeypatch):
    with FakeLLM("strict", answer="YES") as f:
        res = _review(monkeypatch, f, key="sk-test")
    assert not res.errors, res.errors
    assert res.rounds[0].verdict == "all_clear"
    assert f.native_calls == 0
    assert len(f.bodies) == 3, "每一欄都要真的問到"
    assert f.auth[-1] == "Bearer sk-test", "設定的金鑰要帶上（原本這條路完全不帶）"
    parts = f.bodies[-1]["messages"][0]["content"]
    assert any(p.get("type") == "image_url" for p in parts)


def test_field_review_on_ollama_still_uses_the_native_endpoint(monkeypatch):
    """反向對照：Ollama 照舊走原生 `/api/chat`（那條是實測過穩定的）。"""
    with FakeLLM("ollama", answer="YES") as f:
        res = _review(monkeypatch, f)
    assert not res.errors
    assert f.native_calls == 3 and not f.bodies


def test_no_answer_is_not_reported_as_all_clear(monkeypatch):
    """**問不到不等於沒問題** —— 一格都沒回答時要講出來，不可以報「全部填對了」。"""
    with FakeLLM("strict", fail_all=True) as f:
        res = _review(monkeypatch, f)
    assert res.errors == [pf.MESSAGES["no_answer"].replace("{0}", "3")], res.errors
    assert res.rounds[0].verdict != "all_clear"


def test_some_misses_are_reported_on_the_round(monkeypatch):
    calls = {"n": 0}
    real = pf._ask

    def flaky(client, model, prompt, png, timeout):
        calls["n"] += 1
        if calls["n"] == 2:
            return "", "HTTP 500"
        return real(client, model, prompt, png, timeout)

    monkeypatch.setattr(pf, "_ask", flaky)
    with FakeLLM("strict", answer="YES") as f:
        res = _review(monkeypatch, f)
    assert not res.errors
    assert res.rounds[0].error == pf.MESSAGES["partial"].replace("{0}", "1").replace("{1}", "3")


def test_the_field_review_messages_are_translated():
    for loc in ("en", "ja"):
        cat = json.loads((ROOT / "app" / "i18n" / f"{loc}.json").read_text(encoding="utf-8"))
        for key, msg in pf.MESSAGES.items():
            assert msg in cat, f"{loc}.json 沒有 {key}：{msg}"


# ------------------------------------------------ 4. 金鑰加密存放

def test_the_key_is_stored_encrypted_and_never_shown(saved_settings):
    llm_settings.update({"api_key": "sk-secret-123"})
    raw = llm_settings._path.read_text(encoding="utf-8")
    assert "sk-secret-123" not in raw
    assert json.loads(raw).get("api_key_enc")
    assert llm_settings.get()["api_key"] == ls_mod.SECRET_KEPT
    assert "api_key_enc" not in llm_settings.get()
    assert llm_settings.api_key() == "sk-secret-123"


def test_kept_keeps_and_empty_clears(saved_settings):
    llm_settings.update({"api_key": "sk-a"})
    llm_settings.update({"api_key": ls_mod.SECRET_KEPT, "model": "x"})
    assert llm_settings.api_key() == "sk-a"
    llm_settings.update({"api_key": ""})
    assert llm_settings.api_key() is None
    assert llm_settings.get()["api_key"] == ""


def test_an_old_plaintext_key_is_moved_on_first_read(saved_settings):
    data = json.loads(llm_settings._path.read_text(encoding="utf-8"))
    data.pop("api_key_enc", None)
    data["api_key"] = "sk-legacy"
    llm_settings._path.write_text(json.dumps(data), encoding="utf-8")
    assert llm_settings.api_key() == "sk-legacy"
    assert "sk-legacy" not in llm_settings._path.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="Windows 沒有 POSIX 權限位元")
def test_the_settings_file_is_not_world_readable(saved_settings):
    llm_settings.update({"api_key": "sk-b"})
    assert (llm_settings._path.stat().st_mode & 0o077) == 0


def test_the_client_sends_the_real_key(saved_settings):
    with FakeLLM("strict") as f:
        llm_settings.update({"enabled": True, "base_url": f.base, "api_key": "sk-real"})
        llm_settings.make_client().text_query("hi", "m")
    assert f.auth[-1] == "Bearer sk-real", f.auth


def test_the_page_and_the_api_do_not_carry_the_key(client, saved_settings):
    llm_settings.update({"api_key": "sk-visible?"})
    assert "sk-visible?" not in client.get("/admin/llm-settings").text
    assert "sk-visible?" not in client.get("/admin/api/llm/settings").text
    r = client.post("/admin/api/llm/settings", json={"model": "gemma4:26b"})
    assert "sk-visible?" not in r.text


def test_a_saved_key_only_goes_to_the_saved_address(client, saved_settings):
    """「測試連線」帶著替身字串時，**存著的金鑰只送給存檔時的那個位址** ——
    不然在表單上改一個還沒存的位址，就等於把金鑰交給任意一台主機。"""
    with FakeLLM("strict") as saved, FakeLLM("strict") as other:
        llm_settings.update({"base_url": saved.base, "api_key": "sk-mine"})
        client.post("/admin/api/llm/test-connection",
                    json={"base_url": other.base, "api_key": ls_mod.SECRET_KEPT})
        client.post("/admin/api/llm/test-connection",
                    json={"base_url": saved.base, "api_key": ls_mod.SECRET_KEPT})
    assert other.auth and all(a == "" for a in other.auth), other.auth
    assert "Bearer sk-mine" in saved.auth, "同一個位址時要帶上存著的金鑰"


def test_export_and_import_rekey_the_llm_key(saved_settings, tmp_path):
    from app.core import settings_export as se
    llm_settings.update({"api_key": "sk-export"})
    blob = se._rekey_export_blob("llm_settings.json")
    assert blob and "sk-export" in blob, "備份裡要是明文（換一台機器才解得開）"
    target = tmp_path / "llm_settings.json"
    target.write_text(blob, encoding="utf-8")
    se._rekey_after_import(target)
    again = target.read_text(encoding="utf-8")
    assert "sk-export" not in again
    assert ls_mod.decrypt_secret(json.loads(again)["api_key_enc"]) == "sk-export"


def test_nobody_reads_the_key_from_the_settings_dict():
    """`get()` 裡的 `api_key` 現在是替身字串 —— 誰再拿它當金鑰，送出去的就是
    `Bearer __KEPT__`。要真正的金鑰一律 `llm_settings.api_key()`。"""
    bad = []
    for py in (ROOT / "app").rglob("*.py"):
        if py.name == "llm_settings.py":
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # 請求 body 裡的 `api_key`（管理頁送來的）不算 —— 那是輸入不是設定
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and node.args
                    and not (isinstance(node.func.value, ast.Name)
                             and node.func.value.id == "body")
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "api_key"):
                bad.append(f"{py.relative_to(ROOT).as_posix()}:{node.lineno}")
            if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
                    and node.slice.value == "api_key"):
                bad.append(f"{py.relative_to(ROOT).as_posix()}:{node.lineno}")
    assert not bad, f"這些地方從設定 dict 讀金鑰：{bad}"
