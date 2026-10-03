"""延後 ACK：存好逐字稿之後，先留一段時間給使用者補專有名詞、只重跑校正。

JTLW 的 `POST /jobs/{id}/retry` 帶新的 `glossary` 只重跑校正（不重新辨識，
`raw` 與發言者逐段相同、`seq` 不變）—— 但 **ACK 之後內容就清掉了**，
retry 會回 409 `content_cleared`。所以要重跑，ACK 就不能存好就送。

**JTLW 的條件（2026-10-02 確認）**：

* 延後**最多 24 小時** —— 那是會議內容，多留一天已經是上限。
* 使用者表示不需要了（按「完成」）就**立刻** ACK。

所以這裡負責三件事：記下「哪一件還沒 ACK、最晚什麼時候要送」、
到期自動送（`start_scheduler`，每 10 分鐘看一次）、以及「立刻送」。

## ⚠ 兩條不可以放鬆的

1. **到期的判斷要提早一個巡檢間隔**：每 10 分鐘看一次的話，剛好在兩次之間到期的
   會晚最多 10 分鐘才送 —— 那就超過 24 小時了。所以「還剩不到一個間隔」就算到期。
2. **管理員把保留時間改短，要對已經在等的那幾件也生效**：到期時間在巡檢當下用
   「完成時間 ＋ 目前的設定」重算、取比較早的那一個。改成 0 ＝ 下一輪全部送出。

**清單存在資料目錄**（不是暫存區）：服務重啟之後還要記得哪幾件沒送 ——
忘掉的話那幾件就一直留在對方那邊，直到對方 7 天後自己清掉。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

from ..config import settings
from ..logging_setup import get_logger
from . import atomic_json

logger = get_logger(__name__)

#: JTLW 的上限。**設定再大也夾在這裡**。
MAX_HOURS = 24
#: 巡檢間隔（秒）。到期判斷會提早這麼久，理由見模組說明第 1 點。
SWEEP_S = 600
#: 對方沒 ACK 的終態作業保留 7 天；過了這麼久還送不出去（例如一直連不上），
#: 對方那邊也已經清掉了 —— 從清單拿掉，不要永遠留著重試。
GIVE_UP_S = 8 * 86400

_LOCK = threading.RLock()
_STOP = threading.Event()
_THREAD: Optional[threading.Thread] = None


def _path() -> Path:
    return Path(settings.data_dir) / "jtlw_pending_ack.json"


def _load() -> dict[str, dict[str, Any]]:
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict[str, dict[str, Any]]) -> None:
    atomic_json.write_json(_path(), d, mode=0o600)


def window_hours() -> float:
    """管理員設定的保留時間，夾在 0 ~ 24 小時。"""
    from . import jtlw_settings
    try:
        h = float(jtlw_settings.get().get("retry_window_hours", MAX_HOURS))
    except (TypeError, ValueError):
        h = float(MAX_HOURS)
    return max(0.0, min(float(MAX_HOURS), h))


def _effective_due(row: dict[str, Any]) -> float:
    """到期時間 = 存下的時間與「完成時間 ＋ 目前設定」取早的那一個（模組說明第 2 點）。"""
    created = float(row.get("created_at") or 0)
    stored = float(row.get("due_at") or 0)
    now_cap = created + window_hours() * 3600
    return min(stored, now_cap) if stored else now_cap


def defer(remote_id: str, upload_id: str, *, now: Optional[float] = None) -> Optional[float]:
    """記下這一件先不 ACK，回傳最晚送出的時間。保留時間是 0 時回 None（呼叫端要立刻送）。"""
    hours = window_hours()
    if hours <= 0 or not remote_id:
        return None
    t = time.time() if now is None else now
    due = t + hours * 3600
    with _LOCK:
        d = _load()
        d[remote_id] = {"upload_id": upload_id, "created_at": t, "due_at": due}
        _save(d)
    return due


def due_at(remote_id: str) -> Optional[float]:
    """還在等的話回到期時間；已經送了（或從來沒延後過）回 None。"""
    with _LOCK:
        row = _load().get(remote_id)
    return _effective_due(row) if row else None


def forget(remote_id: str) -> None:
    """從清單拿掉（對方說內容已經清掉了，例如 retry 回 `content_cleared`）。"""
    with _LOCK:
        d = _load()
        if d.pop(remote_id, None) is not None:
            _save(d)


def ack_now(remote_id: str, client=None) -> bool:
    """立刻送 ACK。送成功（或對方說那件作業已經不在了）就從清單拿掉，回 True。

    送不出去（連不上、對方說還在處理）回 False、**留在清單裡**，下一輪巡檢再試 ——
    並把到期時間改成現在，不要因為這次失敗又多等一輪。
    """
    from . import jtlw_client
    try:
        cli = client or jtlw_client.JtlwClient()
        cli.ack(remote_id)
    except jtlw_client.JtlwError as exc:
        if exc.status == 404 or exc.code == "not_found":
            forget(remote_id)
            return True
        logger.warning("ACK JTLW 作業 %s 失敗，下一輪再試：%s", remote_id, exc)
        with _LOCK:
            d = _load()
            if remote_id in d:
                d[remote_id]["due_at"] = time.time()
                _save(d)
        return False
    forget(remote_id)
    return True


def sweep(*, now: Optional[float] = None, client=None) -> int:
    """送出到期（或再過一個巡檢間隔就到期）的 ACK，回傳送出幾件。

    正在重跑校正的那一件對方會以「還在處理」退回（409）—— `ack_now` 留著它，
    下一輪再送；重跑做完時那一支自己也會看要不要立刻送。
    """
    t = time.time() if now is None else now
    with _LOCK:
        rows = dict(_load())
    sent = 0
    for rid, row in rows.items():
        created = row.get("created_at")
        if t - (float(created) if created is not None else t) > GIVE_UP_S:
            logger.warning("JTLW 作業 %s 一直送不出 ACK，對方應該已經自己清掉了，不再重試", rid)
            forget(rid)
            continue
        if _effective_due(row) - t <= SWEEP_S:
            if ack_now(rid, client):
                sent += 1
    return sent


def _loop() -> None:
    if _STOP.wait(60):                     # 開機時不要跟其他東西搶
        return
    while not _STOP.is_set():
        try:
            from . import jtlw_settings
            if _load() and jtlw_settings.is_configured():
                n = sweep()
                if n:
                    logger.info("延後的 JTLW ACK：送出 %d 件", n)
        except Exception:  # noqa: BLE001
            logger.exception("延後的 JTLW ACK 巡檢失敗")
        if _STOP.wait(SWEEP_S):
            return


def start_scheduler() -> None:
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, name="jtlw-ack", daemon=True)
        _THREAD.start()


def stop_scheduler() -> None:
    _STOP.set()
    if _THREAD is not None:
        _THREAD.join(timeout=5)
