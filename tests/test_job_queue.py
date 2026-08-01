"""背景工作的佇列 / 持久化 / 記憶體准入。

這三件事以前都沒有：工作只活在記憶體的 dict 裡（重啟就蒸發）、排隊完全交給
ThreadPoolExecutor（看不到也停不了）、併行數固定不看記憶體（同時開幾個大檔轉換
就可能把機器打到 OOM）。

**OOM 相關的測試特別重要**：`test_low_ram_holds_in_queue` 與
`test_never_deadlock_when_nothing_running` 是一組的 —— 前者確認記憶體不夠時會
排隊而不是硬開，後者確認「排隊」不會變成「永遠不動」。少了後面那個，記憶體一
吃緊整個服務就靜止了。
"""
from __future__ import annotations

import threading
import time

import pytest

from app.core import job_store
from app.core.job_manager import TERMINAL, Job, JobManager


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr("app.config.settings.data_dir", d)
    # 每個測試用自己的 jobs.sqlite（db 模組是 thread-local 連線快取，路徑換了
    # 就會開新連線）
    job_store.init()
    return d


@pytest.fixture
def mgr():
    m = JobManager(workers=1)
    yield m
    m.set_paused(False)


def _blocker():
    """回一個 (fn, release) —— fn 會卡住直到 release 被呼叫。"""
    ev = threading.Event()

    def fn(job):
        ev.wait(timeout=10)
    return fn, ev.set


