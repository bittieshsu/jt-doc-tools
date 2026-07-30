"""工作（job）持久化 —— `data/jobs.sqlite`。

**為什麼需要**：job_manager 原本是純記憶體 dict，行程一重啟（`jtdt update`、
改設定、當機）所有工作紀錄就蒸發：進行中的沒了、已完成的也找不回來 —— 結果檔還
躺在 temp 裡，但沒有任何人知道它的 job_id。19 分鐘的轉檔遇到一次更新就白做。

**記憶體上限是設計前提**（避免 OOM）：

* 這裡是**長期儲存**，記憶體那份只留「進行中 + 最近完成」的少量快取；查詢歷史
  一律走 SQL 加 LIMIT，不把整張表讀進記憶體。
* `meta` 欄位可能塞進預覽圖清單等較大的結構 → 寫入前做大小上限截斷，免得單筆
  job 就吃掉數 MB。

**不存什麼**：結果檔本身（只存路徑，檔案由既有的 TTL 清理機制管）。

重啟後 `running` / `pending` 的列會被標成 `interrupted` —— 執行緒已經不存在，
繼續顯示「轉換中」只會讓使用者一直等一個永遠不會完成的工作。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from . import db as _db

logger = logging.getLogger("app.job_store")

# 單筆 meta 的序列化上限。超過就丟掉 meta 只留下標記 —— job 列表不該因為某個
# 工具塞了一大包預覽資料而把 DB 和記憶體撐爆。
_META_MAX_BYTES = 64 * 1024

# 列表查詢的硬上限。UI 再怎麼要求也不會一次撈出十萬筆。
_LIST_HARD_CAP = 500


def db_path() -> Path:
    from ..config import settings
    return settings.data_dir / "jobs.sqlite"


def _m1_initial(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id              TEXT PRIMARY KEY,
            tool_id         TEXT NOT NULL,
            status          TEXT NOT NULL,
            progress        REAL NOT NULL DEFAULT 0,
            message         TEXT NOT NULL DEFAULT '',
            error           TEXT,
            result_path     TEXT,
            result_filename TEXT,
            owner_id        INTEGER,
            owner_label     TEXT,
            client_ip       TEXT,
            meta            TEXT,
            created_at      REAL NOT NULL,
            updated_at      REAL NOT NULL,
            finished_at     REAL
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_owner
            ON jobs(owner_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_created
            ON jobs(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_status
            ON jobs(status);
    """)


def _m2_metrics(conn: sqlite3.Connection) -> None:
    """資源使用率的時序取樣。

    作業量可以從 jobs 表的起訖時間推導（見 `history()`），但 CPU 與記憶體沒有
    起訖時間可推 —— 只能定期取樣。每分鐘一筆、保留 7 天 = 約 1 萬列，很小。
    刻意放在 jobs.sqlite 而不是另開一個檔：這兩種資料一起看才有意義（「那個時間
    點在跑什麼」），而且共用同一套保留期清理。
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS metrics (
            ts           REAL PRIMARY KEY,
            cpu_pct      REAL,
            mem_used_mb  REAL,
            mem_total_mb REAL,
            running      INTEGER,
            queued       INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts DESC);
    """)


MIGRATIONS = [_m1_initial, _m2_metrics]

# 已結束的狀態（不會再變動）
TERMINAL = ("done", "error", "cancelled", "interrupted")


def init() -> int:
    """建表 + 把上次行程遺留的未完成工作標成 interrupted。"""
    path = db_path()
    final = _db.migrate(path, MIGRATIONS)
    try:
        conn = _db.get_conn(path)
        with _db.tx(conn):
            n = conn.execute(
                "UPDATE jobs SET status='interrupted', "
                "       message='服務重新啟動，作業已中斷', "
                "       updated_at=?, finished_at=? "
                " WHERE status IN ('pending','running')",
                (time.time(), time.time()),
            ).rowcount
        if n:
            logger.info("job store: %d 筆未完成工作標記為 interrupted", n)
    except sqlite3.Error as e:
        logger.warning("job store: 標記中斷工作失敗：%s", e)
    logger.info("job DB ready at %s (schema v%d)", path, final)
    return final


