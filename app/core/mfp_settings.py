"""事務機掃描取件：管理員設定（`data/mfp_settings.json`）。

* `allow_user_devices`（預設 True，使用者 2026-10-14）：一般使用者可不可以自己新增事務機。
  關掉之後：
  * 一般使用者不能做連線測試、不能儲存新的事務機（回 403 `USER_DEVICES_DISABLED`）；
  * 他們以前自己新增的**暫停使用**（清單上標「已暫停」，不能測試、掃描、取件），資料保留，
    重新打開就恢復；本人仍然可以移除（那是他自己的資料）；
  * 管理員設定的共用事務機照常；管理員自己不受限（未啟用登入時本機那一位就是管理員）。

檔案很小、每次請求讀一次（不快取：測試會換資料目錄，多個行程也不必同步快取）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from . import atomic_json

DEFAULTS: dict[str, Any] = {"allow_user_devices": True, "updated_at": 0.0}
_LOCK = threading.Lock()


def _path() -> Path:
    from ..config import settings
    return settings.data_dir / "mfp_settings.json"


def get() -> dict[str, Any]:
    out = dict(DEFAULTS)
    p = _path()
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            if isinstance(raw.get("allow_user_devices"), bool):
                out["allow_user_devices"] = raw["allow_user_devices"]
            if isinstance(raw.get("updated_at"), (int, float)):
                out["updated_at"] = float(raw["updated_at"])
    return out


def save(patch: dict[str, Any]) -> dict[str, Any]:
    """只收認得的欄位、型別要對（`allow_user_devices` 一定要是 true / false），否則 `ValueError`。"""
    if not isinstance(patch, dict):
        raise ValueError("settings must be an object")
    with _LOCK:
        cur = get()
        if "allow_user_devices" in patch:
            v = patch["allow_user_devices"]
            if not isinstance(v, bool):
                raise ValueError("allow_user_devices must be true or false")
            cur["allow_user_devices"] = v
        cur["updated_at"] = time.time()
        atomic_json.write_json(_path(), cur, mode=0o600)
        return dict(cur)


def allow_user_devices() -> bool:
    return bool(get()["allow_user_devices"])
