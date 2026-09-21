"""殺掉一棵子行程樹 —— 而且**確認殺的是自己那一棵**。

## 為什麼要有這支

1. **`kill(pid)` 殺不掉真正在做事的那個行程。**
   `/opt/oxoffice/program/soffice` 是 shell 包裝腳本，真正解析檔案的
   `soffice.bin` 是它 fork 的子行程 —— 殺掉包裝腳本，子行程**變成孤兒繼續空轉**
   （正式機 2026-09-18 實測：逾時 4 分鐘後 PPID=1、狀態 R、還在燒 CPU）。
   所以要殺**整個行程群組**（我們啟動時就讓它自己一個 session）。

2. **pid 會被重用。** 服務跑了 77 天、pid 繞過一輪之後，
   `_subprocs` 裡記著的號碼可能已經是**別人的行程** ——
   而我們要殺的是「整個群組」，殺錯的代價是把不相干的東西一起帶走。
   所以登記時記下**指紋**（行程的啟動時間 tick），殺之前比對。

> 這跟「不可以用名字樣式批次殺」是同一條原則的另一面：
> **動手之前要能證明那真的是自己的東西。**
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys

logger = logging.getLogger(__name__)

_IS_WIN = sys.platform.startswith("win")


def fingerprint(pid: int) -> str | None:
    """行程的身分指紋。認不出來時回 `None`（那就退回只用 pid）。

    Linux 用 `/proc/<pid>/stat` 的第 22 欄（starttime，開機後的 tick 數）——
    **同一個 pid 被重用時這個值一定不同**。
    """
    if _IS_WIN:
        return None
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read()
        # comm 欄位可能含空白與括號 → 從最後一個 ')' 之後切
        tail = raw[raw.rindex(b")") + 2:].split()
        return tail[19].decode()          # starttime 是第 22 欄（tail 的第 20 個）
    except (OSError, ValueError, IndexError):
        return None


def looks_like(pid: int, want: str | None) -> bool:
    """這個 pid 還是當初登記的那個行程嗎？

    **沒有指紋時一律回 True** —— 拿不到證據時不要因此拒絕收尾
    （那會讓孤兒行程永遠留著，比誤殺的風險更常發生）。
    """
    if want is None:
        return True
    got = fingerprint(pid)
    return got is None or got == want


def kill_tree(pid: int, want: str | None = None) -> bool:
    """把 `pid` 所在的行程群組整棵殺掉。回 True 表示真的下手了。

    **絕不丟例外** —— 呼叫這支的地方幾乎都是錯誤處理或取消路徑，
    在那裡再炸一次的話使用者看到的會是堆疊而不是「已取消」。
    """
    if pid <= 0:
        return False
    if not looks_like(pid, want):
        logger.debug("proc_tree: pid %s 已經不是當初那個行程，不動它", pid)
        return False
    try:
        if _IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10)
            return True
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False                      # 已經結束了 —— 那是好事
    except Exception as e:                # noqa: BLE001
        logger.debug("proc_tree: 殺 pid %s 失敗：%s", pid, e)
        try:
            os.kill(pid, signal.SIGKILL)  # 退一步只殺它自己，總比不殺好
            return True
        except Exception:                 # noqa: BLE001
            return False
