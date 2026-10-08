"""知識庫的儲存層：SQLite（中繼資料、切段、逐字索引、向量）＋ 原檔目錄。

## 放在哪裡

* `data/knowledge/kb.sqlite` —— 資料集、文件版本、切段、FTS5 逐字索引、向量。
* `data/knowledge/files/<版本 id><副檔名>` —— 管理員上傳的原檔。

**不在暫存區**：暫存區兩小時就清，知識庫的原檔要一直留著（重建索引、
換切段規則都要從原檔重新抽字）。也**不在設定備份裡**（見
`tools/check_settings_export_coverage.py` 的豁免理由）。

## 慣例（照 `app/core/db.py`）

WAL、`busy_timeout`、外鍵開著、遷移用 `PRAGMA user_version`。寫入只包在
`db.tx()` 那幾行裡 —— **交易裡不做檔案 I/O、不呼叫嵌入服務**。

## 「世代」計數

每一次會改變檢索結果的寫入（文件狀態、切段、向量、資料集的存取範圍）都把
`kb_meta.gen` 加一。檢索那邊把向量矩陣快取在記憶體裡，用這個數字判斷要不要
重新載入 —— 比每次查詢都整份讀出來省，也不會拿到過期的矩陣。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from .. import db
from ...config import settings
from ...logging_setup import get_logger

logger = get_logger(__name__)

#: 資料目錄底下的子目錄與檔名。**設定備份的涵蓋檢查會掃 `data_dir / "…"`**，
#: 改名要一起改 `tools/check_settings_export_coverage.py` 的豁免清單。
_DB_FILENAME = "kb.sqlite"
_FILES_DIRNAME = "files"


def kb_dir() -> Path:
    return settings.data_dir / "knowledge"


def files_dir() -> Path:
    return kb_dir() / _FILES_DIRNAME


def db_path() -> Path:
    return kb_dir() / _DB_FILENAME


# ---------------------------------------------------------------- 類別與用途
#: 資料集類別 → (顯示名稱, 用途)。**用途由類別決定**，不讓管理員另外選 ——
#: 「把寫作範例當成法規依據」是規格裡明文禁止的事，分開兩個欄位就有機會被設錯。
CATEGORIES: dict[str, tuple[str, str]] = {
    "writing_rules": ("文書規範", "format_reference"),
    "agency_rules": ("機關規定", "format_reference"),
    "business_law": ("業務法規", "substantive_basis"),
    "examples": ("公文範例", "style_example"),
}

#: 用途 → 顯示名稱。
PURPOSES: dict[str, str] = {
    "format_reference": "格式與用語參考",
    "substantive_basis": "業務依據",
    "style_example": "寫作範例（不是依據）",
}

#: 文件版本的狀態。`uploaded → indexing → ready → active`；失敗 `failed`、停用 `inactive`。
STATUSES = ("uploaded", "indexing", "ready", "active", "failed", "inactive")

#: 文件的副檔名白名單。
ALLOWED_EXTS = (".pdf", ".docx", ".odt", ".txt", ".md")

#: 欄位長度上限（存檔前截斷會讓人以為存進去了，所以超過一律拒絕）。
MAX_NAME = 100
MAX_DESC = 1000
MAX_TITLE = 200
MAX_URL = 1000
MAX_LABEL = 100

_UUID_RE = re.compile(r"^[0-9a-f]{32}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class KBError(ValueError):
    """管理操作被拒絕的原因（訊息是給管理員看的固定句子）。"""


class KBNotFound(KBError):
    """找不到（或無權存取 —— 兩者刻意不分，不透露存在與否）。"""


def is_id(s: object) -> bool:
    return isinstance(s, str) and bool(_UUID_RE.match(s))


def new_id() -> str:
    return secrets.token_hex(16)


# ---------------------------------------------------------------- 連線與遷移
_INIT_LOCK = threading.Lock()
_INITED: set[str] = set()


def _m1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS kb_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kb_datasets (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            category    TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            access      TEXT NOT NULL DEFAULT 'all',
            enabled     INTEGER NOT NULL DEFAULT 1,
            created_at  REAL NOT NULL,
            created_by  TEXT NOT NULL DEFAULT '',
            updated_at  REAL NOT NULL,
            updated_by  TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS kb_dataset_groups (
            dataset_id TEXT NOT NULL REFERENCES kb_datasets(id) ON DELETE CASCADE,
            group_id   INTEGER NOT NULL,
            PRIMARY KEY (dataset_id, group_id)
        );
        CREATE TABLE IF NOT EXISTS kb_versions (
            id              TEXT PRIMARY KEY,
            dataset_id      TEXT NOT NULL REFERENCES kb_datasets(id) ON DELETE CASCADE,
            title           TEXT NOT NULL,
            filename        TEXT NOT NULL,
            ext             TEXT NOT NULL,
            stored_name     TEXT NOT NULL,
            size            INTEGER NOT NULL,
            sha256          TEXT NOT NULL,
            source_url      TEXT NOT NULL DEFAULT '',
            publisher       TEXT NOT NULL DEFAULT '',
            version_label   TEXT NOT NULL DEFAULT '',
            published_on    TEXT NOT NULL DEFAULT '',
            effective_on    TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL,
            error           TEXT NOT NULL DEFAULT '',
            warning         TEXT NOT NULL DEFAULT '',
            note            TEXT NOT NULL DEFAULT '',
            page_count      INTEGER NOT NULL DEFAULT 0,
            chars           INTEGER NOT NULL DEFAULT 0,
            chunk_count     INTEGER NOT NULL DEFAULT 0,
            chunker_version TEXT NOT NULL DEFAULT '',
            uploaded_by     TEXT NOT NULL DEFAULT '',
            uploaded_at     REAL NOT NULL,
            activated_by    TEXT NOT NULL DEFAULT '',
            activated_at    REAL,
            updated_at      REAL NOT NULL,
            job_id          TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_kb_versions_ds ON kb_versions(dataset_id, status);
        -- 同一個資料集裡同一份內容只放一次（重複上傳由程式先擋並給訊息；
        -- 這條是同時兩次上傳時的最後一道）
        CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_versions_sha ON kb_versions(dataset_id, sha256);
        -- `rid` 是 INTEGER PRIMARY KEY（＝rowid 的別名），**VACUUM 不會重編**；
        -- FTS 的 rowid 對的就是它。用隱含的 rowid 的話 VACUUM 之後會對錯段落。
        CREATE TABLE IF NOT EXISTS kb_chunks (
            rid          INTEGER PRIMARY KEY,
            id           TEXT NOT NULL UNIQUE,
            version_id   TEXT NOT NULL REFERENCES kb_versions(id) ON DELETE CASCADE,
            seq          INTEGER NOT NULL,
            text         TEXT NOT NULL,
            heading      TEXT NOT NULL DEFAULT '',
            heading_path TEXT NOT NULL DEFAULT '[]',
            parent_ref   TEXT NOT NULL DEFAULT '',
            part         INTEGER NOT NULL DEFAULT 1,
            parts        INTEGER NOT NULL DEFAULT 1,
            page_from    INTEGER,
            page_to      INTEGER,
            chars        INTEGER NOT NULL,
            prev_id      TEXT NOT NULL DEFAULT '',
            next_id      TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_kb_chunks_ver ON kb_chunks(version_id, seq);
        CREATE TABLE IF NOT EXISTS kb_vectors (
            chunk_id    TEXT NOT NULL REFERENCES kb_chunks(id) ON DELETE CASCADE,
            fingerprint TEXT NOT NULL,
            dim         INTEGER NOT NULL,
            vec         BLOB NOT NULL,
            PRIMARY KEY (chunk_id, fingerprint)
        );
        CREATE INDEX IF NOT EXISTS idx_kb_vectors_fp ON kb_vectors(fingerprint);
        """
    )
    if fts5_available(conn):
        conn.executescript(
            "CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5("
            "  body, tokenize='unicode61 remove_diacritics 2');")


