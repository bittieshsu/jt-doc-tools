"""乘車證明整理 — 欄位定義 + per-user 設定 + 顯示格式化。

設計對齊「電子發票處理」：
- 內部儲存永遠正規化（ISO 日期 / 整數金額）；顯示 / 匯出時依 field_formats 套用。
- 每個 user 一份設定（visible_columns / column_order / field_formats / export_labels），
  認證 OFF 時共用 default。
- FIELD_DEFINITIONS 是前後端 single source of truth（前端透過 GET /settings 取得）。
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Optional

_AMOUNT_FORMATS = {
    "default": "plain",
    "options": [
        {"id": "plain", "label": "純數字", "example": "950"},
        {"id": "comma", "label": "千分位", "example": "1,050"},
    ],
}
# 費用預設千分位（報帳金額易讀；使用者要求）。
_FARE_FORMATS = {
    "default": "comma",
    "options": [
        {"id": "comma", "label": "千分位", "example": "1,050"},
        {"id": "plain", "label": "純數字", "example": "1050"},
    ],
}
_DATE_FORMATS = {
    "default": "iso",
    "options": [
        {"id": "iso", "label": "ISO", "example": "2026-06-09"},
        {"id": "slash", "label": "西元 / 斜線", "example": "2026/06/09"},
        {"id": "roc", "label": "民國 / 斜線", "example": "115/06/09"},
        {"id": "roc_chinese", "label": "民國 / 中文", "example": "民國115年06月09日"},
    ],
}

# id / label / default_visible / default_order (+ 可選 formats)。
# 預設只開：日期 / 交通工具 / 來源-目的 / 費用（使用者要求）。
FIELD_DEFINITIONS = [
    {"id": "seq",            "label": "序號",       "default_visible": False, "default_order": 1},
    {"id": "date",           "label": "日期",       "default_visible": True,  "default_order": 2, "formats": _DATE_FORMATS},
    {"id": "transport",      "label": "交通工具",   "default_visible": True,  "default_order": 3},
    {"id": "route",          "label": "來源-目的",  "default_visible": True,  "default_order": 4},
    {"id": "transport_route", "label": "交通工具-來源-目的", "default_visible": False, "default_order": 4.5},
    {"id": "fare",           "label": "費用",       "default_visible": True,  "default_order": 5},
    {"id": "subject",        "label": "科目",       "default_visible": False, "default_order": 5.5},
    {"id": "depart_time",    "label": "出發時間",   "default_visible": False, "default_order": 6},
    {"id": "arrive_time",    "label": "到達時間",   "default_visible": False, "default_order": 7},
    {"id": "origin",         "label": "起站",       "default_visible": False, "default_order": 8},
    {"id": "destination",    "label": "到站",       "default_visible": False, "default_order": 9},
    {"id": "train",          "label": "車種 / 車次", "default_visible": False, "default_order": 10},
    {"id": "ticket_type",    "label": "票種",       "default_visible": False, "default_order": 11},
    {"id": "ticket_no",      "label": "票號 / 卡號", "default_visible": False, "default_order": 12},
    {"id": "amount_untaxed", "label": "銷售額",     "default_visible": False, "default_order": 13},
    {"id": "tax",            "label": "營業稅額",   "default_visible": False, "default_order": 14},
    {"id": "buyer_tax_id",   "label": "統一編號",   "default_visible": False, "default_order": 15},
    {"id": "source_file",    "label": "來源檔案",   "default_visible": False, "default_order": 16},
    {"id": "note",           "label": "備註",       "default_visible": False, "default_order": 17},
]
# 金額欄位不再提供格式切換：畫面一律千分位（易讀）、匯出一律純數字（避免破壞
# CSV 匯入 / 會計軟體）。_AMOUNT_FORMATS / _FARE_FORMATS 保留供相容不再掛到欄位。

_FIELD_DEF_BY_ID = {f["id"]: f for f in FIELD_DEFINITIONS}
VALID_FIELD_IDS = set(_FIELD_DEF_BY_ID)
_AMOUNT_FIELD_IDS = {"fare", "amount_untaxed", "tax"}

DEFAULT_VISIBLE = [f["id"] for f in FIELD_DEFINITIONS if f["default_visible"]]
DEFAULT_ORDER = [f["id"] for f in sorted(FIELD_DEFINITIONS, key=lambda f: f["default_order"])]


# ───────────────────────────── 格式化 ─────────────────────────────

def _fmt_amount(value, format_id: str) -> str:
    if value is None or value == "":
        return ""
    try:
        n = int(value)
    except (ValueError, TypeError):
        return str(value)
    return f"{n:,}" if format_id == "comma" else str(n)


def _fmt_date(value, format_id: str) -> str:
    """ISO 'YYYY-MM-DD' → 各顯示格式。"""
    if not value:
        return ""
    parts = str(value).split("-")
    if len(parts) != 3:
        return str(value)
    try:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return str(value)
    if format_id == "slash":
        return f"{y:04d}/{m:02d}/{d:02d}"
    if format_id == "roc":
        return f"{y - 1911}/{m:02d}/{d:02d}"
    if format_id == "roc_chinese":
        return f"民國{y - 1911}年{m:02d}月{d:02d}日"
    return f"{y:04d}-{m:02d}-{d:02d}"  # iso


def _format_id_for(field_id: str, field_formats: dict) -> Optional[str]:
    if isinstance(field_formats, dict):
        fid = field_formats.get(field_id)
        if fid:
            return fid
    d = _FIELD_DEF_BY_ID.get(field_id)
    fmts = d.get("formats") if d else None
    return fmts["default"] if fmts else None


def apply_format(field_id: str, value, field_formats: Optional[dict] = None) -> str:
    """套用使用者選定格式；未指定則用該欄位預設；無格式欄位原樣回字串。"""
    field_formats = field_formats or {}
    fmt = _format_id_for(field_id, field_formats)
    if field_id in _AMOUNT_FIELD_IDS:
        return _fmt_amount(value, fmt or "plain")
    if field_id == "date":
        return _fmt_date(value, fmt or "iso")
    return "" if value is None else str(value)


# ─────────────────────────── per-user 設定 ───────────────────────────

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


def _settings_dir() -> Path:
    from ...config import settings as app_settings
    return app_settings.data_dir / "transit_proof_settings"


def _settings_path(user: Optional[Any]) -> Path:
    return _settings_dir() / f"{_user_key(user)}.json"


def _default_settings() -> dict:
    return {
        "visible_columns": list(DEFAULT_VISIBLE),
        "column_order": list(DEFAULT_ORDER),
        "field_formats": {},
        "export_labels": {},
    }


def get_settings(user: Optional[Any]) -> dict:
    path = _settings_path(user)
    out = _default_settings()
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                rv = raw.get("visible_columns")
                if isinstance(rv, list):
                    vis = [c for c in rv if c in VALID_FIELD_IDS]
                    if vis:
                        out["visible_columns"] = vis
                ro = raw.get("column_order")
                if isinstance(ro, list):
                    order = [c for c in ro if c in VALID_FIELD_IDS]
                    # 補齊任何新欄位到尾端（跨版本相容）
                    for c in DEFAULT_ORDER:
                        if c not in order:
                            order.append(c)
                    out["column_order"] = order
                rf = raw.get("field_formats")
                if isinstance(rf, dict):
                    out["field_formats"] = {k: v for k, v in rf.items()
                                            if k in VALID_FIELD_IDS and isinstance(v, str)}
                rl = raw.get("export_labels")
                if isinstance(rl, dict):
                    out["export_labels"] = {k: str(v)[:64] for k, v in rl.items()
                                            if k in VALID_FIELD_IDS and isinstance(v, str) and v.strip()}
    except (json.JSONDecodeError, OSError):
        pass
    return out


def save_settings(user: Optional[Any], new: dict) -> dict:
    cur = get_settings(user)
    if isinstance(new.get("visible_columns"), list):
        vis = [c for c in new["visible_columns"] if c in VALID_FIELD_IDS]
        cur["visible_columns"] = vis
    if isinstance(new.get("column_order"), list):
        order = [c for c in new["column_order"] if c in VALID_FIELD_IDS]
        for c in DEFAULT_ORDER:
            if c not in order:
                order.append(c)
        cur["column_order"] = order
    if isinstance(new.get("field_formats"), dict):
        cur["field_formats"] = {k: v for k, v in new["field_formats"].items()
                                if k in VALID_FIELD_IDS and isinstance(v, str)}
    if isinstance(new.get("export_labels"), dict):
        cur["export_labels"] = {k: str(v)[:64] for k, v in new["export_labels"].items()
                                if k in VALID_FIELD_IDS and isinstance(v, str) and v.strip()}
    path = _settings_path(user)
    with _get_lock(_user_key(user)):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    return cur
