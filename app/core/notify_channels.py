"""通知管道發送器：Email / Telegram / Slack / Teams / Discord / Zulip /
Nextcloud Talk / LINE / 通用 Webhook。

做法沿用 jt-ipam 的 `notify_channels.py`（同一位作者的另一個專案，已在正式環境
跑過），差別是**這裡是同步的** —— 作業完成的通知是在背景執行緒裡送出，硬要在
執行緒內起 event loop 只會讓錯誤更難查。多管道之間用小型執行緒池並行，最壞情況
是「最慢的單一管道」而不是各管道相加。

## 信任模型（與 SMTP 主機相同）

目標端點由**管理員**設定，因此：

* **不套 SSRF 白名單** —— 自架的 Nextcloud Talk / Zulip 幾乎都在內網，套白名單
  等於讓這兩個管道不能用。能設定這裡的人本來就有主機權限。
* `follow_redirects=False` —— 避免用重導把訊息帶去別的地方。
* 逾時必填，且不重試 —— 通知是附屬品，不該把作業執行緒卡住。

## 失敗處理

每個 `send_*` 成功回 None、失敗丟例外（測試按鈕要看得到原因）。
`broadcast()` 逐管道 best-effort：單一管道失敗不影響其他管道，更不影響作業本身。

## LINE

**LINE Notify 已於 2025-03-31 停止服務**，因此這裡用的是 Messaging API 的
push message —— 需要 LINE 官方帳號、channel access token，以及收訊者的 user id
（不是 LINE ID）。門檻比其他管道高，UI 要寫清楚前置需求。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets as _secrets
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

logger = logging.getLogger("app.notify")

#: webhook 型管道（email 走 SMTP，另外處理）
WEBHOOK_CHANNELS = ("telegram", "slack", "teams", "discord", "zulip",
                    "nextcloud", "line", "webhook")

ALL_CHANNELS = ("email",) + WEBHOOK_CHANNELS

#: 顯示名稱與前置需求（給 admin UI 用）
#: **只有 Email 對真實服務端到端驗證過**（實機 SMTP 寄出、收件者確認收到）。
#: 其餘管道有單元測試（把網路呼叫換掉、驗載荷格式），但沒有人真的接上 Slack /
#: Teams / Telegram … 實際收過訊息 —— 所以標為開發階段，讓管理員知道這件事，
#: 而不是設定下去之後才發現收不到。
#:
#: 驗證過一個就把它從這裡拿掉（並在 CHANGELOG 註明是用哪個服務驗的）。
DEV_CHANNELS: frozenset[str] = frozenset({
    "telegram", "slack", "teams", "discord", "zulip", "nextcloud",
    "line", "webhook",
})

CHANNEL_INFO: dict[str, dict] = {
    "email":     {"label": "Email", "needs": "SMTP 主機"},
    "telegram":  {"label": "Telegram", "needs": "Bot token + Chat ID"},
    "slack":     {"label": "Slack", "needs": "Incoming Webhook URL"},
    "teams":     {"label": "Microsoft Teams", "needs": "Workflows 或舊版連接器的 Webhook URL"},
    "discord":   {"label": "Discord", "needs": "Webhook URL"},
    "zulip":     {"label": "Zulip", "needs": "站台網址 + Bot Email + API Key；頻道（使用者可改填自己的 email 收私訊）"},
    "nextcloud": {"label": "Nextcloud Talk", "needs": "站台網址 + 對話 token + Bot 密鑰（使用者可改填自己的一對一對話 token）"},
    "line":      {"label": "LINE", "needs": "官方帳號的 Channel Access Token + 收訊者 User ID"
                                            "（LINE Notify 已於 2025 年停止服務）"},
    "webhook":   {"label": "通用 Webhook", "needs": "URL（可選 Bearer token）"},
}

_TIMEOUT = 12.0


def _msg(subject: str, text: str | None) -> str:
    return f"{subject}\n{text}" if text else subject


def _post(url: str, *, json: dict | None = None, data: dict | None = None,
          headers: dict | None = None, auth: tuple[str, str] | None = None) -> None:
    import httpx
    # follow_redirects=False：避免以重導把訊息帶往設定以外的位置
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as c:
        r = c.post(url, json=json, data=data, headers=headers, auth=auth)
        if r.status_code >= 300:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")


# ---------- 各管道 ----------

def send_telegram(cfg: dict[str, Any], subject: str, text: str | None) -> None:
    token, chat = cfg.get("telegram_token"), cfg.get("telegram_chat_id")
    if not (token and chat):
        raise RuntimeError("尚未設定 Telegram bot token / chat id")
    _post(f"https://api.telegram.org/bot{token}/sendMessage",
          json={"chat_id": chat, "text": _msg(subject, text),
                "disable_web_page_preview": True})


def send_slack(cfg: dict[str, Any], subject: str, text: str | None) -> None:
    url = cfg.get("slack_webhook")
    if not url:
        raise RuntimeError("尚未設定 Slack webhook URL")
    _post(url, json={"text": f"*{subject}*\n{text}" if text else f"*{subject}*"})


def send_teams(cfg: dict[str, Any], subject: str, text: str | None) -> None:
    url = cfg.get("teams_webhook")
    if not url:
        raise RuntimeError("尚未設定 Teams webhook URL")
    body = f"**{subject}**\n\n{text}" if text else f"**{subject}**"
    try:
        # 舊版 Office 365 連接器：純 text
        _post(url, json={"text": body})
    except Exception:
        # 新版 Teams「Workflows」的 incoming webhook 需要 Adaptive Card
        # （微軟已淘汰舊連接器）—— 兩種格式都試，管理員不必自己分辨用的是哪種
        _post(url, json={
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard", "version": "1.4",
                    "body": [{"type": "TextBlock", "text": body, "wrap": True}],
                },
            }],
        })


def send_discord(cfg: dict[str, Any], subject: str, text: str | None) -> None:
    url = cfg.get("discord_webhook")
    if not url:
        raise RuntimeError("尚未設定 Discord webhook URL")
    _post(url, json={"embeds": [{
        "title": subject[:256],
        "description": (text or "")[:4000],
        "color": 0x2563EB,
    }]})


def send_zulip(cfg: dict[str, Any], subject: str, text: str | None) -> None:
    """Zulip：使用者填了自己的 email 就送**私訊**，否則送到管理員設定的頻道。

    Zulip 是唯一不用換憑證就能私訊的管道 —— 同一支 `/api/v1/messages`，把
    `type` 改成 `private` 即可（新版叫 `direct`，但 `private` 仍相容）。
    Slack / Teams / Discord 的 incoming webhook URL 天生綁死一個頻道，要私訊
    得改用 bot token，屬於另一種憑證型態。
    """
    site = (cfg.get("zulip_site") or "").rstrip("/")
    email, api_key = cfg.get("zulip_bot_email"), cfg.get("zulip_api_key")
    if not (site and email and api_key):
        raise RuntimeError("尚未設定 Zulip 站台 / bot email / API key")
    body = f"**{subject}**\n{text}" if text else f"**{subject}**"
    to_me = (cfg.get("zulip_to") or "").strip()
    if to_me:
        # 私訊。`to` 要是 JSON 陣列（可放 email 或 user id）
        payload = {"type": "private", "to": json.dumps([to_me]), "content": body}
    else:
        stream = cfg.get("zulip_stream")
        if not stream:
            raise RuntimeError("尚未設定 Zulip 頻道，且未填個人收訊 email")
        payload = {"type": "stream", "to": stream,
                   "topic": cfg.get("zulip_topic") or "jt-doc-tools",
                   "content": body}
    _post(f"{site}/api/v1/messages", auth=(email, api_key), data=payload)


def send_nextcloud(cfg: dict[str, Any], subject: str, text: str | None) -> None:
    """Nextcloud Talk bot：對話 token + bot 密鑰（HMAC-SHA256 簽 random+message）。

    **一對一對話本身也有 token** —— 使用者填了自己那個就是私訊，沒填就送到管理員
    設定的群組對話。憑證（bot 密鑰）完全不用換。
    """
    site = (cfg.get("nextcloud_url") or "").rstrip("/")
    secret = cfg.get("nextcloud_secret")
    # 使用者自己的對話 token 優先
    token = (cfg.get("nextcloud_to") or "").strip() or cfg.get("nextcloud_token")
    if not (site and token and secret):
        raise RuntimeError("尚未設定 Nextcloud 站台 / 對話 token / bot 密鑰")
    message = _msg(subject, text)
    rnd = _secrets.token_hex(32)
    sig = hmac.new(secret.encode(), (rnd + message).encode(),
                   hashlib.sha256).hexdigest()
    _post(f"{site}/ocs/v2.php/apps/spreed/api/v1/bot/{token}/message",
          json={"message": message},
          headers={
              "OCS-APIRequest": "true",
              "Content-Type": "application/json",
              "Accept": "application/json",
              "X-Nextcloud-Talk-Bot-Random": rnd,
              "X-Nextcloud-Talk-Bot-Signature": sig,
          })


def send_line(cfg: dict[str, Any], subject: str, text: str | None) -> None:
    """LINE Messaging API 的 push message。

    **不是 LINE Notify** —— 那個服務已於 2025-03-31 終止。這裡需要官方帳號的
    channel access token 與收訊者的 user id（`U` 開頭的那串，不是 LINE ID）。
    """
    token = cfg.get("line_token")
    to = cfg.get("line_to")
    if not (token and to):
        raise RuntimeError("尚未設定 LINE channel access token / 收訊者 user id")
    _post("https://api.line.me/v2/bot/message/push",
          headers={"Authorization": f"Bearer {token}"},
          json={"to": to,
                "messages": [{"type": "text", "text": _msg(subject, text)[:4900]}]})


def send_webhook(cfg: dict[str, Any], subject: str, text: str | None) -> None:
    """通用 webhook：POST JSON 到自訂 URL。方便串 n8n / 自寫端點。"""
    url = cfg.get("webhook_url")
    if not url:
        raise RuntimeError("尚未設定 Webhook URL")
    headers = {}
    tok = cfg.get("webhook_token")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    _post(url, json={"app": "jt-doc-tools", "subject": subject,
                     "text": text or ""}, headers=headers or None)


def send_email(cfg: dict[str, Any], subject: str, text: str | None,
               html: str | None = None,
               images: dict[str, bytes] | None = None) -> None:
    """SMTP 寄信。用標準函式庫的 smtplib（不引入新相依）。

    有 `html` 時送 `multipart/alternative`：讀信軟體挑得到哪個就顯示哪個。
    純文字版**一定要留** —— 命令列讀信、無障礙輔助、以及把信轉成摘要的服務
    都靠它，而且有些企業信件閘道會直接丟掉只有 HTML 的信。
    """
    import smtplib
    from email.message import EmailMessage

    host = cfg.get("smtp_host")
    to = cfg.get("email_to")
    if not host:
        raise RuntimeError("尚未設定 SMTP 主機")
    if not to:
        raise RuntimeError("沒有收件者信箱")
    port = int(cfg.get("smtp_port") or 587)
    mode = (cfg.get("smtp_tls") or "starttls").lower()
    sender = cfg.get("smtp_from") or cfg.get("smtp_username") or "jt-doc-tools"

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text or subject)
    if html:
        # 順序有意義：`add_alternative` 後加的擺在後面，讀信軟體優先顯示最後一個。
        msg.add_alternative(html, subtype="html")
        if images:
            # 內嵌圖片要掛在**那一段 HTML 底下**（變成 multipart/related），
            # 不是掛在最外層 —— 掛錯地方的話 `cid:` 參照不到，圖會變成附件。
            part = msg.get_payload()[-1]
            for cid, data in images.items():
                if not data:
                    continue
                part.add_related(data, maintype="image", subtype="png",
                                 cid=f"<{cid}>")

    if mode == "ssl":
        client = smtplib.SMTP_SSL(host, port, timeout=_TIMEOUT)
    else:
        client = smtplib.SMTP(host, port, timeout=_TIMEOUT)
    try:
        client.ehlo()
        if mode == "starttls":
            client.starttls()
            client.ehlo()
        user, pw = cfg.get("smtp_username"), cfg.get("smtp_password")
        if user:
            client.login(user, pw or "")
        client.send_message(msg)
    finally:
        try:
            client.quit()
        except Exception:  # noqa: BLE001
            pass


_SENDERS: dict[str, Callable[[dict, str, Optional[str]], None]] = {
    "email": send_email,
    "telegram": send_telegram,
    "slack": send_slack,
    "teams": send_teams,
    "discord": send_discord,
    "zulip": send_zulip,
    "nextcloud": send_nextcloud,
    "line": send_line,
    "webhook": send_webhook,
}


def send_one(cfg: dict[str, Any], channel: str, subject: str,
             text: str | None) -> None:
    """送單一管道（測試按鈕用）。失敗丟例外，讓管理員看得到原因。"""
    fn = _SENDERS.get(channel)
    if fn is None:
        raise RuntimeError(f"未知的通知管道：{channel}")
    fn(cfg, subject, text)


def broadcast(cfg: dict[str, Any], channels: list[str], subject: str,
              text: str | None = None, html: str | None = None,
              images: dict[str, bytes] | None = None) -> dict[str, str]:
    """送到多個管道，回 {管道: "" 或錯誤訊息}。

    並行送出 —— 最壞情況是最慢的單一管道（約一個逾時），而不是各管道相加。
    任何管道失敗都只記錄，不往外丟：通知是附屬品，**絕不能影響作業本身**。
    """
    channels = [c for c in channels if c in _SENDERS]
    if not channels:
        return {}
    results: dict[str, str] = {}

    def _one(ch: str) -> tuple[str, str]:
        try:
            # 只有 Email 吃得下 HTML；其餘管道（Slack / Telegram …）各有自己的
            # 格式，硬塞 HTML 進去會變成一堆標籤。
            if ch == "email" and html:
                _SENDERS[ch](cfg, subject, text, html, images)
            else:
                _SENDERS[ch](cfg, subject, text)
            return ch, ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("通知管道 %s 失敗：%s: %s", ch, type(exc).__name__, exc)
            return ch, f"{type(exc).__name__}: {exc}"[:200]

    with ThreadPoolExecutor(max_workers=min(len(channels), 8),
                            thread_name_prefix="notify") as ex:
        for ch, err in ex.map(_one, channels):
            results[ch] = err
    return results
