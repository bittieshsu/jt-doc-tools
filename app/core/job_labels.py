"""背景作業在「我的作業」、站內通知、作業佇列與通知信上**顯示成哪一支工具**。

## 為什麼要有

作業佇列原本只收工具送出的作業，`tool_id` 一定是註冊表裡的一支工具，顯示名稱
直接查註冊表就好。知識庫（管理區）的匯入、重建索引與政府公開資料下載也走同一個
佇列（關掉分頁照樣跑完、「我的作業」看得到），當初借用了「公文撰擬」的代號 ——
於是管理員收到的通知信寫著「[完成] 公文撰擬：知識庫・全國法規資料庫：下載資料」，
「我的作業」那一列也標成公文撰擬，看起來像公文撰擬出了什麼事（使用者回報）。

## 做法

* 不是工具的作業用自己的代號（`NON_TOOL_JOBS`），名稱與圖示在這裡定義一份，
  清單、通知、通知信、作業佇列都從這裡查 —— 不要在各處各寫一份對照。
* 舊資料（升級前送出的知識庫作業，`tool_id` 是 `official-doc`）靠 `meta["kb"]`
  認出來，顯示時一樣標成知識庫。**資料庫裡存的那一列不改**。
* 這些代號**不是工具**：沒有 `/tools/<代號>/` 的頁面、不進權限矩陣、不會出現在
  側欄或首頁。存取控制照舊由作業的擁有者決定（`_job_access`），跟代號無關。
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

#: 知識庫（管理區）的背景作業：匯入文件、重建索引、政府公開資料。
KB_JOB_ID = "knowledge-base"

#: 不是工具的作業：代號 → 顯示名稱與圖示（`components/icons.html` 的名稱，
#: 跟管理區側欄同一顆）。名稱是語系檔的鍵（前端一律 `tr()`）。
NON_TOOL_JOBS: dict[str, dict[str, str]] = {
    KB_JOB_ID: {"name": "公文知識庫", "icon": "book"},
}

#: 舊的知識庫作業（借用公文撰擬代號時）在 meta 裡留下的種類。
_LEGACY_KB_KINDS = frozenset({"import", "rebuild", "gov"})


def display_id(tool_id: str, meta: Optional[Mapping[str, Any]] = None) -> str:
    """這一列在清單上要當成哪一個代號（決定圖示、名稱與資源標籤）。"""
    tid = str(tool_id or "")
    if (tid == "official-doc" and isinstance(meta, Mapping)
            and meta.get("kb") in _LEGACY_KB_KINDS):
        return KB_JOB_ID
    return tid


def display_name(tool_id: str, meta: Optional[Mapping[str, Any]] = None,
                 names: Optional[Mapping[str, str]] = None) -> str:
    """這一列的「工具」名稱。`names` 是註冊表的 {工具代號: 名稱}（呼叫端已經有就傳進來）。"""
    tid = display_id(tool_id, meta)
    if tid in NON_TOOL_JOBS:
        return NON_TOOL_JOBS[tid]["name"]
    if names is None:
        try:
            from ..tool_registry import discover_tools
            names = {t.metadata.id: t.metadata.name for t in discover_tools()}
        except Exception:  # noqa: BLE001 — 查不到就顯示代號
            names = {}
    return names.get(tid, tid)
