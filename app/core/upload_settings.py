"""應用層的**全域上傳上限**。

## 為什麼需要

原本大小完全靠反向代理擋（doc.jason.tools 量到 300 MB），但**直連應用程式埠
就沒有任何限制** —— 而內網直連是很常見的部署方式（CLAUDE.md 早就記著這件事，
`upload_limits.app_side_limits()` 裡那一筆 `app_global` 也一直寫著「目前沒有」）。

沒有上限時，任何能連到服務的人送一個超大 body 就能吃光磁碟與記憶體，
**不需要任何帳號**（未登入的請求也會先被讀進來才被擋）。

## 判準

* 預設 **500 MB** —— 遠高於任何正常用途（工具自己的上限多在 200 MB 以下，
  浮水印那種大批次也已改成逐檔上傳），但擋得住「隨手丟一個 10 GB」。
* **0 = 不限**（給真的需要的部署留一條路，但要管理員自己明確設定）。
* 這是**最外層**的粗篩，不取代各工具自己的上限。
"""
from __future__ import annotations

import json
import threading
from typing import Any

_LOCK = threading.Lock()
_DEFAULTS: dict[str, Any] = {"max_upload_mb": 500}

#: 這份設定被**最外層的中介層每一個請求都讀一次**（連靜態檔與 healthz 都算）
#: —— 沒有快取的話那是每個請求一次同步檔案 I/O，而且就跑在事件迴圈上
#: （實測 104 µs/次；一頁幾十個靜態檔就是好幾毫秒，純浪費）。
#:
#: 依 **mtime + 大小**失效，所以管理員在畫面上改完立刻生效，不必重啟。
#: 這跟字型名稱快取是同一套做法。
_CACHE: tuple[tuple[float, int] | None, dict[str, Any]] | None = None


def _path():
    from ..config import settings
    return settings.data_dir / "upload_settings.json"


def _stamp(p) -> tuple[float, int] | None:
    try:
        st = p.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return None                      # 檔案不存在＝用預設值


def get() -> dict[str, Any]:
    global _CACHE
    p = _path()
    stamp = _stamp(p)
    cached = _CACHE
    if cached is not None and cached[0] == stamp:
        return dict(cached[1])
    out = dict(_DEFAULTS)
    try:
        if stamp is not None:
            got = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(got, dict):
                out.update({k: v for k, v in got.items() if k in _DEFAULTS})
    except (OSError, ValueError):
        pass          # 設定檔壞掉就用預設值，不可以讓整站起不來
    try:
        out["max_upload_mb"] = max(0, int(out["max_upload_mb"]))
    except (TypeError, ValueError):
        out["max_upload_mb"] = _DEFAULTS["max_upload_mb"]
    _CACHE = (stamp, dict(out))
    return out


def save(new: dict[str, Any]) -> dict[str, Any]:
    cur = get()
    if "max_upload_mb" in (new or {}):
        try:
            cur["max_upload_mb"] = max(0, int(new["max_upload_mb"]))
        except (TypeError, ValueError):
            raise ValueError("上傳上限必須是 0 以上的整數（0 = 不限）")
    with _LOCK:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cur, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    return cur


def max_upload_bytes() -> int:
    """0 = 不限。"""
    return get()["max_upload_mb"] * 1024 * 1024
