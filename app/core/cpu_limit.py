"""CPU 限制 —— 讓「轉檔在忙」永遠不會拖垮網頁回應。

## 這個模組要解決的問題

2026-07-30 正式機（6 核，本身閒置 load 就有 7）遇到整站空轉：CPU 使用率看起來
沒滿，但網頁輪詢從每 2 秒變成最長 **226 秒**才回應一次。原因有兩層，兩層都要處理：

| 層 | 誰在吃 CPU | 對網頁的影響 | 對策 |
|---|---|---|---|
| 子行程 | soffice（轉檔主力） | 搶走 OS 排程時間 | 降優先權 + **限制可用核心** |
| 同行程 | 作業執行緒裡的純 Python 運算 | 握著 GIL，事件迴圈搶不到 | 降執行緒優先權 + 縮短 GIL 切換間隔 |

**原則：網頁回應永遠優先。** 轉檔慢幾秒沒有人在意，網頁卡十秒沒有人能忍。所以
預設就會**保留至少一顆核心不讓 soffice 用**，不需要管理員自己去發現要調。

## 為什麼用「限制核心數」而不是 cgroup 配額

cgroup v2 的 `cpu.max` 才是真正的百分比配額，但 `/sys/fs/cgroup` 只有 root 能寫，
而本服務是以非 root 的 `jtdt` 身分執行（LXC 內更是連 subtree 都沒授權）。要求管理
員為此改成 root 執行，等於為了一個效能旋鈕犧牲整個權限模型 —— 不划算。

CPU affinity（`sched_setaffinity`）非 root 也能對**自己的子行程**設定，會被 fork /
exec 繼承（soffice 是 shell script 再 exec `soffice.bin`，一樣繼承），而且效果直接：
被綁在 N 顆核心上的 soffice，剩下的核心對它來說根本不存在。

代價是顆粒度只到「核心」：8 核機器最細就是 12.5%。實務上夠用 —— 我們要的是
「留一顆給網頁」，不是精準的 37%。

## 平台差異（照實說，不要讓管理員猜）

* **Linux**：affinity 與 per-thread nice 都支援，功能完整。
* **Windows**：affinity 支援（`SetProcessAffinityMask`）；執行緒優先權沒有 per-thread
  nice，改由 Popen 的 `BELOW_NORMAL_PRIORITY_CLASS` 處理 soffice。
* **macOS**：核心**不提供** CPU affinity API，psutil 也沒有實作 → 只能降優先權。
  UI 要寫明這件事（macOS 本來就只當開發 / 單人使用的平台）。
"""
from __future__ import annotations

import logging
import os
import platform
import sys
import threading
from typing import Optional

logger = logging.getLogger("app.cpu_limit")

#: 作業執行緒要降幾階 nice。10 與 soffice 一致 —— 兩者都是「背景工作」，
#: 相對於處理 HTTP 的主執行緒（nice 0）就會讓路。
JOB_THREAD_NICE = 10

_warned_affinity = False


# ---------- 能力偵測 ----------

def affinity_supported() -> bool:
    """這台機器能不能限制行程的可用核心。"""
    if sys.platform == "darwin":
        return False        # macOS 核心沒有這個 API
    if hasattr(os, "sched_setaffinity"):
        return True
    try:                    # Windows 走 psutil
        import psutil
        return hasattr(psutil.Process(), "cpu_affinity")
    except Exception:       # noqa: BLE001
        return False


def thread_nice_supported() -> bool:
    """能不能只降「某一個執行緒」的優先權（不影響同行程的其他執行緒）。

    Linux 的執行緒就是輕量行程，`setpriority(PRIO_PROCESS, tid, n)` 只作用在
    那一個執行緒 —— 這正是我們要的（作業執行緒讓路，處理 HTTP 的不受影響）。
    其他平台的 nice 是整個行程共用，套下去會連網頁一起降權，反而幫倒忙。
    """
    return sys.platform.startswith("linux") and hasattr(os, "setpriority")


def cpu_count() -> int:
    """可用核心數（容器感知）。

    LXC / Docker 裡 `os.cpu_count()` 回的是實體主機的核心數 —— 拿那個算配額會
    超發。優先問 affinity 遮罩（cgroup cpuset 會反映在這裡）。
    """
    try:
        if hasattr(os, "sched_getaffinity"):
            n = len(os.sched_getaffinity(0))
            if n > 0:
                return n
    except Exception:       # noqa: BLE001
        pass
    return os.cpu_count() or 1


# ---------- 配額計算 ----------

