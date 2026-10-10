"""公文撰擬：叫模型時，進度文字要講出「資料已送到 LLM 伺服器、AI 正在回覆」（v1.16.71）。

## 由來

產生草稿要二十秒上下，原本整段只顯示「整理資料（1/3）」「撰寫草稿（2/3）」——
看起來跟程式自己在跑沒有兩樣，使用者感受不到這一步是 AI 在做。

## 判準

* 送出時：「整理資料：已送到 LLM 伺服器，等待 AI 回應（1/3）」
* AI 開始回覆後：「撰寫草稿：AI 正在回覆，已收到 N 字（2/3）」—— 字數是**真的收到的**
  （`llm_client` 收到第一段正文就回報一次，之後每 32 段一次）
* 格式不對重問時：「…AI 回覆的格式不對，重新詢問…」
* 段名跟著目前的段走（整理資料 → 撰寫草稿），**不可以停在第一段**
* 「檢查草稿」那一段不叫模型，不會出現這幾句
* 進度回報壞掉不可以讓生成或草稿失敗（那只是進度）
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.test_official_doc_tool import FakeLLM, _start, _wait_job  # noqa: F401


# ------------------------------------------------------------------ llm_client

def _sse_server(n_chunks: int):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # /api/version：不是 Ollama
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for _ in range(n_chunks):
                c = json.dumps({"choices": [{"delta": {"content": "甲乙"}}]})
                self.wfile.write(f"data: {c}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def test_the_stream_reports_how_much_has_arrived():
    from app.core.llm_client import LLMClient
    srv, port = _sse_server(100)
    try:
        client = LLMClient(base_url=f"http://127.0.0.1:{port}/v1", timeout=30)
        seen: list[int] = []
        out = client.text_query("x", model="m", on_progress=seen.append)
        assert out == "甲乙" * 100
        # 第一段一到就要講（「AI 開始回覆了」最需要讓人看到的就是那一刻）
        assert seen[0] == 2, seen[:3]
        # 之後每 32 段一次，數字是真的收到的正文字數
        assert seen[1:] == [64, 128, 192], seen
    finally:
        srv.shutdown()


def test_a_broken_progress_callback_does_not_break_the_generation():
    from app.core.llm_client import LLMClient
    srv, port = _sse_server(40)
    try:
        client = LLMClient(base_url=f"http://127.0.0.1:{port}/v1", timeout=30)

        def boom(_n):
            raise RuntimeError("畫面那邊壞了")
        assert client.text_query("x", model="m", on_progress=boom) == "甲乙" * 40
    finally:
        srv.shutdown()


# ------------------------------------------------------------------ 作業的進度文字

def _running_job():
    from app.core.job_manager import job_manager
    js = [j for j in job_manager._jobs.values()
          if j.tool_id == "official-doc" and j.status == "running"]
    assert len(js) == 1, [j.status for j in js]
    return js[0]


class _Watching(FakeLLM):
    """每次被叫時記下當下的進度文字；有 `on_progress` 就照真的那樣回報兩次。"""

    def __init__(self):
        super().__init__()
        self.seen: list[tuple[str, str]] = []

    def text_query(self, prompt, model=None, on_progress=None, **kw):
        job = _running_job()
        self.seen.append(("sent", job.message))
        if on_progress is not None:
            on_progress(3)
            self.seen.append(("first", job.message))
            on_progress(57)
            self.seen.append(("later", job.message))
        return super().text_query(prompt, model=model, **kw)


@pytest.fixture
def watching_llm(monkeypatch):
    from app.core import llm_settings as ls
    fake = _Watching()
    monkeypatch.setattr(ls.llm_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(ls.llm_settings, "make_client", lambda *a, **k: fake)
    monkeypatch.setattr(ls.llm_settings, "get_model_for", lambda _t: "fake-model")
    return fake


def test_the_job_says_the_llm_server_is_working(client, auth_off, watching_llm):
    r = _start(client)
    assert r.status_code == 200, r.text
    j = _wait_job(r.json()["job_id"])
    assert j.status == "done", getattr(j, "error", None)
    seen = watching_llm.seen
    assert len(seen) == 6, seen      # 整理資料、撰寫草稿各叫一次
    assert seen[0] == ("sent", "整理資料：已送到 LLM 伺服器，等待 AI 回應（1/3）"), seen
    assert seen[1] == ("first", "整理資料：AI 正在回覆，已收到 3 字（1/3）"), seen
    assert seen[2] == ("later", "整理資料：AI 正在回覆，已收到 57 字（1/3）"), seen
    # 段名要跟著換 —— 停在「整理資料」的話使用者會以為卡在第一步
    assert seen[3] == ("sent", "撰寫草稿：已送到 LLM 伺服器，等待 AI 回應（2/3）"), seen
    assert seen[5] == ("later", "撰寫草稿：AI 正在回覆，已收到 57 字（2/3）"), seen
    assert j.message == "完成"


def test_asking_again_says_so(monkeypatch):
    """格式不對重問時講出來（不然畫面像是倒退回「等待回應」）。"""
    import importlib
    r = importlib.import_module("app.tools.official_doc.router")
    calls = []

    class _C:
        def text_query(self, prompt, model=None, on_progress=None, **kw):
            calls.append(on_progress)
            on_progress(9)
            return "{}"

    monkeypatch.setattr(r.llm_settings, "make_client", lambda *a, **k: _C())
    monkeypatch.setattr(r.llm_settings, "get_model_for", lambda _t: "m")

    class _J:
        cancelled = False
    got = []
    ask = r._ask_for(_J(), lambda kind, n: got.append((kind, n)))
    ask("x")
    ask.retry("x")
    assert got == [("wait", 0), ("stream", 9), ("retry", 0), ("stream", 9)], got
    assert all(c is not None for c in calls), "重問那一次沒有接上進度回報"


def test_a_broken_status_callback_does_not_stop_the_draft(monkeypatch):
    import importlib
    r = importlib.import_module("app.tools.official_doc.router")

    class _C:
        def text_query(self, prompt, model=None, on_progress=None, **kw):
            on_progress(1)
            return "好"

    monkeypatch.setattr(r.llm_settings, "make_client", lambda *a, **k: _C())
    monkeypatch.setattr(r.llm_settings, "get_model_for", lambda _t: "m")

    class _J:
        cancelled = False

    def boom(kind, n):
        raise RuntimeError("壞了")
    ask = r._ask_for(_J(), boom)
    assert ask("x") == "好"
    assert ask.retry("x") == "好"


def test_the_check_stage_never_calls_the_model():
    """「檢查草稿」是程式比對原文，不叫模型 —— 所以進度文字只為前兩段準備譯文。
    哪天檢查那一段也要叫模型，這條會紅，譯文要跟著補。"""
    import inspect
    from app.core import official_doc as od
    for fn in (od.run_sign, od.run_letter, od.run_endorse):
        src = inspect.getsource(fn)
        tail = src.split("on_stage(2, STAGES[2])", 1)[1]
        assert "_ask_json(" not in tail and "ask(" not in tail, fn.__name__