def _m2_gov_imports(conn: sqlite3.Connection) -> None:
    """政府公開資料（`kb/gov.py`）匯入的紀錄。**只新增兩張表，不動既有的**。

    * `kb_gov_items`：一部法規 / 一則規則 / 一份釋例 ＝ 一個資料集。穩定識別是
      (來源群組, 代碼)：法規用 pcode（改名也不變）。記最後一次匯入的異動日期，
      「檢查更新」只在日期變了才建新版本。資料集被管理員刪掉時 `dataset_id` 變 NULL
      （下次匯入重建），**不會連帶刪掉這一列**。
    * `kb_gov_versions`：每一個匯入的版本是從哪裡來的 —— 格式（決定怎麼切段）、
      異動日期、授權與顯名文字（**每個版本各存一份**：下一版的顯名可能不同，
      舊版本被引用時要顯示當時的出處）、施行日期說明。版本刪掉時跟著刪。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS kb_gov_items (
            group_id    TEXT NOT NULL,
            item_key    TEXT NOT NULL,
            dataset_id  TEXT REFERENCES kb_datasets(id) ON DELETE SET NULL,
            name        TEXT NOT NULL,
            modified_on TEXT NOT NULL DEFAULT '',
            abolished   INTEGER NOT NULL DEFAULT 0,
            updated_at  REAL NOT NULL,
            PRIMARY KEY (group_id, item_key)
        );
        CREATE TABLE IF NOT EXISTS kb_gov_versions (
            version_id  TEXT PRIMARY KEY REFERENCES kb_versions(id) ON DELETE CASCADE,
            group_id    TEXT NOT NULL,
            item_key    TEXT NOT NULL,
            format      TEXT NOT NULL DEFAULT '',
            modified_on TEXT NOT NULL DEFAULT '',
            license     TEXT NOT NULL DEFAULT '',
            attribution TEXT NOT NULL DEFAULT '',
            notice      TEXT NOT NULL DEFAULT '',
            abolished   INTEGER NOT NULL DEFAULT 0,
            imported_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_kb_gov_versions_item ON kb_gov_versions(group_id, item_key);
        """
    )


