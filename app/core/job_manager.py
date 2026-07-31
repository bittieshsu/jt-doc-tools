"""背景工作（job）排程與狀態管理。

## 為什麼不是「直接丟進 ThreadPoolExecutor」

原本的做法是 `ThreadPoolExecutor(max_workers=2).submit(...)`，排隊完全交給
executor 的內部佇列。那樣有四個問題：

1. **看不到排隊** —— 使用者不知道自己前面還有幾個，管理員也看不到積了多少。
2. **不能暫停** —— executor 沒有「先別派新工作」這種概念。維護前想讓手上的跑完
   卻不要再開新的，做不到。
3. **改不了併行度** —— `max_workers` 是建構時決定的，要調就得換掉整個 executor，
   而正在跑的工作還握著舊的。
4. **不看記憶體** —— 併行數是固定的，不管當下還剩多少 RAM 都照開。轉檔類工作
   單一個就可能吃掉數百 MB，同時開幾個直接把機器打到 OOM。

所以改成「自己管 pending 佇列 + 自己控制准入」：executor 只當執行緒池（上限開得
寬），真正要不要開下一個由 `_dispatch()` 決定 —— 同時看併行上限、暫停旗標、
**以及當下的可用記憶體**。

## OOM 防線（鐵則：寧可排隊，不可打爆機器）

* `_dispatch()` 每次派送前估算「再開一個要多少記憶體」，不夠就**留在佇列裡**，
  不是硬開。等到有工作結束、記憶體釋放，或看門狗定時重試時再派。
* **永遠允許至少一個在跑**：如果一個都沒在跑，就算記憶體看起來吃緊也要派一個
  出去 —— 否則佇列會永遠卡住（沒有工作結束 → 記憶體不會釋放 → 永遠不派送）。
  這種情況下該失敗的就讓它失敗，總比整個服務靜止不動好。
* 記憶體讀取走 `host_stats`（容器感知）—— LXC / Docker 裡 psutil 讀到的是**實體
  主機**的數字，用那個判斷等於沒判斷。
* 記憶體只留「進行中 + 最近完成」的 job，上限 `_MEM_KEEP`；歷史查 SQLite。

## 持久化

狀態變化時寫進 `data/jobs.sqlite`（見 `job_store`）。進度的逐次更新**不寫 DB**
—— 那是每秒數次的高頻寫入，而重啟後進行中的工作本來就活不下來；即時進度只給
同一個行程內的輪詢用。
"""
from __future__ import annotations

import contextvars
import logging
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from ..config import settings

logger = logging.getLogger("app.job_manager")

JobStatus = Literal["pending", "running", "done", "error", "cancelled",
                    "interrupted"]

#: 已結束、不會再變動的狀態
TERMINAL: tuple[str, ...] = ("done", "error", "cancelled", "interrupted")

#: 記憶體中保留的 job 數上限（超過就只留在 SQLite）
_MEM_KEEP = 300

#: 執行緒池的硬上限。實際同時執行數由 `_max_concurrent` 控制，這裡開寬是為了讓
#: 管理員調高併行度時不必重建 executor（正在跑的工作握著舊的那個）。
_POOL_CEILING = 32

#: 記憶體不足而壓著沒派送時，多久重試一次（秒）
_RETRY_INTERVAL = 5.0

# 目前請求的使用者 / 來源 IP。由 auth 中介層設定，`submit()` 讀取 —— 這樣 25 個
# 工具的呼叫端都不必各自傳 request 進來，也不會有新工具忘記傳的問題。
_current_actor: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "jtdt_current_actor", default=None)

# 執行中的作業 id（在 `_run` 的執行緒內設定）。子行程要掛到哪個作業底下靠這個 ——
# 不必把 job 物件一路傳進 office_convert 那些既有函式。
_current_job_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "jtdt_current_job_id", default=None)


