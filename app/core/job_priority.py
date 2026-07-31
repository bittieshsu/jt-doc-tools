"""優先派送名單 —— 指定的使用者送出的背景作業會排到佇列最前面。

## 為什麼需要

背景作業是先進先出的。平常沒問題，但實務上會有「這份等不了」的情況 ——
高階主管要開會前的簡報、法務要當天送件的文件。前面排著十份年報轉檔時，
他們得等上半小時。

管理員在名單裡指定少數幾位，他們送出的作業就插到佇列最前面，**下一個被派送的
就是他們的**。

## 三條界線

1. **只插隊，不搶跑。** 已經在跑的作業不會被中斷 —— 轉檔跑到一半殺掉只會留下
   半成品，而且原本那個人也白等了。插隊的效果是「下一個換你」，不是「現在就換
   你」。
2. **名單內的人彼此仍照先來後到。** 插到最前面時要插在**已經排在前面的其他優先
   作業之後**，否則後送出的主管會跑到先送出的主管前面 —— 同一群人之間變成後進
   先出，那是壞掉，不是功能。
3. **身分只從伺服器端的作業擁有者判斷。** 不看任何請求參數 —— 讓前端傳
   `priority=1` 就等於開放所有人插隊。

## 記憶體准入仍然優先

插隊只改**順序**，不改「記憶體不夠就排隊」那條鐵則。優先作業一樣要等得到記憶體
才會被派出去；否則一個主管的大檔轉檔就能把機器打爆，其他人連排隊的機會都沒有。

## 認證關閉時等於沒有這個功能

沒有帳號就沒有「誰」可以指定。此時名單一律視為空的，UI 也會說明要先啟用認證。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("app.job_priority")

_LOCK = threading.RLock()
_CACHE: Optional[set[int]] = None

#: 名單人數上限。這是「少數例外」的機制 —— 名單一長就等於沒有優先順序可言，
#: 反而讓一般使用者永遠排在最後。
MAX_USERS = 50


def _path() -> Path:
    from ..config import settings
    return settings.data_dir / "job_priority.json"


def _auth_on() -> bool:
    try:
        from . import auth_settings
        return bool(auth_settings.is_enabled())
    except Exception:  # noqa: BLE001
        return False


def get_user_ids() -> set[int]:
    """名單裡的使用者 id。認證關閉時一律回空集合。"""
    if not _auth_on():
        return set()
    global _CACHE
    with _LOCK:
        if _CACHE is not None:
            return set(_CACHE)
        ids: set[int] = set()
        p = _path()
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                for v in (raw.get("user_ids") or []):
                    try:
                        ids.add(int(v))
                    except (TypeError, ValueError):
                        continue
            except (OSError, ValueError) as e:
                logger.warning("優先派送名單讀取失敗：%s", e.__class__.__name__)
        _CACHE = ids
        return set(ids)


def set_user_ids(ids) -> set[int]:
    """覆寫名單。回傳實際存下來的內容（去重、夾在上限內）。"""
    clean: list[int] = []
    for v in (ids or []):
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0 and n not in clean:
            clean.append(n)
    clean = clean[:MAX_USERS]
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"user_ids": clean}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(p)
    global _CACHE
    with _LOCK:
        _CACHE = set(clean)
    return set(clean)


def invalidate_cache() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None


def is_priority(owner_id: Optional[int]) -> bool:
    """這位使用者的作業要不要插隊。

    只吃作業上記錄的擁有者 id（送出當下由伺服器決定），不吃任何請求參數。
    """
    if owner_id is None:
        return False
    try:
        return int(owner_id) in get_user_ids()
    except (TypeError, ValueError):
        return False
