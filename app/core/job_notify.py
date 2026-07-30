"""作業完成通知 —— 由 `job_manager` 在作業結束時呼叫。

## 為什麼要有

既有的「記錄轉送」（syslog / CEF / GELF）是給 **SIEM 與管理員**看的稽核軌跡，
不會告訴「送出那份 19 分鐘轉檔的人」說他的檔案好了。這支補的就是那一段：
**通知送出者本人**。

## 幾個刻意的選擇

* **短作業不通知**（預設 60 秒以下）—— 兩秒就跑完的合併不需要打擾任何人，
  而通知的價值正是「久到你已經去做別的事了」。
* **不阻塞作業**：整段包在 try 裡，任何失敗只記錄。通知是附屬品，
  絕不能讓一個已經成功的轉換看起來像失敗。
* **不重試**：外部服務掛掉就算了，重試只會拖住執行緒；失敗原因寫進記錄。
* 訊息**不含檔案內容**，只有工具名、檔名、狀態、耗時與取件連結 —— 這些通知會
  離開本機（Slack / Telegram 等都是外部服務），內容要克制。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("app.job_notify")


def _fmt_elapsed(sec: float) -> str:
    sec = int(max(0, sec))
    m, s = divmod(sec, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h} 小時 {m} 分"
    return f"{m} 分 {s} 秒" if m else f"{s} 秒"


def _tool_name(tool_id: str) -> str:
    try:
        from ..tool_registry import discover_tools
        for t in discover_tools():
            if t.metadata.id == tool_id:
                return t.metadata.name
    except Exception:  # noqa: BLE001
        pass
    return tool_id


def build_message(job: Any) -> tuple[str, str]:
    """組出 (標題, 內文)。刻意只放 metadata，不放檔案內容。"""
    tool = _tool_name(job.tool_id)
    fname = (job.meta or {}).get("filename") or job.result_filename or ""
    ok = job.status == "done"
    subject = f"[{'完成' if ok else '失敗'}] {tool}" + (f"：{fname}" if fname else "")
    lines = [f"工具：{tool}"]
    if fname:
        lines.append(f"檔案：{fname}")
    lines.append(f"狀態：{'已完成' if ok else '失敗'}")
    lines.append(f"耗時：{_fmt_elapsed(job.elapsed())}")
    if not ok and job.error:
        lines.append(f"原因：{str(job.error)[:300]}")
    ws = (job.meta or {}).get("workspace") or {}
    if ok:
        if ws.get("saved"):
            lines.append("結果已自動存入「我的工作區」。")
        else:
            lines.append("可到「我的作業」頁下載結果。")
    return subject, "\n".join(lines)


#: 內嵌圖片的 Content-ID。固定字串即可 —— 一封信裡不會重複。
LOGO_CID = "jtdt-logo"
ICON_CID = "jtdt-tool-icon"


def build_images(job: Any) -> dict[str, bytes]:
    """通知信要內嵌的圖片。取不到就少一張，不影響信件本身。"""
    out: dict[str, bytes] = {}
    try:
        from . import notify_email_assets as assets
        logo = assets.site_logo_png()
        if logo:
            out[LOGO_CID] = logo
        icon = assets.tool_icon_png(job.tool_id)
        if icon:
            out[ICON_CID] = icon
    except Exception as e:  # noqa: BLE001
        logger.info("通知信圖片產生失敗：%s", e.__class__.__name__)
    return out


def build_html(job: Any) -> str:
    """通知信的 HTML 版（版型在 `notify_email_html`）。

    失敗就回空字串 —— 寧可寄出純文字版，也不要因為排版出錯而完全不通知。
    """
    try:
        from . import branding, notify_email_html
        ws = (job.meta or {}).get("workspace") or {}
        ok = job.status == "done"
        note = ""
        if ok:
            note = ("結果已自動存入「我的工作區」。"
                    if ws.get("saved") else "可到「我的作業」頁下載結果。")
        return notify_email_html.render(
            site_name=branding.get_site_name("Jason Tools 文件工具箱"),
            ok=ok,
            tool=_tool_name(job.tool_id),
            filename=(job.meta or {}).get("filename") or job.result_filename or "",
            elapsed=_fmt_elapsed(job.elapsed()),
            error=str(job.error or "") if not ok else "",
            note=note,
            action_url=_site_url("/my-jobs"),
            logo_cid=LOGO_CID,
            icon_cid=ICON_CID,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("通知信 HTML 產生失敗，改用純文字：%s", e)
        return ""


def _site_url(path: str) -> str:
    """組出對外可點的網址。

    伺服器自己**不知道**使用者是從哪個網址進來的（可能經反向代理、也可能是
    內網 IP），所以要由管理員在通知設定填「站台網址」。沒填就不放按鈕 ——
    放一個指向 `localhost` 的連結比沒有連結更糟。
    """
    try:
        from . import notify_settings
        base = (notify_settings.get().get("site_url") or "").strip()
    except Exception:  # noqa: BLE001
        base = ""
    if not base:
        return ""
    return base.rstrip("/") + path


def on_job_finished(job: Any) -> None:
    """作業結束時呼叫。**絕不丟例外。**"""
    try:
        _notify(job)
    except Exception as e:  # noqa: BLE001
        logger.warning("job %s 通知失敗：%s", getattr(job, "id", "?"), e)


def _notify(job: Any) -> None:
    from . import notify_channels, notify_settings, workspace

    cfg = notify_settings.get()
    if not cfg.get("enabled"):
        return
    if job.status not in (cfg.get("notify_on") or []):
        return
    min_sec = int(cfg.get("min_seconds") or 0)
    if min_sec and job.elapsed() < min_sec:
        # 短作業不打擾 —— 使用者根本還盯著畫面
        return

    try:
        key = workspace.key_for_user_id(getattr(job, "owner_id", None))
    except Exception:  # noqa: BLE001 — 認證開啟但作業沒有歸屬 → 沒有人可通知
        return

    channels, merged = notify_settings.resolve_for_user(key)
    if not channels:
        return
    subject, text = build_message(job)
    html = build_html(job)
    images = build_images(job) if html else {}
    results = notify_channels.broadcast(merged, channels, subject, text,
                                        html, images)
    failed = {c: e for c, e in results.items() if e}
    if failed:
        logger.warning("job %s 部分通知失敗：%s", job.id, failed)
    else:
        logger.info("job %s 已通知 %s", job.id, ",".join(results))