def _uid_of(user: Optional[dict]) -> Optional[int]:
    """從 session 的 user dict 取出使用者 id。

    **鍵名是 `user_id` 不是 `id`** —— `sessions.lookup()` 回的是
    `{"user_id", "username", "display_name", "source", "is_admin_seed"}`。
    取錯鍵不會報錯，只會靜靜回 None，然後所有工作都變成沒有主人（「我的工作」
    永遠是空的）。同一類錯誤讓 history 顯示「(匿名)」拖了 7 個版本才修好，
    見 feedback_request_state_user_is_dict。
    """
    if not user:
        return None
    raw = user.get("user_id", user.get("id"))
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def set_current_actor(user: Optional[dict], client_ip: str = "") -> None:
    """由中介層在每個請求開頭呼叫，讓之後的 `submit()` 知道是誰送的工作。"""
    try:
        from .sessions import user_label
        label = user_label(user) if user else ""
    except Exception:  # noqa: BLE001
        label = ""
    _current_actor.set({
        "owner_id": _uid_of(user),
        "owner_label": label,
        "client_ip": client_ip or "",
    })


@dataclass
class Job:
    id: str
    tool_id: str
    status: JobStatus = "pending"
    progress: float = 0.0
    message: str = ""
    result_path: Optional[Path] = None
    result_filename: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    meta: dict[str, Any] = field(default_factory=dict)
    cancelled: bool = False
    # 送出當下就決定（取自請求的使用者），不再等第一次輪詢才標記 —— 要做「我的
    # 工作」與完成通知，就必須在使用者關掉頁面之後仍然知道這是誰的工作。
    owner_id: Optional[int] = None
    owner_label: str = ""
    client_ip: str = ""
    #: 最後一次有人查詢這個作業狀態的時間。頁面開著時每 1~2 秒就會輪詢一次，
    #: 所以「最近有沒有被輪詢」就等於「使用者還在不在看」——「完成後自動存入
    #: 工作區」只在他**已經離開**時才需要（人還在的話按下載就好，自動存只是
    #: 多一份重複檔案並吃掉額度）。
    last_polled_at: Optional[float] = None

    def elapsed(self) -> float:
        end = self.updated_at if self.status in TERMINAL else time.time()
        return max(0.0, end - (self.started_at or self.created_at))

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "tool_id": self.tool_id,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "elapsed": round(self.elapsed(), 1),
            "queued": self.status == "pending",
            "has_result": self.result_path is not None and self.result_path.exists(),
            "result_filename": self.result_filename,
            "meta": self.meta or {},
        }