def _wait(pred, timeout=5.0, interval=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return False


# ---------------- 佇列 ----------------

def test_second_job_queues_when_at_limit(mgr):
    """併行上限 1 時，第二個工作要排隊，不是同時開跑。"""
    fn, release = _blocker()
    j1 = mgr.submit("t", fn)
    j2 = mgr.submit("t", fn)
    assert _wait(lambda: j1.status == "running"), j1.status
    time.sleep(0.15)
    assert j2.status == "pending", "超過併行上限的工作應該排隊"
    assert mgr.stats()["queued"] == 1
    release()
    assert _wait(lambda: j2.status in TERMINAL), j2.status


def test_raising_limit_dispatches_queued_jobs(mgr):
    """調高併行度要立刻把排隊中的派出去，不必等下一次送出。"""
    fn, release = _blocker()
    jobs = [mgr.submit("t", fn) for _ in range(3)]
    assert _wait(lambda: mgr.stats()["running"] == 1)
    mgr.set_max_concurrent(3)
    assert _wait(lambda: mgr.stats()["running"] == 3), mgr.stats()
    release()
    assert _wait(lambda: all(j.status in TERMINAL for j in jobs))


def test_pause_holds_new_jobs_but_not_running_one(mgr):
    """暫停只擋「還沒開始的」—— 已經在跑的 soffice 是獨立子行程，凍結不了。"""
    fn, release = _blocker()
    j1 = mgr.submit("t", fn)
    assert _wait(lambda: j1.status == "running")
    mgr.set_paused(True)
    j2 = mgr.submit("t", lambda job: None)
    release()
    assert _wait(lambda: j1.status == "done"), j1.status   # 手上的照樣跑完
    time.sleep(0.2)
    assert j2.status == "pending", "暫停期間不可派新工作"
    mgr.set_paused(False)
    assert _wait(lambda: j2.status == "done"), j2.status


def test_cancel_queued_job_removes_it_from_queue(mgr):
    fn, release = _blocker()
    mgr.submit("t", fn)
    j2 = mgr.submit("t", fn)
    assert _wait(lambda: mgr.stats()["queued"] == 1)
    assert mgr.cancel(j2.id) is True
    assert j2.status == "cancelled"
    assert mgr.stats()["queued"] == 0
    release()


def test_cancel_finished_job_returns_false(mgr):
    j = mgr.submit("t", lambda job: None)
    assert _wait(lambda: j.status == "done")
    assert mgr.cancel(j.id) is False


# ---------------- 記憶體准入（OOM 防線）----------------

def test_low_ram_holds_in_queue(mgr, monkeypatch):
    """記憶體不足時，第二個工作要**留在佇列**，不是硬開下去把機器打爆。"""
    monkeypatch.setattr("app.core.concurrency_settings.available_mb",
                        lambda: 900)
    monkeypatch.setattr("app.core.concurrency_settings.reserve_mb",
                        lambda: 768)
    # 估 800MB + 保留 768MB = 1568 > 900 → 不准再開
    mgr.set_max_concurrent(4)
    fn, release = _blocker()
    j1 = mgr.submit("pdf-to-office", fn)
    j2 = mgr.submit("pdf-to-office", fn)
    assert _wait(lambda: j1.status == "running")
    time.sleep(0.3)
    assert j2.status == "pending", "記憶體不足時應排隊"
    assert mgr.stats()["held_for_ram"] is True
    release()
    assert _wait(lambda: j2.status in TERMINAL, timeout=8), j2.status


def test_never_deadlock_when_nothing_running(mgr, monkeypatch):
    """**沒有任何工作在跑時，就算記憶體看起來不夠也要派一個出去。**

    否則會永遠卡住：不派工 → 沒有工作結束 → 記憶體不會釋放 → 永遠不派工。
    這種情況下讓它跑（可能失敗）也遠比整個服務靜止不動好。
    """
    monkeypatch.setattr("app.core.concurrency_settings.available_mb",
                        lambda: 10)
    monkeypatch.setattr("app.core.concurrency_settings.reserve_mb",
                        lambda: 4096)
    j = mgr.submit("pdf-to-office", lambda job: None)
    assert _wait(lambda: j.status == "done", timeout=5), j.status


def test_ram_hold_retries_without_new_submissions(mgr, monkeypatch):
    """被記憶體壓著的工作要靠看門狗自己重試 —— 不能等下一個人送工作才解開。"""
    import app.core.job_manager as jm_mod
    monkeypatch.setattr(jm_mod, "_RETRY_INTERVAL", 0.2)
    avail = {"mb": 900}
    monkeypatch.setattr("app.core.concurrency_settings.available_mb",
                        lambda: avail["mb"])
    monkeypatch.setattr("app.core.concurrency_settings.reserve_mb",
                        lambda: 768)
    mgr.set_max_concurrent(4)
    fn, release = _blocker()
    mgr.submit("pdf-to-office", fn)
    j2 = mgr.submit("pdf-to-office", fn)
    assert _wait(lambda: mgr.stats()["held_for_ram"] is True)
    avail["mb"] = 32000            # 記憶體回來了，但沒有人送新工作
    assert _wait(lambda: j2.status == "running", timeout=5), j2.status
    release()


def test_unknown_memory_does_not_block(mgr, monkeypatch):
    """讀不到記憶體資訊（psutil 缺 / 容器怪）時要放行，不能因此停擺。"""
    monkeypatch.setattr("app.core.concurrency_settings.available_mb",
                        lambda: None)
    mgr.set_max_concurrent(2)
    fn, release = _blocker()
    mgr.submit("pdf-to-office", fn)
    j2 = mgr.submit("pdf-to-office", fn)
    assert _wait(lambda: j2.status == "running"), j2.status
    release()


def test_office_tools_estimate_more_memory():
    from app.core import concurrency_settings as cs
    assert cs.estimated_job_mb("pdf-to-office") > cs.estimated_job_mb("pdf-split")


def test_office_tool_list_matches_actual_imports():
    """OFFICE_TOOL_IDS 是人工清單 —— 用實際掃描結果比對，加新工具漏加就會紅。"""
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
    from tools.check_docs_tool_coverage import (load_tools,
                                                office_dependent_tool_ids)
    from app.core.concurrency_settings import OFFICE_TOOL_IDS

    pkg = {}
    for t in load_tools():
        td = getattr(t, "templates_dir", None)
        name = _P(td).parent.name if td else t.metadata.id.replace("-", "_")
        pkg[name] = t.metadata.id
    actual = office_dependent_tool_ids(pkg)
    assert actual <= set(OFFICE_TOOL_IDS), (
        f"這些工具會起 soffice 但沒列進 OFFICE_TOOL_IDS："
        f"{sorted(actual - set(OFFICE_TOOL_IDS))} —— 併行度會低估記憶體用量")


# ---------------- 持久化 ----------------

def test_job_row_written_on_submit_and_completion(mgr):
    j = mgr.submit("pdf-merge", lambda job: None, meta={"count": 3})
    assert _wait(lambda: j.status == "done")
    row = job_store.get(j.id)
    assert row is not None, "工作沒寫進 jobs.sqlite"
    assert row["tool_id"] == "pdf-merge"
    assert row["status"] == "done"
    assert row["finished_at"] is not None


def test_error_is_persisted(mgr):
    def boom(job):
        raise RuntimeError("壞掉了")
    j = mgr.submit("t", boom)
    assert _wait(lambda: j.status == "error")
    row = job_store.get(j.id)
    assert row["status"] == "error" and "壞掉了" in (row["error"] or "")


def test_restart_marks_unfinished_as_interrupted():
    """重啟後「進行中」必須變成已中斷 —— 執行緒早就不在了，繼續顯示轉換中只會
    讓使用者一直等一個永遠不會完成的工作。"""
    j = Job(id="a" * 32, tool_id="pdf-to-office", status="running")
    job_store.upsert(j)
    q = Job(id="b" * 32, tool_id="pdf-to-office", status="pending")
    job_store.upsert(q)
    job_store.init()               # 模擬服務重新啟動
    for jid in (j.id, q.id):
        row = job_store.get(jid)
        assert row["status"] == "interrupted", row["status"]
        assert row["finished_at"] is not None


def test_get_falls_back_to_db_after_memory_eviction(mgr, tmp_path):
    """記憶體裡被汰除（或重啟）之後仍要查得到、拿得到結果檔路徑。"""
    out = tmp_path / "r.pdf"
    out.write_bytes(b"%PDF-1.4")

    def run(job):
        job.result_path = out
        job.result_filename = "r.pdf"
    j = mgr.submit("pdf-merge", run)
    assert _wait(lambda: j.status == "done")
    mgr._jobs.clear()              # 模擬記憶體汰除 / 行程重啟
    again = mgr.get(j.id)
    assert again is not None, "記憶體沒有就查不到 → 使用者的結果檔等於遺失"
    assert again.status == "done"
    assert again.result_path == out
    assert again.to_public()["has_result"] is True


def test_owner_recorded_at_submit_not_on_first_poll(mgr, monkeypatch):
    """歸屬要在送出當下就決定。原本是「第一次輪詢時才標記」—— 那個設計本身就
    假設頁面還開著，使用者一關分頁工作就變成沒有主人。"""
    from app.core import job_manager as jm_mod
    jm_mod.set_current_actor({"user_id": 42, "username": "alice",
                              "source": "local"}, "10.1.2.3")
    j = mgr.submit("pdf-merge", lambda job: None)
    assert j.owner_id == 42
    assert "alice" in j.owner_label
    assert j.client_ip == "10.1.2.3"
    assert _wait(lambda: j.status == "done")
    assert job_store.get(j.id)["owner_id"] == 42


def test_list_filters_by_owner(mgr):
    from app.core import job_manager as jm_mod
    jm_mod.set_current_actor({"user_id": 1, "username": "a",
                              "source": "local"}, "")
    mgr.submit("pdf-merge", lambda job: None)
    jm_mod.set_current_actor({"user_id": 2, "username": "b",
                              "source": "local"}, "")
    j2 = mgr.submit("pdf-split", lambda job: None)
    assert _wait(lambda: j2.status == "done")
    mine = job_store.list_jobs(owner_id=2)
    assert [r["tool_id"] for r in mine] == ["pdf-split"]
    assert job_store.count_jobs(owner_id=1) == 1


def test_list_limit_is_capped():
    """列表查詢一定要有上限 —— 不能讓一個請求把整張表讀進記憶體。"""
    for i in range(5):
        job_store.upsert(Job(id=f"{i:032d}", tool_id="t", status="done"))
    assert len(job_store.list_jobs(limit=10**9)) <= job_store._LIST_HARD_CAP


def test_oversized_meta_is_truncated():
    """單筆 meta 不可無限大 —— 預覽清單之類的東西會把 DB 和記憶體撐爆。"""
    j = Job(id="c" * 32, tool_id="t", status="done",
            meta={"filename": "x.pdf", "blob": "y" * (200 * 1024)})
    job_store.upsert(j)
    row = job_store.get(j.id)
    assert row["meta"].get("_truncated") is True
    assert row["meta"].get("filename") == "x.pdf", "小欄位應保留"


def test_memory_trim_keeps_recent_only(mgr, monkeypatch):
    import app.core.job_manager as jm_mod
    monkeypatch.setattr(jm_mod, "_MEM_KEEP", 5)
    for _ in range(20):
        mgr.submit("t", lambda job: None)
    assert _wait(lambda: mgr.stats()["running"] == 0 and
                 mgr.stats()["queued"] == 0, timeout=10)
    assert len(mgr._jobs) <= 5, f"記憶體沒有收斂：{len(mgr._jobs)}"
    # 但 DB 裡要留著（歷史查得到）
    assert job_store.count_jobs() == 20


def test_persist_failure_does_not_break_the_job(mgr, monkeypatch):
    """持久化壞掉（磁碟滿 / DB 鎖住）不可以害轉檔失敗。"""
    def boom(job):
        raise OSError("disk full")
    monkeypatch.setattr(job_store, "upsert", boom)
    j = mgr.submit("t", lambda job: None)
    assert _wait(lambda: j.status == "done"), j.status


def test_owner_key_matches_real_session_shape():
    """`sessions.lookup()` 回的鍵是 `user_id` 不是 `id`。取錯不會報錯，只會讓
    每個工作都沒有主人 —— 而「我的工作」空空如也時，使用者只會覺得功能壞了，
    不會知道是鍵名打錯。這裡直接對真正的 session 形狀驗一次。"""
    import inspect

    from app.core import sessions
    from app.core.job_manager import _uid_of

    src = inspect.getsource(sessions.lookup)
    assert '"user_id"' in src, "session 形狀變了，這個測試要跟著更新"
    assert _uid_of({"user_id": 7, "username": "x", "source": "local"}) == 7
    assert _uid_of(None) is None
    assert _uid_of({}) is None


# ---------- 外部服務名額：同執行緒巢狀取用不可卡死 ----------

def test_remote_limit_is_reentrant_within_a_thread():
    """同一個執行緒重複取名額要放行，不可以等一個只有自己能釋放的名額。

    真的踩過：逐句翻譯改成背景作業時，工作端包了一層 `with slot():`，而
    `llm_client.text_query` 內部本來就會取一次 —— 名額只有 1，那個執行緒
    就卡死自己。症狀是整個作業停在「準備中」，從外面完全看不出原因
    （堆疊顯示兩個 worker 都卡在 `remote_limit.__enter__`）。
    """
    from app.core import remote_limit
    done = []

    def work():
        with remote_limit.slot():
            with remote_limit.slot():        # 巢狀
                done.append(1)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout=5)
    assert done == [1], "巢狀取用卡住了（或執行緒沒跑完）"
    assert remote_limit.stats()["in_use"] == 0, "離開之後名額沒有還回去"