MIGRATIONS = [_m1, _m2_gov_imports]


def fts5_available(conn: sqlite3.Connection) -> bool:
    """這個 SQLite 有沒有 FTS5。沒有的話關鍵字檢索退回逐段比對（語料小，撐得住）。"""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp._kb_fts_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS temp._kb_fts_probe")
        return True
    except sqlite3.Error:
        return False


def has_fts(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kb_fts'").fetchone() is not None


def conn() -> sqlite3.Connection:
    """取得目前執行緒的連線；第一次用到時建表，並把**上一個行程**留下的中斷狀態收掉。"""
    path = db_path()
    key = str(path)
    if key not in _INITED:
        with _INIT_LOCK:
            if key not in _INITED:
                path.parent.mkdir(parents=True, exist_ok=True)
                files_dir().mkdir(parents=True, exist_ok=True)
                db.migrate(path, MIGRATIONS)
                _recover_interrupted(db.get_conn(path))
                _INITED.add(key)
    return db.get_conn(path)


#: 服務重啟時被中斷的匯入：留在「處理中」的話管理員永遠等不到結果。
_INTERRUPTED_MSG = "處理到一半時服務重新啟動，請按「重新處理」。"


def _recover_interrupted(c: sqlite3.Connection) -> None:
    """**每個行程第一次開這個資料庫時**呼叫：那時還不可能有這個行程的匯入在跑，
    所以停在 uploaded / indexing 的一定是上一個行程留下的。"""
    with db.tx(c):
        n = c.execute(
            "UPDATE kb_versions SET status='failed', error=?, note='', updated_at=? "
            "WHERE status IN ('uploaded','indexing')", (_INTERRUPTED_MSG, time.time())).rowcount
        st = _meta_get(c, "rebuild_state")
        if st:
            try:
                d = json.loads(st)
            except ValueError:
                d = {}
            if d.get("running"):
                d.update(running=False, error="重建到一半時服務重新啟動，原本的索引沒有動；請重新按「重建索引」。",
                         finished_at=time.time())
                _meta_set(c, "rebuild_state", json.dumps(d, ensure_ascii=False))
        if n:
            _bump_gen(c)
    if n:
        logger.warning("知識庫：%d 份文件在上次服務停止時還在處理，已標成失敗", n)


# ---------------------------------------------------------------- meta / 世代
def _meta_get(c: sqlite3.Connection, key: str) -> Optional[str]:
    r = c.execute("SELECT value FROM kb_meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def _meta_set(c: sqlite3.Connection, key: str, value: str) -> None:
    c.execute("INSERT INTO kb_meta(key, value) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def meta_get(key: str) -> Optional[str]:
    return _meta_get(conn(), key)


def meta_set(key: str, value: str) -> None:
    c = conn()
    with db.tx(c):
        _meta_set(c, key, value)


def delete_meta_prefix_except(prefix: str, keep: str) -> None:
    """刪掉 key 以 `prefix` 開頭的 meta（`keep` 那一筆留著）。"""
    c = conn()
    with db.tx(c):
        c.execute("DELETE FROM kb_meta WHERE substr(key, 1, ?) = ? AND key <> ?",
                  (len(prefix), prefix, keep))


def _bump_gen(c: sqlite3.Connection) -> None:
    c.execute("INSERT INTO kb_meta(key, value) VALUES('gen','1') "
              "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)")


def generation() -> int:
    v = _meta_get(conn(), "gen")
    try:
        return int(v or 0)
    except ValueError:
        return 0


# ---------------------------------------------------------------- 驗證
def _clean_text(s: object, limit: int, field: str, *, required: bool = False,
                multiline: bool = False) -> str:
    if s is None:
        s = ""
    if not isinstance(s, str):
        raise KBError(f"{field}格式不對。")
    s = s.strip()
    if not multiline:
        s = re.sub(r"\s+", " ", s)
    s = _CTRL_RE.sub("", s)
    if required and not s:
        raise KBError(f"{field}不可以空白。")
    if len(s) > limit:
        raise KBError(f"{field}太長（最多 {limit} 字）。")
    return s


def clean_url(s: object) -> str:
    """來源網址**只是記錄**，不會去抓。仍然只收 http(s) —— 它會變成畫面上的連結，
    收了 `javascript:` 就是一個存在資料庫裡的 XSS。"""
    s = _clean_text(s, MAX_URL, "來源網址")
    if not s:
        return ""
    if not re.match(r"^https?://[^\s<>\"']+$", s, re.I):
        raise KBError("來源網址要是 http:// 或 https:// 開頭的網址。")
    return s


def clean_date(s: object, field: str) -> str:
    """日期：`YYYY-MM-DD` 或空白（未知就留空，**不用發布日期代填生效日期**）。"""
    s = _clean_text(s, 10, field)
    if not s:
        return ""
    if not _DATE_RE.match(s):
        raise KBError(f"{field}要寫成 YYYY-MM-DD，或留空。")
    try:
        time.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise KBError(f"{field}不是有效的日期。") from None
    return s


# ---------------------------------------------------------------- 資料集
def _ds_row(c: sqlite3.Connection, r: sqlite3.Row) -> dict:
    cat = r["category"]
    label, purpose = CATEGORIES.get(cat, (cat, "format_reference"))
    groups = [g["group_id"] for g in c.execute(
        "SELECT group_id FROM kb_dataset_groups WHERE dataset_id=? ORDER BY group_id",
        (r["id"],)).fetchall()]
    return {
        "id": r["id"], "name": r["name"], "category": cat, "category_label": label,
        "purpose": purpose, "purpose_label": PURPOSES.get(purpose, purpose),
        "description": r["description"], "access": r["access"], "group_ids": groups,
        "enabled": bool(r["enabled"]),
        "created_at": r["created_at"], "created_by": r["created_by"],
        "updated_at": r["updated_at"], "updated_by": r["updated_by"],
    }


def _validate_dataset(data: dict) -> dict:
    name = _clean_text(data.get("name"), MAX_NAME, "資料集名稱", required=True)
    cat = data.get("category")
    if cat not in CATEGORIES:
        raise KBError("資料集類別不對。")
    desc = _clean_text(data.get("description"), MAX_DESC, "說明", multiline=True)
    access = data.get("access") or "all"
    if access not in ("all", "groups"):
        raise KBError("存取範圍不對。")
    gids_raw = data.get("group_ids") or []
    if not isinstance(gids_raw, list) or len(gids_raw) > 200:
        raise KBError("群組清單格式不對。")
    gids: list[int] = []
    for g in gids_raw:
        try:
            gi = int(g)
        except (TypeError, ValueError):
            raise KBError("群組清單格式不對。") from None
        if gi > 0 and gi not in gids:
            gids.append(gi)
    if access == "groups" and not gids:
        raise KBError("存取範圍選了「指定群組」，至少要選一個群組。")
    return {"name": name, "category": cat, "description": desc, "access": access,
            "group_ids": gids if access == "groups" else [],
            "enabled": bool(data.get("enabled", True))}


def create_dataset(data: dict, *, actor: str = "") -> dict:
    d = _validate_dataset(data or {})
    c = conn()
    did = new_id()
    now = time.time()
    with db.tx(c):
        if c.execute("SELECT 1 FROM kb_datasets WHERE name=?", (d["name"],)).fetchone():
            raise KBError("已經有同名的資料集。")
        c.execute("INSERT INTO kb_datasets(id, name, category, description, access, enabled, "
                  "created_at, created_by, updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (did, d["name"], d["category"], d["description"], d["access"],
                   1 if d["enabled"] else 0, now, actor, now, actor))
        for g in d["group_ids"]:
            c.execute("INSERT INTO kb_dataset_groups(dataset_id, group_id) VALUES (?,?)", (did, g))
        _bump_gen(c)
    return get_dataset(did)


def update_dataset(dataset_id: str, data: dict, *, actor: str = "") -> dict:
    if not is_id(dataset_id):
        raise KBNotFound("找不到這個資料集。")
    d = _validate_dataset(data or {})
    c = conn()
    with db.tx(c):
        if not c.execute("SELECT 1 FROM kb_datasets WHERE id=?", (dataset_id,)).fetchone():
            raise KBNotFound("找不到這個資料集。")
        if c.execute("SELECT 1 FROM kb_datasets WHERE name=? AND id<>?",
                     (d["name"], dataset_id)).fetchone():
            raise KBError("已經有同名的資料集。")
        c.execute("UPDATE kb_datasets SET name=?, category=?, description=?, access=?, enabled=?, "
                  "updated_at=?, updated_by=? WHERE id=?",
                  (d["name"], d["category"], d["description"], d["access"],
                   1 if d["enabled"] else 0, time.time(), actor, dataset_id))
        c.execute("DELETE FROM kb_dataset_groups WHERE dataset_id=?", (dataset_id,))
        for g in d["group_ids"]:
            c.execute("INSERT INTO kb_dataset_groups(dataset_id, group_id) VALUES (?,?)",
                      (dataset_id, g))
        _bump_gen(c)
    return get_dataset(dataset_id)


def get_dataset(dataset_id: str) -> dict:
    if not is_id(dataset_id):
        raise KBNotFound("找不到這個資料集。")
    c = conn()
    r = c.execute("SELECT * FROM kb_datasets WHERE id=?", (dataset_id,)).fetchone()
    if not r:
        raise KBNotFound("找不到這個資料集。")
    return _ds_row(c, r)


def list_datasets_admin() -> list[dict]:
    """管理頁用：全部資料集 ＋ 各狀態的文件數與段數。"""
    c = conn()
    rows = c.execute("SELECT * FROM kb_datasets ORDER BY name").fetchall()
    counts: dict[str, dict] = {}
    for r in c.execute(
            "SELECT dataset_id, status, COUNT(*) AS n, COALESCE(SUM(chunk_count),0) AS ch "
            "FROM kb_versions GROUP BY dataset_id, status").fetchall():
        d = counts.setdefault(r["dataset_id"], {"docs": {}, "active_chunks": 0})
        d["docs"][r["status"]] = r["n"]
        if r["status"] == "active":
            d["active_chunks"] = r["ch"]
    out = []
    for r in rows:
        d = _ds_row(c, r)
        cnt = counts.get(r["id"], {"docs": {}, "active_chunks": 0})
        d["doc_counts"] = cnt["docs"]
        d["docs_total"] = sum(cnt["docs"].values())
        d["active_chunks"] = cnt["active_chunks"]
        out.append(d)
    return out


def delete_dataset(dataset_id: str) -> int:
    """刪掉資料集與裡面每一份文件（含原檔）。回刪掉的文件數。"""
    if not is_id(dataset_id):
        raise KBNotFound("找不到這個資料集。")
    c = conn()
    vids = [r["id"] for r in c.execute(
        "SELECT id FROM kb_versions WHERE dataset_id=?", (dataset_id,)).fetchall()]
    for vid in vids:
        delete_version(vid)
    with db.tx(c):
        n = c.execute("DELETE FROM kb_datasets WHERE id=?", (dataset_id,)).rowcount
        if not n:
            raise KBNotFound("找不到這個資料集。")
        _bump_gen(c)
    return len(vids)


# ---------------------------------------------------------------- 文件版本
_VERSION_COLS = ("id", "dataset_id", "title", "filename", "ext", "size", "sha256", "source_url",
                 "publisher", "version_label", "published_on", "effective_on", "status", "error",
                 "warning", "note", "page_count", "chars", "chunk_count", "chunker_version",
                 "uploaded_by", "uploaded_at", "activated_by", "activated_at", "updated_at",
                 "job_id")


def _ver_row(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in _VERSION_COLS}


def stored_path(version: dict) -> Path:
    """原檔在磁碟上的位置。檔名一律是**我們產生的** `<版本 id><白名單副檔名>`。"""
    vid, ext = version["id"], version["ext"]
    if not is_id(vid) or ext not in ALLOWED_EXTS:
        raise KBNotFound("找不到這份文件。")
    return files_dir() / f"{vid}{ext}"


def find_duplicate(dataset_id: str, sha256: str) -> Optional[dict]:
    r = conn().execute("SELECT * FROM kb_versions WHERE dataset_id=? AND sha256=?",
                       (dataset_id, sha256)).fetchone()
    return _ver_row(r) if r else None


def create_version(dataset_id: str, *, title: str, filename: str, ext: str, data: bytes,
                   sha256: str, meta: dict, actor: str = "") -> dict:
    """存原檔並建一筆 `uploaded` 的版本。重複內容（同資料集同 sha256）丟 `KBError`。"""
    get_dataset(dataset_id)
    if ext not in ALLOWED_EXTS:
        raise KBError("不支援的檔案格式。")
    title = _clean_text(title, MAX_TITLE, "文件標題", required=True)
    filename = _clean_text(filename, 255, "檔名", required=True)
    m = {
        "source_url": clean_url(meta.get("source_url")),
        "publisher": _clean_text(meta.get("publisher"), MAX_NAME, "發布機關"),
        "version_label": _clean_text(meta.get("version_label"), MAX_LABEL, "版本標籤"),
        "published_on": clean_date(meta.get("published_on"), "發布日期"),
        "effective_on": clean_date(meta.get("effective_on"), "生效日期"),
    }
    vid = new_id()
    p = files_dir() / f"{vid}{ext}"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".part")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)
    c = conn()
    now = time.time()
    try:
        with db.tx(c):
            dup = c.execute("SELECT title FROM kb_versions WHERE dataset_id=? AND sha256=?",
                            (dataset_id, sha256)).fetchone()
            if dup:
                raise KBError("這個資料集裡已經有內容完全相同的文件，不重複建立。")
            c.execute(
                "INSERT INTO kb_versions(id, dataset_id, title, filename, ext, stored_name, size, "
                "sha256, source_url, publisher, version_label, published_on, effective_on, status, "
                "uploaded_by, uploaded_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (vid, dataset_id, title, filename, ext, p.name, len(data), sha256,
                 m["source_url"], m["publisher"], m["version_label"], m["published_on"],
                 m["effective_on"], "uploaded", actor, now, now))
    except BaseException:
        try:
            p.unlink()
        except OSError:
            pass
        raise
    return get_version(vid)


