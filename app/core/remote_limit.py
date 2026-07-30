"""外部服務（LLM / 遠端 GPU OCR）的同時呼叫上限。

## 為什麼需要獨立的一個上限

「最大同時作業數」管的是**本機**能同時跑幾個作業，判斷依據是本機的 CPU 與記憶體。
但遠端 OCR 與遠端 LLM 的重活**不在本機**：

* 本機那一側只是上傳圖片 / 送 prompt、等回應 —— 記憶體估算（250 MB）大致正確，
  所以准入檢查不會擋。
* 真正的瓶頸在對方機器上。`jt-ocr-server` 是「一張 GPU 載一個模型」；Ollama 也是
  單一模型常駐。同時打八個請求過去，那邊會互相搶顯存或直接排隊到逾時。

也就是說：**排隊實際上發生在對方機器，而且完全不受本站控制**。把「最大同時作業
數」調高會意外變成對外部服務的壓力測試。

所以外部呼叫另外給一個上限，**預設 1** —— 一次只有一個作業在跟外部服務講話，
其餘的在本機這邊等。這對使用者是好事：與其八個請求一起在遠端互相拖慢到全部逾時，
不如一個一個過去、每個都在合理時間內完成。

## 實作

與 `office_convert` 用同一種可調整上限的號誌。**注意這個等待發生在作業的執行緒
裡**（不是在派工階段），因為「要不要呼叫外部服務」是工具執行到一半才知道的事
（例如 OCR 可能先試本機再退回遠端）。因此它佔用的是一個 worker 名額 —— 這是刻意
的取捨：把邏輯放在真正的出口點，比在派工階段猜「這個作業會不會用到外部服務」可靠。
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("app.remote_limit")

_DEFAULT_LIMIT = 1


class _ResizableSemaphore:
    """可在執行期改變上限的號誌（同 office_convert 的做法）。

    不能直接換掉 `threading.Semaphore` 物件 —— 正在等待/持有的執行緒握著舊的那
    一個，換掉之後計數就對不上。自己用 Condition + 計數實作，改上限只是改一個
    數字，正在進行的呼叫不受影響。
    """

    def __init__(self, limit: int = _DEFAULT_LIMIT) -> None:
        self._cond = threading.Condition()
        self._limit = max(1, int(limit))
        self._in_use = 0
        self._waiting = 0
        # 同一個執行緒的巢狀取用深度。**沒有這個會自己卡死自己**：
        # 呼叫端若在外層也包一次 `with slot():`，內層的 `text_query` 再取一次，
        # 而名額只有 1 → 那個執行緒等一個只有它自己能釋放的名額，永遠等下去。
        # 實際踩過（逐句翻譯改成背景作業時），症狀是作業卡在「準備中」，
        # 從外面完全看不出原因。上限的用意是「同時有幾個**請求**打到外部服務」，
        # 同一個執行緒的巢狀呼叫本來就只有一個請求在飛，放行是正確的語意。
        self._depth: dict[int, int] = {}

    def set_limit(self, limit: int) -> int:
        with self._cond:
            self._limit = max(1, int(limit))
            self._cond.notify_all()
            return self._limit

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def in_use(self) -> int:
        return self._in_use

    @property
    def waiting(self) -> int:
        return self._waiting

    def __enter__(self):
        tid = threading.get_ident()
        with self._cond:
            if self._depth.get(tid):        # 這個執行緒已經持有 → 直接放行
                self._depth[tid] += 1
                return self
            self._waiting += 1
            try:
                while self._in_use >= self._limit:
                    self._cond.wait()
            finally:
                self._waiting -= 1
            self._in_use += 1
            self._depth[tid] = 1
        return self

    def __exit__(self, *exc) -> None:
        tid = threading.get_ident()
        with self._cond:
            d = self._depth.get(tid, 0)
            if d > 1:
                self._depth[tid] = d - 1
                return None
            self._depth.pop(tid, None)
            self._in_use -= 1
            self._cond.notify()
        return None


_sem = _ResizableSemaphore(_DEFAULT_LIMIT)


def slot():
    """取得一個外部呼叫名額。用法：`with remote_limit.slot(): ...`"""
    return _sem


def set_limit(n: int) -> int:
    return _sem.set_limit(n)


def stats() -> dict:
    return {"limit": _sem.limit, "in_use": _sem.in_use,
            "waiting": _sem.waiting}
