"""知識庫的 embedding（向量）設定與用戶端。

## 跟「LLM 設定」分開

規格 8.2：embedding 可以是另一台伺服器、另一個模型；換生成模型**不用**重建索引，
換 embedding 模型才要。兩邊共用一份設定的話，管理員改個聊天模型就會把向量索引
弄成「跟查詢對不上」而且看不出來。所以這裡有自己的設定檔
`data/knowledge_settings.json`（原子寫入、金鑰加密，照 `llm_settings` 的
`api_key_enc` / `__KEPT__` 做法）。

## 兩種 API

* **Ollama 原生**：`POST /api/embed`，送 `"truncate": false` ——
  Ollama 預設會**安靜地截掉**超過模型長度的輸入，截掉的那段永遠查不到，
  而畫面看起來一切正常（規格：「逐段檢查不能靜默截斷」）。寧可整份失敗、講出原因。
* **OpenAI 相容**：`POST /v1/embeddings`（vLLM、LiteLLM、llama.cpp server、
  LM Studio、Ollama 的相容端點都是這個路徑）。

位址只取 `scheme://主機:埠`（`url_safety.safe_remote_base_url`，擋雲端中繼資料
位址），路徑由這裡接固定的 —— 管理員填的路徑不會被拿去組請求（SSRF 的慣例）。

## 預設沿用 LLM 伺服器（2026-10-08 使用者：「預設 繼承上面有設的 llm server設定」）

`use_llm_server`（預設開）：位址與金鑰**不另外存**，每次照「LLM 設定」那一份取 ——
而且取的是**公文撰擬用的那一台**（`server_per_tool["official-doc"]`，沒指定就是全站那台）。
管理員把公文撰擬指到機關自己的伺服器，多半是因為資料只能送去那一台；embedding 送的是
同一批公文資料，不可以安靜地送去全站那台。LLM 設定改了位址，這裡跟著改，不必再填一次。
API 種類在存檔時問一次對方是不是 Ollama（`LLMClient.is_ollama()`）記下來 —— 不每次問，
不然對方暫時連不上會讓種類跳來跳去、索引被判成要重建。

舊設定檔沒有這個欄位：已經填了位址的 → 當成「另外指定」（不改掉管理員填好的）；
沒填的 → 沿用。

## 前綴：管理員不設，照模型自動套用（2026-10-09 使用者：「換nemotron，參數或前綴請調整好」）

2026-10-08 使用者說過不要用前綴：「查詢前綴 / 文件前綴」欄位與「套用建議前綴」拿掉了，舊設定檔裡的
前綴讀進來就丟掉。少數模型是**照固定前綴訓練的**（官方模型卡寫明），不加的話檢索明顯變差 ——
那幾族由程式照官方寫法自動加（`MODEL_PROFILES`），管理員不必也不能自己填，畫面講出用了什麼。
其他模型照舊原樣送，上層標題接在本文前面。

## index fingerprint

`模型 ＋ API 種類 ＋ 自動套用的前綴 ＋ 向量維度 ＋ 切段版本` 的雜湊（沒有前綴的模型那兩格是空字串，
跟拿掉前綴設定之前建的索引一樣）。**只有 fingerprint
相同的向量可以互相比較**。伺服器位址、金鑰、批次大小、逾時不算在內 —— 換一台跑
同一個模型的伺服器不需要重建。
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .. import atomic_json, secret_box
from ...config import settings
from ...logging_setup import get_logger
from .chunker import CHUNKER_VERSION

logger = get_logger(__name__)

#: 管理頁看到的金鑰欄位：已設定就是這個字串，不回傳金鑰本身（同 LLM 設定）。
SECRET_KEPT = "__KEPT__"

KINDS = ("ollama", "openai")

DEFAULT_EMBED: dict = {
    "kind": "ollama",
    "base_url": "",
    "api_key_enc": "",
    "model": "",
    #: 位址與金鑰沿用「LLM 設定」裡公文撰擬用的那台（見模組說明）。
    "use_llm_server": True,
    #: 一次送幾段。Ollama 一次收一批，太大會讓單次請求太久而逾時。
    "batch_size": 16,
    "timeout_seconds": 120,
}

BATCH_RANGE = (1, 128)
TIMEOUT_RANGE = (5, 1800)


# ---------------------------------------------------------------- 依模型自動套用的用法
#: 照固定前綴訓練的嵌入模型（官方模型卡寫明要加）。**只收量過的家族**，不推廣：
#:
#: * NVIDIA Nemotron-3-Embed（`nvidia/Nemotron-3-Embed-8B-BF16` 模型卡與它的
#:   `config_sentence_transformers.json`：查詢 `query: `、文件 `passage: `）。2026-10-09 拿實際部署的
#:   公文知識庫實測（42 題、1,503 段）：8B 不加前綴 R@6 0.705、加了 0.881（長篇查詢 0.615 → 0.800）。
#:   同一套前綴套在**上一代** llama-nemotron-embed-1b-v2 上反而略差（0.746 → 0.727），所以比對的是
#:   「Nemotron-3-Embed」這幾個字，不是「nemotron」。
#:
#: `num_ctx`：Ollama 載入這個模型時的上下文長度（只對 Ollama 原生 API 送，OpenAI 相容的端點不理它）。
#: 不送的話照伺服器的預設 —— 推論機設成 131072 時 Nemotron-3-Embed-8B 一載入就佔 28.3 GB，
#: 8192 只要 10.7 GB，嵌出來的向量一樣。一段最多 800 字、查詢最多 500 字，遠在 8192 tokens 以內；
#: 真的超過時 `truncate: false` 會明講失敗，不會安靜截掉。
MODEL_PROFILES: tuple = (
    {"family": "nemotron3embed", "label": "NVIDIA Nemotron-3-Embed",
     "query_prefix": "query: ", "document_prefix": "passage: ", "num_ctx": 8192},
)

_NO_PROFILE = {"family": "", "label": "", "query_prefix": "", "document_prefix": "", "num_ctx": 0}


def _family_key(model: str) -> str:
    """模型名稱只留英數字、轉小寫：`hf.co/Abiray/Nemotron-3-Embed-8B-GGUF:Q8_0` →
    `hfcoabiraynemotron3embed8bggufq80`（各家上傳者的命名不同，比對家族名稱就好）。"""
    return re.sub(r"[^a-z0-9]", "", str(model or "").lower())


def model_profile(model: str) -> dict:
    """這個模型要自動套用的前綴與載入參數；不在表上的回空的（原樣送、照伺服器預設載入）。"""
    key = _family_key(model)
    for p in MODEL_PROFILES:
        if p["family"] in key:
            return dict(p)
    return dict(_NO_PROFILE)


def usage_public(model: str) -> Optional[dict]:
    """給畫面講「這個模型會自動加什麼」；不在表上的回 None。"""
    p = model_profile(model)
    if not p["family"]:
        return None
    return {k: p[k] for k in ("label", "query_prefix", "document_prefix", "num_ctx")}


class EmbedError(RuntimeError):
    """嵌入服務連不上 / 回了錯的東西。訊息給管理員看（不含金鑰）。"""


# ---------------------------------------------------------------- 設定檔
def settings_path() -> Path:
    return settings.data_dir / "knowledge_settings.json"


_LOCK = threading.RLock()


def encrypt_secret(plaintext: str) -> str:
    """設定匯出 / 匯入換金鑰時用（`settings_export._rekey_specs`）。"""
    return secret_box.encrypt(plaintext)


def decrypt_secret(ciphertext: str) -> str:
    return secret_box.decrypt(ciphertext, label="knowledge_settings.api_key_enc")


def _read() -> dict:
    """讀檔（**內部用，含密文**）。"""
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    emb = copy.deepcopy(DEFAULT_EMBED)
    if isinstance(data.get("embed"), dict):
        raw = data["embed"]
        emb.update({k: v for k, v in raw.items() if k in DEFAULT_EMBED})
        if "use_llm_server" not in raw:
            # 舊設定檔：已經填了位址的是管理員另外指定的，不改掉
            emb["use_llm_server"] = not str(raw.get("base_url") or "").strip()
    emb["use_llm_server"] = bool(emb.get("use_llm_server"))
    active = data.get("active") if isinstance(data.get("active"), dict) else None
    return {"embed": emb, "active": active, "updated_at": data.get("updated_at")}


def _write(data: dict) -> None:
    data = dict(data)
    data["updated_at"] = time.time()
    # 0600：裡面有密文
    atomic_json.write_json(settings_path(), data, mode=0o600)


def _public_cfg(cfg: dict) -> dict:
    """金鑰只說有沒有。**記憶體裡解密後的金鑰欄位叫 `secret`，不叫 `api_key`** ——
    全站有一支檢查擋「從設定 dict 讀 `api_key`」（那個值是替身字串 `__KEPT__`，
    讀了就會送出 `Bearer __KEPT__`），名字錯開就不會被誤用。"""
    rest = {k: v for k, v in cfg.items() if k not in ("api_key_enc", "secret")}
    return {**rest, "api_key": SECRET_KEPT if cfg.get("api_key_enc") else ""}


def get_public() -> dict:
    """給管理頁看的設定（金鑰只說有沒有）。"""
    with _LOCK:
        d = _read()
    act = d["active"]
    src = llm_source()
    return {
        "embed": _public_cfg(d["embed"]),
        "active": _public_cfg(act) if act else None,
        "configured": is_complete(d["embed"]),
        "needs_rebuild": needs_rebuild(d),
        "chunker_version": CHUNKER_VERSION,
        # 已儲存的模型會自動加什麼前綴（不在表上的是 None）
        "usage": usage_public(d["embed"].get("model") or ""),
        # 沿用的那台（畫面講出會送去哪裡）；金鑰只說有沒有
        "llm_server": ({"base_url": src["base_url"], "name": src["name"],
                        "pinned": src["pinned"], "has_key": bool(src["secret"]),
                        "problem": src["problem"]} if src else None),
    }


#: 公文撰擬的工具代碼：知識庫目前只給它查（`server_per_tool` 用這個鍵）。
LLM_TOOL_ID = "official-doc"


def llm_source() -> Optional[dict]:
    """「LLM 設定」裡公文撰擬用的那台：`{base_url, secret, name, pinned, problem}`。

    * 公文撰擬指定了另一台 → 那一台（`pinned=True`）；那台不見了 → `base_url` 空、
      `problem` 寫原因 —— **不退回全站那台**（同 `LLMServerUnavailable` 的理由）。
    * 沒指定 → 全站那台。讀不到設定回 None。
    """
    try:
        from ..llm_settings import llm_settings
        sid = llm_settings.server_for(LLM_TOOL_ID)
        if sid:
            srv = llm_settings.server(sid)
            if not srv:
                return {"base_url": "", "secret": "", "name": "", "pinned": True,
                        "problem": "公文撰擬指定的 LLM 伺服器已不存在，請到上面重新指定。"}
            return {"base_url": srv.get("base_url") or "", "secret": llm_settings.server_api_key(sid) or "",
                    "name": srv.get("name") or "", "pinned": True, "problem": ""}
        s = llm_settings.get()
        return {"base_url": s.get("base_url") or "", "secret": llm_settings.api_key() or "",
                "name": "", "pinned": False, "problem": ""}
    except Exception:      # noqa: BLE001 — 讀不到 LLM 設定時這一區照樣打得開
        logger.warning("讀不到 LLM 設定（embedding 沿用 LLM 伺服器）", exc_info=True)
        return None


def resolved(cfg: dict) -> dict:
    """沿用 LLM 伺服器時把位址與金鑰換成那一台的（`secret` 是解密後的金鑰，只在記憶體裡用）。

    不沿用的話原樣回傳（`secret` 由呼叫端放）。
    """
    out = dict(cfg)
    if out.get("use_llm_server"):
        src = llm_source() or {}
        out["base_url"] = src.get("base_url") or ""
        out["secret"] = src.get("secret") or ""
    return out


def detect_llm_kind() -> Optional[str]:
    """沿用的那台是 Ollama 還是 OpenAI 相容（存檔、測試連線時問一次）。**問不到回 None**。

    不借用 `LLMClient.is_ollama()`：它把「連不上」也當成「不是 Ollama」（那邊的最壞情況只是
    模型多想一會兒），這裡卻會因此把種類改成 OpenAI 相容、讓索引被判成要重建。
    所以自己問：`/api/version` 回版本＝Ollama；**有回應**但不是＝OpenAI 相容；連不上＝不知道。
    """
    src = llm_source()
    if not src or not src.get("base_url"):
        return None
    try:
        import httpx
        from ..llm_client import LLMClient
        cli = LLMClient(base_url=src["base_url"], api_key=src.get("secret") or None, timeout=5)
        r = httpx.get(cli._native_root() + "/api/version", headers=cli._headers(), timeout=5.0)
    except Exception:      # noqa: BLE001 — 位址不合格、連不上、逾時：都是「不知道」
        return None
    try:
        d = r.json() if r.status_code == 200 else None
    except ValueError:
        d = None
    if isinstance(d, dict) and d.get("version"):
        return "ollama"
    return None if r.status_code >= 500 else "openai"


def is_complete(cfg: dict) -> bool:
    if not (cfg.get("model") or "").strip():
        return False
    if cfg.get("use_llm_server"):
        return bool((resolved(cfg).get("base_url") or "").strip())
    return bool((cfg.get("base_url") or "").strip())


def _clamp(v, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def validate_base_url(url: str) -> str:
    """只收 http(s)、擋雲端中繼資料位址；回 `scheme://主機:埠`（路徑一律丟掉）。"""
    from ..url_safety import safe_remote_base_url
    return safe_remote_base_url(url)


def save(changes: dict) -> dict:
    """存 embedding 設定。回公開版本。

    基本內容（模型、API 種類）跟目前使用中的索引一樣時，連線資訊
    （位址、金鑰、批次、逾時）**一起套用到使用中的那一份** —— 換伺服器不需要重建。
    不一樣的話使用中的索引照舊，畫面提示要重建。
    """
    changes = dict(changes or {})
    with _LOCK:
        d = _read()
        cfg = d["embed"]
        if "kind" in changes:
            if changes["kind"] not in KINDS:
                raise ValueError("API 種類不對。")
            cfg["kind"] = changes["kind"]
        if "base_url" in changes:
            raw = (changes.get("base_url") or "").strip()
            if raw:
                validate_base_url(raw)       # 不合格就丟 ValueError；存原字串給畫面看
            cfg["base_url"] = raw
        if "model" in changes:
            m = (changes.get("model") or "").strip()
            if len(m) > 200 or any(ord(ch) < 32 for ch in m):
                raise ValueError("模型名稱格式不對。")
            cfg["model"] = m
        if "use_llm_server" in changes:
            cfg["use_llm_server"] = bool(changes["use_llm_server"])
        elif (changes.get("base_url") or "").strip():
            # 呼叫端（API、舊的腳本）只給了位址、沒講要不要沿用：給位址就是要另外指定那一台
            cfg["use_llm_server"] = False
        if "batch_size" in changes:
            cfg["batch_size"] = _clamp(changes["batch_size"], *BATCH_RANGE, DEFAULT_EMBED["batch_size"])
        if "timeout_seconds" in changes:
            cfg["timeout_seconds"] = _clamp(changes["timeout_seconds"], *TIMEOUT_RANGE,
                                            DEFAULT_EMBED["timeout_seconds"])
        if "key_input" in changes:
            # 管理頁送來的金鑰欄位（路由層把 body 的 api_key 改名成 key_input 再交進來）
            val = changes.get("key_input")
            if val == SECRET_KEPT:
                pass
            elif isinstance(val, str) and val.strip():
                key = val.strip()
                if key.lower().startswith("bearer "):
                    key = key[7:].strip()    # 照著對方文件連「Bearer 」一起貼進來（LLM 設定踩過）
                cfg["api_key_enc"] = encrypt_secret(key)
            else:
                cfg["api_key_enc"] = ""
        act = d["active"]
        if act and basis(act) == basis(cfg):
            for k in ("base_url", "api_key_enc", "batch_size", "timeout_seconds", "use_llm_server"):
                act[k] = cfg[k]
        _write({"embed": cfg, "active": act})
    return get_public()


# ---------------------------------------------------------------- fingerprint
def basis(cfg: dict) -> str:
    """fingerprint 不含維度的那一部分（維度要真的嵌入一次才知道）。

    前綴只看**模型自動套用的那一套**（`model_profile`）—— 設定檔或快照裡留著的舊前綴不算。
    沒有前綴的模型兩格是空字串，跟拿掉前綴設定之前建的索引 fingerprint 一樣、不必重建。
    `num_ctx` 不算在內（不改變向量；一段放不下時會明講失敗）。"""
    model = (cfg.get("model") or "").strip()
    prof = model_profile(model)
    return json.dumps({"kind": cfg.get("kind"), "model": model,
                       "qp": prof["query_prefix"], "dp": prof["document_prefix"],
                       "chunker": CHUNKER_VERSION}, ensure_ascii=False, sort_keys=True)


def fingerprint(cfg: dict, dim: int) -> str:
    raw = basis(cfg) + f"|dim={int(dim)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def needs_rebuild(d: Optional[dict] = None) -> bool:
    """設定好了、但使用中的索引不是照這份設定建的（或還沒有索引）。"""
    d = d or _read()
    cfg, act = d["embed"], d["active"]
    if not is_complete(cfg):
        return False
    if not act:
        return True
    return basis(act) != basis(cfg) or act.get("fingerprint") != fingerprint(act, act.get("dim", 0))


def active_snapshot() -> Optional[dict]:
    """使用中的索引是用哪一份設定建的（含解密後的金鑰；**只在記憶體裡用**）。"""
    with _LOCK:
        act = _read()["active"]
    if not act or not act.get("fingerprint"):
        return None
    snap = dict(act)
    snap["secret"] = decrypt_secret(act.get("api_key_enc", ""))
    return resolved(snap)


def configured_snapshot() -> Optional[dict]:
    with _LOCK:
        cfg = _read()["embed"]
    if not is_complete(cfg):
        return None
    snap = dict(cfg)
    snap["secret"] = decrypt_secret(cfg.get("api_key_enc", ""))
    return resolved(snap)


def set_active(snapshot: dict, *, dim: int, fp: str, chunks: int) -> None:
    """重建成功之後切換使用中的索引（原子寫入，一次換掉）。"""
    with _LOCK:
        d = _read()
        act = {k: snapshot.get(k, DEFAULT_EMBED.get(k)) for k in DEFAULT_EMBED}
        act.update(dim=int(dim), fingerprint=fp, built_at=time.time(), chunks=int(chunks))
        _write({"embed": d["embed"], "active": act})


def clear_active() -> None:
    """停用向量檢索（只剩關鍵字）。"""
    with _LOCK:
        d = _read()
        _write({"embed": d["embed"], "active": None})


# ---------------------------------------------------------------- 用戶端
@dataclass
class EmbedClient:
    kind: str
    base: str                 # scheme://host:port（已驗證）
    model: str
    api_key: str = ""
    batch_size: int = 16
    timeout: float = 120.0
    #: 照模型自動套用（`model_profile`），不從設定檔讀
    query_prefix: str = ""
    document_prefix: str = ""
    num_ctx: int = 0

    @classmethod
    def from_snapshot(cls, snap: dict) -> "EmbedClient":
        if snap.get("kind") not in KINDS:
            raise EmbedError("嵌入服務的 API 種類設定不對。")
        try:
            base = validate_base_url(snap.get("base_url") or "")
        except ValueError as e:
            raise EmbedError("嵌入服務的位址不合格（只收 http / https，不可以是雲端中繼資料位址）。") from e
        model = (snap.get("model") or "").strip()
        prof = model_profile(model)
        return cls(kind=snap["kind"], base=base, model=model,
                   api_key=snap.get("secret") or "",
                   batch_size=_clamp(snap.get("batch_size"), *BATCH_RANGE, 16),
                   timeout=float(_clamp(snap.get("timeout_seconds"), *TIMEOUT_RANGE, 120)),
                   query_prefix=prof["query_prefix"], document_prefix=prof["document_prefix"],
                   num_ctx=int(prof["num_ctx"] or 0))

    # ---- 文字前處理
    def query_text(self, q: str) -> str:
        return self.query_prefix + q

    def document_text(self, text: str, title: str = "") -> str:
        """上層標題接在本文前面一起嵌入；照模型自動套用的文件前綴放在最前面（見模組說明）。"""
        return self.document_prefix + ((title.strip() + "\n") if title and title.strip() else "") + text

    # ---- 呼叫
    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _post(self, texts: list[str]) -> list[list[float]]:
        import httpx
        if self.kind == "ollama":
            url = self.base + "/api/embed"
            payload = {"model": self.model, "input": texts, "truncate": False}
            if self.num_ctx:
                payload["options"] = {"num_ctx": self.num_ctx}
        else:
            url = self.base + "/v1/embeddings"
            payload = {"model": self.model, "input": texts}
        try:
            r = httpx.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        except httpx.TimeoutException as e:
            raise EmbedError("嵌入服務在時限內沒有回應（可以調高「逾時」或調小「批次大小」）。") from e
        except httpx.HTTPError as e:
            raise EmbedError("連不上嵌入服務，請確認位址與網路。") from e
        if r.status_code >= 400:
            detail = _error_detail(r)
            logger.warning("embedding 服務回 HTTP %s：%s", r.status_code, detail[:300])
            if r.status_code in (401, 403):
                raise EmbedError("嵌入服務拒絕了請求（金鑰錯誤或沒有權限）。")
            if r.status_code == 404:
                raise EmbedError("嵌入服務找不到這個模型（或這個 API 路徑）；請確認模型名稱與 API 種類。")
            low = detail.lower()
            if "context" in low or "too long" in low or "exceed" in low or "length" in low:
                raise EmbedError("有一段文字超過這個模型能處理的長度。這個模型的上下文太短，"
                                 "請換一個上下文較長的模型（不會為了配合而安靜截掉內容）。")
            raise EmbedError(f"嵌入服務回報錯誤（HTTP {r.status_code}）。")
        try:
            data = r.json()
        except ValueError as e:
            raise EmbedError("嵌入服務回的不是 JSON。") from e
        if self.kind == "ollama":
            vecs = data.get("embeddings")
        else:
            items = data.get("data")
            vecs = None
            if isinstance(items, list):
                try:
                    vecs = [it["embedding"] for it in sorted(items, key=lambda x: x.get("index", 0))]
                except (KeyError, TypeError):
                    vecs = None
        if not isinstance(vecs, list) or len(vecs) != len(texts):
            raise EmbedError("嵌入服務回的向量數量跟送出的段數對不上。")
        return vecs

    def embed(self, texts: list[str]) -> "np.ndarray":
        """一批文字 → (n, 維度) 的 float32 矩陣，每一列都正規化成單位長度。"""
        import numpy as np
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self._post(texts[i:i + self.batch_size]))
        if not out:
            return np.zeros((0, 0), dtype=np.float32)
        try:
            m = np.asarray(out, dtype=np.float32)
        except (ValueError, TypeError) as e:
            raise EmbedError("嵌入服務回的向量格式不對（維度不一致）。") from e
        if m.ndim != 2 or m.shape[1] == 0:
            raise EmbedError("嵌入服務回的向量格式不對。")
        if not np.all(np.isfinite(m)):
            raise EmbedError("嵌入服務回的向量裡有無效的數值。")
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        if np.any(norms < 1e-12):
            raise EmbedError("嵌入服務回了全是 0 的向量。")
        return m / norms

    def embed_query(self, q: str) -> "np.ndarray":
        return self.embed([self.query_text(q)])[0]


def _error_detail(r) -> str:
    try:
        j = r.json()
        if isinstance(j, dict):
            e = j.get("error")
            if isinstance(e, dict):
                return str(e.get("message") or e)
            return str(e or j.get("detail") or j)
        return str(j)
    except ValueError:
        return (r.text or "")[:500]


def probe(snap: dict, sample: str = "公文處理手冊的函稿撰擬原則") -> dict:
    """「測試連線」：真的嵌入一句（以查詢與文件各一次），回維度與耗時。

    `snap` 的金鑰欄位是 `secret`（解密後的明文，只在記憶體裡）。"""
    cli = EmbedClient.from_snapshot(snap)
    t0 = time.monotonic()
    m = cli.embed([cli.query_text(sample), cli.document_text(sample, "")])
    ms = (time.monotonic() - t0) * 1000
    cos = float((m[0] * m[1]).sum())
    return {"ok": True, "dim": int(m.shape[1]), "ms": round(ms, 1),
            "self_similarity": round(cos, 4) if not math.isnan(cos) else None,
            "model": cli.model, "kind": cli.kind, "usage": usage_public(cli.model)}


# ---------------------------------------------------------------- 伺服器上有哪些嵌入模型
#: 名稱看起來像嵌入模型（對方沒說模型能做什麼時才用：OpenAI 相容的 `/v1/models` 只給名稱）
_EMBED_NAME_HINTS = ("embed", "bge-", "bge_", "e5-", "gte-", "minilm", "mpnet", "nomic", "arctic", "mxbai")


def _looks_embedding(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in _EMBED_NAME_HINTS)


def list_models(snap: dict) -> dict:
    """「嵌入模型」下拉的清單（2026-10-08 使用者：「應該是列出 llm server 上符合的 model 不是自己填」）。

    回 `{"ok", "models": [{"id", "embedding", "dim", "ctx", "size"}], "capability_known"}`：

    * Ollama（`/api/tags`）：0.6 起每個模型帶 `capabilities`，有 `embedding` 的才是嵌入模型 ——
      **只列那幾個**（`capability_known=True`）；還附維度與上下文長度。
    * OpenAI 相容（`/v1/models`）或舊版 Ollama：對方沒說能做什麼 → 全部列出，`embedding` 是
      照名稱猜的（畫面分成兩組，選了可以按「測試連線」確認）。
    * 只讀清單（`/api/tags` 不會讓對方載入模型）；位址照 `validate_base_url` 擋。
    """
    import httpx
    if snap.get("kind") not in KINDS:
        raise EmbedError("嵌入服務的 API 種類設定不對。")
    try:
        base = validate_base_url(snap.get("base_url") or "")
    except ValueError as e:
        raise EmbedError("嵌入服務的位址不合格（只收 http / https，不可以是雲端中繼資料位址）。") from e
    headers = {"Accept": "application/json"}
    if snap.get("secret"):
        headers["Authorization"] = f"Bearer {snap['secret']}"
    url = base + ("/api/tags" if snap["kind"] == "ollama" else "/v1/models")
    try:
        r = httpx.get(url, headers=headers, timeout=10.0)
    except httpx.TimeoutException as e:
        raise EmbedError("伺服器在時限內沒有回應模型清單。") from e
    except httpx.HTTPError as e:
        raise EmbedError("連不上伺服器，請確認位址與網路。") from e
    if r.status_code in (401, 403):
        raise EmbedError("伺服器拒絕列出模型（金鑰錯誤或沒有權限）。")
    if r.status_code >= 400:
        logger.warning("列出模型時伺服器回 HTTP %s：%s", r.status_code, _error_detail(r)[:300])
        raise EmbedError("伺服器沒有回模型清單（API 種類可能不對）。")
    try:
        data = r.json()
    except ValueError as e:
        raise EmbedError("伺服器回的模型清單看不懂（API 種類可能不對）。") from e
    out: list[dict] = []
    known = False
    if snap["kind"] == "ollama":
        for m in (data.get("models") if isinstance(data, dict) else None) or []:
            if not isinstance(m, dict) or not m.get("name"):
                continue
            caps = m.get("capabilities")
            det = m.get("details") if isinstance(m.get("details"), dict) else {}
            if isinstance(caps, list):
                known = True
                emb: Optional[bool] = "embedding" in caps
            else:
                emb = None
            out.append({"id": str(m["name"])[:200], "embedding": emb,
                        "dim": det.get("embedding_length") if isinstance(det.get("embedding_length"), int) else None,
                        "ctx": det.get("context_length") if isinstance(det.get("context_length"), int) else None,
                        "size": m.get("size") if isinstance(m.get("size"), int) else 0})
    else:
        for m in (data.get("data") if isinstance(data, dict) else None) or []:
            if isinstance(m, dict) and m.get("id"):
                out.append({"id": str(m["id"])[:200], "embedding": None, "dim": None, "ctx": None, "size": 0})
    if known:
        out = [m for m in out if m["embedding"]]
    else:
        for m in out:
            m["embedding"] = _looks_embedding(m["id"])
    out.sort(key=lambda m: (not m["embedding"], m["id"].lower()))
    for m in out:
        m["usage"] = usage_public(m["id"])       # 選了這個模型會自動加什麼（畫面講出來）
    return {"ok": True, "models": out, "capability_known": known, "kind": snap["kind"]}