def get_version(version_id: str) -> dict:
    if not is_id(version_id):
        raise KBNotFound("找不到這份文件。")
    r = conn().execute("SELECT * FROM kb_versions WHERE id=?", (version_id,)).fetchone()
    if not r:
        raise KBNotFound("找不到這份文件。")
    return _ver_row(r)


def list_versions(dataset_id: str) -> list[dict]:
    get_dataset(dataset_id)
    rows = conn().execute(
        "SELECT * FROM kb_versions WHERE dataset_id=? ORDER BY uploaded_at DESC",
        (dataset_id,)).fetchall()
    return [_ver_row(r) for r in rows]


def update_version_meta(version_id: str, data: dict) -> dict:
    v = get_version(version_id)
    title = _clean_text(data.get("title", v["title"]), MAX_TITLE, "文件標題", required=True)
    vals = (
        title,
        clean_url(data.get("source_url", v["source_url"])),
        _clean_text(data.get("publisher", v["publisher"]), MAX_NAME, "發布機關"),
        _clean_text(data.get("version_label", v["version_label"]), MAX_LABEL, "版本標籤"),
        clean_date(data.get("published_on", v["published_on"]), "發布日期"),
        clean_date(data.get("effective_on", v["effective_on"]), "生效日期"),
        time.time(), version_id,
    )
    c = conn()
    with db.tx(c):
        c.execute("UPDATE kb_versions SET title=?, source_url=?, publisher=?, version_label=?, "
                  "published_on=?, effective_on=?, updated_at=? WHERE id=?", vals)
        _bump_gen(c)
    return get_version(version_id)