def effective_cores(percent: Optional[int] = None) -> int:
    """依設定算出「soffice 可以用幾顆核心」。

    `percent` 為 0（預設）時＝自動：**總核心數減 1**，也就是永遠留一顆給網頁。
    這是「網頁回應優先」的具體落實，不需要管理員自己想到要調。
    """
    total = cpu_count()
    if percent is None:
        percent = int(get_percent())
    if percent >= 100:
        return total                    # 明確表示不限制
    if percent <= 0:
        return max(1, total - 1)        # 自動：留一顆給網頁
    want = int(round(total * percent / 100.0))
    # 就算填 90%，在多核機器上也不允許把每一顆都吃掉。
    return max(1, min(want, max(1, total - 1)))


def reserved_cores(percent: Optional[int] = None) -> int:
    return max(0, cpu_count() - effective_cores(percent))


def _core_set(n: int) -> list[int]:
    """挑哪幾顆核心給 soffice。

    從**編號大的**往前取，把 CPU 0 留給網頁：中斷處理、以及不少排程器的預設
    偏好都集中在低編號核心，把背景工作推到另一端可以少一點互相干擾。
    """
    total = cpu_count()
    try:
        avail = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") \
            else list(range(total))
    except Exception:       # noqa: BLE001
        avail = list(range(total))
    if n >= len(avail):
        return avail
    return avail[-n:]


# ---------- 設定值 ----------

def get_percent() -> int:
    try:
        from . import concurrency_settings
        return int(concurrency_settings.get().get("soffice_cpu_percent") or 0)
    except Exception:       # noqa: BLE001
        return 0


# ---------- 套用 ----------

def apply_to_pid(pid: int) -> Optional[list[int]]:
    """把 CPU 限制套到剛啟動的子行程（soffice）上。

    回傳實際綁定的核心清單；沒套用（不支援 / 不限制 / 行程已結束）回 None。
    **任何失敗都只記 debug 就算了** —— 限不到 CPU 只是效能差一點，讓轉檔失敗
    才是真的問題。
    """
    global _warned_affinity
    total = cpu_count()
    if total <= 1:
        return None                     # 單核機器綁了也沒意義
    n = effective_cores()
    if n >= total:
        return None                     # 設成不限制
    if not affinity_supported():
        if not _warned_affinity:
            _warned_affinity = True
            logger.info("此平台不支援 CPU 核心限制（%s）；只會降低轉檔優先權",
                        platform.system())
        return None
    cores = _core_set(n)
    try:
        if hasattr(os, "sched_setaffinity"):
            os.sched_setaffinity(pid, set(cores))
        else:
            import psutil
            psutil.Process(pid).cpu_affinity(list(cores))
        logger.debug("soffice pid %s 綁定核心 %s（共 %s 核）", pid, cores, total)
        return cores
    except (OSError, ProcessLookupError, PermissionError) as e:
        logger.debug("設定 pid %s 的 CPU 核心失敗：%s", pid, e)
    except Exception as e:              # noqa: BLE001 — psutil 各種例外
        logger.debug("設定 pid %s 的 CPU 核心失敗：%s", pid, e)
    return None


def lower_current_thread() -> bool:
    """把「呼叫者這個執行緒」降到背景優先權。作業執行緒進場時呼叫。

    這是給**同一個行程內**的純 Python 運算用的（jtdt-layout 的版面重組、PyMuPDF
    的座標計算等）。那種工作不會 fork 子行程，所以 soffice 那套限制管不到它；
    只有降執行緒優先權 + 縮短 GIL 切換間隔（見 `main.py` 啟動段）才有用。
    """
    if not thread_nice_supported():
        return False
    try:
        tid = threading.get_native_id()
        cur = os.getpriority(os.PRIO_PROCESS, tid)
        if cur >= JOB_THREAD_NICE:
            return True                 # 已經夠低（執行緒會被重用）
        os.setpriority(os.PRIO_PROCESS, tid, JOB_THREAD_NICE)
        return True
    except (OSError, AttributeError) as e:
        logger.debug("降低作業執行緒優先權失敗：%s", e)
        return False


# ---------- 給 admin UI ----------

def describe() -> dict:
    total = cpu_count()
    pct = get_percent()
    cores = effective_cores(pct)
    return {
        "percent": pct,
        "cpu_count": total,
        "effective_cores": cores,
        "reserved_cores": max(0, total - cores),
        "affinity_supported": affinity_supported(),
        "thread_nice_supported": thread_nice_supported(),
        "auto": pct <= 0,
        "unlimited": cores >= total,
        "note": ("" if affinity_supported() else
                 "macOS 沒有提供限制核心的介面，因此只會降低轉檔的優先權"),
    }
