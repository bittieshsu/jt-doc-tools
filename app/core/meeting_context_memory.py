"""記住「這份逐字稿上一次分析時填的會議背景」（2026-10-03 使用者要求）。

同一場會議常常會分析不只一次（換排法、補背景、改了發言者名字再跑一次）。
每一次分析的背景原本各存在那一次的結果裡，新上傳的逐字稿背景框是空的 ——
使用者以為背景「沒帶回來」。現在上傳時認出「這份逐字稿分析過」，把上一次的背景帶進框裡；
**只是預先填好**，使用者可以直接改、也可以清掉（清掉之後就不再帶入）。

**「同一份逐字稿」怎麼認**：只看說話的內容 —— 發言者名字、時間、斷段方式都不算。
從轉逐字稿送過來的 JSON、存進工作區的純文字、改過名字再存的那一份，內容都一樣；
只比整份檔案的話這幾種一律對不上。拿掉所有空白後取 SHA-256，所以斷行與合併段落不影響。
依會議背景替換過錯字的段落會帶著 `orig_text`，比的是原文（替換是這次分析的選擇，不是另一份逐字稿）。

**每個人各存一份**，鍵是伺服器端認出的使用者（跟上傳歸屬同一套），
前端送不了別人的身分；查詢與清除都只收「這次上傳的編號」，不收指紋 ——
否則任何人送一個指紋就能問出別人有沒有分析過那份逐字稿。
認證關閉時整站是同一個人（本來就沒有帳號的概念）。

刪帳號時一併刪掉（`user_manager.delete`）；每人最多記 200 份，超過丟最舊的。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Optional

from ..config import settings
from . import atomic_json

#: 每個人最多記幾份（超過丟最舊的）
MAX_ENTRIES = 200
#: 背景本身的長度上限 —— 跟送進分析的上限一致（`meeting_insight.MAX_CONTEXT_CHARS`）
MAX_CONTEXT = 4000
#: 內容太短的逐字稿不記：兩三句話的東西撞在一起的機會太高，帶錯背景比沒帶更糟
MIN_CHARS = 20

_LOCK = threading.Lock()
_WS = re.compile(r"\s+")


def _dir() -> Path:
    return settings.data_dir / "meeting_contexts"


def _file(owner: Optional[int]) -> Path:
    # 只用伺服器端認出的整數編號組檔名（認證關閉時是 None）—— 不收任何前端送來的字串
    name = f"u{int(owner)}.json" if owner is not None else "anonymous.json"
    return _dir() / name


def fingerprint(segments: Iterable[Any]) -> Optional[str]:
    """這份逐字稿的指紋（只看說話的內容）。內容太短回 None（不記）。"""
    parts: list[str] = []
    for s in segments or []:
        if not isinstance(s, dict):
            continue
        t = s.get("orig_text") if isinstance(s.get("orig_text"), str) else s.get("text")
        if isinstance(t, str):
            parts.append(t)
    joined = _WS.sub("", unicodedata.normalize("NFKC", "".join(parts)))
    if len(joined) < MIN_CHARS:
        return None
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _load(owner: Optional[int]) -> dict[str, dict]:
    try:
        data = json.loads(_file(owner).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {k: v for k, v in entries.items()
            if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("context"), str)}


def _save(owner: Optional[int], entries: dict[str, dict]) -> None:
    p = _file(owner)
    if not entries:
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    # 0600：裡面是使用者自己寫的背景（與會者姓名、職稱）
    atomic_json.write_json(p, {"v": 1, "entries": entries}, mode=0o600)


def recall(owner: Optional[int], fp: Optional[str]) -> Optional[dict]:
    """上一次這份逐字稿分析時填的背景：`{"context", "saved_at"}`，沒有回 None。"""
    if not fp:
        return None
    with _LOCK:
        e = _load(owner).get(fp)
    if not e or not e.get("context", "").strip():
        return None
    return {"context": e["context"], "saved_at": e.get("saved_at")}


def remember(owner: Optional[int], fp: Optional[str], context: str) -> None:
    """分析送出時記下用了什麼背景。**空的就是清掉**（使用者把預先填好的刪了再分析）。"""
    if not fp:
        return
    ctx = str(context or "")[:MAX_CONTEXT]
    with _LOCK:
        entries = _load(owner)
        if not ctx.strip():
            if entries.pop(fp, None) is None:
                return
        else:
            entries[fp] = {"context": ctx, "saved_at": time.time()}
            if len(entries) > MAX_ENTRIES:
                keep = sorted(entries.items(), key=lambda kv: kv[1].get("saved_at") or 0,
                              reverse=True)[:MAX_ENTRIES]
                entries = dict(keep)
        _save(owner, entries)


def forget(owner: Optional[int], fp: Optional[str]) -> bool:
    """清掉這份逐字稿記住的背景。有清到回 True。"""
    if not fp:
        return False
    with _LOCK:
        entries = _load(owner)
        if entries.pop(fp, None) is None:
            return False
        _save(owner, entries)
    return True


def purge_user(user_id: int) -> None:
    """刪帳號時呼叫 —— 那個人記住的背景（裡面可能有別人的姓名職稱）一起刪。"""
    with _LOCK:
        _file(int(user_id)).unlink(missing_ok=True)