def set_status(version_id: str, status: str, *, error: str = "", note: str = "",
               warning: Optional[str] = None, job_id: Optional[str] = None,
               activated_by: Optional[str] = None) -> None:
    if status not in STATUSES:
        raise ValueError(status)
    c = conn()
    now = time.time()
    with db.tx(c):
        sets = ["status=?", "error=?", "note=?", "updated_at=?"]
        vals: list = [status, error, note, now]
        if warning is not None:
            sets.append("warning=?")
            vals.append(warning)
        if job_id is not None:
            sets.append("job_id=?")
            vals.append(job_id)
        if activated_by is not None:
            sets += ["activated_by=?", "activated_at=?"]
            vals += [activated_by, now]
        vals.append(version_id)
        c.execute("UPDATE kb_versions SET " + ", ".join(sets) + " WHERE id=?", vals)
        _bump_gen(c)


def set_job_id(version_id: str, job_id: str) -> None:
    c = conn()
    with db.tx(c):
        c.execute("UPDATE kb_versions SET job_id=? WHERE id=?", (job_id, version_id))


def set_note(version_id: str, note: str) -> None:
    """進度說明（不改狀態、不動世代 —— 只是給畫面看的）。"""
    c = conn()
    with db.tx(c):
        c.execute("UPDATE kb_versions SET note=?, updated_at=? WHERE id=?",
                  (note[:200], time.time(), version_id))


