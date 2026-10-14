"""事務機排隊：同一台實體設備一次只做一件事。

使用者 2026-10-14：「要避免同時多人存取事務機 要排隊機制，以免塞爆事務機作業」。

* **依實體設備排隊**：鍵是設備的位址（DNS 解析、通過位址檢查之後的 IP）。同一台被不同人各自
  加進清單、或同時是共用事務機，都排在同一條隊伍 —— 依清單項目排隊的話，三個人各加一次就是三條隊伍。
* 連線測試、掃描（以後的信箱取件）都要先拿到這台的號碼牌才能連設備。
* **先來先做**；排隊中的可以取消（直接移出隊伍，不會碰到設備）。
* **有上限，超過直接拒絕、不往設備送**：每台最多 `MAX_PER_DEVICE` 件（含正在做的）、每個人在同一台
  最多 `MAX_PER_OWNER` 件。
* 只存在這個服務行程裡（本服務是單一行程）；重啟後隊伍清空，掃描紀錄另外標成「中斷」。
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

#: 每台設備最多幾件（含正在做的那一件）。
MAX_PER_DEVICE = 10
#: 每個人在同一台最多幾件。
MAX_PER_OWNER = 2

MESSAGES: dict[str, str] = {
    "DEVICE_QUEUE_FULL": "這台事務機目前排隊的工作太多，請稍後再試",
    "OWNER_QUEUE_FULL": "你在這台事務機已經有工作在排隊或進行中，請等它完成",
    "DEVICE_BUSY": "這台事務機正在處理其他人的工作，請稍後再試",
}


class QueueError(Exception):
    def __init__(self, code: str, waiting: int = 0):
        super().__init__(code)
        self.code = code
        self.message = MESSAGES.get(code, code)
        self.waiting = waiting


@dataclass(eq=False)          # 用物件本身比對：同一張號碼牌才算同一件
class Ticket:
    id: str
    device: str
    owner: str
    kind: str
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    cancelled: bool = False


_LOCK = threading.Lock()
_COND = threading.Condition(_LOCK)
#: 設備 → 號碼牌（第一張是正在做的或輪到的）。
_QUEUES: dict[str, deque] = {}


def _holds(t: Ticket) -> bool:
    """這張號碼牌還佔著位置嗎：沒取消的，或已經開始做的（取消了也要等做事的那一方真的停下來）。"""
    return not t.cancelled or t.started_at is not None


def _q(device: str) -> deque:
    q = _QUEUES.get(device)
    if q is None:
        q = _QUEUES[device] = deque()
    return q


def enqueue(device: str, owner: str, kind: str) -> Ticket:
    """排進隊伍。超過上限丟 `QueueError`（不排、不碰設備）。"""
    if not device:
        raise ValueError("device key required")
    with _LOCK:
        q = _q(device)
        live = [t for t in q if _holds(t)]
        if len(live) >= MAX_PER_DEVICE:
            raise QueueError("DEVICE_QUEUE_FULL", waiting=len(live))
        if sum(1 for t in live if t.owner == owner) >= MAX_PER_OWNER:
            raise QueueError("OWNER_QUEUE_FULL", waiting=len(live))
        t = Ticket(id=secrets.token_hex(8), device=device, owner=owner, kind=kind)
        q.append(t)
        return t


def position(t: Ticket) -> int:
    """前面還有幾件（0 ＝ 輪到了或正在做）。已經不在隊伍裡回 -1。"""
    with _LOCK:
        q = _QUEUES.get(t.device) or deque()
        n = 0
        for x in q:
            if x is t:
                return n
            if _holds(x):
                n += 1
        return -1


def wait_turn(t: Ticket, timeout: Optional[float] = None,
              on_wait: Optional[Callable[[int], None]] = None,
              poll: float = 1.0) -> bool:
    """等到輪到這張號碼牌（排在最前面）。輪到 → True；逾時或被取消 → 從隊伍移除並回 False。

    `on_wait(前面還有幾件)` 在等待期間大約每 `poll` 秒呼叫一次（拿來更新畫面上的排隊位置）。"""
    deadline = None if timeout is None else time.monotonic() + timeout
    with _COND:
        while True:
            q = _QUEUES.get(t.device) or deque()
            if t.cancelled or not any(x is t for x in q):
                _drop_locked(t)
                return False
            ahead = 0
            for x in q:
                if x is t:
                    break
                if _holds(x):
                    ahead += 1
            if ahead == 0:
                t.started_at = time.time()
                return True
            if on_wait is not None:
                _COND.release()
                try:
                    on_wait(ahead)
                finally:
                    _COND.acquire()
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                _drop_locked(t)
                _COND.notify_all()
                return False
            _COND.wait(poll if left is None else min(poll, left))


def done(t: Ticket) -> None:
    """做完（或放棄）—— 移出隊伍，叫下一位。"""
    with _COND:
        _drop_locked(t)
        _COND.notify_all()


def cancel(t: Ticket) -> None:
    """取消：還在排的直接移出；正在做的只標記（由做事的那一方看到後停下來再 `done`）。"""
    with _COND:
        t.cancelled = True
        q = _QUEUES.get(t.device) or deque()
        if q and q[0] is not t:
            _drop_locked(t)
        _COND.notify_all()


def _drop_locked(t: Ticket) -> None:
    q = _QUEUES.get(t.device)
    if q is None:
        return
    try:
        q.remove(t)
    except ValueError:
        pass
    # 前面那些被取消、還沒輪到的一起清掉，免得擋住後面
    while q and q[0].cancelled and q[0].started_at is None:
        q.popleft()
    if not q:
        _QUEUES.pop(t.device, None)


def snapshot(device: str) -> dict:
    """畫面用：這台有沒有在做事、後面排了幾件。"""
    with _LOCK:
        q = [x for x in (_QUEUES.get(device) or ()) if _holds(x)]
        return {"busy": bool(q), "waiting": max(0, len(q) - 1),
                "kind": q[0].kind if q else None}


@contextmanager
def turn(device: str, owner: str, kind: str, timeout: float) -> Iterator[Ticket]:
    """同步的用法（連線測試）：排隊、最多等 `timeout` 秒，輪到才進 with 區塊，離開時叫下一位。

    等不到丟 `QueueError("DEVICE_BUSY")`。"""
    t = enqueue(device, owner, kind)
    try:
        if not wait_turn(t, timeout=timeout):
            raise QueueError("DEVICE_BUSY", waiting=max(0, snapshot(device)["waiting"]))
        yield t
    finally:
        done(t)


def _reset_for_tests() -> None:
    with _COND:
        _QUEUES.clear()
        _COND.notify_all()