def test_remote_limit_still_serialises_across_threads():
    """可重入不可以順便把「跨執行緒只准一個」這件事也放寬。"""
    from app.core import remote_limit
    remote_limit.set_limit(1)
    peak = {"n": 0}
    cur = {"n": 0}
    lock = threading.Lock()

    def work():
        with remote_limit.slot():
            with lock:
                cur["n"] += 1
                peak["n"] = max(peak["n"], cur["n"])
            time.sleep(0.05)
            with lock:
                cur["n"] -= 1

    ts = [threading.Thread(target=work, daemon=True) for _ in range(4)]
    for t in ts: t.start()
    for t in ts: t.join(timeout=5)
    assert peak["n"] == 1, f"同時有 {peak['n']} 個執行緒進去了（上限是 1）"
    assert remote_limit.stats()["in_use"] == 0


def test_remote_limit_releases_on_exception():
    """呼叫外部服務失敗（逾時 / 斷線）時名額要確實還回去。"""
    from app.core import remote_limit
    try:
        with remote_limit.slot():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert remote_limit.stats()["in_use"] == 0


def test_terminal_status_hits_the_db_before_autosave(mgr, monkeypatch):
    """作業一結束就要先把最終狀態寫進 DB，**再**去做自動存入工作區。

    自動存入要複製檔案，可能花上幾百毫秒到幾秒。原本的順序是「autosave → persist」，
    那段時間裡記憶體已經是 done、DB 還停在 running —— 從 DB 讀到的狀態是錯的
    （作業從記憶體淘汰之後更會讀到 running 的殭屍狀態）。整套測試一起跑時實際
    出現過這個假性失敗。
    """
    seen = {}

    def slow_autosave(job):
        # 在 autosave 進行中偷看一眼 DB：這時就該已經是終局狀態了
        row = job_store.get(job.id)
        seen["status_during_autosave"] = row["status"] if row else None
        return None

    monkeypatch.setattr("app.core.job_autosave.on_job_finished", slow_autosave)
    j = mgr.submit("pdf-merge", lambda job: None)
    assert _wait(lambda: j.status == "done")
    assert seen.get("status_during_autosave") == "done", (
        f"自動存入進行中時 DB 還是 {seen.get('status_during_autosave')}"
        " —— 順序反了")
