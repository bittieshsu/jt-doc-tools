"""把「目錄裡已經找不到 / AD 端已停用」的帳號停用。

## 為什麼要有這一層

目錄同步是**只增不減**的：AD 那邊把人停用或刪掉之後，本站的帳號、角色指派、
群組成員關係全部還在。那個人登不進來（bind 會失敗），但「這個系統裡還有誰」
這個問題答不出來 —— 內控盤點與離職交接都對不起來。

手動一個一個關，幾百個帳號沒有人會去做；所以要有批次，也要有排程。

## 安全閥（這一段是整個模組的重點）

自動停用是**破壞性**的，而且錯的時候是一次錯一大片：

* service account 的密碼過期 → 同步查不到任何人 → 「全公司都不見了」。
* 有人改了搜尋 base DN → 同一個結果。
* 目錄暫時只回了一部分 → 部分部門集體消失。

所以：

1. **判定只認完整掃描**（`view="missing"` 在 `user_manager` 裡已經強制這一點）。
2. **一次最多動 20%**：超過就整批中止並回報，寧可什麼都不做也不要把公司關掉。
   要真的處理那麼多人，管理員自己確認過再 `force=True`。
3. **絕不動管理員**：seed 管理員跳過；停用後至少留一個啟用中的管理員。
4. **只停用不刪除**：帳號還在、權限指派還在，人回來時管理員按一下就恢復。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

#: 一次批次最多能停用「目錄帳號總數」的多少比例。
#: 超過這個比例通常代表同步本身出了問題（service account 密碼過期、base DN 被改），
#: 而不是真的有那麼多人離職。
SAFETY_MAX_FRACTION = 0.20

#: 支援自動停用的檢視。**不開放任意字串** —— 那等於讓設定檔決定要停用誰。
AUTO_DISABLE_VIEWS = ("missing", "dir_disabled")


def _directory_account_total() -> int:
    """本站鏡射自目錄、且目前啟用中的帳號數（安全閥的分母）。"""
    from . import auth_db
    row = auth_db.conn().execute(
        "SELECT COUNT(*) c FROM users WHERE source IN ('ldap','ad') "
        "AND enabled=1").fetchone()
    return int(row["c"] if row else 0)


def candidates(view: str) -> list[dict]:
    """這個檢視底下、**目前還啟用中**的帳號。

    已經停用的不算 —— 它們不會被再停用一次，計入的話會讓安全閥的分子虛胖。
    """
    from . import user_manager
    if view not in AUTO_DISABLE_VIEWS:
        return []
    return [u for u in user_manager.list_users(view) if u["enabled"]]


def _remaining_admin_count(excluding: set[int]) -> int:
    from . import user_manager
    n = 0
    for u in user_manager.list_users("all"):
        if u["id"] in excluding or not u["enabled"]:
            continue
        if "admin" in (u.get("roles") or []) or u.get("is_admin_seed"):
            n += 1
    return n


def disable_view(view: str, *, actor: str = "", ip: str = "",
                 force: bool = False, dry_run: bool = False,
                 trigger: str = "manual") -> dict:
    """停用這個檢視底下的所有帳號。

    回 `{ok, view, total, candidates, disabled, skipped, aborted, reason}`。
    `aborted=True` 時**一個都沒有動**。
    """
    from . import audit_db, user_manager
    if view not in AUTO_DISABLE_VIEWS:
        return {"ok": False, "aborted": True, "disabled": 0,
                "reason": f"不支援的檢視：{view}"}

    cands = candidates(view)
    total = _directory_account_total()
    result = {"ok": True, "view": view, "total": total,
              "candidates": len(cands), "disabled": 0, "skipped": [],
              "aborted": False, "reason": "", "trigger": trigger,
              "at": time.time()}
    if not cands:
        return result

    # ---- 安全閥
    limit = int(total * SAFETY_MAX_FRACTION)
    if not force and total > 0 and len(cands) > limit:
        result.update(
            ok=False, aborted=True,
            reason=(f"這次會停用 {len(cands)} 個帳號，超過目錄帳號總數 {total} 的 "
                    f"{int(SAFETY_MAX_FRACTION * 100)}%（上限 {limit}）。"
                    "這通常代表同步本身有問題（服務帳號密碼過期、搜尋範圍被改），"
                    "而不是真的有那麼多人離職 —— 已整批中止，一個都沒有動。"))
        audit_db.log_event("user_auto_disable_aborted", username=actor or "system",
                           ip=ip, target=view,
                           details={"candidates": len(cands), "total": total,
                                    "limit": limit, "trigger": trigger})
        logger.warning("目錄自動停用已中止：%s", result["reason"])
        return result

    # ---- 管理員保護
    todo: list[int] = []
    for u in cands:
        if u.get("is_admin_seed"):
            result["skipped"].append({"id": u["id"], "username": u["username"],
                                      "reason": "內建管理員帳號"})
            continue
        todo.append(u["id"])
    # 只有「這一批真的含管理員」時才需要問這個問題。原本無條件檢查，結果在
    # 一個管理員都沒有的資料庫上（本來就是 0）永遠成立，把完全不相干的批次
    # 整個擋掉 —— 自己的測試當場抓到。
    todo_has_admin = any(
        ("admin" in (u.get("roles") or []) or u.get("is_admin_seed"))
        for u in cands if u["id"] in set(todo))
    if todo_has_admin and _remaining_admin_count(set(todo)) == 0:
        result.update(
            ok=False, aborted=True,
            reason="這樣會停用最後一個管理員 —— 已整批中止，請先確認管理員帳號。")
        return result

    if dry_run:
        result["would_disable"] = todo
        return result

    for uid in todo:
        try:
            user_manager.update(uid, enabled=False)
            result["disabled"] += 1
        except ValueError as exc:
            result["skipped"].append({"id": uid, "reason": str(exc)})

    audit_db.log_event(
        "user_auto_disable", username=actor or "system", ip=ip, target=view,
        details={"disabled": result["disabled"], "candidates": len(cands),
                 "total": total, "trigger": trigger,
                 "skipped": result["skipped"][:50], "ids": todo[:200]})
    return result


def run_scheduled(get_settings, set_settings) -> Optional[dict]:
    """排程同步結束後呼叫。設定沒開就什麼都不做。

    **只在完整掃描之後才有意義**：呼叫端要自己確認這一次是完整掃描
    （帶了名稱過濾的同步只看得到一部分目錄）。
    """
    cfg = get_settings()
    view = (cfg.get("auto_disable") or "off").strip()
    if view in ("", "off"):
        return None
    if view not in AUTO_DISABLE_VIEWS:
        logger.warning("auto_disable 設定值無法辨識（%s）—— 當成關閉", view)
        return None
    res = disable_view(view, actor="system", trigger="scheduled")
    try:
        set_settings({"last_auto_disable": res})
    except Exception as exc:  # noqa: BLE001 — 記不下來不該讓同步失敗
        logger.warning("寫入自動停用結果失敗：%s", exc)
    return res
