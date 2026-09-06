"""Per-user 乘車證明暫存清單（JSON 檔）＋ 原始證明檔。

- 認證 ON：每個 user 一個檔 `<data_dir>/transit_proof_buffer/<key>.json`。
- 認證 OFF：共用 `default.json`。
- **原始 PDF**：`<data_dir>/transit_proof_files/<key>/<entry_id>.pdf`。
  歸屬**由路徑結構決定** —— 取檔時只從「當前使用者自己的那個目錄」找，
  所以 A 拿著 B 的 entry_id 也讀不到東西。這比事後檢查 ACL 難寫錯
  （本專案在 fail-open 的 ACL 上吃過虧）。
  檔案與 entry **在同一個鎖裡一起建立、一起刪除**，不會出現有紀錄沒檔案。
- 去重：以 (transport, ticket_no) 或（無票號時）(transport,date,origin,destination,fare)。
- 上限：每人 2000 筆（差旅報帳量級足夠）。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_MAX_ENTRIES_PER_USER = 2000

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _user_key(user: Optional[Any]) -> str:
    if not user:
        return "default"
    if isinstance(user, dict):
        uid = user.get("user_id") or user.get("username")
    else:
        uid = getattr(user, "user_id", None) or getattr(user, "username", None)
    if not uid:
        return "default"
    return hashlib.blake2b(str(uid).encode("utf-8"), digest_size=16).hexdigest()


def _get_lock(key: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.Lock()
        return lk


def _buffer_dir() -> Path:
    from ...config import settings as app_settings
    return app_settings.data_dir / "transit_proof_buffer"


def _files_dir(user: Optional[Any]) -> Path:
    """這位使用者的原始證明目錄。**路徑由使用者身分算出，不吃任何請求參數。**"""
    from ...config import settings as app_settings
    return app_settings.data_dir / "transit_proof_files" / _user_key(user)


def entry_file(user: Optional[Any], entry_id: str) -> Optional[Path]:
    """取某一筆的原始證明（找不到回 None）。

    `entry_id` 只當檔名用，呼叫端必須先驗過格式（十六進位 uuid）；
    再加一層防呆：任何帶路徑分隔或 `..` 的一律拒絕。
    """
    if not entry_id or "/" in entry_id or "\\" in entry_id or ".." in entry_id:
        return None
    p = _files_dir(user) / f"{entry_id}.pdf"
    return p if p.is_file() else None


def _forget_files(user: Optional[Any], ids) -> None:
    """刪掉這些 entry 的原始證明。**每一條刪除路徑都要走這裡** ——
    漏掉一條就會留下永遠沒人清的孤兒檔（而且畫面上看不出來）。"""
    d = _files_dir(user)
    for i in ids:
        if not i:
            continue
        try:
            (d / f"{i}.pdf").unlink(missing_ok=True)
        except OSError:
            pass


def _buffer_path(user: Optional[Any]) -> Path:
    return _buffer_dir() / f"{_user_key(user)}.json"


def _read(path: Path) -> dict:
    if not path.exists():
        return {"entries": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        backup = path.with_suffix(f".corrupt-{int(time.time())}.json")
        try:
            path.rename(backup)
        except OSError:
            pass
        return {"entries": []}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _dedup_key(e: dict) -> str:
    tno = (e.get("ticket_no") or "").strip()
    if tno:
        return f"{e.get('transport','')}|{tno}"
    return "|".join(str(e.get(k, "")) for k in
                    ("transport", "date", "origin", "destination", "fare"))


def list_entries(user: Optional[Any]) -> list[dict]:
    """回該 user 全部乘車證明（最新在前）。"""
    with _get_lock(_user_key(user)):
        data = _read(_buffer_path(user))
    entries = data.get("entries", [])
    return sorted(entries, key=lambda x: x.get("added_at", ""), reverse=True)


def add_entries(user: Optional[Any], parsed: list[dict]) -> dict:
    """加入解析結果。回 {added:[...], duplicates:int, cap_reached:bool}。"""
    if not parsed:
        return {"added": [], "duplicates": 0, "cap_reached": False}
    path = _buffer_path(user)
    now = datetime.now(timezone.utc).isoformat()
    with _get_lock(_user_key(user)):
        data = _read(path)
        entries = data.get("entries", [])
        existing = {_dedup_key(e) for e in entries}
        added, dups, cap = [], 0, False
        for p in parsed:
            k = _dedup_key(p)
            if k in existing:
                dups += 1
                continue
            if len(entries) + len(added) >= _MAX_ENTRIES_PER_USER:
                cap = True
                break
            entry = dict(p)
            entry["id"] = uuid.uuid4().hex
            entry["added_at"] = now
            added.append(entry)
            existing.add(k)
        if added:
            data["entries"] = entries + added
            _write(path, data)
    return {"added": added, "duplicates": dups, "cap_reached": cap}


def update_entry(user: Optional[Any], entry_id: str, fields: dict) -> Optional[dict]:
    """更新一筆的可編輯欄位。回更新後的 entry 或 None（找不到）。"""
    editable = {"transport", "date", "depart_time", "arrive_time", "origin",
                "destination", "fare", "train", "ticket_type", "ticket_no",
                "amount_untaxed", "tax", "buyer_tax_id", "note", "subject"}
    path = _buffer_path(user)
    with _get_lock(_user_key(user)):
        data = _read(path)
        for e in data.get("entries", []):
            if e.get("id") == entry_id:
                for k, v in (fields or {}).items():
                    if k in editable:
                        e[k] = v
                _write(path, data)
                return e
    return None


def delete_entry(user: Optional[Any], entry_id: str) -> bool:
    path = _buffer_path(user)
    with _get_lock(_user_key(user)):
        data = _read(path)
        entries = data.get("entries", [])
        new = [e for e in entries if e.get("id") != entry_id]
        if len(new) == len(entries):
            return False
        data["entries"] = new
        _write(path, data)
        return True


def delete_entries(user: Optional[Any], ids: list[str]) -> int:
    """批次刪除。回實際刪除筆數。"""
    id_set = {str(i) for i in (ids or []) if i}
    if not id_set:
        return 0
    path = _buffer_path(user)
    with _get_lock(_user_key(user)):
        data = _read(path)
        entries = data.get("entries", [])
        new = [e for e in entries if e.get("id") not in id_set]
        removed = len(entries) - len(new)
        if removed:
            data["entries"] = new
            _write(path, data)
    return removed


def clear_all(user: Optional[Any]) -> int:
    path = _buffer_path(user)
    with _get_lock(_user_key(user)):
        data = _read(path)
        n = len(data.get("entries", []))
        _write(path, {"entries": []})
    return n
