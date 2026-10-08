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

import re as _re

import logging
from typing import Any

logger = logging.getLogger("app.job_notify")


#: 例外訊息裡常見的絕對路徑（POSIX 與 Windows）。
_ABS_PATH = _re.compile(r"(?:[A-Za-z]:)?[\\/](?:[^\s'\"\\/]+[\\/])+[^\s'\"]*")


def _safe_reason(err) -> str:
    """把失敗原因整理成可以送到外部服務的樣子。

    通知會送到 Slack / Telegram / Discord 這些**外部**服務，而 `job.error`
    是任意例外的 `str(e)` —— PyMuPDF 的訊息長這樣：

        Failed to open file '/tmp/jtdt/temp/機密_王小明_薪資.pdf'.

    也就是**伺服器的絕對路徑**會一起送出去（v1.14.31 對抗式驗證實測）。
    這個模組開頭寫的是「訊息不含檔案內容，只有工具名、檔名、狀態、耗時與
    取件連結」—— 檔名是刻意放的，路徑不在那份清單裡。

    只把路徑換成檔名（使用者需要知道是哪一個檔），其餘照舊。
    """
    def _leaf(m) -> str:
        # **不可以用 `os.path.basename`** —— 在 Linux 上它不把 `\` 當分隔符，
        # Windows 的路徑會原封不動留下來（伺服器也可能是 Windows）。
        raw = m.group(0)
        for sep in ("\\", "/"):
            raw = raw.rsplit(sep, 1)[-1]
        return raw or "檔案"

    s = str(err or "")[:300]
    return _ABS_PATH.sub(_leaf, s)


def _fmt_elapsed(sec: float) -> str:
    sec = int(max(0, sec))
    m, s = divmod(sec, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h} 小時 {m} 分"
    return f"{m} 分 {s} 秒" if m else f"{s} 秒"


def _tool_name(tool_id: str, meta: Any = None) -> str:
    """信件 / 訊息上的「工具」名稱。

    知識庫這類**不是工具**的作業走 `job_labels`（原本借用公文撰擬的代號，
    通知信就寫成「[完成] 公文撰擬：知識庫・…」）。
    """
    try:
        from .job_labels import display_name
        return display_name(tool_id, meta)
    except Exception:  # noqa: BLE001
        return tool_id


def build_message(job: Any) -> tuple[str, str]:
    """組出 (標題, 內文)。刻意只放 metadata，不放檔案內容。"""
    tool = _tool_name(job.tool_id, job.meta)
    fname = (job.meta or {}).get("filename") or job.result_filename or ""
    ok = job.status == "done"
    subject = f"[{'完成' if ok else '失敗'}] {tool}" + (f"：{fname}" if fname else "")
    lines = [f"工具：{tool}"]
    if fname:
        lines.append(f"檔案：{fname}")
    lines.append(f"狀態：{'已完成' if ok else '失敗'}")
    lines.append(f"耗時：{_fmt_elapsed(job.elapsed())}")
    if not ok and job.error:
        lines.append(f"原因：{_safe_reason(job.error)}")
    ws = (job.meta or {}).get("workspace") or {}
    if ok:
        # 有網址就把網址寫在後面 —— Slack / Zulip / Teams 收到的是這個純文字版，
        # 它們會自己把網址變成連結
        if ws.get("saved"):
            url = _site_url("/workspace", job)
            lines.append("結果已自動存入「我的工作區」" + (f"：{url}" if url else "。"))
        else:
            url = _site_url("/my-jobs", job)
            lines.append("可到「我的作業」頁下載結果" + (f"：{url}" if url else "。"))
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
        from .job_labels import display_id
        icon = assets.tool_icon_png(display_id(job.tool_id, job.meta))
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
        # 傳「哪一種情況」而不是傳現成的句子 —— 句子與連結由版型組，
        # 呼叫端不碰 HTML（避免哪天有人把沒跳脫的字串傳進去）。
        note_kind = ""
        if ok:
            note_kind = "workspace" if ws.get("saved") else "jobs"
        return notify_email_html.render(
            site_name=branding.get_site_name("Jason Tools 文件工具箱"),
            ok=ok,
            tool=_tool_name(job.tool_id, job.meta),
            filename=(job.meta or {}).get("filename") or job.result_filename or "",
            elapsed=_fmt_elapsed(job.elapsed()),
            error=str(job.error or "") if not ok else "",
            note_kind=note_kind,
            workspace_url=_site_url("/workspace", job),
            action_url=_site_url("/my-jobs", job),
            logo_cid=LOGO_CID,
            icon_cid=ICON_CID,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("通知信 HTML 產生失敗，改用純文字：%s", e)
        return ""


def _site_url(path: str, job: Any = None) -> str:
    """組出對外可點的網址。

    伺服器自己**不知道**對外網址（可能經反向代理、也可能是內網 IP）。依序用：

    1. 管理員在通知設定填的「站台網址」—— 明確設定的優先。
    2. 送出這件作業時，**使用者瀏覽器所在的網址**（`job.meta["origin"]`，送出那一刻
       由 `Origin` 標頭記下）。那正是這位使用者連得回來的網址，而這封通知只寄給他本人。
       v1.16.57 以前沒有這一層，管理員沒填站台網址的話，信裡「我的作業」就只是純文字。
    3. 都沒有就不放連結 —— 放一個指向 `localhost` 的連結比沒有連結更糟。

    從資料庫讀回來的作業，`origin` 要再驗一次形狀（不信任存著的字串）。
    """
    try:
        from . import notify_settings
        base = (notify_settings.get().get("site_url") or "").strip()
    except Exception:  # noqa: BLE001
        base = ""
    if not base and job is not None:
        try:
            from .http_utils import browser_origin
            base = browser_origin({"origin": str((job.meta or {}).get("origin") or "")})
        except Exception:  # noqa: BLE001
            base = ""
    if not base:
        return ""
    return base.rstrip("/") + path


class _SampleJob:
    """「傳送測試」用的假作業：只帶通知信用得到的欄位。"""

    def __init__(self, origin: str = ""):
        self.id = "sample"
        self.tool_id = "doc-translate"
        self.status = "done"
        self.error = None
        self.result_filename = ""
        self.owner_id = None
        self.meta = {"filename": "測試通知範例.docx", "origin": origin or ""}

    def elapsed(self) -> float:
        return 83.0


def build_sample(origin: str = "") -> tuple[str, str, str, dict[str, bytes]]:
    """管理頁「傳送測試」寄的信：**跟作業完成時寄出的同一個版面**，內容是範例
    （2026-10-08 使用者：測試信只有一行純文字，要等一件超過 60 秒的作業才看得到卡片在
    讀信軟體裡長怎樣）。`origin` 是按測試的那個瀏覽器所在的網址（信裡「我的作業」連得回來）。

    回 `(標題, 純文字, HTML, 內嵌圖片)`；HTML 組不出來時是空字串（照樣寄純文字）。
    """
    job = _SampleJob(origin)
    subject, text = build_message(job)
    subject = "[測試] " + subject
    text = "這是一封測試信，版面跟作業完成時寄出的通知一樣（內容是範例）。\n\n" + text
    html = build_html(job)
    return subject, text, html, (build_images(job) if html else {})


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