def transition(version_id: str, *, allowed_from: Iterable[str], to: str,
               activated_by: Optional[str] = None) -> dict:
    """狀態轉換（啟用 / 停用）。目前狀態不在 `allowed_from` 就拒絕。"""
    v = get_version(version_id)
    if v["status"] not in tuple(allowed_from):
        raise KBError("這份文件目前的狀態不能做這個操作。")
    set_status(version_id, to, warning=None, activated_by=activated_by,
               error="", note="")
    return get_version(version_id)


def delete_version(version_id: str) -> None:
    v = get_version(version_id)
    c = conn()
    with db.tx(c):
        if has_fts(c):
            c.execute("DELETE FROM kb_fts WHERE rowid IN "
                      "(SELECT rid FROM kb_chunks WHERE version_id=?)", (version_id,))
        c.execute("DELETE FROM kb_versions WHERE id=?", (version_id,))
        _bump_gen(c)
    try:
        stored_path(v).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- 切段
def replace_chunks(version_id: str, chunks: list[dict], *, chunker_version: str,
                   page_count: int, chars: int) -> list[str]:
    """整份換掉這個版本的切段（含逐字索引）。回新的段落 id（照順序）。

    舊段落的向量跟著外鍵一起刪掉 —— 段落換了，舊向量就不再對應任何東西。
    """
    from .. import cjk_fts
    c = conn()
    ids = [new_id() for _ in chunks]
    with db.tx(c):
        fts = has_fts(c)
        if fts:
            c.execute("DELETE FROM kb_fts WHERE rowid IN "
                      "(SELECT rid FROM kb_chunks WHERE version_id=?)", (version_id,))
        c.execute("DELETE FROM kb_chunks WHERE version_id=?", (version_id,))
        for i, ch in enumerate(chunks):
            cur = c.execute(
                "INSERT INTO kb_chunks(id, version_id, seq, text, heading, heading_path, "
                "parent_ref, part, parts, page_from, page_to, chars, prev_id, next_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ids[i], version_id, i, ch["text"], ch.get("heading", ""),
                 json.dumps(ch.get("heading_path") or [], ensure_ascii=False),
                 ch.get("parent_ref", ""), ch.get("part", 1), ch.get("parts", 1),
                 ch.get("page_from"), ch.get("page_to"), len(ch["text"]),
                 ids[i - 1] if i > 0 else "", ids[i + 1] if i + 1 < len(ids) else ""))
            if fts:
                c.execute("INSERT INTO kb_fts(rowid, body) VALUES (?,?)",
                          (cur.lastrowid, cjk_fts.tokenize(index_text(ch))))
        c.execute("UPDATE kb_versions SET chunk_count=?, chunker_version=?, page_count=?, chars=?, "
                  "updated_at=? WHERE id=?",
                  (len(chunks), chunker_version, page_count, chars, time.time(), version_id))
        _bump_gen(c)
    return ids


