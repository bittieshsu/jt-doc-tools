"""事務機取件：事務機連線、共用事務機的成員、掃描紀錄（`data/mfp.sqlite`）。

**每一筆連線都屬於一個擁有者**（`owner_scope`）：
* 使用者自己新增的：啟用認證時是 `u<user_id>`，未啟用認證時是 `local`。所有讀寫都要帶擁有者 ——
  拿別人的 id 查不到、改不到、刪不到（回「不存在」，不透露那筆是別人的）。同一個 IP 不同人各自新增，互不影響。
* **共用事務機**（`shared`，使用者 2026-10-14）：管理員在設定裡建立，再把使用者、群組、組織單位加成成員
  （`mfp_members`，主體的寫法跟權限系統同一套）。成員看得到、可以測試與掃描，不能改名或移除。

目前（Beta、尚未實機驗證）只存設備位址、辨識結果與最近一次連線測試的報告（已遮蔽），
**不存任何帳密** —— 規格書規定「尚未驗證的密碼不永久保存」，而登入要等實機驗證後才做。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import db

#: 每位使用者最多幾台。
MAX_PER_OWNER = 20
#: 存進資料庫的報告上限（位元組）。正常一份只有幾十 KB。
MAX_REPORT_BYTES = 512 * 1024


class StoreError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


DUPLICATE_MESSAGE = "這台事務機（同一個位址與連線方式）已經在你的清單裡"
LIMIT_MESSAGE = "已達每人可新增的事務機上限"
NOT_FOUND_MESSAGE = "找不到這台事務機"


def db_path() -> Path:
    from ..config import settings
    return settings.data_dir / "mfp.sqlite"


def _m1_initial(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mfp_connections (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_scope    TEXT NOT NULL,
            owner_user_id  INTEGER,
            display_name   TEXT NOT NULL,
            host           TEXT NOT NULL,
            port           INTEGER,
            scheme         TEXT,
            vendor         TEXT,
            family         TEXT,
            model          TEXT,
            firmware       TEXT,
            detect_status  TEXT,
            last_test_at   REAL,
            last_reachable INTEGER,
            last_report    TEXT,
            config_version INTEGER NOT NULL DEFAULT 1,
            enabled        INTEGER NOT NULL DEFAULT 1,
            created_at     REAL NOT NULL,
            updated_at     REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_mfp_conn_target
            ON mfp_connections(owner_scope, host, COALESCE(port, 0), COALESCE(scheme, ''));
        CREATE INDEX IF NOT EXISTS idx_mfp_conn_owner
            ON mfp_connections(owner_scope, created_at);
        CREATE INDEX IF NOT EXISTS idx_mfp_conn_user
            ON mfp_connections(owner_user_id);
    """)


