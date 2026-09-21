"""按下取消要**真的把工作停掉**，不是只把狀態改成「已停止」。

## 為什麼

轉檔那幾分鐘整個卡在 `proc.communicate()` 裡，我們自己的 checkpoint
（`job.cancelled`）**永遠不會被檢查到**。所以原本的 `cancel()` 只做了標記：
畫面顯示「已停止」，而**伺服器照樣把那次算完**，soffice 繼續吃 CPU。

本專案在「重繪中又有新動作」那一輪記過同一條：
**中止請求不等於中止工作。**

## pid 重用

服務跑了 77 天、pid 繞過一輪之後，`_subprocs` 裡記的號碼可能已經是別人的
行程 —— 而我們殺的是**整個行程群組**，殺錯的代價是把不相干的東西一起帶走。
所以登記時記下身分指紋（行程啟動時間），殺之前比對。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from app.core import proc_tree

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"),
                                reason="行程群組的作法在 Windows 不同（走 taskkill /T）")


def _spawn_tree():
    """做一棵「包裝腳本 → 真正在算的子行程」的樹，跟 soffice 同形狀。"""
    script = ('import subprocess, sys, time\n'
              'p = subprocess.Popen([sys.executable, "-c", "import time\\n'
              'while True: time.sleep(0.2)"])\n'
              'print(p.pid, flush=True)\n'
              'p.wait()\n')
    proc = subprocess.Popen([sys.executable, "-c", script],
                            stdout=subprocess.PIPE, start_new_session=True)
    child = int(proc.stdout.readline().strip())
    return proc, child


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_kill_tree_takes_the_grandchild():
    proc, child = _spawn_tree()
    fp = proc_tree.fingerprint(proc.pid)
    assert _alive(child)
    assert proc_tree.kill_tree(proc.pid, fp)
    for _ in range(50):
        if not _alive(child):
            break
        time.sleep(0.1)
    assert not _alive(child), "整棵樹都要死掉，不可以留下孤兒繼續燒 CPU"
    proc.wait(timeout=5)


def test_a_reused_pid_is_not_killed():
    """**這條是指紋存在的理由。** 指紋對不上就不動它。"""
    proc, child = _spawn_tree()
    try:
        assert not proc_tree.kill_tree(proc.pid, "0")   # 假的指紋
        assert _alive(child), "指紋對不上卻還是殺了 —— 那會誤殺不相干的行程"
    finally:
        proc_tree.kill_tree(proc.pid, proc_tree.fingerprint(proc.pid))
        proc.wait(timeout=5)


def test_no_fingerprint_means_go_ahead():
    """拿不到證據時不要因此拒絕收尾 —— 那會讓孤兒行程永遠留著，
    比誤殺的風險更常發生。"""
    proc, child = _spawn_tree()
    assert proc_tree.kill_tree(proc.pid, None)
    proc.wait(timeout=5)


def test_kill_tree_never_raises():
    assert proc_tree.kill_tree(0) is False
    assert proc_tree.kill_tree(-1) is False
    assert proc_tree.kill_tree(2 ** 30) is False       # 幾乎不可能存在的 pid


def test_cancel_kills_the_registered_subprocess(monkeypatch):
    """端到端：作業登記了子行程 → 取消 → **那個子行程真的死掉**。

    只驗「狀態變成 cancelled」證明不了任何事 —— 原本的程式就是那樣，
    而 soffice 照樣跑完。
    """
    from app.core import job_manager as jm

    proc, child = _spawn_tree()
    started = {"ok": False}

    def _work(job):
        started["ok"] = True
        jm.job_manager.register_subprocess(proc.pid)
        # **一定要給逾時** —— 沒有的話，取消功能壞掉時這條測試會整個卡住
        # （而不是紅），而卡住的測試在 CI 上只會變成一個很久的逾時，
        # 沒有人看得出是哪裡壞了。變異驗證時我就這樣卡了兩分鐘。
        proc.wait(timeout=25)
        return {"done": True}

    job = jm.job_manager.submit("markdown-to-doc", _work)
    # **等派送要等久一點** —— 作業佇列只有兩個 worker，而且有記憶體准入。
    # 整份測試一起跑（瀏覽器測試把機器壓滿）時，這件作業會排隊好幾秒。
    # 原本只等 5 秒，於是**單跑全綠、合跑紅**（2026-09-18 實際踩到）。
    # 等待的是「這條測試的前置」，不是它要驗的東西，放寬不會讓它變鬆。
    for _ in range(600):
        if started["ok"] and jm.job_manager.get(job.id).status == "running":
            break
        time.sleep(0.05)
    # 失敗時要分得出「沒排到」與「跑了但沒登記」—— 只說「沒跑起來」的話
    # 下一個人得重跑一次才知道是哪一種。
    st = jm.job_manager.get(job.id)
    assert started["ok"], (
        f"作業沒有跑起來（狀態 {st.status if st else '?'}），這條測試驗不到東西")

    assert jm.job_manager.cancel(job.id) is True
    for _ in range(80):
        if not _alive(child):
            break
        time.sleep(0.1)
    assert not _alive(child), "取消之後子行程還活著 —— 那就只是畫面上停了"
    proc.wait(timeout=5)