def _dump_meta(meta: Optional[dict]) -> Optional[str]:
    if not meta:
        return None
    try:
        blob = json.dumps(meta, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
    if len(blob.encode("utf-8")) > _META_MAX_BYTES:
        # 保留有用的小欄位，丟掉大的（多半是預覽圖清單）
        small = {k: v for k, v in meta.items()
                 if isinstance(v, (str, int, float, bool)) or v is None}
        small["_truncated"] = True
        try:
            blob = json.dumps(small, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return None
    return blob


def _load_meta(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {}
    except ValueError:
        return {}


def upsert(job: Any) -> None:
    """寫入 / 更新一筆 job。失敗只記 log —— 持久化不該讓轉檔本身失敗。"""
    try:
        conn = _db.get_conn(db_path())
        finished = (job.updated_at
                    if job.status in TERMINAL else None)
        with _db.tx(conn):
            conn.execute(
                """INSERT INTO jobs (id, tool_id, status, progress, message,
                                     error, result_path, result_filename,
                                     owner_id, owner_label, client_ip, meta,
                                     created_at, updated_at, finished_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                        status=excluded.status,
                        progress=excluded.progress,
                        message=excluded.message,
                        error=excluded.error,
                        result_path=excluded.result_path,
                        result_filename=excluded.result_filename,
                        meta=excluded.meta,
                        updated_at=excluded.updated_at,
                        finished_at=excluded.finished_at""",
                (job.id, job.tool_id, job.status, float(job.progress or 0),
                 job.message or "", job.error,
                 str(job.result_path) if job.result_path else None,
                 job.result_filename,
                 getattr(job, "owner_id", None),
                 getattr(job, "owner_label", None),
                 getattr(job, "client_ip", None),
                 _dump_meta(getattr(job, "meta", None)),
                 job.created_at, job.updated_at, finished),
            )
    except sqlite3.Error as e:
        logger.warning("job store: 寫入 %s 失敗：%s", getattr(job, "id", "?"), e)


def get(job_id: str) -> Optional[dict]:
    """單筆查詢（記憶體找不到時的後備，例如重啟後仍要能下載結果）。"""
    try:
        row = _db.fetchone(_db.get_conn(db_path()),
                           "SELECT * FROM jobs WHERE id=?", (job_id,))
    except sqlite3.Error:
        return None
    return _row_to_dict(row) if row else None


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["meta"] = _load_meta(d.get("meta"))
    return d


def list_jobs(*, owner_id: Optional[int] = None, client_ip: str = "",
              active_only: bool = False, tool_id: str = "", limit: int = 100,
              offset: int = 0) -> list[dict]:
    """查詢工作清單。

    * `owner_id` —— 認證啟用時的歸屬（帳號）。
    * `client_ip` —— **認證關閉時**的歸屬。沒有帳號可用，只能靠來源 IP 區分：
      單機自用只有 127.0.0.1（結果完全正確），辦公室內網至少不同電腦看不到彼此
      的。NAT 後面會混在一起，UI 要照實說明，不可當成真正的權限。
    * 兩者都不給 = 不限（管理區用）；呼叫端負責權限判斷。

    limit 一律夾在 _LIST_HARD_CAP 以內，避免一次撈爆記憶體。
    """
    limit = max(1, min(int(limit or 100), _LIST_HARD_CAP))
    offset = max(0, int(offset or 0))
    where, params = [], []
    if owner_id is not None:
        where.append("owner_id=?")
        params.append(int(owner_id))
    if client_ip:
        where.append("client_ip=?")
        params.append(client_ip)
    if active_only:
        where.append("status IN ('pending','running')")
    if tool_id:
        where.append("tool_id=?")
        params.append(tool_id)
    sql = "SELECT * FROM jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    try:
        rows = _db.fetchall(_db.get_conn(db_path()), sql, tuple(params))
    except sqlite3.Error as e:
        logger.warning("job store: 查詢失敗：%s", e)
        return []
    return [_row_to_dict(r) for r in rows]


def count_jobs(*, owner_id: Optional[int] = None, client_ip: str = "",
               active_only: bool = False) -> int:
    where, params = [], []
    if owner_id is not None:
        where.append("owner_id=?")
        params.append(int(owner_id))
    if client_ip:
        where.append("client_ip=?")
        params.append(client_ip)
    if active_only:
        where.append("status IN ('pending','running')")
    sql = "SELECT COUNT(*) FROM jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    try:
        row = _db.fetchone(_db.get_conn(db_path()), sql, tuple(params))
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def record_metrics(cpu_pct, mem_used_mb, mem_total_mb, running, queued) -> None:
    """寫入一筆資源取樣。失敗只記 log —— 監控壞掉不該影響服務。"""
    import time as _t
    try:
        conn = _db.get_conn(db_path())
        with _db.tx(conn):
            conn.execute(
                "INSERT OR REPLACE INTO metrics "
                "(ts, cpu_pct, mem_used_mb, mem_total_mb, running, queued) "
                "VALUES (?,?,?,?,?,?)",
                (round(_t.time()), cpu_pct, mem_used_mb, mem_total_mb,
                 int(running or 0), int(queued or 0)))
    except sqlite3.Error as e:
        logger.debug("job store: 取樣寫入失敗：%s", e)


def metrics_history(hours: int = 24, buckets: int = 96) -> list[dict]:
    """資源使用率歷史（取樣後再依桶取平均與峰值）。

    峰值與平均都給 —— 只看平均會把短暫的尖峰抹平，而「何時是高峰」問的正是尖峰。
    """
    import time as _t
    hours = max(1, min(int(hours or 24), 24 * 30))
    buckets = max(6, min(int(buckets or 96), 480))
    now = _t.time()
    start = now - hours * 3600
    width = (now - start) / buckets
    acc = [{"ts": start + i * width, "n": 0, "cpu_sum": 0.0, "cpu_max": 0.0,
            "mem_sum": 0.0, "mem_max": 0.0, "run_max": 0, "queue_max": 0}
           for i in range(buckets)]
    try:
        rows = _db.fetchall(_db.get_conn(db_path()),
                            "SELECT * FROM metrics WHERE ts >= ? ORDER BY ts",
                            (start,))
    except sqlite3.Error:
        rows = []
    for r in rows:
        i = min(buckets - 1, max(0, int((float(r["ts"]) - start) / width)))
        b = acc[i]
        b["n"] += 1
        b["cpu_sum"] += float(r["cpu_pct"] or 0)
        b["cpu_max"] = max(b["cpu_max"], float(r["cpu_pct"] or 0))
        b["mem_sum"] += float(r["mem_used_mb"] or 0)
        b["mem_max"] = max(b["mem_max"], float(r["mem_used_mb"] or 0))
        b["run_max"] = max(b["run_max"], int(r["running"] or 0))
        b["queue_max"] = max(b["queue_max"], int(r["queued"] or 0))
    out = []
    for b in acc:
        n = b["n"] or 1
        out.append({"ts": b["ts"], "samples": b["n"],
                    "cpu_avg": round(b["cpu_sum"] / n, 1),
                    "cpu_max": round(b["cpu_max"], 1),
                    "mem_avg_mb": round(b["mem_sum"] / n, 1),
                    "mem_max_mb": round(b["mem_max"], 1),
                    "running_max": b["run_max"], "queued_max": b["queue_max"]})
    return out


def history(hours: int = 24, buckets: int = 96) -> list[dict]:
    """作業量的歷史分佈 —— 用來看「什麼時候是高峰」。

    **不需要另外存時序資料**：每筆作業都有 `created_at` / `finished_at`，把時間
    軸切成固定寬度的桶，再算每個桶內「有幾筆作業處於執行中」與「有幾筆被建立」
    即可。另存一份取樣資料會多一套寫入與清理，而且取樣間隔一長就會漏掉短暫的
    尖峰 —— 從起訖時間推導反而更精確。

    回傳每個桶：{ts, started（該桶新建立的）, active（該桶內曾在執行的）}。
    """
    import time as _t
    hours = max(1, min(int(hours or 24), 24 * 30))
    buckets = max(6, min(int(buckets or 96), 480))
    now = _t.time()
    start = now - hours * 3600
    width = (now - start) / buckets
    out = [{"ts": start + i * width, "started": 0, "active": 0}
           for i in range(buckets)]
    try:
        rows = _db.fetchall(
            _db.get_conn(db_path()),
            "SELECT created_at, finished_at, updated_at FROM jobs "
            " WHERE COALESCE(finished_at, updated_at) >= ? OR created_at >= ?",
            (start, start))
    except sqlite3.Error as e:
        logger.warning("job store: 歷史查詢失敗：%s", e)
        return out
    for r in rows:
        c = float(r["created_at"] or 0)
        f = float(r["finished_at"] or r["updated_at"] or now)
        if c >= start:
            i = min(buckets - 1, max(0, int((c - start) / width)))
            out[i]["started"] += 1
        # 這筆作業「活著」的區間覆蓋到哪些桶
        lo = min(buckets - 1, max(0, int((max(c, start) - start) / width)))
        hi = min(buckets - 1, max(0, int((min(f, now) - start) / width)))
        for i in range(lo, hi + 1):
            out[i]["active"] += 1
    return out


def delete_older_than(cutoff_ts: float) -> int:
    """清掉舊紀錄（結果檔由既有 TTL 機制刪，這裡只清 DB 列）。"""
    try:
        conn = _db.get_conn(db_path())
        with _db.tx(conn):
            n = conn.execute(
                "DELETE FROM jobs WHERE updated_at < ? AND status NOT IN "
                "('pending','running')", (float(cutoff_ts),)).rowcount
            # 取樣資料另外保留 7 天（比作業紀錄短，量大但價值隨時間下降很快）
            import time as _t
            conn.execute("DELETE FROM metrics WHERE ts < ?",
                         (_t.time() - 7 * 86400,))
            return n
    except sqlite3.Error as e:
        logger.warning("job store: 清理失敗：%s", e)
        return 0


# ---------- 資源取樣執行緒 ----------

_SAMPLE_INTERVAL = 60.0
_sampler: object = None


def start_sampler() -> None:
    """每分鐘記一筆資源使用率，供管理頁畫歷史圖表（看得出何時是高峰）。

    間隔取 60 秒是刻意的取捨：更密會讓資料量與寫入次數上升，而管理員要看的是
    「哪個時段忙」，不是秒級波動。**作業量另外從 jobs 表推導**（見 `history()`），
    所以就算取樣漏了幾筆，作業的尖峰仍然算得準。
    """
    global _sampler
    import threading
    if _sampler is not None and getattr(_sampler, "is_alive", lambda: False)():
        return

    def _loop() -> None:
        import time as _t
        while True:
            try:
                from . import concurrency_settings as cs
                from .job_manager import job_manager
                cpu = cs.cpu_snapshot()
                total = cs.total_mb()
                avail = cs.available_mb()
                st = job_manager.stats()
                record_metrics(
                    cpu.get("percent"),
                    (total - avail) if (total and avail is not None) else None,
                    total, st.get("running"), st.get("queued"))
            except Exception as e:  # noqa: BLE001 — 監控壞掉不該影響服務
                logger.debug("job store: 取樣失敗：%s", e)
            _t.sleep(_SAMPLE_INTERVAL)

    t = threading.Thread(target=_loop, name="metrics-sampler", daemon=True)
    t.start()
    _sampler = t
