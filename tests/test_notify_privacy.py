"""通知送出去的內容不可以外洩多餘的東西。

通知會送到 **外部服務**（Slack / Telegram / Discord / Nextcloud …），
所以「送了什麼」跟「送給誰」都是隱私問題。v1.14.31 的對抗式驗證抓到三件：

1. **失敗原因裡有伺服器的絕對路徑**：`job.error` 是任意例外的 `str(e)`，
   PyMuPDF 會給 `Failed to open file '/tmp/jtdt/temp/機密_王小明_薪資.pdf'.`
   —— 路徑一起送出去了。模組自己寫的清單是「工具名、檔名、狀態、耗時與
   取件連結」，路徑不在裡面。
2. **從未設定過偏好的使用者，預設會收到「所有」管道**，包含 Slack /
   Discord 這種**團隊共用頻道** —— 等於把
   「[完成] 文件去識別化：2026年度_資遣名單_王小明等12人.pdf」貼到全公司
   看得到的地方，而使用者根本不知道有這回事。
3. **一般使用者填得到的 `nextcloud_to` 會進網址的路徑段**，可以用 `../../`
   把請求改寫成打同一台 Nextcloud 的別的端點（還帶著 bot 的簽章標頭）。
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# 1. 失敗原因不可以帶伺服器路徑
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("err,leaf", [
    ("Failed to open file '/tmp/jtdt/temp/機密_王小明_薪資.pdf'.",
     "機密_王小明_薪資.pdf"),
    ("[Errno 2] No such file or directory: "
     "'/opt/jt-doc-tools/data/temp/annual_report.pdf'", "annual_report.pdf"),
    (r"cannot open C:\Users\jason\AppData\Local\Temp\x.pdf", "x.pdf"),
])
def test_error_reason_keeps_filename_but_drops_path(err, leaf):
    from app.core.job_notify import _safe_reason

    got = _safe_reason(err)
    assert leaf in got, f"檔名不該被一起拿掉：{got}"
    for frag in ("/tmp", "/opt", "/data/", "\\Users", "\\Temp"):
        assert frag not in got, f"伺服器路徑外洩：{got}"


def test_error_reason_leaves_normal_messages_alone():
    from app.core.job_notify import _safe_reason

    assert _safe_reason("document closed or encrypted") == \
        "document closed or encrypted"


# ---------------------------------------------------------------------------
# 2. 團隊共用頻道不可以是預設
# ---------------------------------------------------------------------------

def test_team_channels_are_not_on_by_default(tmp_path, monkeypatch):
    """使用者從未設定過偏好時，只能預設開「送給本人」的管道。"""
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from app.core import notify_settings as ns

    cfg = ns.get(reveal=True)
    cfg["enabled"] = True
    for ch in ("email", "slack", "discord"):
        cfg.setdefault("channels", {})[ch] = {"enabled": True}
    cfg["channels"]["slack"]["slack_webhook"] = "https://hooks.slack.com/T/B/x"
    cfg["channels"]["discord"]["discord_webhook"] = "https://discord.com/api/webhooks/x"
    cfg["channels"]["email"]["smtp_host"] = "smtp.example.com"
    ns.save(cfg)

    chosen, _merged = ns.resolve_for_user("u42")
    for team in ("slack", "discord", "teams", "webhook"):
        assert team not in chosen, (
            f"「{team}」是團隊共用頻道，不可以在使用者沒勾選時就預設送出 —— "
            "那等於把檔名貼到全公司看得到的地方")


def test_personal_channels_stay_on_by_default(tmp_path, monkeypatch):
    """個人管道要維持原本的預設行為（那是刻意的，有回報過收不到信）。"""
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from app.core import notify_settings as ns

    cfg = ns.get(reveal=True)
    cfg["enabled"] = True
    cfg.setdefault("channels", {})["email"] = {
        "enabled": True, "smtp_host": "smtp.example.com"}
    ns.save(cfg)

    # 個人管道要有目的地才會送（`_PERSONAL_FIELD` 的保險）。收件信箱**只認
    # 帳號上的那一個**，不是使用者在通知設定裡填的 —— 所以這裡要換掉那個
    # 查詢，而不是去寫 prefs（寫 prefs 會讓 `channels_set` 變 True，
    # 那就不是「從未設定過」的情境了）。
    monkeypatch.setattr(ns, "_account_email", lambda key: "someone@example.com")

    chosen, _ = ns.resolve_for_user("u42")
    assert "email" in chosen, (
        f"個人信箱的預設行為不可以被一起改掉（實得 {chosen}）")


# ---------------------------------------------------------------------------
# 3. 使用者可控的值不可以改寫請求網址
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token", [
    "../../../../ocs/v2.php/apps/provisioning_api/api/v1/users",
    "abc/../../x", "a b", "a?x=1", "a#f", "a%2f..%2f", "",
])
def test_nextcloud_token_must_be_alphanumeric(token, monkeypatch, tmp_path):
    """`nextcloud_to` 是一般使用者填得到的欄位，而它會進網址的路徑段。"""
    from app.core import notify_channels as nc

    sent = []
    monkeypatch.setattr(nc, "_post", lambda url, **kw: sent.append(url))
    cfg = {"nextcloud_url": "https://cloud.example.com",
           "nextcloud_to": token, "nextcloud_secret": "s" * 32,
           "nextcloud_token": token}
    with pytest.raises(Exception):
        nc.send_nextcloud(cfg, "標題", "內文")
    assert not sent, f"不合格的 token 仍然送出了請求：{sent}"


def test_nextcloud_accepts_a_normal_token(monkeypatch):
    from app.core import notify_channels as nc

    sent = []
    monkeypatch.setattr(nc, "_post", lambda url, **kw: sent.append(url))
    nc.send_nextcloud({"nextcloud_url": "https://cloud.example.com",
                       "nextcloud_to": "abc123XYZ_-",
                       "nextcloud_secret": "s" * 32}, "標題", "內文")
    assert sent and sent[0].endswith("/bot/abc123XYZ_-/message"), sent


def test_built_message_has_no_server_paths():
    """整合層：**實際組出來的通知內文**不可以有伺服器路徑。

    只測 `_safe_reason` 不夠 —— 把它從 `build_message` 拔掉之後那些單元測試
    照樣全綠（實測變異驗證沒抓到）。這裡走真正的組訊息路徑。
    """
    from app.core.job_manager import Job
    from app.core.job_notify import build_message

    job = Job(id="a" * 32, tool_id="doc-deident", status="error")
    job.meta = {"filename": "薪資表.pdf"}
    job.error = ("Failed to open file "
                 "'/opt/jt-doc-tools/data/temp/機密_王小明_薪資.pdf'.")

    subject, body = build_message(job)
    whole = subject + "\n" + body
    for frag in ("/opt", "/data/", "/temp/"):
        assert frag not in whole, f"通知內文含伺服器路徑：{whole}"
    assert "機密_王小明_薪資.pdf" in whole, "檔名還是要留著（使用者要知道是哪個檔）"