class JobManager:
    def __init__(self, workers: int = 2) -> None:
        self._jobs: dict[str, Job] = {}
        self._fns: dict[str, Callable[[Job], None]] = {}
        self._pending: deque[str] = deque()
        self._running: set[str] = set()
        self._subprocs: dict[str, set[int]] = {}
        self._lock = threading.RLock()
        self._max_concurrent = max(1, int(workers))
        self._paused = False
        self._held_for_ram = False
        self._retry_timer: Optional[threading.Timer] = None
        self._executor = ThreadPoolExecutor(max_workers=_POOL_CEILING,
                                            thread_name_prefix="job")

    # ---------- 設定 ----------

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    def set_max_concurrent(self, n: int) -> int:
        """調整同時執行數。調高會立刻把排隊中的工作派出去。"""
        with self._lock:
            self._max_concurrent = max(1, min(int(n), _POOL_CEILING))
        self._dispatch()
        return self._max_concurrent

    def set_paused(self, paused: bool) -> bool:
        """暫停 / 恢復派送。

        **只影響尚未開始的工作** —— 已經在跑的 soffice 是獨立子行程，沒辦法凍結
        （中途暫停只會留下半成品），要停只能取消。UI 必須照實說明，不要讓管理員
        以為按了暫停手上那個就停住了。
        """
        with self._lock:
            self._paused = bool(paused)
        if not paused:
            self._dispatch()
        return self._paused

    @property
    def paused(self) -> bool:
        return self._paused

    #: 多久沒被輪詢就算「使用者已經離開」（秒）。頁面每 1~2 秒輪詢一次，
    #: 取 30 秒足以涵蓋分頁被切到背景時瀏覽器降頻的情況。
    IDLE_AFTER = 30.0

    def mark_polled(self, job_id: str) -> None:
        """有人查詢了這個作業的狀態 —— 代表頁面還開著。"""
        job = self._jobs.get(job_id)
        if job is not None:
            job.last_polled_at = time.time()

    def is_being_watched(self, job_id: str) -> bool:
        """使用者是不是還盯著這個作業？

        **從沒被輪詢過**算「沒在看」—— 那是透過 API 送出、或送出後立刻關掉頁面
        的情況，正是最需要自動保存的。
        """
        job = self._jobs.get(job_id)
        if job is None or not job.last_polled_at:
            return False
        return (time.time() - job.last_polled_at) <= self.IDLE_AFTER

    def live_snapshot(self) -> dict[str, dict]:
        """記憶體中作業的即時狀態：{id: {status, progress, message}}。

        **列表端點必須疊上這份**：清單本身讀的是 `jobs.sqlite`，而進度**刻意
        不寫 DB**（每秒數次的高頻寫入，且重啟後進行中的作業本來就活不下來）。
        少了這一步，進度條永遠是 0 —— 使用者看到的就是一條空白的進度條配著
        「進行中」，完全不知道跑到哪了。
        """
        with self._lock:
            return {j.id: {"status": j.status, "progress": j.progress,
                           "message": j.message}
                    for j in self._jobs.values()}

    def queue_positions(self) -> dict[str, int]:
        """排隊中的作業 → 第幾位（1 起算）。

        以 `_pending` 這個 deque 的**實際順序**為準，而不是拿建立時間去猜 ——
        取消、記憶體回壓都會讓順序與時間不一致，猜出來的號碼會對不上真正的派送
        順序，使用者就會覺得「明明排我前面怎麼比較慢」。
        """
        with self._lock:
            return {jid: i + 1 for i, jid in enumerate(self._pending)}

    def register_subprocess(self, pid: int) -> None:
        """把子行程（soffice 等）掛到目前這個作業底下，供資源用量顯示。

        轉檔真正吃記憶體的是 soffice 子行程，不是我們自己的執行緒 —— 只看本行程
        的用量會看到一個跟實際完全無關的數字。
        """
        jid = _current_job_id.get()
        if not jid:
            return
        with self._lock:
            self._subprocs.setdefault(jid, set()).add(int(pid))

    def unregister_subprocess(self, pid: int) -> None:
        jid = _current_job_id.get()
        if not jid:
            return
        with self._lock:
            s = self._subprocs.get(jid)
            if s:
                s.discard(int(pid))
                if not s:
                    self._subprocs.pop(jid, None)

    def resource_usage(self) -> dict[str, dict]:
        """執行中作業的資源用量：{job_id: {"rss_mb", "cpu_pct", "procs"}}。

        沒有 psutil、或子行程已結束時回空的 —— 顯示不出來就不顯示，不要編一個
        數字出來（估計值另外標示為估計，不混在一起）。
        """
        try:
            import psutil
        except ImportError:
            return {}
        with self._lock:
            snapshot = {j: set(p) for j, p in self._subprocs.items()}
            running = set(self._running)
        out: dict[str, dict] = {}
        for jid in running:
            pids = snapshot.get(jid) or set()
            rss = 0
            cpu = 0.0
            alive = 0
            for pid in pids:
                try:
                    pr = psutil.Process(pid)
                    with pr.oneshot():
                        rss += pr.memory_info().rss
                        cpu += pr.cpu_percent(interval=None)
                        alive += 1
                    for ch in pr.children(recursive=True):
                        try:
                            rss += ch.memory_info().rss
                            alive += 1
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            if alive:
                out[jid] = {"rss_mb": round(rss / 1048576, 1),
                            "cpu_pct": round(cpu, 1), "procs": alive}
        return out

    def stats(self) -> dict:
        with self._lock:
            return {
                "running": len(self._running),
                "queued": len(self._pending),
                "max_concurrent": self._max_concurrent,
                "paused": self._paused,
                "held_for_ram": self._held_for_ram,
            }

    # ---------- 送出 / 派送 ----------

    def submit(
        self,
        tool_id: str,
        fn: Callable[["Job"], None],
        meta: Optional[dict] = None,
        request: Any = None,
    ) -> Job:
        job = Job(id=uuid.uuid4().hex, tool_id=tool_id, meta=meta or {})
        self._attach_actor(job, request)
        with self._lock:
            self._jobs[job.id] = job
            self._fns[job.id] = fn
            self._pending.append(job.id)
            if not job.message:
                job.message = "排隊中…"
        self._persist(job)
        self._dispatch()
        return job

    def _attach_actor(self, job: Job, request: Any) -> None:
        """優先用明確傳入的 request，其次用中介層設定的 contextvar。"""
        actor = None
        if request is not None:
            try:
                from .client_ip import real_client_ip
                from .sessions import user_label
                user = getattr(request.state, "user", None)
                actor = {"owner_id": _uid_of(user),
                         "owner_label": user_label(user) if user else "",
                         "client_ip": real_client_ip(request)}
            except Exception:  # noqa: BLE001 — 取不到歸屬不該擋下轉檔
                actor = None
        if actor is None:
            actor = _current_actor.get()
        if actor:
            job.owner_id = actor.get("owner_id")
            job.owner_label = actor.get("owner_label") or ""
            job.client_ip = actor.get("client_ip") or ""

    def _dispatch(self) -> None:
        """把排隊中的工作派出去 —— 直到達到併行上限**或記憶體不夠**。"""
        to_start: list[str] = []
        hold = False
        with self._lock:
            # 注意：迴圈內就把 jid 放進 `_running`，所以計數只看 `_running`。
            # 之前多加了一個 `len(to_start)` → 重複計算，調高上限時每次只會多派
            # 一個工作出去（測試 test_raising_limit_dispatches_queued_jobs 抓到）。
            while (not self._paused
                   and len(self._running) < self._max_concurrent
                   and self._pending):
                jid = self._pending[0]
                job = self._jobs.get(jid)
                if job is None or job.cancelled:
                    self._pending.popleft()
                    continue
                if self._running and not _ram_allows_start(job.tool_id):
                    # 記憶體不足 → 留在佇列裡等，不硬開。busy==0 時不做這個判斷，
                    # 否則沒有任何工作在跑時佇列會永遠解不開。
                    hold = True
                    break
                self._pending.popleft()
                self._running.add(jid)
                to_start.append(jid)
            self._held_for_ram = hold
        for jid in to_start:
            self._executor.submit(self._run, jid)
        if hold:
            self._schedule_retry()

    def _schedule_retry(self) -> None:
        """記憶體不足而壓著時，定時再試 —— 否則沒有任何事件會喚醒佇列。"""
        with self._lock:
            if self._retry_timer is not None and self._retry_timer.is_alive():
                return
            t = threading.Timer(_RETRY_INTERVAL, self._retry_tick)
            t.daemon = True
            self._retry_timer = t
        t.start()

    def _retry_tick(self) -> None:
        with self._lock:
            self._retry_timer = None
            pending = bool(self._pending)
        if pending:
            self._dispatch()

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            fn = self._fns.get(job_id)
        if job is None or fn is None:
            self._finish_slot(job_id)
            return
        try:
            if job.cancelled:
                job.status = "cancelled"
                job.updated_at = time.time()
                self._persist(job)
                return
            _current_job_id.set(job.id)
            # 作業執行緒一律降到背景優先權：同行程的純 Python 運算（版面重組、
            # 座標計算）會長時間握著 GIL，處理 HTTP 的執行緒就搶不到。網頁回應
            # 永遠優先於轉檔快慢。（Linux 才有 per-thread nice；見 cpu_limit）
            try:
                from . import cpu_limit
                cpu_limit.lower_current_thread()
            except Exception:  # noqa: BLE001
                pass
            job.status = "running"
            job.started_at = time.time()
            job.updated_at = job.started_at
            job.message = ""
            self._persist(job)
            try:
                fn(job)
                if job.cancelled:
                    job.status = "cancelled"
                elif job.status != "error":
                    job.status = "done"
                    job.progress = 1.0
            except Exception as e:  # noqa: BLE001
                if job.cancelled:
                    job.status = "cancelled"
                else:
                    job.status = "error"
                    job.error = str(e)
                    logger.warning("job %s (%s) failed: %s",
                                   job.id, job.tool_id, e)
            finally:
                job.updated_at = time.time()
                # 自動存入送出者的工作區（工作區停用 / 額度不足時只記原因，
                # 不影響作業本身的成敗）—— 要在 persist 之前做，結果才會一起
                # 寫進 DB，使用者重新整理就看得到。
                self._autosave(job)
                self._persist(job)
                self._notify(job)
        finally:
            with self._lock:
                self._fns.pop(job_id, None)
                self._subprocs.pop(job_id, None)
            _current_job_id.set(None)
            self._finish_slot(job_id)

    def _finish_slot(self, job_id: str) -> None:
        with self._lock:
            self._running.discard(job_id)
        self._trim_memory()
        self._dispatch()

    def _autosave(self, job: Job) -> None:
        try:
            from . import job_autosave
            res = job_autosave.on_job_finished(job)
            if res is not None:
                job.meta = dict(job.meta or {})
                job.meta["workspace"] = res
        except Exception as e:  # noqa: BLE001 — 存檔失敗不該讓轉換變成失敗
            logger.warning("job %s autosave failed: %s", job.id, e)

    def _notify(self, job: Job) -> None:
        """工作結束時送通知（管道未設定就是 no-op）。"""
        try:
            from . import job_notify
            job_notify.on_job_finished(job)
        except ImportError:
            pass
        except Exception as e:  # noqa: BLE001 — 通知失敗絕不影響工作本身
            logger.warning("job %s notify failed: %s", job.id, e)

    # ---------- 查詢 ----------

    def get(self, job_id: str) -> Optional[Job]:
        job = self._jobs.get(job_id)
        if job is not None:
            return job
        return self._from_store(job_id)

    def _from_store(self, job_id: str) -> Optional[Job]:
        """記憶體沒有就回 DB 撈 —— 重啟後仍要能查狀態、下載既有結果。"""
        try:
            from . import job_store
            row = job_store.get(job_id)
        except Exception:  # noqa: BLE001
            return None
        if not row:
            return None
        job = Job(id=row["id"], tool_id=row["tool_id"],
                  status=row["status"], progress=row["progress"] or 0.0,
                  message=row["message"] or "", error=row["error"],
                  created_at=row["created_at"], updated_at=row["updated_at"],
                  meta=row["meta"] or {},
                  owner_id=row["owner_id"],
                  owner_label=row["owner_label"] or "",
                  client_ip=row["client_ip"] or "")
        if row["result_path"]:
            job.result_path = Path(row["result_path"])
        job.result_filename = row["result_filename"]
        return job

    def cancel(self, job_id: str) -> bool:
        """標記 job 取消。執行中的會在下一個 checkpoint 中止並丟棄結果；還在
        排隊的直接移出佇列。回 True 表示成功標記（存在且尚未結束）。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in TERMINAL:
                return False
            job.cancelled = True
            job.status = "cancelled"
            job.message = "已停止"
            job.updated_at = time.time()
            try:
                self._pending.remove(job_id)
            except ValueError:
                pass          # 已經在跑了 —— 靠 job.cancelled checkpoint 收尾
        self._persist(job)
        self._dispatch()
        return True

    # ---------- 維護 ----------

    def _persist(self, job: Job) -> None:
        try:
            from . import job_store
            job_store.upsert(job)
        except Exception as e:  # noqa: BLE001 — 持久化失敗不該中斷轉檔
            logger.warning("job %s persist failed: %s", job.id, e)

    def _trim_memory(self) -> None:
        """記憶體只留進行中 + 最近完成的（OOM 防線）。"""
        with self._lock:
            if len(self._jobs) <= _MEM_KEEP:
                return
            done = [(j.updated_at, j.id) for j in self._jobs.values()
                    if j.status in TERMINAL and j.id not in self._running]
            done.sort()
            for _, jid in done[:len(self._jobs) - _MEM_KEEP]:
                self._jobs.pop(jid, None)

    def cleanup_expired(self) -> int:
        cutoff = time.time() - settings.job_ttl_seconds
        removed = 0
        with self._lock:
            for jid in list(self._jobs.keys()):
                j = self._jobs[jid]
                if j.updated_at < cutoff and j.status in TERMINAL:
                    if j.result_path and j.result_path.exists():
                        try:
                            j.result_path.unlink()
                        except OSError:
                            pass
                    del self._jobs[jid]
                    removed += 1
        try:
            from . import job_store
            job_store.delete_older_than(cutoff)
        except Exception:  # noqa: BLE001
            pass
        return removed


# ---------- 記憶體准入判斷 ----------

def _ram_allows_start(tool_id: str) -> bool:
    """再開一個這種工作，記憶體撐得住嗎？

    估算值刻意保守：轉檔類（會起 soffice）抓較高，其餘抓較低。取不到記憶體資訊
    時回 True —— 寧可讓它跑，也不要因為讀不到數字就整個服務停擺。
    """
    from . import concurrency_settings as cs
    need_mb = cs.estimated_job_mb(tool_id)
    avail_mb = cs.available_mb()
    if avail_mb is None:
        return True
    return avail_mb - need_mb >= cs.reserve_mb()


job_manager = JobManager()