def index_text(ch: dict) -> str:
    """關鍵字與向量索引用的文字：上層標題 ＋ 段落本文。

    上層標題要進索引 —— 「簽的段落有哪些」這種問題，關鍵詞常常只在章節標題裡。
    """
    head = (ch.get("heading") or "").strip()
    return (head + "\n" + ch["text"]) if head else ch["text"]


_CHUNK_COLS = ("id", "version_id", "seq", "text", "heading", "heading_path", "parent_ref",
               "part", "parts", "page_from", "page_to", "chars", "prev_id", "next_id")


def chunk_row(r: sqlite3.Row) -> dict:
    d = {k: r[k] for k in _CHUNK_COLS}
    try:
        d["heading_path"] = json.loads(d["heading_path"] or "[]")
    except ValueError:
        d["heading_path"] = []
    return d


def list_chunks(version_id: str, *, limit: int = 20, offset: int = 0) -> list[dict]:
    rows = conn().execute(
        "SELECT * FROM kb_chunks WHERE version_id=? ORDER BY seq LIMIT ? OFFSET ?",
        (version_id, max(1, min(int(limit), 500)), max(0, int(offset)))).fetchall()
    return [chunk_row(r) for r in rows]


def get_chunk_row(chunk_id: str) -> Optional[dict]:
    if not is_id(chunk_id):
        return None
    r = conn().execute("SELECT * FROM kb_chunks WHERE id=?", (chunk_id,)).fetchone()
    return chunk_row(r) if r else None


# ---------------------------------------------------------------- 向量
def put_vectors(fingerprint: str, items: list[tuple[str, bytes]], dim: int) -> None:
    c = conn()
    with db.tx(c):
        c.executemany(
            "INSERT INTO kb_vectors(chunk_id, fingerprint, dim, vec) VALUES (?,?,?,?) "
            "ON CONFLICT(chunk_id, fingerprint) DO UPDATE SET dim=excluded.dim, vec=excluded.vec",
            [(cid, fingerprint, dim, blob) for cid, blob in items])
        _bump_gen(c)


def delete_vectors(fingerprint: str) -> int:
    c = conn()
    with db.tx(c):
        n = c.execute("DELETE FROM kb_vectors WHERE fingerprint=?", (fingerprint,)).rowcount
        _bump_gen(c)
    return n


def delete_vectors_except(fingerprint: str) -> int:
    c = conn()
    with db.tx(c):
        n = c.execute("DELETE FROM kb_vectors WHERE fingerprint<>?", (fingerprint,)).rowcount
        _bump_gen(c)
    return n


def count_vectors(fingerprint: str) -> int:
    return conn().execute("SELECT COUNT(*) FROM kb_vectors WHERE fingerprint=?",
                          (fingerprint,)).fetchone()[0]


# ---------------------------------------------------------------- 政府公開資料
# 給 `kb/gov.py` 用：哪一部法規對到哪個資料集、每個版本從哪裡來（見 `_m2_gov_imports`）。
_GOV_VERSION_COLS = ("version_id", "group_id", "item_key", "format", "modified_on", "license",
                     "attribution", "notice", "abolished", "imported_at")
_GOV_ITEM_COLS = ("group_id", "item_key", "dataset_id", "name", "modified_on", "abolished",
                  "updated_at")


