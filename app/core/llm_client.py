"""OpenAI-compat HTTP client for vision LLM (gemma4, gemma3, etc.).

This module is part of the **附加功能** (add-on) LLM review feature. It is
imported lazily — never from core code paths — so missing / misconfigured
LLM backends never break the core PDF tools.

Usage::

    from app.core.llm_settings import llm_settings
    client = llm_settings.make_client()
    if client:
        result = client.test_connection()

Backend compatibility (all are OpenAI-compat HTTP):
- Ollama  (http://localhost:11434/v1)
- vLLM    (http://localhost:8000/v1)
- LM Studio  (http://localhost:1234/v1)
- jan.ai  (http://localhost:1337/v1)
- DGX Spark + Ollama (http://<lan-ip>:11434/v1)  ← deployment 預設場景
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import httpx


# v1.5.8: SSRF validator 搬到 app.core.url_safety 以絕對 import 路徑走,
# 讓 CodeQL MaD barrierModel 認得（同檔 private function 不被 API graph 抓到）。
# 維持 _validate_llm_base_url 別名給 backward-compat;`__init__` 走別名 OK,
# regression tests `tests/test_llm_url_ssrf.py` 也走別名。
from app.core.url_safety import validate_llm_base_url as _validate_llm_base_url  # noqa: F401


@dataclass
class ModelInfo:
    """Single model entry from /v1/models. ``size_bytes`` is best-effort
    (not all backends report it; Ollama does)."""
    id: str
    owned_by: str = ""
    size_bytes: int = 0

    @property
    def looks_vision(self) -> bool:
        """Heuristic: model id mentions vision / multimodal naming."""
        n = self.id.lower()
        return any(t in n for t in (
            "vl", "vision", "llava", "minicpm-v", "gemma3", "gemma4",
        ))


@dataclass
class ConnectionResult:
    ok: bool
    latency_ms: int = 0
    models: list[ModelInfo] = field(default_factory=list)
    error: Optional[str] = None


class LLMError(Exception):
    """Raised when the LLM backend returns malformed / unexpected response."""


def _extract_json_from_response(content: str) -> dict:
    """Find + parse a JSON object inside free-form LLM output.

    The LLM is asked to output JSON, but when we don't force json_object
    mode (we don't, because it hurts reasoning) the model may wrap the JSON
    in prose ("好的，以下是我的分析：{...}") or markdown fences (```json...```).
    We try, in order:
    1. Parse the entire content as JSON
    2. Strip ```json ... ``` fence and parse
    3. Find the first balanced {...} block via bracket counting and parse

    Raises ``LLMError`` when none of the above yields valid JSON.
    """
    s = (content or "").strip()
    if not s:
        raise LLMError("empty response")

    # 1. whole thing is JSON?
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # 2. markdown fence?
    if "```" in s:
        # Extract content between first ``` and last ```
        lines = s.splitlines()
        in_block = False
        block: list[str] = []
        for ln in lines:
            if ln.strip().startswith("```"):
                if in_block:
                    break
                in_block = True
                continue
            if in_block:
                block.append(ln)
        if block:
            try:
                return json.loads("\n".join(block).strip())
            except json.JSONDecodeError:
                pass

    # 3. Find the first balanced { ... } block. Naive bracket counting works
    # because JSON strings with embedded { } are rare in typical model output,
    # and we're just looking for the outermost object.
    depth = 0
    start = -1
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = s[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    # keep scanning — maybe there's another {...}
                    start = -1
                    continue

    raise LLMError(f"no parseable JSON in response: {s[:300]!r}")


class StreamDeadline(Exception):
    """整次生成超過上限。**不是連線問題，是模型停不下來。**

    `stream=True` 時 httpx 的 `timeout` 是**每個 chunk 的讀取逾時** —— 只要
    模型一直吐 token，逾時就永遠不會觸發，而設定裡那個
    `timeout_seconds` 的說明寫的是「單次 HTTP 呼叫上限」。實測：一段表格的
    填空文字（`For the transition period from<16 個不斷行空白> to`）讓
    gemma4:26b 停不下來，**整份文件的翻譯就永遠卡在那一段**，畫面顯示
    「翻譯中… N/M」不動，也沒有任何錯誤訊息。
    """


def _check_deadline(t0: float, limit: float) -> None:
    import time as _t
    if limit and _t.monotonic() - t0 > limit:
        raise StreamDeadline(
            f"單次生成超過 {limit:.0f} 秒仍未結束（模型可能停不下來）")


#: `[DONE]` 的記號（SSE 串流的結尾）
_SSE_DONE = object()


def _sse_delta(line: str, stats: Optional[dict] = None):
    """SSE 的一行 → 這一段新增的文字；`[DONE]` 回 `_SSE_DONE`；不是資料行回空字串。

    * 給了 `stats` 的話，順便數**思考內容**有幾個字：`reasoning_content`（vLLM、LiteLLM、
      DeepSeek）、`reasoning`（Ollama 的 OpenAI 相容端點），以及寫在正文裡的 `<think>`。
      思考沒被關掉時回應會慢很多倍，而**畫面上完全看不出來**（客戶 2026-09-30 經 LiteLLM
      翻一份 415 KB 的文件要 400 分鐘，自己查了半天）。

    * **`data:` 後面的空白可有可無**（SSE 規格：冒號後若是一個空白就去掉）。原本只認
      `data: `，送 `data:{…}` 的服務每一行都會被略過 —— 回來的是空字串，看起來像
      模型什麼都沒說，而其實是我們丟掉的。
    * **串流中途的 `{"error": …}` 要丟例外**，不可以當成空白略過：對方說了為什麼失敗，
      略過的話呼叫端拿到空字串，原因就沒了。原因寫進記錄；例外訊息是固定的一句
      （對方的訊息可能帶內部位址，不送到畫面上）。
    """
    if not line or not line.startswith("data:"):
        return ""
    data = line[5:]
    if data.startswith(" "):
        data = data[1:]
    data = data.strip()
    if data == "[DONE]":
        return _SSE_DONE
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        return ""
    if isinstance(chunk, dict) and chunk.get("error"):
        err = chunk["error"]
        detail = err.get("message") if isinstance(err, dict) else err
        import logging as _lg
        _lg.getLogger("app.llm.client").warning(
            "LLM 服務在串流中途回報錯誤：%s", str(detail)[:500])
        raise LLMError("LLM 服務在回覆途中回報錯誤（原因記在服務記錄）")
    try:
        delta = chunk["choices"][0].get("delta", {}) or {}
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""
    content = delta.get("content") or ""
    if stats is not None and isinstance(delta, dict):
        for key in ("reasoning_content", "reasoning"):
            r = delta.get(key)
            if isinstance(r, str) and r:
                stats["reasoning_chars"] = stats.get("reasoning_chars", 0) + len(r)
        if isinstance(content, str) and "<think>" in content:
            stats["think_tag"] = True
    return content if isinstance(content, str) else ""


def _log_http_error(r: httpx.Response) -> None:
    """4xx / 5xx 時把對方回的內容寫進記錄。

    串流請求的 `raise_for_status()` 不會讀 body —— 例外只說「HTTP 400」，
    而真正的原因（例如「Unrecognized request argument supplied: think」）在 body 裡。
    """
    if r.status_code < 400:
        return
    try:
        r.read()
        body = r.text[:500]
    except Exception:          # noqa: BLE001 — 讀不到就算了，不可以蓋掉原本的錯誤
        body = "(讀不到回應內容)"
    import logging as _lg
    _lg.getLogger("app.llm.client").warning(
        "LLM 服務回 HTTP %d：%s", r.status_code, body)


def _body_text(r: httpx.Response) -> str:
    """讀錯誤回應的內容（串流請求要先 `read()`）；讀不到回空字串。"""
    try:
        r.read()
        return r.text[:2000]
    except Exception:          # noqa: BLE001
        return ""


#: **關閉思考用的參數**。不同的 LLM 伺服器 / 閘道各認得其中一種，所以兩個都送：
#:
#: * `reasoning_effort: "none"` —— OpenAI 的標準參數。OpenAI、Azure OpenAI、LiteLLM
#:   （`ollama_chat/` 會轉成 Ollama 的 `think:false`）、OpenRouter、Ollama 0.33+ 都認得。
#: * `chat_template_kwargs: {"enable_thinking": false}` —— vLLM、SGLang、llama.cpp server、
#:   LM Studio 這類直接套對話範本的伺服器，Qwen3 等混合思考模型靠它關掉思考。
#:
#: Ollama 另外送它自己的 `think: false`。**對方不收（HTTP 400 / 422）就拿掉重送**；
#: 確定拿掉之後成功了，才記住「這個位址 ＋ 這個模型不收這個參數」（`_REJECT_TTL`）。
#: 原本（v1.16.17）是「只對 Ollama 送」—— 經過 LiteLLM 等閘道時偵測不到 Ollama，
#: 於是完全沒送，思考整個開著（客戶 2026-09-30：415 KB 的文件翻譯 400 分鐘）。
_THINK_OFF_OLLAMA = (("think", False), ("reasoning_effort", "none"))
_THINK_OFF_GENERIC = (("reasoning_effort", "none"),
                      ("chat_template_kwargs", {"enable_thinking": False}))
_REJECTED_PARAMS: dict[tuple[str, str], tuple[float, frozenset]] = {}
_REJECT_TTL = 3600.0

#: 最近一次看到模型在思考：(base_url, model) → (時間, 思考的字數)。管理頁的測試連線讀這個。
_THINKING_SEEN: dict[tuple[str, str], tuple[float, int]] = {}
_THINKING_WARN_EVERY = 600.0
_THINKING_WARNED: dict[tuple[str, str], float] = {}


#: 「對方是不是 Ollama」的判斷結果，依 base_url 快取。
#: 是的話保留 5 分鐘；不是 / 問不到只保留 30 秒（暫時連不上不要讓 Ollama 被當成別家太久）。
_BACKEND_CACHE: dict[str, tuple[float, bool]] = {}
_BACKEND_TTL_YES = 300.0
_BACKEND_TTL_NO = 30.0


class LLMClient:
    """Thin OpenAI-compat client. Stateless — safe to construct per-request."""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: float = 60.0,
    ):
        # v1.5.8: 用絕對 import 形式 call,讓 CodeQL barrierModel 認得
        from app.core.url_safety import validate_llm_base_url
        self.base_url = validate_llm_base_url(base_url)
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _native_root(self) -> str:
        """Ollama 原生 API 的根（`…/v1` 去掉 `/v1`）。"""
        b = self.base_url.rstrip("/")
        return b[:-3] if b.endswith("/v1") else b

    def is_ollama(self) -> bool:
        """對方是不是 Ollama —— **只有是的時候才送 Ollama 專屬的欄位**。

        `think`、`reasoning_effort: "none"`、`options`、`chat_template_kwargs` 是為了
        關掉 Ollama 上模型的思考（實測 gemma4 不關的話一段翻譯寫兩萬多字的思考）。
        但檢查嚴格的 OpenAI 相容服務（OpenAI 官方、Azure、Gemini 相容層、預設設定的
        LiteLLM）對不認得的參數回 400 —— **每一個 LLM 工具每一次都會失敗**。
        原本的註解寫「OpenAI / LiteLLM 忽略不認的欄位」，那是沒驗證過的假設。

        判準：`GET /api/version` 回 `{"version": …}`（Ollama 才有這支）。
        問不到就當成不是 —— 最壞情況是模型多想一會兒，不會失敗。
        """
        now = time.monotonic()
        hit = _BACKEND_CACHE.get(self.base_url)
        if hit and now - hit[0] < (_BACKEND_TTL_YES if hit[1] else _BACKEND_TTL_NO):
            return hit[1]
        yes = False
        try:
            r = httpx.get(f"{self._native_root()}/api/version", headers=self._headers(),
                          timeout=min(float(self.timeout or 5), 5.0))
            d = r.json() if r.status_code == 200 else None
            yes = isinstance(d, dict) and bool(d.get("version"))
        except Exception:      # noqa: BLE001 — 連不上、不是 JSON 都算「不是」
            yes = False
        _BACKEND_CACHE[self.base_url] = (now, yes)
        return yes

    # ----- 關閉思考：對方不收就拿掉重送 -----------------------------------

    def _think_off(self, model: str, ollama: bool) -> dict:
        """這一次要送的關閉思考參數（扣掉這個位址 ＋ 模型已知不收的）。"""
        import copy
        hit = _REJECTED_PARAMS.get((self.base_url, model))
        rejected = hit[1] if hit and time.monotonic() - hit[0] < _REJECT_TTL else frozenset()
        base = _THINK_OFF_OLLAMA if ollama else _THINK_OFF_GENERIC
        return {k: copy.deepcopy(v) for k, v in base if k not in rejected}

    def _remember_rejected(self, model: str, keys) -> None:
        key = (self.base_url, model)
        hit = _REJECTED_PARAMS.get(key)
        old = hit[1] if hit else frozenset()
        _REJECTED_PARAMS[key] = (time.monotonic(), frozenset(old | set(keys)))

    @staticmethod
    def _drop_rejected(r: httpx.Response, payload: dict, optional: list) -> list:
        """對方回 400 / 422 時：它點名的參數拿掉；沒點名就把關閉思考的全部拿掉。回拿掉了哪些。"""
        body = _body_text(r)
        named = [k for k in optional if k in body]
        drop = named or list(optional)
        import logging as _lg
        _lg.getLogger("app.llm.client").info(
            "LLM 服務不收 %s（HTTP %d），拿掉重送", ", ".join(drop), r.status_code)
        for k in drop:
            payload.pop(k, None)
        return drop

    def _note_thinking(self, model: str, stats: dict, *, wanted: bool = False) -> None:
        """模型還是在思考的話記下來，並寫一行警告（同一個模型 10 分鐘一次）。

        `wanted`：呼叫端自己要求思考（`think=True`）—— 那不是「關不掉」，不記也不警告
        （不然設定頁會說這個模型關不掉思考、記錄裡多一行說我們送了關閉參數的假話）。"""
        if wanted:
            return
        chars = int(stats.get("reasoning_chars") or 0)
        if not chars and not stats.get("think_tag"):
            return
        key = (self.base_url, model)
        now = time.monotonic()
        _THINKING_SEEN[key] = (now, chars)
        if now - _THINKING_WARNED.get(key, -1e9) >= _THINKING_WARN_EVERY:
            _THINKING_WARNED[key] = now
            import logging as _lg
            _lg.getLogger("app.llm.client").warning(
                "模型 %s 回答前還是先「思考」了（這次 %d 字%s）—— 翻譯等工具會慢很多倍。"
                "我們已經送了關閉思考的參數（reasoning_effort / chat_template_kwargs），但 LLM 伺服器"
                "或閘道沒有照做；請在伺服器那一側關閉（見 LLM.md「關閉思考」）",
                model, chars, "，正文裡有 <think>" if stats.get("think_tag") else "")

    def _chat_stream(self, payload: dict, optional: list, *, model: str, stop_when=None,
                     thinking_wanted: bool = False) -> str:
        """POST `/chat/completions`（串流），回完整的正文。

        `optional`：payload 裡**可以拿掉**的鍵（關閉思考用的）。對方回 400 / 422 就拿掉重送；
        拿掉之後成功了，才記住這個位址 ＋ 模型不收（別的原因造成的 400 不會被誤記）。
        外部服務的同時呼叫上限（`remote_limit`）包住整段，重送不會多佔名額。
        """
        from . import remote_limit
        stats: dict = {"reasoning_chars": 0, "think_tag": False}
        optional = [k for k in optional if k in payload]
        dropped: list = []
        parts: list[str] = []
        with remote_limit.slot():
            while True:
                with httpx.stream("POST", f"{self.base_url}/chat/completions",
                                  headers=self._headers(), json=payload,
                                  timeout=self.timeout) as r:
                    if r.status_code in (400, 422) and optional:
                        drop = self._drop_rejected(r, payload, optional)
                        optional = [k for k in optional if k not in drop]
                        dropped += drop
                        continue
                    _log_http_error(r)
                    r.raise_for_status()
                    if dropped:
                        self._remember_rejected(model, dropped)
                    t0 = time.monotonic()
                    for line in r.iter_lines():
                        # **整次生成也要有上限**，不是只有每個 chunk（見 `StreamDeadline`）。
                        # 少了這一行，模型停不下來時整份文件的翻譯會永遠卡住而且沒有錯誤訊息。
                        _check_deadline(t0, self.timeout)
                        delta = _sse_delta(line, stats)
                        if delta is _SSE_DONE:
                            break
                        if delta:
                            parts.append(delta)
                            # 呼叫端可以叫它提早停（例如模型在打轉）：每 32 段看一次最後一截
                            if stop_when and len(parts) % 32 == 0 and \
                                    stop_when("".join(parts[-1024:])):
                                stats["stopped_early"] = True
                                break
                    break
        self.last_stats = stats
        self._note_thinking(model, stats, wanted=thinking_wanted)
        return "".join(parts)

    def _chat_post(self, payload: dict, optional: list, *, model: str,
                   timeout: float) -> dict:
        """POST `/chat/completions`（不串流），回 JSON。拿掉重送的規則同 `_chat_stream`。"""
        optional = [k for k in optional if k in payload]
        dropped: list = []
        while True:
            r = httpx.post(f"{self.base_url}/chat/completions", json=payload,
                           headers=self._headers(), timeout=timeout)
            if r.status_code in (400, 422) and optional:
                drop = self._drop_rejected(r, payload, optional)
                optional = [k for k in optional if k not in drop]
                dropped += drop
                continue
            _log_http_error(r)
            r.raise_for_status()
            if dropped:
                self._remember_rejected(model, dropped)
            data = r.json() or {}
            msg = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
            reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
            content = msg.get("content") or ""
            self._note_thinking(model, {
                "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else 0,
                "think_tag": isinstance(content, str) and "<think>" in content})
            return data

    # ----- 上下文長度（Ollama）-------------------------------------------------
    #
    # Ollama 出廠的上下文長度不大（舊版 2048、之後 4096），**提示超過時它安靜地截掉開頭** ——
    # 被截掉的正是指令，結果亂掉而且沒有任何錯誤。實測（2026-10-08，Ollama 0.33.3）：
    # * `/api/ps` 的 `context_length` 是模型**實際載入**的大小（`.40` 是 131072，伺服器設了環境變數）；
    # * OpenAI 相容端點（我們用的那一支）**不理** `options.num_ctx` —— 只有原生 API 理，而那會讓
    #   模型照新的大小重新載入，跟同一台的其他程式（OpenWebUI…）用不同大小時會來回重載；
    # * 用 `/api/create` 從原模型建一個帶 `num_ctx` 參數的**新名字**（權重共用、不複製），
    #   經 OpenAI 相容端點呼叫時就是那個大小，原模型不受影響。

    #: 公文撰擬（需求 4,000 字＋參考資料＋指令）、會議摘要、文件翻譯一次送的提示加上輸出，
    #: 抓 16K 才夠；低於這個就在設定頁提醒
    CONTEXT_RECOMMENDED = 16384

    def context_probe(self, model: str) -> dict:
        """這個模型在 Ollama 上的上下文長度（設定頁「測試連線」用）。

        回 `{"ollama": bool, "loaded": int|None, "param": int|None, "max": int|None,
        "effective": int|None, "low": bool}`：`loaded` 是現在載入的大小（沒載入時 None）、
        `param` 是模型自己的 `num_ctx` 參數、`max` 是模型最多支援多少。不是 Ollama 就只回
        `{"ollama": False}`（閘道後面看不到，無從判斷）。"""
        import re as _re
        if not self.is_ollama():
            return {"ollama": False}
        root = self._native_root()
        t = min(float(self.timeout or 10), 10.0)
        loaded = param = mx = None
        want = model if ":" in model else model + ":latest"
        try:
            r = httpx.get(f"{root}/api/ps", headers=self._headers(), timeout=t)
            for m in (r.json().get("models") or []) if r.status_code == 200 else []:
                if m.get("name") in (model, want) or m.get("model") in (model, want):
                    v = m.get("context_length")
                    loaded = int(v) if isinstance(v, int) and v > 0 else None
        except Exception:      # noqa: BLE001 — 問不到就不知道，不影響其他檢查
            pass
        try:
            r = httpx.post(f"{root}/api/show", headers=self._headers(), json={"model": model}, timeout=t)
            d = r.json() if r.status_code == 200 else {}
            m = _re.search(r"(?m)^\s*num_ctx\s+(\d+)", str(d.get("parameters") or ""))
            param = int(m.group(1)) if m else None
            for k, v in (d.get("model_info") or {}).items():
                if str(k).endswith(".context_length") and isinstance(v, int):
                    mx = v
        except Exception:      # noqa: BLE001
            pass
        eff = loaded or param
        return {"ollama": True, "loaded": loaded, "param": param, "max": mx, "effective": eff,
                "low": bool(eff and eff < self.CONTEXT_RECOMMENDED)}

    #: 建新版本時給選的大小（K）—— 白名單，不收任意數字
    CONTEXT_CHOICES = (16384, 32768, 65536, 131072)

    @staticmethod
    def context_variant_name(model: str, num_ctx: int) -> str:
        """`gemma4:26b` + 32768 → `gemma4:26b-ctx32k`（沒寫 tag 的補 `latest`）。"""
        name, _, tag = model.partition(":")
        return f"{name}:{tag or 'latest'}-ctx{num_ctx // 1024}k"

    def make_context_variant(self, model: str, num_ctx: int) -> str:
        """在 Ollama 上建一個帶 `num_ctx` 的新名字（`/api/create`，權重共用），回新名字。

        原本那個模型**不動**（同一台的其他程式照用它原本的大小）。失敗丟 `LLMError`（固定訊息）。"""
        import re as _re
        if num_ctx not in self.CONTEXT_CHOICES:
            raise LLMError("上下文長度只能選 16K、32K、64K、128K")
        if not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-/]*(:[A-Za-z0-9._\-]+)?", model or "") \
                or len(model) > 200:
            raise LLMError("模型名稱不合法")
        if not self.is_ollama():
            raise LLMError("這台不是 Ollama（或經過閘道看不到），只能在伺服器那一側設定")
        new = self.context_variant_name(model, num_ctx)
        try:
            r = httpx.post(f"{self._native_root()}/api/create", headers=self._headers(),
                           json={"model": new, "from": model, "parameters": {"num_ctx": num_ctx},
                                 "stream": False},
                           timeout=max(float(self.timeout or 60), 60.0))
        except httpx.HTTPError as exc:
            import logging as _lg
            _lg.getLogger("app.llm.client").warning("建立上下文版本失敗：%s", type(exc).__name__)
            raise LLMError("連不上 Ollama，沒有建立") from exc
        if r.status_code != 200 or '"error"' in (r.text or ""):
            import logging as _lg
            _lg.getLogger("app.llm.client").warning(
                "建立上下文版本失敗（HTTP %d）：%s", r.status_code, (r.text or "")[:300])
            raise LLMError("Ollama 沒有建立（原因記在服務記錄）")
        return new

    def thinking_probe(self, model: str) -> dict:
        """問模型一個極短的問題，看它**有沒有先思考**（管理頁的測試連線用）。

        `max_tokens` 給小一點：思考沒關的話，思考的內容也算在裡面，很快就會停，
        不會為了一次檢查讓模型想上好幾分鐘。

        **回答是空的也算在思考**：有些閘道**不把思考內容轉出來**（實測 LiteLLM 的 `ollama/`
        開頭：思考開著時花了 54 秒，回來的思考字數卻是 0）。這時短問題的額度被思考用光，
        回來的正文是空的 —— 只看思考欄位的話會誤報成「不會先思考」。
        """
        t0 = time.monotonic()
        answer = self.text_query("Reply with exactly one word: OK", model, max_tokens=64)
        stats = getattr(self, "last_stats", {}) or {}
        hit = _REJECTED_PARAMS.get((self.base_url, model))
        empty = not (answer or "").strip()
        return {
            "thinking": bool(stats.get("reasoning_chars") or stats.get("think_tag") or empty),
            "reasoning_chars": int(stats.get("reasoning_chars") or 0),
            "think_tag": bool(stats.get("think_tag")),
            "empty_answer": empty,
            "seconds": round(time.monotonic() - t0, 1),
            "answer": (answer or "")[:40],
            "rejected_params": sorted(hit[1]) if hit else [],
            "ollama": self.is_ollama(),
        }

    def short_vision_answer(self, png: bytes, prompt: str, model: str, *,
                            timeout: float, max_tokens: int = 256) -> str:
        """一張小圖 ＋ 一個短問題（是非題 / 讀出框裡的字），不串流，回純文字。

        給表單填寫的逐欄校驗用（`llm_review_per_field`）。**原本只打 Ollama 原生的
        `/api/chat`** —— 接 vLLM / LM Studio / 雲端服務時每一欄都 404，而呼叫端把
        「沒回答」當成「沒問題」，於是報告整份都填對了（v1.16.17 修）。

        * Ollama：照舊走原生 `/api/chat`（OpenAI 相容端點對影像有時不回內容，
          見 `vision_query` 的 Fallback B；這條路是實測過穩定的那一條）。
        * 其他：`/chat/completions`，影像用 `image_url`。
        失敗一律丟 httpx 的例外，由呼叫端分類。
        """
        b64 = base64.b64encode(png).decode("ascii")
        if self.is_ollama():
            payload = {
                "model": model, "stream": False, "think": False,
                "options": {"temperature": 0.0},
                "messages": [{"role": "user", "content": prompt, "images": [b64]}],
            }
            r = httpx.post(f"{self._native_root()}/api/chat", json=payload,
                           headers=self._headers(), timeout=timeout)
            _log_http_error(r)
            r.raise_for_status()
            return (((r.json() or {}).get("message") or {}).get("content") or "").strip()
        payload = {
            "model": model, "temperature": 0.0, "stream": False,
            "max_tokens": int(max_tokens),
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": prompt},
            ]}],
        }
        extras = self._think_off(model, ollama=False)
        payload.update(extras)
        data = self._chat_post(payload, list(extras), model=model, timeout=timeout)
        choices = data.get("choices") or [{}]
        return (((choices[0] or {}).get("message") or {}).get("content") or "").strip()

    # ----- /v1/models ----------------------------------------------------

    def list_models(self) -> list[ModelInfo]:
        """GET {base_url}/models — return list of available models.

        Handles two response shapes:
        - OpenAI standard: ``{"data": [{"id":..., "owned_by":...}]}``
        - Ollama-extended: same, plus ``"size"`` field per model
        """
        r = httpx.get(
            f"{self.base_url}/models",
            headers=self._headers(),
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        items = []
        if isinstance(data, dict):
            items = data.get("data", []) or []
        out: list[ModelInfo] = []
        for m in items:
            if not isinstance(m, dict):
                continue
            out.append(ModelInfo(
                id=str(m.get("id", "")),
                owned_by=str(m.get("owned_by", "")),
                size_bytes=int(m.get("size") or 0),
            ))
        return out

    def test_connection(self) -> ConnectionResult:
        """Round-trip check: tries to fetch model list, measures latency.
        Never raises — returns ConnectionResult with ``ok=False`` on failure."""
        try:
            t0 = time.monotonic()
            models = self.list_models()
            latency = int((time.monotonic() - t0) * 1000)
            return ConnectionResult(ok=True, latency_ms=latency, models=models)
        except httpx.ConnectError as e:
            return ConnectionResult(ok=False, error=f"連線失敗：{e}")
        except httpx.TimeoutException:
            return ConnectionResult(ok=False, error=f"逾時 ({self.timeout:.0f}s)")
        except httpx.HTTPStatusError as e:
            return ConnectionResult(
                ok=False,
                error=f"HTTP {e.response.status_code}：{e.response.text[:200]}",
            )
        except Exception as e:  # noqa: BLE001
            return ConnectionResult(ok=False, error=f"未預期錯誤：{type(e).__name__}: {e}")

    # ----- /v1/chat/completions ------------------------------------------

    def text_query(
        self,
        prompt: str,
        model: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        think: bool = False,
        system: str | None = None,
        stop_when=None,
    ) -> str:
        """Send a plain-text prompt (no images), return the raw model
        output. Used by features like paragraph reflow that expect prose
        back, not JSON.

        ``stop_when``: optional ``Callable[[str], bool]`` called on the tail of
        the text streamed so far; ``True`` stops the generation early and
        returns what has arrived (a model stuck repeating itself would
        otherwise run all the way to ``max_tokens``).

        ``think=False`` (default) tries to suppress chain-of-thought on
        models that support it. This is a best-effort belt-and-braces:

        - Qwen3 / QwQ honour a literal ``/no_think`` marker in the user
          message, so we prepend it.
        - Ollama's OpenAI-compat endpoint accepts a top-level ``think``
          field (ignored by vanilla OpenAI) — we set it explicitly.
        - Ollama also accepts ``options.think`` and ``chat_template_kwargs``.
        - For models without a native toggle, we add a system message
          spelling out "no reasoning, output only the answer".

        Any backend that doesn't recognise these just ignores them.
        """
        messages: list[dict] = []
        if not think:
            default_sys = (
                "Respond with ONLY the requested output. No reasoning "
                "traces, no <think> tags, no prefaces, no explanations. "
                "Output the final answer directly."
            )
            messages.append({
                "role": "system",
                "content": (system + "\n\n" + default_sys) if system else default_sys,
            })
            # Qwen3 / QwQ family — literal marker disables reasoning
            user_prompt = "/no_think\n\n" + prompt
        else:
            if system:
                messages.append({"role": "system", "content": system})
            user_prompt = prompt
        messages.append({"role": "user", "content": user_prompt})

        payload: dict = {
            "model": model,
            "temperature": temperature,
            "stream": True,
            "messages": messages,
        }
        optional: list = []
        if not think:
            # **關閉思考的參數一律送**，不再只給偵測得到的 Ollama（v1.16.30）：
            # 經過 LiteLLM 等閘道時偵測不到 Ollama，思考就整個開著。
            # 對方不收的，`_chat_stream` 會拿掉重送並記住（見 `_THINK_OFF_GENERIC`）。
            extras = self._think_off(model, self.is_ollama())
            payload.update(extras)
            optional = list(extras)
        if max_tokens:
            payload["max_tokens"] = max_tokens
        return self._chat_stream(payload, optional, model=model, stop_when=stop_when,
                                 thinking_wanted=think).strip()

    def vision_query(
        self,
        png_bytes,  # bytes OR list[bytes] — single or multiple images
        prompt: str,
        model: str,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        parse_json: bool = True,
        think: bool = False,
        repeat_penalty: float = 1.0,  # > 1.0 抑制重複生成迴圈;OCR 場景建議 1.1-1.2
    ):
        """parse_json=True (預設，向後相容): 回 dict（會強行 JSON parse，
        失敗會 raise LLMError）。
        parse_json=False: 回 raw str 純文字，由 caller 自行處理。
        """
        """Send one-or-more images + a text prompt, expect JSON response.

        ``png_bytes`` may be a single ``bytes`` blob or a ``list[bytes]``;
        when a list, multiple ``image_url`` parts are sent in order so the
        prompt can refer to "first image" vs "second image" (used by the
        review feature to send before / after of the same PDF page).

        Forces ``response_format=json_object`` so the model returns parseable
        JSON. Most modern vision models honour this; if a particular backend
        ignores it the parser strips ```json fences before parsing.
        """
        # Normalize to list
        imgs = png_bytes if isinstance(png_bytes, list) else [png_bytes]
        content: list[dict] = []
        for png in imgs:
            b64 = base64.b64encode(png).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })

        # Model profile：依 model family 套用 thinking 抑制方式
        from .llm_model_profile import get_profile as _get_profile
        profile = _get_profile(model, self.base_url)
        # think=False AND model 真的是 thinking model → 才套用抑制
        # （非 thinking model 加 /no_think marker 是無害但無意義噪音）
        suppress_thinking = (not think) and profile.is_thinking

        text_prompt = prompt
        if suppress_thinking and profile.no_think_marker:
            text_prompt = profile.no_think_marker + "\n\n" + prompt
        content.append({"type": "text", "text": text_prompt})

        # IMPORTANT design notes:
        # 1. We DON'T set response_format=json_object. For many vision models
        #    that constraint makes the model much less thorough. We let it
        #    output prose + extract JSON ourselves.
        # 2. We USE stream=True so the HTTP socket stays active via chunked
        #    SSE events. Non-streaming requests hold the socket silent for
        #    the entire generation, which trips httpx's read_timeout on
        #    anything longer than ~120s (vision reasoning often is).
        # 對 thinking model 加 system 指令再次強調不要 reasoning trace
        messages: list[dict] = []
        if suppress_thinking:
            messages.append({
                "role": "system",
                "content": (
                    "Respond with ONLY the requested output. No reasoning "
                    "traces, no <think> tags, no prefaces, no explanations. "
                    "Output the final answer directly."
                ),
            })
        messages.append({"role": "user", "content": content})
        payload = {
            "model": model,
            "temperature": temperature,
            "stream": True,
            "messages": messages,
        }
        if max_tokens is not None and max_tokens > 0:
            payload["max_tokens"] = int(max_tokens)
        # 防 LLM 重複生成迴圈(qwen2.5vl 等在 temp=0 + OCR 任務常陷入重複)
        # OpenAI-compat: 用 frequency_penalty;Ollama 透過 options.repeat_penalty
        ollama = self.is_ollama()
        if repeat_penalty and repeat_penalty != 1.0:
            # OpenAI frequency_penalty 範圍 -2.0~2.0,大致 (repeat_penalty - 1) * 2
            payload["frequency_penalty"] = max(-2.0, min(2.0, (repeat_penalty - 1.0) * 2.0))
            if ollama:                    # `options` 是 Ollama 專屬（見 `is_ollama`）
                payload.setdefault("options", {})["repeat_penalty"] = float(repeat_penalty)
        # **關閉思考的參數一律送**（v1.16.30），不再看模型名稱 —— 名稱判斷漏掉的模型
        # （或經過閘道改了名字的）思考就整個開著。對方不收的，`_chat_stream` 拿掉重送。
        # * **Ollama 0.33 起 `think:false` 對 gemma4 已經沒有用**（實測：同一句翻譯
        #   還是產生 794~23,650 字的思考內容）；關得掉的是 `reasoning_effort: "none"`
        #   —— 實測 reasoning 0 字、0.3 秒。`low` 沒有用（仍然 1,161 字）。
        # * `options.think` 是 Ollama 專屬、`options` 也裝著 repeat_penalty，所以不列入可拿掉的。
        optional: list = []
        if not think:
            extras = self._think_off(model, ollama)
            if ollama and suppress_thinking:
                payload.setdefault("options", {})["think"] = False
                if profile.use_chat_template_kwargs:
                    extras.setdefault("chat_template_kwargs", {"enable_thinking": False})
            payload.update(extras)
            optional = list(extras)
        parts = [self._chat_stream(payload, optional, model=model, thinking_wanted=think)]
        full_content = "".join(parts)
        # Diagnostic log: how many SSE chunks did we get? Helps catch
        # cases where Ollama opens the stream but never sends any deltas
        # (model crashed / refused content / vision encoding hung).
        import logging as _lg
        _llog = _lg.getLogger("app.llm.client")
        _llog.info(
            "vision_query: model=%s chars=%d reasoning_chars=%d",
            model, len(full_content),
            int((getattr(self, "last_stats", {}) or {}).get("reasoning_chars") or 0),
        )

        # === Fallback A：streaming 0 chunks → 改用 non-streaming OpenAI-compat 重試 ===
        if not full_content.strip():
            _llog.warning("vision_query stream returned empty, retrying non-stream OpenAI-compat...")
            payload_ns = dict(payload)
            payload_ns["stream"] = False
            try:
                with httpx.Client(timeout=self.timeout) as cli:
                    r2 = cli.post(
                        f"{self.base_url}/chat/completions",
                        headers=self._headers(),
                        json=payload_ns,
                    )
                    r2.raise_for_status()
                    data = r2.json()
                    full_content = (
                        data.get("choices", [{}])[0]
                            .get("message", {})
                            .get("content", "")
                    ) or ""
                    _llog.info("vision_query: non-stream fallback chars=%d", len(full_content))
            except Exception as e:
                _llog.warning("vision_query non-stream fallback failed: %s", e)

        # === Fallback B：OpenAI-compat 都拿不到 → 改用 Ollama 原生 /api/chat ===
        # Ollama OpenAI-compat 的 /v1/chat/completions 對 vision input 有時不傳
        # delta（GPU 確實有跑、但 SSE 內容為空）。原生 /api/chat 用 messages[].images
        # 欄位處理影像，多數 vision 模型在這條路上正常。
        if not full_content.strip() and ollama:
            ollama_base = self.base_url.rsplit("/v1", 1)[0]
            _llog.warning("vision_query OpenAI-compat empty, trying Ollama native /api/chat at %s", ollama_base)
            try:
                # 把第一張 png 轉 base64（Ollama 接 list[str]）
                first_png = imgs[0] if isinstance(imgs, list) else imgs
                img_b64 = base64.b64encode(first_png).decode("ascii")
                native_msgs: list[dict] = []
                if suppress_thinking:
                    native_msgs.append({
                        "role": "system",
                        "content": ("Respond with ONLY the requested output. "
                                    "No reasoning traces, no <think> tags."),
                    })
                native_user_prompt = (
                    profile.no_think_marker + "\n\n" + prompt
                    if suppress_thinking and profile.no_think_marker
                    else prompt
                )
                native_msgs.append({
                    "role": "user",
                    "content": native_user_prompt,
                    "images": [img_b64],
                })
                native_payload = {
                    "model": model,
                    "messages": native_msgs,
                    "stream": False,
                    "options": {"temperature": float(temperature)},
                }
                if suppress_thinking:
                    native_payload["think"] = False
                    native_payload["options"]["think"] = False
                if max_tokens is not None and max_tokens > 0:
                    native_payload["options"]["num_predict"] = int(max_tokens)
                if repeat_penalty and repeat_penalty != 1.0:
                    native_payload["options"]["repeat_penalty"] = float(repeat_penalty)
                with httpx.Client(timeout=self.timeout) as cli:
                    r3 = cli.post(f"{ollama_base}/api/chat",
                                  headers={"Content-Type": "application/json"},
                                  json=native_payload)
                    r3.raise_for_status()
                    raw_body = r3.text
                    _llog.info(
                        "vision_query Ollama native: HTTP %d body_bytes=%d body_repr=%r",
                        r3.status_code, len(raw_body), raw_body[:500],
                    )
                    data3 = r3.json()
                    msg = data3.get("message", {})
                    full_content = msg.get("content", "") or ""
                    _llog.info(
                        "vision_query: Ollama native /api/chat content_chars=%d msg_keys=%s done_reason=%s",
                        len(full_content), list(msg.keys()), data3.get("done_reason", "?"),
                    )
            except Exception as e:
                _llog.warning("vision_query Ollama native /api/chat also failed: %s", e)

        if not full_content.strip():
            raise LLMError(
                f"Model '{model}' 對影像 input 沒有回傳任何內容（OpenAI-compat 與 Ollama 原生 /api/chat 都拿不到）。"
                f"請確認該模型真的支援 vision — 跑 `ollama show {model}` 看 capability 欄位。"
                f"若無 vision 能力請改用其他 model（qwen2.5vl:7b、minicpm-v、llava 等）。"
            )
        if not parse_json:
            return full_content
        return _extract_json_from_response(full_content)