def _m2_shared_and_scans(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mfp_members (
            conn_id      INTEGER NOT NULL REFERENCES mfp_connections(id) ON DELETE CASCADE,
            subject_type TEXT NOT NULL,
            subject_key  TEXT NOT NULL,
            added_at     REAL NOT NULL,
            PRIMARY KEY (conn_id, subject_type, subject_key)
        );
        CREATE INDEX IF NOT EXISTS idx_mfp_members_subject ON mfp_members(subject_type, subject_key);
        CREATE TABLE IF NOT EXISTS mfp_scans (
            id                TEXT PRIMARY KEY,
            conn_id           INTEGER,
            owner_scope       TEXT NOT NULL,
            owner_user_id     INTEGER,
            device            TEXT NOT NULL,
            conn_name         TEXT,
            status            TEXT NOT NULL,
            settings          TEXT,
            pages             INTEGER,
            bytes             INTEGER,
            file_name         TEXT,
            result_path       TEXT,
            workspace_file_id TEXT,
            workspace_note    TEXT,
            tls_verified      INTEGER,
            error_code        TEXT,
            error_detail      TEXT,
            created_at        REAL NOT NULL,
            started_at        REAL,
            finished_at       REAL
        );
        CREATE INDEX IF NOT EXISTS idx_mfp_scans_owner ON mfp_scans(owner_scope, created_at);
        CREATE INDEX IF NOT EXISTS idx_mfp_scans_user ON mfp_scans(owner_user_id);
    """)


_MIGRATIONS = [_m1_initial, _m2_shared_and_scans]
_INITED: set[str] = set()
_INIT_LOCK = threading.Lock()


def _conn() -> sqlite3.Connection:
    p = db_path()
    key = str(p)
    if key not in _INITED:
        with _INIT_LOCK:
            if key not in _INITED:
                p.parent.mkdir(parents=True, exist_ok=True)
                db.migrate(p, _MIGRATIONS)
                _INITED.add(key)
    return db.get_conn(p)


#: 共用事務機的擁有者範圍（管理員建立；成員在 `mfp_members`）。
SHARED = "shared"
#: 共用事務機最多幾台。
MAX_SHARED = 100
_OWNER_RE = re.compile(r"^(u\d+|local|shared)$")


def _check_owner(owner: str) -> str:
    if not _OWNER_RE.match(owner or ""):
        raise ValueError("invalid owner scope")
    return owner


def clean_name(name: str, fallback: str) -> str:
    s = "".join(ch for ch in (name or "") if ch.isprintable()).strip()
    return (s or fallback)[:64]


def _report_of(r: sqlite3.Row) -> Optional[dict]:
    try:
        return json.loads(r["last_report"]) if r["last_report"] else None
    except ValueError:
        return None


def _row(r: sqlite3.Row, with_report: bool = False) -> dict[str, Any]:
    d = {k: r[k] for k in r.keys() if k != "last_report"}
    d["last_reachable"] = None if r["last_reachable"] is None else bool(r["last_reachable"])
    d["enabled"] = bool(r["enabled"])
    d["shared"] = r["owner_scope"] == SHARED
    d.pop("owner_scope", None)
    d.pop("owner_user_id", None)
    rep = _report_of(r)
    # 掃描能力（連線測試時讀到的 eSCL 能力表）—— 清單上決定要不要出現「掃描」
    d["escl"] = (rep or {}).get("escl") or None
    if with_report:
        d["last_report"] = rep
    return d


def _report_json(report: Optional[dict]) -> Optional[str]:
    if report is None:
        return None
    s = json.dumps(report, ensure_ascii=False)
    if len(s.encode("utf-8")) > MAX_REPORT_BYTES:
        slim = {k: v for k, v in report.items() if k not in ("attempts",)}
        slim["truncated"] = True
        s = json.dumps(slim, ensure_ascii=False)
    return s


def _fields_from_report(report: Optional[dict]) -> dict[str, Any]:
    d = (report or {}).get("detection") or {}
    summ = (report or {}).get("summary") or {}
    model = d.get("model")
    if not model and not d.get("family"):
        # 不在型號辨識名單裡的（例如 HP、Epson）：eSCL 能力表自己報的型號名稱
        model = ((report or {}).get("escl") or {}).get("model") or None
    return {"vendor": d.get("vendor"), "family": d.get("family"), "model": model,
            "firmware": d.get("firmware"), "detect_status": d.get("status"),
            "last_reachable": 1 if summ.get("reachable") else 0}


def list_for(owner: str) -> list[dict]:
    c = _conn()
    rows = c.execute("SELECT * FROM mfp_connections WHERE owner_scope=? ORDER BY created_at",
                     (_check_owner(owner),)).fetchall()
    return [_row(r) for r in rows]


def get_for(owner: str, conn_id: int, with_report: bool = False) -> Optional[dict]:
    r = _conn().execute("SELECT * FROM mfp_connections WHERE id=? AND owner_scope=?",
                        (int(conn_id), _check_owner(owner))).fetchone()
    return _row(r, with_report) if r else None


def create(owner: str, owner_user_id: Optional[int], host: str, port: Optional[int],
           scheme: Optional[str], display_name: str, report: Optional[dict]) -> dict:
    _check_owner(owner)
    c = _conn()
    now = time.time()
    fields = _fields_from_report(report)
    with db.tx(c):
        n = c.execute("SELECT COUNT(*) FROM mfp_connections WHERE owner_scope=?", (owner,)).fetchone()[0]
        if n >= (MAX_SHARED if owner == SHARED else MAX_PER_OWNER):
            raise StoreError("LIMIT", LIMIT_MESSAGE)
        try:
            cur = c.execute(
                "INSERT INTO mfp_connections(owner_scope, owner_user_id, display_name, host, port, scheme,"
                " vendor, family, model, firmware, detect_status, last_test_at, last_reachable,"
                " last_report, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (owner, owner_user_id, clean_name(display_name, host), host, port, scheme,
                 fields["vendor"], fields["family"], fields["model"], fields["firmware"],
                 fields["detect_status"], now if report else None,
                 fields["last_reachable"] if report else None, _report_json(report), now, now))
        except sqlite3.IntegrityError:
            raise StoreError("DUPLICATE", DUPLICATE_MESSAGE) from None
        new_id = cur.lastrowid
    return get_for(owner, new_id)  # type: ignore[return-value]


def rename(owner: str, conn_id: int, display_name: str) -> Optional[dict]:
    cur = get_for(owner, conn_id)
    if cur is None:
        return None
    c = _conn()
    with db.tx(c):
        c.execute("UPDATE mfp_connections SET display_name=?, updated_at=? WHERE id=? AND owner_scope=?",
                  (clean_name(display_name, cur["host"]), time.time(), int(conn_id), owner))
    return get_for(owner, conn_id)


def save_test(owner: str, conn_id: int, report: dict) -> Optional[dict]:
    if get_for(owner, conn_id) is None:
        return None
    f = _fields_from_report(report)
    c = _conn()
    now = time.time()
    with db.tx(c):
        c.execute("UPDATE mfp_connections SET vendor=?, family=?, model=?, firmware=?, detect_status=?,"
                  " last_test_at=?, last_reachable=?, last_report=?, updated_at=?"
                  " WHERE id=? AND owner_scope=?",
                  (f["vendor"], f["family"], f["model"], f["firmware"], f["detect_status"], now,
                   f["last_reachable"], _report_json(report), now, int(conn_id), owner))
    return get_for(owner, conn_id)


def delete(owner: str, conn_id: int) -> bool:
    c = _conn()
    with db.tx(c):
        n = c.execute("DELETE FROM mfp_connections WHERE id=? AND owner_scope=?",
                      (int(conn_id), _check_owner(owner))).rowcount
    return n > 0


def purge_user(user_id: int) -> int:
    """帳號被刪除時一起清掉他的連線、共用事務機的成員資格、掃描紀錄（規格 §14）。

    **他建立的共用事務機不刪**（那是給別人用的；建立者欄位清掉）。"""
    p = db_path()
    if not p.exists():
        return 0
    c = _conn()
    uid = int(user_id)
    with db.tx(c):
        n = c.execute("DELETE FROM mfp_connections WHERE owner_scope=? OR (owner_user_id=? AND owner_scope<>?)",
                      (f"u{uid}", uid, SHARED)).rowcount
        c.execute("UPDATE mfp_connections SET owner_user_id=NULL WHERE owner_scope=? AND owner_user_id=?",
                  (SHARED, uid))
        c.execute("DELETE FROM mfp_members WHERE subject_type='user' AND subject_key=?", (str(uid),))
        rows = c.execute("SELECT result_path FROM mfp_scans WHERE owner_user_id=? OR owner_scope=?",
                         (uid, f"u{uid}")).fetchall()
        c.execute("DELETE FROM mfp_scans WHERE owner_user_id=? OR owner_scope=?", (uid, f"u{uid}"))
    _remove_result_files([r["result_path"] for r in rows])
    return n


def purge_subject(subject_type: str, subject_key: str) -> int:
    """群組 / 組織單位被刪除時，從所有共用事務機的成員裡拿掉。"""
    p = db_path()
    if not p.exists():
        return 0
    c = _conn()
    with db.tx(c):
        return c.execute("DELETE FROM mfp_members WHERE subject_type=? AND subject_key=?",
                         (str(subject_type), str(subject_key))).rowcount


# --------------------------------------------------------------------------- 看得到哪些

def list_visible(owner: str, subjects: list[tuple[str, str]], *, all_shared: bool = False) -> list[dict]:
    """「我的事務機」：自己新增的＋自己是成員的共用事務機（`all_shared`：未啟用登入時全部共用的都看得到）。"""
    own = list_for(owner)
    c = _conn()
    if all_shared:
        rows = c.execute("SELECT * FROM mfp_connections WHERE owner_scope=? ORDER BY display_name",
                         (SHARED,)).fetchall()
    elif subjects:
        cond = " OR ".join(["(m.subject_type=? AND m.subject_key=?)"] * len(subjects))
        args: list[Any] = [SHARED]
        for st, sk in subjects:
            args += [str(st), str(sk)]
        rows = c.execute(
            "SELECT DISTINCT c.* FROM mfp_connections c JOIN mfp_members m ON m.conn_id=c.id "
            f"WHERE c.owner_scope=? AND ({cond}) ORDER BY c.display_name", args).fetchall()
    else:
        rows = []
    return own + [_row(r) for r in rows]


def get_visible(owner: str, subjects: list[tuple[str, str]], conn_id: int, *,
                all_shared: bool = False, with_report: bool = False) -> Optional[dict]:
    mine = get_for(owner, conn_id, with_report)
    if mine:
        return mine
    r = _conn().execute("SELECT * FROM mfp_connections WHERE id=? AND owner_scope=?",
                        (int(conn_id), SHARED)).fetchone()
    if not r:
        return None
    if all_shared or is_member(int(conn_id), subjects):
        return _row(r, with_report)
    return None


def is_member(conn_id: int, subjects: list[tuple[str, str]]) -> bool:
    if not subjects:
        return False
    cond = " OR ".join(["(subject_type=? AND subject_key=?)"] * len(subjects))
    args: list[Any] = [int(conn_id)]
    for st, sk in subjects:
        args += [str(st), str(sk)]
    return _conn().execute(f"SELECT 1 FROM mfp_members WHERE conn_id=? AND ({cond}) LIMIT 1",
                           args).fetchone() is not None


# --------------------------------------------------------------------------- 共用事務機（管理員）

def list_shared() -> list[dict]:
    c = _conn()
    rows = c.execute("SELECT * FROM mfp_connections WHERE owner_scope=? ORDER BY display_name",
                     (SHARED,)).fetchall()
    counts = {r["conn_id"]: r["n"] for r in c.execute(
        "SELECT conn_id, COUNT(*) AS n FROM mfp_members GROUP BY conn_id").fetchall()}
    out = []
    for r in rows:
        d = _row(r)
        d["member_count"] = counts.get(r["id"], 0)
        out.append(d)
    return out


#: 成員的主體種類（跟權限系統一樣；組織單位的 key 是 DN）。
MEMBER_TYPES = ("user", "group", "ou")
MAX_MEMBERS = 500


def get_members(conn_id: int) -> list[dict]:
    rows = _conn().execute(
        "SELECT subject_type, subject_key FROM mfp_members WHERE conn_id=? ORDER BY subject_type, subject_key",
        (int(conn_id),)).fetchall()
    return [{"type": r["subject_type"], "key": r["subject_key"]} for r in rows]


def set_members(conn_id: int, members: list[tuple[str, str]]) -> list[dict]:
    """整份換掉成員清單（只限共用事務機）。種類不對或太多丟 `ValueError`。"""
    if get_for(SHARED, conn_id) is None:
        raise LookupError("not a shared connection")
    clean: list[tuple[str, str]] = []
    for st, sk in members:
        st, sk = str(st), str(sk).strip()
        if st not in MEMBER_TYPES or not sk or len(sk) > 500:
            raise ValueError("invalid member")
        if st in ("user", "group") and not sk.isdigit():
            raise ValueError("invalid member")
        if (st, sk) not in clean:
            clean.append((st, sk))
    if len(clean) > MAX_MEMBERS:
        raise ValueError("too many members")
    c = _conn()
    now = time.time()
    with db.tx(c):
        c.execute("DELETE FROM mfp_members WHERE conn_id=?", (int(conn_id),))
        c.executemany("INSERT INTO mfp_members(conn_id, subject_type, subject_key, added_at) VALUES (?,?,?,?)",
                      [(int(conn_id), st, sk, now) for st, sk in clean])
    return get_members(conn_id)


# --------------------------------------------------------------------------- 掃描紀錄

#: 每個擁有者保留最近幾筆掃描紀錄。
MAX_SCANS_PER_OWNER = 50


def _scan_row(r: sqlite3.Row) -> dict[str, Any]:
    d = {k: r[k] for k in r.keys()}
    for k in ("settings", "error_detail"):
        try:
            d[k] = json.loads(d[k]) if d[k] else None
        except ValueError:
            d[k] = None
    d["tls_verified"] = None if d.get("tls_verified") is None else bool(d["tls_verified"])
    return d


def create_scan(scan_id: str, *, conn_id: int, owner: str, owner_user_id: Optional[int], device: str,
                conn_name: str, settings: dict, file_name: str) -> dict:
    _check_owner(owner)
    c = _conn()
    with db.tx(c):
        c.execute("INSERT INTO mfp_scans(id, conn_id, owner_scope, owner_user_id, device, conn_name, status,"
                  " settings, file_name, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (scan_id, int(conn_id), owner, owner_user_id, device, (conn_name or "")[:64], "queued",
                   json.dumps(settings, ensure_ascii=False), file_name, time.time()))
        old = c.execute("SELECT id, result_path FROM mfp_scans WHERE owner_scope=? AND status NOT IN"
                        " ('queued','running') ORDER BY created_at DESC LIMIT -1 OFFSET ?",
                        (owner, MAX_SCANS_PER_OWNER)).fetchall()
        if old:
            c.executemany("DELETE FROM mfp_scans WHERE id=?", [(r["id"],) for r in old])
    _remove_result_files([r["result_path"] for r in old])
    return get_scan(owner, scan_id)  # type: ignore[return-value]


_SCAN_FIELDS = {"status", "started_at", "finished_at", "pages", "bytes", "result_path", "workspace_file_id",
                "workspace_note", "error_code", "error_detail", "settings", "tls_verified"}


def update_scan(scan_id: str, **fields) -> None:
    sets, args = [], []
    for k, v in fields.items():
        if k not in _SCAN_FIELDS:
            raise ValueError(k)
        if k in ("settings", "error_detail") and v is not None:
            v = json.dumps(v, ensure_ascii=False)
        if k == "tls_verified" and v is not None:
            v = 1 if v else 0
        sets.append(f"{k}=?")
        args.append(v)
    if not sets:
        return
    c = _conn()
    with db.tx(c):
        c.execute(f"UPDATE mfp_scans SET {', '.join(sets)} WHERE id=?", (*args, scan_id))


def finish_scan(scan_id: str, *, status: str, **fields) -> None:
    update_scan(scan_id, status=status, finished_at=time.time(), **fields)


def get_scan(owner: str, scan_id: str) -> Optional[dict]:
    r = _conn().execute("SELECT * FROM mfp_scans WHERE id=? AND owner_scope=?",
                        (str(scan_id), _check_owner(owner))).fetchone()
    return _scan_row(r) if r else None


def get_scan_any(scan_id: str) -> Optional[dict]:
    r = _conn().execute("SELECT * FROM mfp_scans WHERE id=?", (str(scan_id),)).fetchone()
    return _scan_row(r) if r else None


def list_scans(owner: str, limit: int = 20) -> list[dict]:
    rows = _conn().execute("SELECT * FROM mfp_scans WHERE owner_scope=? ORDER BY created_at DESC LIMIT ?",
                           (_check_owner(owner), max(1, min(int(limit), MAX_SCANS_PER_OWNER)))).fetchall()
    return [_scan_row(r) for r in rows]


def mark_interrupted_scans() -> int:
    """服務重啟：還在排隊或掃描中的（隊伍只在記憶體裡）一律標成中斷。"""
    p = db_path()
    if not p.exists():
        return 0
    c = _conn()
    with db.tx(c):
        return c.execute("UPDATE mfp_scans SET status='error', error_code='SCAN_INTERRUPTED', finished_at=?"
                         " WHERE status IN ('queued','running')", (time.time(),)).rowcount


def _remove_result_files(paths: list) -> None:
    import shutil
    from ..config import settings
    root = (settings.temp_dir / "mfp-scans").resolve()
    for rp in paths:
        if not rp:
            continue
        try:
            d = Path(rp).resolve().parent
            d.relative_to(root)
        except (ValueError, OSError):
            continue
        shutil.rmtree(d, ignore_errors=True)