def _gov_item_row(r: sqlite3.Row) -> dict:
    d = {k: r[k] for k in _GOV_ITEM_COLS}
    d["abolished"] = bool(d["abolished"])
    return d


def gov_items(group_id: str) -> dict[str, dict]:
    """這個來源群組匯入過的項目：代碼 → 紀錄。"""
    rows = conn().execute("SELECT * FROM kb_gov_items WHERE group_id=?", (group_id,)).fetchall()
    return {r["item_key"]: _gov_item_row(r) for r in rows}


def gov_item(group_id: str, item_key: str) -> Optional[dict]:
    r = conn().execute("SELECT * FROM kb_gov_items WHERE group_id=? AND item_key=?",
                       (group_id, item_key)).fetchone()
    return _gov_item_row(r) if r else None


def put_gov_item(group_id: str, item_key: str, *, dataset_id: Optional[str], name: str,
                 modified_on: str, abolished: bool) -> None:
    c = conn()
    with db.tx(c):
        c.execute(
            "INSERT INTO kb_gov_items(group_id, item_key, dataset_id, name, modified_on, abolished, "
            "updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(group_id, item_key) DO UPDATE SET "
            "dataset_id=excluded.dataset_id, name=excluded.name, modified_on=excluded.modified_on, "
            "abolished=excluded.abolished, updated_at=excluded.updated_at",
            (group_id, item_key, dataset_id, name[:MAX_TITLE], modified_on, 1 if abolished else 0,
             time.time()))


def put_gov_version(version_id: str, *, group_id: str, item_key: str, fmt: str,
                    modified_on: str, license: str, attribution: str, notice: str = "",
                    abolished: bool = False) -> None:
    c = conn()
    with db.tx(c):
        c.execute(
            "INSERT OR REPLACE INTO kb_gov_versions(version_id, group_id, item_key, format, "
            "modified_on, license, attribution, notice, abolished, imported_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (version_id, group_id, item_key, fmt, modified_on, license[:200],
             attribution[:2000], notice[:2000], 1 if abolished else 0, time.time()))


def gov_version(version_id: str) -> Optional[dict]:
    if not is_id(version_id):
        return None
    r = conn().execute("SELECT * FROM kb_gov_versions WHERE version_id=?",
                       (version_id,)).fetchone()
    if not r:
        return None
    d = {k: r[k] for k in _GOV_VERSION_COLS}
    d["abolished"] = bool(d["abolished"])
    return d


def gov_versions_of_dataset(dataset_id: str) -> dict[str, dict]:
    """這個資料集裡由政府公開資料匯入的版本：版本 id → 來源紀錄（管理頁顯示出處用）。"""
    rows = conn().execute(
        "SELECT g.* FROM kb_gov_versions g JOIN kb_versions v ON v.id=g.version_id "
        "WHERE v.dataset_id=?", (dataset_id,)).fetchall()
    out = {}
    for r in rows:
        d = {k: r[k] for k in _GOV_VERSION_COLS}
        d["abolished"] = bool(d["abolished"])
        out[r["version_id"]] = d
    return out


def gov_item_versions(group_id: str, item_key: str) -> list[dict]:
    """一個項目匯入過的每一個版本（新的在前），帶著版本的狀態。"""
    rows = conn().execute(
        "SELECT g.version_id, g.modified_on, g.abolished, v.status, v.dataset_id "
        "FROM kb_gov_versions g JOIN kb_versions v ON v.id=g.version_id "
        "WHERE g.group_id=? AND g.item_key=? ORDER BY g.imported_at DESC",
        (group_id, item_key)).fetchall()
    return [dict(r) for r in rows]


def gov_formats() -> dict[str, str]:
    """版本 id → 切段格式（只列有特殊格式的；一般上傳的文件不在裡面）。"""
    rows = conn().execute(
        "SELECT version_id, format FROM kb_gov_versions WHERE format<>''").fetchall()
    return {r["version_id"]: r["format"] for r in rows}


def gov_format(version_id: str) -> str:
    r = conn().execute("SELECT format FROM kb_gov_versions WHERE version_id=?",
                       (version_id,)).fetchone()
    return (r["format"] or "") if r else ""


def set_dataset_description(dataset_id: str, description: str, *, actor: str = "") -> None:
    """只改說明（政府公開資料標「已廢止」用；其他欄位是管理員的設定，不動）。"""
    desc = _clean_text(description, MAX_DESC, "說明", multiline=True)
    c = conn()
    with db.tx(c):
        c.execute("UPDATE kb_datasets SET description=?, updated_at=?, updated_by=? WHERE id=?",
                  (desc, time.time(), actor, dataset_id))


def dataset_name_taken(name: str) -> bool:
    return conn().execute("SELECT 1 FROM kb_datasets WHERE name=?", (name,)).fetchone() is not None
