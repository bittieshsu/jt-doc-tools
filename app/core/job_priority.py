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
2. **名單本身有順序。** 管理員排的順序就是優先順序（第 1 位最優先）—— 沒有這個的
   話「插隊」只有一級，董事長跟部門主管會互相卡。插隊時要插在**排名同等或更前面
   的優先作業之後**：跨排名照排名，同一位使用者的多件作業之間照先來後到，否則後
   送出的那件會跑到自己先送出的那件前面，變成後進先出。
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
#: 名單是**有順序的** —— 排前面的人優先權更高。所以快取存 list 不存 set。
_CACHE: Optional[list[int]] = None

#: 名單人數上限。這是「少數例外」的機制 —— 名單一長就等於沒有優先順序可言，
#: 反而讓一般使用者永遠排在最後（而且順序要一個一個拖，太長根本排不動）。
MAX_USERS = 15


def _path() -> Path:
    from ..config import settings
    return settings.data_dir / "job_priority.json"


def _auth_on() -> bool:
    try:
        from . import auth_settings
        return bool(auth_settings.is_enabled())
    except Exception:  # noqa: BLE001
        return False


def get_ordered() -> list[int]:
    """名單裡的使用者 id，**照優先順序**（排前面的先派送）。

    認證關閉時一律回空清單 —— 沒有帳號就沒有「誰」可以指定。
    """
    if not _auth_on():
        return []
    global _CACHE
    with _LOCK:
        if _CACHE is not None:
            return list(_CACHE)
        ids: list[int] = []
        p = _path()
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                for v in (raw.get("user_ids") or []):
                    try:
                        n = int(v)
                    except (TypeError, ValueError):
                        continue
                    if n not in ids:
                        ids.append(n)
            except (OSError, ValueError) as e:
                logger.warning("優先派送名單讀取失敗：%s", e.__class__.__name__)
        _CACHE = ids
        return list(ids)


def get_user_ids() -> set[int]:
    """名單成員（不含順序）。只在「這個人在不在名單裡」的判斷用。"""
    return set(get_ordered())


def rank_of(owner_id: Optional[int]) -> Optional[int]:
    """這位使用者排第幾（0 起算，數字越小越優先）。不在名單裡回 None。

    順序是管理員排的：同樣是優先使用者，排前面那位的作業要先派。沒有這個的話
    「插隊」只有一級，董事長跟部門主管會互相卡（先送出的那個先跑）。
    """
    if owner_id is None:
        return None
    try:
        n = int(owner_id)
    except (TypeError, ValueError):
        return None
    ordered = get_ordered()
    return ordered.index(n) if n in ordered else None


def set_user_ids(ids) -> list[int]:
    """覆寫名單。**順序就是優先順序**，原樣保留。

    回傳實際存下來的內容（去重、夾在上限內）。
    """
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
        _CACHE = list(clean)
    return list(clean)


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
