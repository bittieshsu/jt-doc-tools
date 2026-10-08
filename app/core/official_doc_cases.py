"""公文撰擬的案件：歷史案件管理（2026-10-08 使用者：「公文撰擬 請參考送件前檢核 加入歷史案件管理」）。

原本一個案件是 `data/temp/` 裡的幾個 `od_<案件編號>_*` 檔，跟著暫存的保留期走 ——
作業結束一天多之後草稿、版本與歸屬紀錄就一個一個被清掉，「我的作業」按開啟得到 410，
也沒有地方看得到自己寫過哪些。現在照「送件前檢核」的做法，每個案件一個目錄：

    data/official_doc_cases/<案件編號 32 碼>/
        meta.json        案件資料：擁有者、文別、名稱、標題、版本數、建立 / 更新時間、刪除記錄
        case.json        建立案件時的輸入（重新產生時換成那一次的）
        result.json      最新的草稿、檢查結果、參考資料
        revisions.json   版本清單

預覽圖與下載用的 `.odt` 仍在暫存（隨時可以重新產生）。

* **擁有者記在 `meta.json`**，不再靠暫存的 `.owners/` 紀錄（那份會被清掉）。
  權限判斷在公文撰擬 router 的 `_require_case()`，規則與 `upload_owner` 相同（認證關閉放行、
  管理員可以讀但寫稽核、其他人只看得到自己的）。
* **刪除是軟刪除**：使用者那邊看不到、開不起來；管理員在清單上看得到（反灰，寫著誰刪的）。
  真的從磁碟移除由保留期清理做（`purge_older_than`）。
* 舊的案件（還在 `data/temp/` 的）照樣讀得到 —— 路徑由 router 的 `_stored()` 先找這裡、
  找不到才看暫存；第一次被打開時整份搬過來（`_adopt_legacy()`）。
* 保留期在「檔案保留 / 清理」（`official_doc_cases_days`，預設一年；已刪除的從刪除那天起算）。
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

from ..config import settings
from . import atomic_json
from .safe_paths import is_uuid_hex

logger = logging.getLogger(__name__)

ROOT_NAME = "official_doc_cases"
#: 案件名稱（使用者自己取的；沒取就顯示草稿的標題）的上限
MAX_NAME_CHARS = 80
#: 清單一次最多列幾件（每件讀一個小 JSON；再多的話請用搜尋）
LIST_LIMIT = 500
_LOCK = threading.Lock()


def root() -> Path:
    return settings.data_dir / ROOT_NAME


def case_dir(case_id: str) -> Path:
    if not is_uuid_hex(case_id):
        raise ValueError("bad case id")
    return root() / case_id


def file(case_id: str, name: str) -> Path:
    if name not in ("meta.json", "case.json", "result.json", "revisions.json"):
        raise ValueError("bad case file")
    return case_dir(case_id) / name


def ensure(case_id: str) -> Path:
    d = case_dir(case_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(p: Path) -> Optional[dict]:
    try:
        got = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def load_meta(case_id: str) -> Optional[dict]:
    if not is_uuid_hex(case_id):
        return None
    return _read(file(case_id, "meta.json"))


def create(case_id: str, *, owner_uid: Optional[int], mode: str) -> dict:
    """新的案件。已經有 `meta.json` 的不覆寫（擁有者不可以被改掉）。"""
    with _LOCK:
        ensure(case_id)
        p = file(case_id, "meta.json")
        old = _read(p)
        if old is not None:
            return old
        now = time.time()
        meta = {"case_id": case_id, "owner_uid": owner_uid, "mode": mode, "name": "",
                "title": "", "revisions": 0, "latest_rev": 0, "issues": {}, "job_id": "",
                "created_at": now, "updated_at": now, "deleted_at": None, "deleted_by": None}
        atomic_json.write_json(p, meta, mode=0o600)
        return meta


def update(case_id: str, **fields: Any) -> Optional[dict]:
    """改案件資料（標題、版本數、文別…）並更新「最後修改」。沒有 `meta.json`（舊的暫存案件）
    回 None —— 那種案件沒有擁有者紀錄，不在這裡憑空建一份（建了就等於重新指定擁有者）。"""
    allowed = {"title", "revisions", "latest_rev", "issues", "mode", "name", "job_id",
               "created_at", "deleted_at", "deleted_by"}
    with _LOCK:
        p = file(case_id, "meta.json")
        meta = _read(p)
        if meta is None:
            return None
        for k, v in fields.items():
            if k in allowed:
                meta[k] = v
        meta["updated_at"] = time.time()
        atomic_json.write_json(p, meta, mode=0o600)
        return meta


def rename(case_id: str, name: str) -> Optional[dict]:
    name = " ".join(str(name or "").split())[:MAX_NAME_CHARS]
    return update(case_id, name=name)


def soft_delete(case_id: str, by: str) -> Optional[dict]:
    return update(case_id, deleted_at=time.time(), deleted_by=by or "")


def restore(case_id: str) -> Optional[dict]:
    return update(case_id, deleted_at=None, deleted_by=None)


def list_cases(*, owner_uid: Optional[int], see_all: bool, include_deleted: bool,
               limit: int = LIST_LIMIT) -> list[dict]:
    """依「最後修改」新到舊。`see_all`：管理員（與認證關閉時）看得到每個人的；
    其他人只看得到自己的，而且看不到已刪除的。"""
    d = root()
    if not d.is_dir():
        return []
    out: list[dict] = []
    for sub in d.iterdir():
        if not sub.is_dir() or not is_uuid_hex(sub.name):
            continue
        meta = _read(sub / "meta.json")
        if meta is None:
            continue
        if not see_all and (owner_uid is None or meta.get("owner_uid") != owner_uid):
            continue
        if meta.get("deleted_at") and not include_deleted:
            continue
        out.append(meta)
    out.sort(key=lambda m: float(m.get("updated_at") or 0), reverse=True)
    return out[:limit]


def purge_older_than(days: int) -> int:
    """保留期清理：最後修改超過 `days` 天的案件整個目錄刪掉；已刪除的案件從刪除那天起算。
    `days <= 0` ＝ 永久保留。回傳刪掉幾件。"""
    if days <= 0:
        return 0
    d = root()
    if not d.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for sub in d.iterdir():
        if not sub.is_dir() or not is_uuid_hex(sub.name):
            continue
        ts = _ts(_read(sub / "meta.json") or {})
        if not ts:
            try:
                ts = sub.stat().st_mtime
            except OSError:
                continue
        if ts < cutoff:
            try:
                shutil.rmtree(sub)
                removed += 1
            except OSError as e:
                logger.warning("official-doc：清不掉過期的案件 %s（%s）", sub.name, type(e).__name__)
    return removed


def _ts(meta: dict) -> float:
    try:
        return float(meta.get("deleted_at") or meta.get("updated_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def stats() -> dict:
    """給「檔案保留 / 清理」頁：佔用空間、件數、最舊一件（依最後修改；已刪除的依刪除時間）。"""
    d = root()
    size, count, oldest = 0, 0, None
    if d.is_dir():
        for sub in d.iterdir():
            if not sub.is_dir() or not is_uuid_hex(sub.name):
                continue
            count += 1
            for f in sub.iterdir():
                try:
                    size += f.stat().st_size
                except OSError:
                    pass
            ts = _ts(_read(sub / "meta.json") or {})
            if ts and (oldest is None or ts < oldest):
                oldest = ts
    return {"size_mb": size / 1024 / 1024, "files": count,
            "oldest_days": None if oldest is None else max(0.0, (time.time() - oldest) / 86400.0)}
