"""事件迴圈延遲監看 + 慢請求記錄。

## 為什麼需要

2026-07-30 在正式機遇到「整站卡住、但 CPU 看起來還有餘裕」的狀況。當時的 access
log 只記了「請求發生了」，**沒有耗時**，所以只能靠人工把時間戳相減才發現輪詢從
每 2 秒變成最長 **226 秒**才回應一次。這種查法既慢又容易錯過。

根因是 **GIL 爭用**：轉檔作業跑在執行緒裡，而 jtdt-layout 那條路有大量**純
Python** 的工作（lxml 操作、座標運算）。兩個這種執行緒各佔滿一顆核心時，同一個
行程裡的 asyncio 事件迴圈就搶不到 GIL —— 總 CPU 使用率看起來還好（其他核心是閒
的），但 HTTP 請求全部排隊。**只看 CPU 使用率永遠診斷不出這件事**，必須直接量
「事件迴圈被餓了多久」。

## 兩個機制

1. **迴圈延遲監看**：每 0.5 秒排一次 `sleep(0.5)`，量實際醒來的時間差。差值就是
   迴圈被卡住的時間；超過門檻寫一筆警告，並記下當時有幾個作業在跑（這樣看 log
   就能把「卡住」與「誰在跑」對起來）。
2. **慢請求記錄**：中介層量每個請求的耗時，超過門檻寫警告。

兩者都只在**超過門檻**時才寫 log —— 每個請求都記耗時會把 log 淹掉，而正常的請求
資訊沒有價值。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger("app.watchdog")

#: 事件迴圈延遲超過這個秒數就記一筆警告。0.5 秒對互動式操作已經是「感覺卡了」。
LAG_WARN_SECONDS = 1.0

#: 請求耗時超過這個秒數就記一筆警告。轉檔是背景作業，所以前景請求本來都該很快。
SLOW_REQUEST_SECONDS = 3.0

_task: Optional[object] = None
_worst_lag = 0.0


def worst_lag() -> float:
    """自啟動以來觀測到的最大迴圈延遲（給管理頁顯示）。"""
    return _worst_lag


async def _monitor() -> None:
    import asyncio
    interval = 0.5
    global _worst_lag
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(interval)
        lag = time.perf_counter() - t0 - interval
        if lag > _worst_lag:
            _worst_lag = lag
        if lag >= LAG_WARN_SECONDS:
            busy = ""
            try:
                from .job_manager import job_manager
                st = job_manager.stats()
                busy = (f"（當時執行中 {st.get('running')} 個作業、"
                        f"排隊 {st.get('queued')} 個）")
            except Exception:  # noqa: BLE001
                pass
            logger.warning(
                "事件迴圈被卡住 %.1f 秒%s —— 網頁在這段時間內不會回應。"
                "常見原因是背景作業在執行緒裡做大量純 Python 運算而搶走 GIL；"
                "調低「最大同時作業數」可緩解。", lag, busy)


def start() -> None:
    """在啟動事件中呼叫（必須已有執行中的 event loop）。"""
    global _task
    import asyncio
    if _task is not None and not getattr(_task, "done", lambda: True)():
        return
    try:
        _task = asyncio.get_running_loop().create_task(_monitor())
        logger.info("事件迴圈延遲監看已啟動（門檻 %.1f 秒）", LAG_WARN_SECONDS)
    except RuntimeError:
        logger.debug("沒有執行中的 event loop，略過延遲監看")
