"""不管前面是哪一種 LLM 伺服器或閘道，思考都要關得掉（v1.16.30）。

## 由來（客戶 2026-09-30）

經過 LiteLLM 接 Ollama 的 gemma4，文件翻譯一份 415 KB 的檔案要 **400 分鐘**；
客戶自己在 LiteLLM 改用 `ollama_chat/` ＋ `reasoning_effort: "none"` 之後變成 62 秒。

原因是 v1.16.17：關閉思考的參數改成**只送給偵測得到的 Ollama**（問 `GET /api/version`），
經過閘道時偵測不到 → 完全沒送 → 思考整個開著，而畫面上看不出任何原因。

現在一律送兩個通用參數（`reasoning_effort: "none"`、`chat_template_kwargs.enable_thinking=false`），
**對方不收就拿掉重送、並記住**；另外偵測回應裡的思考內容。

每一種假服務照那一家實際的行為寫：**不收的參數怎麼回、認得哪一個、沒關掉時思考內容放在哪個欄位**。

**`litellm` 那一種照真的 LiteLLM 1.103.1 實測寫**（2026-09-30，前面接 Ollama 的 gemma4:26b）：
兩個參數都收、不拒絕；`ollama/` 與 `ollama_chat/` 兩種寫法送 `reasoning_effort:"none"` 都關得掉
（一段翻譯 53.8 / 28.7 秒 → 2.4 秒）。**`ollama/` 不把思考內容轉出來**：思考開著時看到的思考字數是 0，
短問題的回答是空的 —— 那是 `hidden` 這一種要驗的。
第一版的假 LiteLLM 是照我以為的行為寫的（會拒絕 `chat_template_kwargs`），真的跑一次才知道不是。
"""
from __future__ import annotations

import json
import socket
import threading
import time

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.core import llm_client as lc

