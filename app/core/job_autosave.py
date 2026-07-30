"""作業完成後自動存入送出者的工作區。

## 為什麼

背景作業本來就是「送出後可以關掉頁面」的東西，但結果檔放在 `data/jobs/` 底下，
由保留設定的 **Job 結果（預設 24 小時）**清掉 —— 使用者隔天回來就沒了。而工作區
本來就是「各工具輸出的檔案放這裡」的地方，有額度、有保留期、有權限，結果理當
自動流進去，不必每次都記得按「存至工作區」。

## 工作區停用時**不另外找地方存**

管理員把工作區關掉是一個明確的決定（多半是磁碟或法遵考量）。這時如果我們偷偷
把檔案存到別處，等於繞過那個決定，還會變成第二個沒人管的磁碟成長來源。正確做法
是**把期限講清楚**：作業清單改顯示「結果將於 X 小時後清除」，讓使用者知道要在
什麼時候之前取走。

## 失敗要看得見

額度滿、格式不支援、尚未登入 —— 這些都記進 `job.meta["workspace"]`，由作業清單
顯示原因，而**下載連結照樣保留**。無聲失敗最糟：使用者以為存好了，隔天檔案卻不在。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("app.job_autosave")

#: 自動存入的結果檔大小上限。超過就不自動存（仍可手動按「存至工作區」）——
#: 一個 500 MB 的產出自動塞進去，等於幫使用者把額度用掉大半。
_AUTO_MAX_BYTES = 200 * 1024 * 1024


def _reason(code: str, detail: str = "") -> dict:
    return {"saved": False, "reason": code, "detail": detail}


def on_job_finished(job: Any) -> Optional[dict]:
    """作業結束時呼叫。回傳寫進 `job.meta["workspace"]` 的結果摘要。

    **絕不丟例外** —— 自動存檔失敗不該把一個已經成功的轉換標記成失敗。
    """
    try:
        return _try_save(job)
    except Exception as e:  # noqa: BLE001
        logger.warning("job %s 自動存入工作區失敗：%s", getattr(job, "id", "?"), e)
        return _reason("error", str(e)[:200])


def _try_save(job: Any) -> Optional[dict]:
    from . import workspace as ws

    if job.status != "done":
        return None
    path = job.result_path
    if not path or not path.exists():
        return None
    if not ws.is_enabled():
        # 管理員關掉了工作區 —— 不自動存，改由 UI 提示保留期限
        return _reason("workspace_disabled")

    # 使用者還開著頁面就不必自動存 —— 他就在那裡，按「下載」或那顆「存至工作區」
    # 就好。硬存只會多一份重複檔案並吃掉他的額度。自動保存的價值在「人已經離開」
    # 的情境（送出後去忙別的、或直接關掉分頁）。
    from .job_manager import job_manager
    if job_manager.is_being_watched(job.id):
        return _reason("still_watching")

    size = path.stat().st_size
    if size > _AUTO_MAX_BYTES:
        return _reason("too_large",
                       f"{size / 1048576:.0f} MB 超過自動存入上限")

    try:
        key = ws.key_for_user_id(getattr(job, "owner_id", None))
    except ws.WorkspaceError:
        # 認證開啟但這個作業沒有歸屬（例如 API token 呼叫）→ 沒有工作區可存
        return _reason("no_owner")

    name = job.result_filename or path.name
    try:
        meta = ws.save_bytes_for_key(key, path.read_bytes(), name,
                                     source_tool=job.tool_id,
                                     user_label=getattr(job, "owner_label", ""))
    except ws.QuotaExceeded as e:
        return _reason("quota", str(e))
    except ws.UnsupportedType as e:
        return _reason("unsupported", str(e))
    except ws.WorkspaceDisabled:
        return _reason("workspace_disabled")
    except OSError as e:
        return _reason("error", str(e)[:200])
    logger.info("job %s 結果已存入工作區（%s）", job.id, meta.get("file_id"))
    return {"saved": True, "file_id": meta.get("file_id"),
            "name": meta.get("name")}
