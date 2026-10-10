"""下載成品與根層級 API 呼叫的稽核記錄。

## 為什麼另外寫一支

原本的稽核只有 `_auth_gate` 記的 `tool_invoke`：`/tools/` 底下的 POST / PUT /
DELETE。所以有兩塊一直沒有紀錄：

* **下載成品**（全部是 GET）：「我的作業」的下載、各工具結果頁的下載鈕、
  工作區的檔案下載。檔案離開系統的那一刻沒有留下任何一筆。
* **根層級的 `/api/` 工具端點**（`/api/convert-to-pdf`、`/api/llm-review`）：
  `/api/` 是公開前綴，`_auth_gate` 一進來就放行，記 `tool_invoke` 的那一段
  走不到。工具自己的 API（`/tools/<工具>/api/<工具>`）不受影響 —— 那條路
  照樣經過 `_auth_gate`，本來就有紀錄。

## 判準

**下載＝GET 請求、回應成功、而且 `Content-Disposition` 是 `attachment`。**
不看路徑清單：之後新加的下載端點只要照慣例回附件，就會自動被記到；
預覽、縮圖、逐頁圖、錄音播放一律是 `inline` 或沒有那個標頭，不會記。
（逐頁預覽一份 30 頁的文件就是 30 次以上的請求，記下來的話真正要查的事
會被淹沒。）

同一個人五分鐘內下載同一個網址只記一筆：大檔的 Range 續傳、使用者連按
兩次都會產生好幾個請求，記好幾筆沒有意義。

**只在啟用認證時記**，跟 `tool_invoke` 一致：認證關閉時沒有帳號，記下來的
只有 IP，而那時整個站本來就是開放的。

稽核員看歷史檔案（`/admin/history/`）已經每次都寫一筆 `auditor_view`，
這裡不重複記。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional
from urllib.parse import unquote

logger = logging.getLogger(__name__)

#: 同一個人下載同一個網址，多久之內只記一筆（秒）。
DEDUPE_SECONDS = 300

#: 去重表的上限。超過就先清掉過期的；還是太多就整份清空（只會多記幾筆，
#: 不會少記）。
_DEDUPE_MAX = 20_000

#: 根層級的 `/api/` 工具端點 → 工具代號。這幾支不經過 `_auth_gate`
#: 的 `tool_invoke`，在這裡補記。
ROOT_API_TOOLS = {
    "/api/convert-to-pdf": "office-to-pdf",
    "/api/llm-review": "pdf-fill",
}

#: 已經由別的事件記錄的路徑前綴（不重複記下載）。
_ALREADY_AUDITED = (
    "/admin/history/",   # auditor_view：稽核員每一次讀取都記
)

_JOB_DL_RE = re.compile(r"^/api/jobs/([A-Za-z0-9_-]{1,64})/download")

_lock = threading.Lock()
_seen: dict[tuple[str, str], float] = {}


def is_download(method: str, status: int, headers) -> bool:
    """這個回應是不是「把檔案交給使用者」。"""
    if (method or "").upper() != "GET":
        return False
    if not (200 <= int(status or 0) < 300):
        return False
    cd = ""
    try:
        cd = headers.get("content-disposition") or ""
    except Exception:  # noqa: BLE001
        return False
    return cd.strip().lower().startswith("attachment")


def filename_from_disposition(cd: str) -> str:
    """從 `Content-Disposition` 取檔名（`filename*=` 優先，那份才是原始檔名）。"""
    if not cd:
        return ""
    m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", cd)
    if m:
        try:
            return unquote(m.group(1).strip().strip('"'))[:255]
        except Exception:  # noqa: BLE001
            pass
    m = re.search(r'filename\s*=\s*"([^"]*)"', cd) or re.search(r"filename\s*=\s*([^;]+)", cd)
    return m.group(1).strip()[:255] if m else ""


def target_for(path: str) -> str:
    """稽核記錄的「目標」欄：工具代號、`workspace`、`admin` 或作業所屬的工具。"""
    if path.startswith("/tools/"):
        return path[len("/tools/"):].split("/", 1)[0]
    m = _JOB_DL_RE.match(path)
    if m:
        try:
            from .job_manager import job_manager
            job = job_manager.get(m.group(1))
            if job is not None and job.tool_id:
                return job.tool_id
        except Exception:  # noqa: BLE001
            pass
        return "jobs"
    if path.startswith("/workspace/"):
        return "workspace"
    if path.startswith("/admin"):
        return "admin"
    seg = path.strip("/").split("/", 1)[0]
    return seg[:64]


def _first_time(username: str, key: str, now: Optional[float] = None) -> bool:
    """這個人在去重時間內第一次下載這個網址嗎？（是 → 登記並回 True）"""
    now = time.time() if now is None else now
    k = (username, key)
    with _lock:
        last = _seen.get(k)
        if last is not None and now - last < DEDUPE_SECONDS:
            return False
        if len(_seen) >= _DEDUPE_MAX:
            for kk in [kk for kk, ts in _seen.items() if now - ts >= DEDUPE_SECONDS]:
                _seen.pop(kk, None)
            if len(_seen) >= _DEDUPE_MAX:
                _seen.clear()
        _seen[k] = now
        return True


def reset() -> None:
    """清掉去重表（測試用）。"""
    with _lock:
        _seen.clear()


def _actor(request) -> Optional[dict]:
    """這個請求是誰。`/api/` 底下的請求 `_auth_gate` 不會設 `request.state.user`，
    瀏覽器帶 session 下載作業結果時要自己查一次。"""
    user = getattr(request.state, "user", None)
    if user:
        return user
    try:
        from . import sessions
        tok = request.cookies.get(sessions.COOKIE_NAME, "")
        return sessions.lookup(tok) if tok else None
    except Exception:  # noqa: BLE001
        return None


def _via(request) -> dict:
    label = getattr(request.state, "api_token_label", None)
    if label is None:
        return {}
    return {"via": "api_token", "token": str(label)[:64]}


def after_response(request, response) -> None:
    """請求處理完之後呼叫。符合條件就寫一筆稽核記錄；任何錯誤都吞掉
    （稽核寫不進去不可以讓使用者拿不到檔案）。"""
    from . import auth_settings
    if not auth_settings.is_enabled():
        return
    path = request.scope.get("path") or ""
    method = request.method.upper()
    status = int(getattr(response, "status_code", 0) or 0)
    headers = getattr(response, "headers", {}) or {}

    if is_download(method, status, headers):
        if path.startswith(_ALREADY_AUDITED):
            return
        user = _actor(request)
        if not user:
            return
        username = user.get("username") or ""
        if not _first_time(username, path):
            return
        from . import audit_db
        from .client_ip import real_client_ip
        size = headers.get("content-length") or ""
        details = {
            "path": path,
            "filename": filename_from_disposition(headers.get("content-disposition") or ""),
            "size_bytes": int(size) if str(size).isdigit() else 0,
            "status": status,
        }
        details.update(_via(request))
        audit_db.log_event("file_download", username=username,
                           ip=real_client_ip(request), target=target_for(path),
                           details=details)
        return

    if method in ("POST", "PUT", "DELETE") and path in ROOT_API_TOOLS:
        user = _actor(request)
        if not user:
            return
        from . import audit_db
        from .client_ip import real_client_ip
        clen = request.headers.get("content-length") or "0"
        details = {
            "method": method,
            "action": "api",
            "path": path,
            "size_bytes": int(clen) if clen.isdigit() else 0,
            "content_type": (request.headers.get("content-type") or "").split(";", 1)[0],
            "status": status,
        }
        for k in ("upload_filename", "upload_filenames", "upload_count"):
            v = getattr(request.state, k, None)
            if v is not None:
                details[k.replace("upload_", "")] = v
        details.update(_via(request))
        audit_db.log_event("tool_invoke", username=user.get("username") or "",
                           ip=real_client_ip(request), target=ROOT_API_TOOLS[path],
                           details=details)