_STANDARD = {"model", "messages", "temperature", "stream", "max_tokens", "top_p",
             "frequency_penalty", "presence_penalty", "stop", "n", "response_format"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Gateway:
    """扮演不同的 LLM 伺服器 / 閘道。

    * `ollama`：有 `/api/version`；什麼都收；`reasoning_effort:"none"` 才關得掉 gemma4 的思考。
    * `litellm`：照真的 LiteLLM：兩個參數都收，`reasoning_effort` 轉給後端；思考內容放在 `reasoning_content`。
    * `names_rejected`：不收 `chat_template_kwargs`，回 400 並點名（嚴格的閘道 / 雲端服務）。
    * `hidden`：不認得任何關閉參數，**也不把思考內容轉出來** —— 思考用光額度、正文是空的
      （LiteLLM `ollama/` 開頭在思考開著時就是這樣）。
    * `vllm`：不收 `reasoning_effort`（回 400 並點名）；靠 `chat_template_kwargs` 關思考。
    * `strict`：不認得的參數一律 400，**訊息不點名是哪一個**（有些雲端服務就是這樣）。
    * `ignores`：什麼都收，但照樣思考（伺服器那一側沒設好）。
    * `think_tag`：什麼都收，思考寫在正文的 `<think>` 裡（沒開 reasoning parser 的 llama.cpp / vLLM）。
    * `broken`：一律 400「超出上下文長度」—— 跟我們的參數無關，**不可以被記成「不收那兩個參數」**。
    """

    def __init__(self, kind: str, answer: str = "OK"):
        self.kind = kind
        self.answer = answer
        self.bodies: list[dict] = []
        self.port = _free_port()
        self.app = self._build()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def _thinks(self, body: dict) -> bool:
        if self.kind in ("ignores", "hidden"):
            return True
        if self.kind in ("ollama", "litellm", "names_rejected"):
            return body.get("reasoning_effort") != "none"
        if self.kind == "vllm":
            return (body.get("chat_template_kwargs") or {}).get("enable_thinking") is not False
        return False

    def _build(self) -> FastAPI:
        app = FastAPI()
        me = self

        @app.get("/api/version")
        async def version():
            if me.kind == "ollama":
                return {"version": "0.33.3"}
            return JSONResponse({"error": "not found"}, status_code=404)

        @app.get("/v1/models")
        async def models():
            return {"data": [{"id": "gemma4:26b", "owned_by": "x"}]}

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            body = await request.json()
            me.bodies.append(body)
            extra = sorted(set(body) - _STANDARD)
            if me.kind == "broken":
                return JSONResponse({"error": {"message": "context length exceeded"}}, status_code=400)
            if me.kind == "names_rejected" and "chat_template_kwargs" in extra:
                return JSONResponse({"error": {"message":
                    "UnsupportedParamsError: this provider does not support parameters: "
                    "['chat_template_kwargs']"}}, status_code=400)
            if me.kind == "vllm" and "reasoning_effort" in extra:
                return JSONResponse({"error": {"message":
                    "1 validation error: reasoning_effort is not supported for this model"}},
                    status_code=422)
            if me.kind == "strict" and extra:
                return JSONResponse({"error": {"message": "Invalid request"}}, status_code=400)
            thinks = me._thinks(body)
            if not body.get("stream"):
                msg = {"content": me.answer}
                if thinks:
                    msg["reasoning_content"] = "let me think " * 50
                return {"choices": [{"message": msg}]}

            def gen():
                if thinks and me.kind == "hidden":
                    # 思考把額度用光、而且思考內容沒轉出來：正文是空的
                    yield "data: [DONE]\n\n"
                    return
                if thinks:
                    field = "reasoning" if me.kind == "ollama" else "reasoning_content"
                    for _ in range(5):
                        yield "data: " + json.dumps(
                            {"choices": [{"delta": {field: "hmm, let me think. "}}]}) + "\n\n"
                content = me.answer
                if me.kind == "think_tag":
                    content = "<think>ponder ponder</think>" + me.answer
                yield "data: " + json.dumps({"choices": [{"delta": {"content": content}}]}) + "\n\n"
                yield "data: [DONE]\n\n"
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
        for d in (lc._BACKEND_CACHE, lc._REJECTED_PARAMS, lc._THINKING_SEEN, lc._THINKING_WARNED):
            d.clear()
        return self

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(timeout=5)
        for d in (lc._BACKEND_CACHE, lc._REJECTED_PARAMS, lc._THINKING_SEEN, lc._THINKING_WARNED):
            d.clear()


def _client(g: Gateway) -> lc.LLMClient:
    return lc.LLMClient(g.base, timeout=10)


# ---------- 每一種伺服器 / 閘道都關得掉 ----------

@pytest.mark.parametrize("kind", ["ollama", "litellm", "names_rejected", "vllm", "strict"])
def test_thinking_is_off_on_every_kind_of_server(kind):
    with Gateway(kind) as g:
        c = _client(g)
        out = c.text_query("translate this", "gemma4:26b")
    assert out == "OK"
    assert c.last_stats["reasoning_chars"] == 0, (
        f"{kind}：模型還在思考 —— 關閉思考的參數沒有送到它認得的那一個")


def test_through_litellm_the_standard_parameter_gets_through():
    """客戶的情境：經 LiteLLM 接 Ollama —— `reasoning_effort:"none"` 要送到，而且一次就過（真的 LiteLLM 兩個都收）。"""
    with Gateway("litellm") as g:
        _client(g).text_query("x", "gemma4:26b")
    assert len(g.bodies) == 1, "真的 LiteLLM 不拒絕這兩個參數，不該有重送"
    assert g.bodies[-1].get("reasoning_effort") == "none", g.bodies[-1]


def test_a_gateway_that_names_the_rejected_parameter_keeps_the_other_one():
    with Gateway("names_rejected") as g:
        _client(g).text_query("x", "gemma4:26b")
    final = g.bodies[-1]
    assert final.get("reasoning_effort") == "none", final
    assert "chat_template_kwargs" not in final


def test_on_vllm_the_template_switch_gets_through():
    with Gateway("vllm") as g:
        _client(g).text_query("x", "qwen3:32b")
    final = g.bodies[-1]
    assert final.get("chat_template_kwargs") == {"enable_thinking": False}, final
    assert "reasoning_effort" not in final


def test_ollama_still_gets_its_own_switch():
    with Gateway("ollama") as g:
        _client(g).text_query("x", "gemma4:26b")
    final = g.bodies[-1]
    assert final.get("think") is False and final.get("reasoning_effort") == "none", final
    assert len(g.bodies) == 1, "Ollama 什麼都收，不該有重送"


# ---------- 對方不收：拿掉重送、記住、而且只在真的是那個原因時才記 ----------

def test_a_rejected_parameter_is_remembered_so_the_next_call_does_not_retry():
    with Gateway("names_rejected") as g:
        c = _client(g)
        c.text_query("a", "gemma4:26b")
        first = len(g.bodies)
        c.text_query("b", "gemma4:26b")
    assert first == 2, "第一次：送出 → 被點名拒絕 → 拿掉重送"
    assert len(g.bodies) == 3, "第二次要直接不送那個參數，不可以每次都先被拒一次"


def test_a_server_that_does_not_say_which_one_gets_both_removed():
    with Gateway("strict") as g:
        out = _client(g).text_query("x", "gemma4:26b")
    assert out == "OK"
    final = g.bodies[-1]
    assert not (set(final) - _STANDARD), final


def test_an_unrelated_400_is_not_remembered_as_a_rejection():
    """「超出上下文長度」跟我們的參數無關 —— 記成「不收」的話，之後一小時思考都關不掉。"""
    with Gateway("broken") as g:
        c = _client(g)
        with pytest.raises(httpx.HTTPStatusError):
            c.text_query("x", "gemma4:26b")
        # **要在 `with` 裡面查** —— 離開時假服務會清掉這份記憶（第一版寫在外面，永遠成立）
        assert not lc._REJECTED_PARAMS, lc._REJECTED_PARAMS
        # 同一個 client 再送一次：兩個參數都要照送（沒有被誤記成「不收」）
        with pytest.raises(httpx.HTTPStatusError):
            c.text_query("y", "gemma4:26b")
        assert set(g.bodies[-2]) - _STANDARD == {"reasoning_effort", "chat_template_kwargs"}, g.bodies[-2]


def test_rejections_are_remembered_per_model():
    with Gateway("vllm") as g:
        c = _client(g)
        c.text_query("x", "qwen3:32b")
        assert (c.base_url, "qwen3:32b") in lc._REJECTED_PARAMS
        assert (c.base_url, "llama3:8b") not in lc._REJECTED_PARAMS


# ---------- 影像的兩條路也一樣 ----------

def test_vision_query_turns_thinking_off_on_a_gateway():
    with Gateway("vllm", answer="hello") as g:
        c = _client(g)
        out = c.vision_query(b"PNG", "read it", "some-vision-model", parse_json=False)
    assert out.strip() == "hello"
    assert g.bodies[-1].get("chat_template_kwargs") == {"enable_thinking": False}
    assert c.last_stats["reasoning_chars"] == 0


def test_short_vision_answer_turns_thinking_off_on_a_gateway():
    with Gateway("names_rejected", answer="YES") as g:
        out = _client(g).short_vision_answer(b"PNG", "is it filled?", "gemma4:26b", timeout=10)
    assert out == "YES"
    final = g.bodies[-1]
    assert final.get("reasoning_effort") == "none" and "chat_template_kwargs" not in final


# ---------- 關不掉的時候要看得出來 ----------

def test_thinking_that_the_server_ignores_is_detected(caplog):
    with Gateway("ignores") as g:
        c = _client(g)
        with caplog.at_level("WARNING", logger="app.llm.client"):
            probe = c.thinking_probe("gemma4:26b")
    assert probe["thinking"] is True and probe["reasoning_chars"] > 0, probe
    assert any("思考" in r.getMessage() for r in caplog.records), "要在服務記錄留一行警告"


def test_thinking_that_is_hidden_by_the_gateway_is_still_detected():
    """閘道不把思考內容轉出來時，只看得到「回答是空的」—— 那也要算成在思考，不可以報「不會先思考」。"""
    with Gateway("hidden") as g:
        probe = _client(g).thinking_probe("gemma4:26b")
    assert probe["reasoning_chars"] == 0, "前提：思考內容確實沒轉出來"
    assert probe["thinking"] is True and probe["empty_answer"] is True, probe


def test_thinking_written_into_the_answer_is_detected():
    with Gateway("think_tag") as g:
        probe = _client(g).thinking_probe("deepseek-r1:14b")
    assert probe["thinking"] is True and probe["think_tag"] is True, probe


def test_a_model_that_does_not_think_is_reported_as_such():
    with Gateway("litellm") as g:
        probe = _client(g).thinking_probe("gemma4:26b")
    assert probe["thinking"] is False and probe["reasoning_chars"] == 0, probe


def test_the_settings_page_check_reports_thinking(client):
    with Gateway("ignores") as g:
        r = client.post("/admin/api/llm/test-connection",
                        json={"base_url": g.base, "probe_model": "gemma4:26b"})
    assert r.status_code == 200, r.text
    tc = r.json().get("thinking_check")
    assert tc and tc["thinking"] is True, r.json()


def test_the_settings_page_does_not_probe_unless_asked(client):
    """開頁面時不檢查（會讓對方把模型載進 GPU）—— 沒帶 `probe_model` 就不問模型。"""
    with Gateway("litellm") as g:
        r = client.post("/admin/api/llm/test-connection", json={"base_url": g.base})
    assert r.json().get("thinking_check") is None
    assert g.bodies == [], "沒要求檢查卻送了對話請求"


# ---------- 全系統的 LLM 呼叫都走同一個出口 ----------

def test_every_llm_call_in_the_app_goes_through_llm_client():
    """**用到 LLM 的地方都要關得掉思考** —— 最可靠的保證是全部走 `llm_client` 那三支。

    有人在工具裡自己打 `/chat/completions` 或 Ollama 原生端點的話，這次的修正就照不到它。
    判準用 AST 的字串常數（說明文字不算）。
    """
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "app"
    bad = []
    for py in root.rglob("*.py"):
        if py.name == "llm_client.py":
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                if node.body and isinstance(node.body[0], ast.Expr) and \
                        isinstance(getattr(node.body[0], "value", None), ast.Constant):
                    docs.add(id(node.body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                if any(s in node.value for s in ("/chat/completions", "/api/chat", "/api/generate")):
                    bad.append(f"{py.relative_to(root.parent).as_posix()}:{node.lineno}")
            if isinstance(node, ast.keyword) and node.arg == "think" and \
                    isinstance(node.value, ast.Constant) and node.value.value is True:
                bad.append(f"{py.relative_to(root.parent).as_posix()}:{node.value.lineno}（think=True）")
    assert not bad, f"這些地方繞過 llm_client 或刻意開著思考：{bad}"
