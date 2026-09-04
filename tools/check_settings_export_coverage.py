#!/usr/bin/env python3
"""發版前檢查：新加的設定檔有沒有跟上「設定備份 / 匯入」。

為什麼需要這支：加新功能時常常順手在 `data/` 開一個新的設定檔，但
`app/core/settings_export.py` 的 CATEGORIES 是**人工維護**的清單 —— 漏加不會有
任何錯誤訊息、測試也不會紅，只會在客戶搬機還原後才發現「設定怎麼不見了」。

實際發生過（v1.14.6 一次補了 10 項）：

* `sso_settings.json`（v1.12.0 加的 OIDC / SAML）從來沒被匯出，而「認證設定」
  分類的說明卻寫著「OIDC / SAML」—— 使用者勾了、以為有備份，實際沒有。
* `log_forwarders.json` / `retention.json` / `directory_sync.json` /
  `dir_filter.json` / `scheduled_export.json` 等 admin 設定通通不在備份內。

用法：
    python tools/check_settings_export_coverage.py     # 有問題回非 0
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# `data_dir / "x"`、`data_dir / "x" / "y"`（最多兩層，夠涵蓋現有用法）。
# `_TAIL` 吃掉三種寫法的結尾，缺一就會整組漏掉（寫這支時兩種都各踩過一次）：
#   settings.data_dir / ...        → 什麼都不用吃
#   Path(app_settings.data_dir) / ...  → 吃一個 `)`
#   _data_dir() / ...              → 吃一組 `()`
_TAIL = r'data_dir(?:\(\)|\))?\s*/\s*'
_REF_RE = re.compile(_TAIL + r'"([^"/]+)"(?:\s*/\s*"([^"/]+)")?')
# `data_dir / _DB_NAME`（模組層常數）—— 只看字面字串會漏掉。抓到常數名後回同一個
# 檔案找 `_DB_NAME = "..."` 解出實際值。
_CONST_REF_RE = re.compile(_TAIL + r'([A-Z_][A-Za-z0-9_]*)\b')

# 執行期產物 / 刻意不備份的東西。**每一項都要寫明理由** —— 這份白名單是唯一能讓
# 檢查靜音的地方，沒有理由的豁免等於把檢查關掉。
EXEMPT: dict[str, str] = {
    "temp":                 "上傳暫存，2 小時後自動清掉",
    "jobs":                 "工作結果暫存，有 TTL",
    "audit.sqlite":         "稽核記錄，量大且屬本機軌跡（要保留請用記錄轉送）",
    "auth.sqlite":          "使用者帳號 / 密碼 hash，跨機不可攜（RBAC 另有分類）",
    "settings_backups":     "備份檔本身，備份備份沒有意義",
    ".session_secret":      "session 簽章金鑰，外流等於可偽造登入 — 絕不可放進備份",
    "vat_db.sqlite":        "統編資料庫，170 萬筆，由財政部官方來源重新下載即可",
    "vat_db_progress.json": "統編下載進度，執行期狀態",
    "jobs.sqlite":          "工作紀錄，綁本機的暫存結果檔路徑，搬機沒有意義",
    "db_backups":           "資料庫熱備份，備份的備份沒有意義（見 db_health）",
    "logs":                 "服務執行記錄，本機軌跡（要留存請用記錄轉送）",
    "sso_store.sqlite":     "SAML 重放快取 / SLO session 索引，屬本機執行期狀態",
    "llm_model_profiles.json": "模型特性快取，連上 LLM 伺服器後自動重建",
}


def declared_coverage() -> set[str]:
    from app.core.settings_export import CATEGORIES
    cov: set[str] = set()
    for c in CATEGORIES:
        cov |= set(c.get("items") or [])
        cov |= set(c.get("dirs") or [])
    return cov


def code_references() -> dict[str, set[str]]:
    """掃出程式碼中所有 data_dir 下的檔案 / 目錄名 → {名稱: {引用它的檔案}}。"""
    refs: dict[str, set[str]] = {}
    for py in (ROOT / "app").rglob("*.py"):
        try:
            src = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(py.relative_to(ROOT))
        for first, second in _REF_RE.findall(src):
            name = f"{first}/{second}" if second else first
            refs.setdefault(name, set()).add(rel)
        for const in _CONST_REF_RE.findall(src):
            m = re.search(rf'^{re.escape(const)}\s*=\s*"([^"]+)"', src, re.M)
            if m:
                refs.setdefault(m.group(1), set()).add(rel)
    return refs


def is_covered(name: str, cov: set[str]) -> bool:
    """涵蓋判定含「上層目錄」與「子路徑」兩個方向。

    * `submission_check` 被 `submission_check/self_entities` 涵蓋（只備份設定的
      那一層，個別 case 屬工作資料）。
    * `fonts/xxx.ttf` 被 `fonts` 涵蓋（整個目錄都收）。
    """
    if name in cov:
        return True
    return any(c.startswith(name + "/") or name.startswith(c + "/")
               for c in cov)


def main() -> int:
    cov = declared_coverage()
    refs = code_references()
    missing: list[tuple[str, set[str]]] = []
    for name, where in sorted(refs.items()):
        if name.split("/")[0] in EXEMPT or name in EXEMPT:
            continue
        if not is_covered(name, cov):
            missing.append((name, where))

    stale = sorted(k for k in EXEMPT
                   if k not in refs and not any(r.startswith(k + "/")
                                                for r in refs))

    print(f"設定備份分類 {len(declared_coverage())} 項｜"
          f"程式碼引用 {len(refs)} 項｜豁免 {len(EXEMPT)} 項")
    if not missing and not stale:
        print("✓ 所有設定檔都在備份範圍內")
        return 0
    if missing:
        print(f"\n✗ {len(missing)} 個設定檔沒被「設定備份 / 匯入」涵蓋：")
        for name, where in missing:
            print(f"  - {name}    （{', '.join(sorted(where)[:2])}）")
        print("\n修法：把它加進 app/core/settings_export.py 的 CATEGORIES；"
              "若屬執行期產物 / 刻意不備份，加進本檔 EXEMPT 並寫明理由。")
    if stale:
        print(f"\n△ EXEMPT 內有 {len(stale)} 項程式碼已不再引用，可移除："
              f"{', '.join(stale)}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
