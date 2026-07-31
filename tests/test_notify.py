"""作業完成通知：管道發送、設定分層、觸發條件。

既有的「記錄轉送」（syslog / CEF / GELF）是給 SIEM 與管理員看的稽核軌跡 ——
它不會告訴「送出那份 19 分鐘轉檔的人」說他的檔案好了。這批補的就是那一段。

**最重要的一條是 `test_notify_failure_never_breaks_the_job`**：通知是附屬品，
任何外部服務掛掉都不能讓一個已經成功的轉換看起來像失敗。
"""
from __future__ import annotations

import json

import pytest

from app.core import notify_channels as nc, notify_settings as ns


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr("app.config.settings.data_dir", d)
    ns.invalidate_cache()
    yield d
    ns.invalidate_cache()


# ---------------- 設定分層 ----------------

def test_secrets_are_encrypted_at_rest(_data_dir):
    """bot token / SMTP 密碼 / webhook URL 不可用明文躺在磁碟上。"""
    ns.save({"enabled": True, "channels": {
        "telegram": {"enabled": True, "telegram_token": "SUPER-SECRET-TOKEN"},
        "slack": {"enabled": True, "slack_webhook": "https://hooks.slack.com/XYZ"},
    }})
    raw = (_data_dir / "notify_settings.json").read_text(encoding="utf-8")
    assert "SUPER-SECRET-TOKEN" not in raw
    assert "hooks.slack.com/XYZ" not in raw


def test_webhook_urls_are_treated_as_secrets():
    """Slack / Teams / Discord 的 incoming webhook URL 本身就等同憑證 ——
    任何人拿到就能往那個頻道貼文，必須跟 token 一樣加密與遮罩。"""
    for ch, field in (("slack", "slack_webhook"), ("teams", "teams_webhook"),
                      ("discord", "discord_webhook")):
        assert (ch, field) in ns.SECRET_FIELDS


def test_get_masks_secrets_by_default(_data_dir):
    ns.save({"channels": {"telegram": {"telegram_token": "abc123"}}})
    masked = ns.get()["channels"]["telegram"]["telegram_token"]
    assert masked and "abc123" not in masked
    assert ns.get(reveal=True)["channels"]["telegram"]["telegram_token"] == "abc123"


def test_unchanged_secret_is_not_wiped(_data_dir):
    """admin UI 顯示的是遮罩，原樣送回來不可把祕密洗掉 —— 這種 bug 的症狀是
    「管理員只改了別的欄位，通知就全部停了」。"""
    ns.save({"channels": {"telegram": {"telegram_token": "keepme"}}})
    masked = ns.get()["channels"]["telegram"]["telegram_token"]
    ns.save({"channels": {"telegram": {"enabled": True,
                                       "telegram_token": masked}}})
    assert ns.get(reveal=True)["channels"]["telegram"]["telegram_token"] == "keepme"


def test_enabled_channels_needs_both_switch_and_config(_data_dir):
    ns.save({"enabled": True, "channels": {"telegram": {"enabled": True}}})
    assert "telegram" not in ns.enabled_channels(), "缺 token 不該算設定完成"
    ns.save({"channels": {"telegram": {"telegram_token": "t"}}})
    assert "telegram" in ns.enabled_channels()


def test_master_switch_off_disables_everything(_data_dir):
    ns.save({"enabled": False, "channels": {
        "telegram": {"enabled": True, "telegram_token": "t"}}})
    assert ns.enabled_channels() == []


# ---------------- 使用者偏好 ----------------

def test_personal_channel_without_destination_is_skipped(_data_dir):
    """個人管道沒有目的地就不送。這不是錯誤，只是還沒設定。

    **Email 的目的地在 v1.14.6 之後只認帳號上的信箱**（使用者不能自己在通知
    設定裡改；目錄帳號由來源同步，本機帳號在「我的帳號」維護）。所以這裡用
    Telegram 驗這條規則 —— 它的目的地仍然由使用者自己填。
    """
    ns.save({"enabled": True, "channels": {
        "telegram": {"enabled": True, "telegram_token": "tok"}}})
    ns.save_prefs("u1", {"enabled": True, "channels": ["telegram"]})
    chans, _ = ns.resolve_for_user("u1")
    assert chans == []
    ns.save_prefs("u1", {"telegram_chat_id": "12345"})
    chans, merged = ns.resolve_for_user("u1")
    assert chans == ["telegram"] and merged["telegram_chat_id"] == "12345"


def test_team_channel_needs_no_user_destination(_data_dir):
    """Slack 這類團隊頻道的目的地在管理員那層，使用者只要選擇接收。"""
    ns.save({"enabled": True, "channels": {
        "slack": {"enabled": True, "slack_webhook": "https://hooks.example/x"}}})
    ns.save_prefs("u2", {"enabled": True, "channels": ["slack"]})
    chans, merged = ns.resolve_for_user("u2")
    assert chans == ["slack"]
    assert merged["slack_webhook"] == "https://hooks.example/x"


def test_user_opt_out(_data_dir):
    ns.save({"enabled": True, "channels": {
        "slack": {"enabled": True, "slack_webhook": "https://hooks.example/x"}}})
    ns.save_prefs("u3", {"enabled": False, "channels": ["slack"]})
    assert ns.resolve_for_user("u3")[0] == []


def test_user_cannot_choose_a_channel_admin_disabled(_data_dir):
    """使用者不能繞過管理員 —— 沒啟用的管道選了也不會送。"""
    ns.save({"enabled": True, "channels": {
        "slack": {"enabled": False, "slack_webhook": "https://hooks.example/x"}}})
    ns.save_prefs("u4", {"enabled": True, "channels": ["slack"]})
    assert ns.resolve_for_user("u4")[0] == []


# ---------------- 發送 ----------------

def test_broadcast_is_best_effort(monkeypatch):
    """單一管道失敗不影響其他管道。"""
    calls = []

    def ok(cfg, s, t):
        calls.append("ok")

    def boom(cfg, s, t):
        raise RuntimeError("服務掛了")

    monkeypatch.setitem(nc._SENDERS, "slack", ok)
    monkeypatch.setitem(nc._SENDERS, "teams", boom)
    res = nc.broadcast({}, ["slack", "teams"], "標題", "內文")
    assert res["slack"] == ""
    assert "服務掛了" in res["teams"]
    assert calls == ["ok"]


def test_unknown_channel_is_ignored():
    assert nc.broadcast({}, ["not-a-channel"], "x") == {}


def test_missing_config_raises_with_a_useful_message():
    """測試按鈕要看得到原因，不能只回一句「失敗」。"""
    with pytest.raises(RuntimeError) as e:
        nc.send_telegram({}, "x", None)
    assert "Telegram" in str(e.value)


def test_line_uses_messaging_api_not_the_dead_notify_service():
    """LINE Notify 已於 2025-03-31 停止服務 —— 必須走 Messaging API 的
    push message，不可還在打 notify-api.line.me。"""
    import inspect
    src = inspect.getsource(nc.send_line)
    assert "api.line.me/v2/bot/message/push" in src
    assert "notify-api" not in src


def test_no_ssrf_allowlist_on_purpose():
    """自架的 Nextcloud Talk / Zulip 幾乎都在內網 —— 套 SSRF 白名單等於讓這兩個
    管道不能用。目標由管理員設定（與 SMTP 主機同一套信任模型），但**不可**跟隨
    重導，否則能用重導把訊息帶去別的地方。"""
    import inspect
    src = inspect.getsource(nc._post)
    assert "follow_redirects=False" in src


# ---------------- 觸發條件 ----------------

def _job(status="done", elapsed=300.0, tool="pdf-to-slides"):
    import time

    from app.core.job_manager import Job
    j = Job(id="a" * 32, tool_id=tool, status=status,
            meta={"filename": "年報.pdf"})
    j.started_at = time.time() - elapsed
    j.updated_at = time.time()
    j.owner_id = None
    return j


def test_short_jobs_do_not_notify(_data_dir, monkeypatch):
    """兩秒就跑完的合併不需要打擾任何人 —— 通知的價值正是「久到你已經去做
    別的事了」。"""
    from app.core import job_notify
    ns.save({"enabled": True, "min_seconds": 60, "channels": {
        "slack": {"enabled": True, "slack_webhook": "https://hooks.example/x"}}})
    ns.save_prefs("__single__", {"enabled": True, "channels": ["slack"]})
    sent = []
    monkeypatch.setattr(nc, "broadcast",
                        lambda *a, **k: sent.append(a) or {})
    job_notify.on_job_finished(_job(elapsed=5))
    assert sent == []
    job_notify.on_job_finished(_job(elapsed=120))
    assert len(sent) == 1


def test_status_filter(_data_dir, monkeypatch):
    from app.core import job_notify
    ns.save({"enabled": True, "min_seconds": 0, "notify_on": ["error"],
             "channels": {"slack": {"enabled": True,
                                    "slack_webhook": "https://hooks.example/x"}}})
    ns.save_prefs("__single__", {"enabled": True, "channels": ["slack"]})
    sent = []
    monkeypatch.setattr(nc, "broadcast", lambda *a, **k: sent.append(a) or {})
    job_notify.on_job_finished(_job(status="done"))
    assert sent == []
    job_notify.on_job_finished(_job(status="error"))
    assert len(sent) == 1


def test_message_has_no_file_contents(_data_dir):
    """通知會離開本機（Slack / Telegram 都是外部服務）—— 只放 metadata。"""
    from app.core import job_notify
    subject, body = job_notify.build_message(_job())
    assert "年報.pdf" in subject or "年報.pdf" in body
    assert "PDF 轉簡報檔" in subject or "PDF 轉簡報檔" in body
    assert "耗時" in body


def test_notify_failure_never_breaks_the_job(_data_dir, monkeypatch):
    """**最重要的一條**：通知是附屬品，外部服務掛掉不可讓成功的轉換變失敗。"""
    from app.core import job_notify
    ns.save({"enabled": True, "min_seconds": 0, "channels": {
        "slack": {"enabled": True, "slack_webhook": "https://hooks.example/x"}}})
    ns.save_prefs("__single__", {"enabled": True, "channels": ["slack"]})

    def boom(*a, **k):
        raise RuntimeError("整個通知子系統壞了")
    monkeypatch.setattr(nc, "broadcast", boom)
    job_notify.on_job_finished(_job())      # 不可丟例外


def test_disabled_by_default(_data_dir):
    """通知預設關閉 —— 升級後不該無預警開始往外送訊息。"""
    assert ns.is_enabled() is False


# ---------------- 跨機還原 ----------------

def test_secrets_survive_machine_change(_data_dir, monkeypatch):
    """與 SSO 同樣的問題：祕密用本機 .session_secret 加密，直接複製檔案到另一台
    機器會解不開 —— 設定看起來都在，通知卻靜悄悄地全部失敗。"""
    import zipfile

    from app.core import settings_export as se

    monkeypatch.setattr("app.core.auth_settings._ensure_secret", lambda: b"A" * 32)
    ns.invalidate_cache()
    ns.save({"enabled": True, "channels": {
        "telegram": {"enabled": True, "telegram_token": "tok-from-machine-A"}}})

    out = _data_dir.parent / "bk.zip"
    se.export_to_zip(out, selected_ids=["notify"])
    with zipfile.ZipFile(out) as z:
        blob = json.loads(z.read("data/notify_settings.json").decode())
    assert blob["channels"]["telegram"]["telegram_token"] == "tok-from-machine-A"

    # 換一台機器（不同 .session_secret）還原
    (_data_dir / "notify_settings.json").unlink()
    monkeypatch.setattr("app.core.auth_settings._ensure_secret", lambda: b"B" * 32)
    ns.invalidate_cache()
    se.import_from_zip(out, selected_ids=["notify"])
    ns.invalidate_cache()

    on_disk = json.loads((_data_dir / "notify_settings.json").read_text())
    ct = on_disk["channels"]["telegram"]["telegram_token"]
    assert ct != "tok-from-machine-A", "落地時必須是密文"
    assert ns.get(reveal=True)["channels"]["telegram"]["telegram_token"] \
        == "tok-from-machine-A"


# ---------------- 站內鈴鐺 ----------------
# 未讀狀態**由作業推導**（見 /api/my/inbox），不另開通知表。理由是：另存一份就得
# 雙寫，兩邊一旦不同步就會出現「通知說完成了、作業清單裡卻沒有」；而且還要另外
# 養一套保留期與清理排程。

def _inbox_client(_data_dir):
    from fastapi.testclient import TestClient

    from app import main as app_main
    from app.core import job_store
    job_store.init()
    return TestClient(app_main.app, client=("10.1.2.3", 1234))


def _seed_done(job_id: str, ip: str, finished: float, tool="pdf-to-slides"):
    from app.core import job_store
    from app.core.job_manager import Job
    j = Job(id=job_id, tool_id=tool, status="done",
            meta={"filename": "年報.pdf"})
    j.client_ip = ip
    j.created_at = finished - 100
    j.updated_at = finished
    job_store.upsert(j)


def test_inbox_counts_unread_since_last_seen(auth_off, _data_dir):
    import time
    c = _inbox_client(_data_dir)
    _seed_done("a" * 32, "10.1.2.3", time.time())
    d = c.get("/api/my/inbox").json()
    assert d["unread"] == 1 and len(d["items"]) == 1

    assert c.post("/api/my/inbox/seen").status_code == 200
    assert c.get("/api/my/inbox").json()["unread"] == 0

    _seed_done("b" * 32, "10.1.2.3", time.time() + 1)
    assert c.get("/api/my/inbox").json()["unread"] == 1


def test_inbox_only_shows_my_jobs(auth_off, _data_dir):
    import time
    c = _inbox_client(_data_dir)
    _seed_done("c" * 32, "10.1.2.3", time.time())
    _seed_done("d" * 32, "10.9.9.9", time.time())     # 別台電腦的
    names = [i["id"] for i in c.get("/api/my/inbox").json()["items"]]
    assert "c" * 32 in names and "d" * 32 not in names


def test_inbox_skips_running_jobs(auth_off, _data_dir):
    """還在跑的不是「通知」—— 鈴鐺講的是「你的東西好了」。"""
    import time

    from app.core import job_store
    from app.core.job_manager import Job
    c = _inbox_client(_data_dir)
    j = Job(id="e" * 32, tool_id="pdf-ocr", status="running")
    j.client_ip = "10.1.2.3"
    j.updated_at = time.time()
    job_store.upsert(j)
    assert c.get("/api/my/inbox").json()["items"] == []


def test_inbox_has_no_separate_table(_data_dir):
    """釘住這個設計決定：通知由作業推導，不另開資料表。若哪天真的要加，
    這個測試會提醒必須連帶處理雙寫同步與保留期。"""
    from app.core import db_health
    assert not any(m["file"] == "notifications.sqlite" for m in db_health.MANAGED)


# ---------------- 兩用管道（可個人 / 可團隊）----------------
# Zulip 與 Nextcloud Talk **不必換憑證**就能私訊 —— 前者同一支 API 改 type，
# 後者一對一對話本身也有 token。Slack / Teams / Discord 的 incoming webhook URL
# 天生綁死一個頻道，要私訊得改用 bot token（另一種憑證型態），因此不在此列。

def test_zulip_sends_direct_message_when_user_filled_address(_data_dir, monkeypatch):
    sent = {}
    monkeypatch.setattr(nc, "_post",
                        lambda url, **kw: sent.update({"url": url, **kw}))
    nc.send_zulip({"zulip_site": "https://z.example", "zulip_bot_email": "b@z",
                   "zulip_api_key": "k", "zulip_stream": "general",
                   "zulip_to": "me@example.test"}, "標題", "內文")
    assert sent["data"]["type"] == "private"
    assert "me@example.test" in sent["data"]["to"]


def test_zulip_falls_back_to_stream(_data_dir, monkeypatch):
    sent = {}
    monkeypatch.setattr(nc, "_post",
                        lambda url, **kw: sent.update({"url": url, **kw}))
    nc.send_zulip({"zulip_site": "https://z.example", "zulip_bot_email": "b@z",
                   "zulip_api_key": "k", "zulip_stream": "general"},
                  "標題", "內文")
    assert sent["data"]["type"] == "stream"
    assert sent["data"]["to"] == "general"


def test_zulip_without_stream_or_personal_is_a_clear_error(_data_dir):
    with pytest.raises(RuntimeError) as e:
        nc.send_zulip({"zulip_site": "https://z.example",
                       "zulip_bot_email": "b@z", "zulip_api_key": "k"},
                      "標題", None)
    assert "頻道" in str(e.value)


def test_nextcloud_prefers_the_users_own_conversation(_data_dir, monkeypatch):
    sent = {}
    monkeypatch.setattr(nc, "_post",
                        lambda url, **kw: sent.update({"url": url, **kw}))
    nc.send_nextcloud({"nextcloud_url": "https://c.example",
                       "nextcloud_token": "GROUP", "nextcloud_secret": "s",
                       "nextcloud_to": "MYOWN"}, "標題", "內文")
    assert "/bot/MYOWN/message" in sent["url"]


def test_nextcloud_falls_back_to_group(_data_dir, monkeypatch):
    sent = {}
    monkeypatch.setattr(nc, "_post",
                        lambda url, **kw: sent.update({"url": url, **kw}))
    nc.send_nextcloud({"nextcloud_url": "https://c.example",
                       "nextcloud_token": "GROUP", "nextcloud_secret": "s"},
                      "標題", "內文")
    assert "/bot/GROUP/message" in sent["url"]


def test_dual_channel_usable_without_user_address(_data_dir):
    """兩用管道即使使用者沒填目的地也該送（走團隊頻道）—— 不可像個人管道那樣被跳過。"""
    ns.save({"enabled": True, "channels": {"zulip": {
        "enabled": True, "zulip_site": "https://z.example",
        "zulip_bot_email": "b@z", "zulip_api_key": "k",
        "zulip_stream": "general"}}})
    ns.save_prefs("u9", {"enabled": True, "channels": ["zulip"]})
    chans, merged = ns.resolve_for_user("u9")
    assert chans == ["zulip"]
    assert merged["zulip_to"] == ""


def test_slack_teams_discord_are_not_dual():
    """這三個要私訊得換憑證型態（bot token）—— 別誤標成兩用，否則使用者會以為
    填了位址就能收私訊，實際上仍然送到頻道。"""
    for ch in ("slack", "teams", "discord"):
        assert ch not in ns.DUAL_CHANNELS
        assert ch not in ns.PERSONAL_CHANNELS


# ---------- 從未設定過偏好的人也要收得到（實際回報的問題） ----------

def _setup_email_ready(uid_email="who@example.test"):
    """管理員把 Email 通知開好、使用者帳號也有信箱 —— 就差使用者自己有沒有設定。"""
    from app.core import notify_settings as ns, roles, user_manager
    roles.seed_builtin_roles()
    uid = user_manager.create_local("notifyme", "收信人", "UserPass1234")
    user_manager.update(uid, email=uid_email)
    ns.save({"enabled": True,
             "channels": {"email": {"enabled": True,
                                    "smtp_host": "smtp.example.test",
                                    "smtp_port": 25, "smtp_from": "a@b.test"}}})
    return uid


def test_user_who_never_configured_still_gets_notified(auth_off):
    """**這是實際回報的 bug**：轉檔跑完 2 分 40 秒，什麼通知都沒有。

    原因是使用者偏好的 `channels` 預設是空陣列 —— 管理員開了通知、設好 SMTP、
    信箱也從目錄同步進來了，卻還缺一個沒人知道要去勾的核取方塊，而且畫面上沒有
    任何地方說「你不會收到」。從未設定過的人應該直接依管理員開好的管道收。
    """
    from app.core import notify_settings as ns
    uid = _setup_email_ready()
    channels, merged = ns.resolve_for_user(f"u{uid}")
    assert "email" in channels, "從未設定過的人收不到通知"
    assert merged["email_to"] == "who@example.test"


def test_explicitly_unchecking_everything_is_respected(auth_off):
    """存過偏好、但一個都沒勾 → 他明確表示不要收，不可以又自動幫他打開。"""
    from app.core import notify_settings as ns
    uid = _setup_email_ready()
    ns.save_prefs(f"u{uid}", {"enabled": True, "channels": []})
    assert ns.resolve_for_user(f"u{uid}")[0] == []


def test_saved_choice_is_respected(auth_off):
    from app.core import notify_settings as ns
    uid = _setup_email_ready()
    ns.save_prefs(f"u{uid}", {"enabled": True, "channels": ["email"]})
    assert ns.resolve_for_user(f"u{uid}")[0] == ["email"]


def test_turning_notifications_off_wins(auth_off):
    from app.core import notify_settings as ns
    uid = _setup_email_ready()
    ns.save_prefs(f"u{uid}", {"enabled": False, "channels": ["email"]})
    assert ns.resolve_for_user(f"u{uid}")[0] == []


def test_no_account_email_means_no_email_channel(auth_off):
    """帳號沒有信箱就不寄 —— 不是錯誤，只是還沒設定。"""
    from app.core import notify_settings as ns, roles, user_manager
    roles.seed_builtin_roles()
    uid = user_manager.create_local("nomail", "沒信箱", "UserPass1234")
    ns.save({"enabled": True,
             "channels": {"email": {"enabled": True,
                                    "smtp_host": "smtp.example.test",
                                    "smtp_port": 25, "smtp_from": "a@b.test"}}})
    assert "email" not in ns.resolve_for_user(f"u{uid}")[0]


def test_user_cannot_set_their_own_notification_email(auth_off):
    """通知信箱只認帳號上的那一個 —— 自己組請求也改不到。

    使用者交代：「使用者不能自己改通知信箱」。擋在**伺服器端**，
    不是只把輸入框從畫面上拿掉。
    """
    from app.core import notify_settings as ns
    uid = _setup_email_ready("account@example.test")
    ns.save_prefs(f"u{uid}", {"enabled": True, "channels": ["email"],
                              "email_to": "elsewhere@example.test"})
    assert ns.get_prefs(f"u{uid}").get("email_to", "") == ""
    _channels, merged = ns.resolve_for_user(f"u{uid}")
    assert merged["email_to"] == "account@example.test"


def test_opening_the_bell_does_not_count_as_choosing(auth_off):
    """點開站內鈴鐺**不算**「我選擇不收通知」。

    實測踩過：鈴鐺會把 `inbox_seen_at` 寫進偏好檔，於是「偏好檔存不存在」這個
    判斷把使用者當成「已設定過、且一個都沒勾」→ 通知全部靜音，而畫面上看不出
    任何異狀。要判斷的是「他有沒有**選過管道**」，不是「有沒有檔案」。
    """
    from app.core import notify_settings as ns
    uid = _setup_email_ready("bell@example.test")
    key = f"u{uid}"
    ns.save_prefs(key, {"inbox_seen_at": 1234567.0})     # 只是點開鈴鐺
    assert not ns.has_chosen_channels(key)
    assert "email" in ns.resolve_for_user(key)[0], "被誤判成『選擇不收』"


def test_saving_channels_counts_as_choosing(auth_off):
    from app.core import notify_settings as ns
    uid = _setup_email_ready("choose@example.test")
    key = f"u{uid}"
    ns.save_prefs(key, {"enabled": True, "channels": []})
    assert ns.has_chosen_channels(key)
    assert ns.resolve_for_user(key)[0] == []


# ---------- 通知信的內嵌圖片（站台 logo + 工具圖示） ----------

def test_email_carries_logo_and_tool_icon_as_inline_images(auth_off, monkeypatch):
    """信裡的圖片必須是**內嵌附件**（`cid:`），不是外部網址。

    外部網址會被讀信軟體預設擋掉（防追蹤像素），使用者只會看到破圖或一條
    「顯示圖片」的提示；`data:` URI 則被 Gmail 直接濾掉。
    """
    from email.message import EmailMessage

    from app.core import job_notify, notify_channels as nc

    class J:
        id = "a" * 32
        tool_id = "pdf-to-slides"
        status = "done"
        error = ""
        result_filename = "out.pptx"
        meta = {"filename": "in.pdf"}

        def elapsed(self):
            return 120.0

    html = job_notify.build_html(J())
    images = job_notify.build_images(J())
    assert f"cid:{job_notify.LOGO_CID}" in html, "HTML 沒有引用 logo"
    assert f"cid:{job_notify.ICON_CID}" in html, "HTML 沒有引用工具圖示"
    assert images.get(job_notify.LOGO_CID), "logo 圖片沒有產生"
    assert images.get(job_notify.ICON_CID), "工具圖示沒有產生"
    for data in images.values():
        assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"

    # 真的組一封信，確認圖片掛在 HTML 那一段底下（掛錯層 cid 會參照不到）
    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def ehlo(self): pass
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): sent["msg"] = msg
        def quit(self): pass
        def close(self): pass

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    nc.send_email({"smtp_host": "smtp.example.test", "email_to": "a@b.test",
                   "smtp_from": "x@y.test", "smtp_tls": "starttls"},
                  "subject", "text", html, images)
    msg: EmailMessage = sent["msg"]
    cids = [p.get("Content-ID", "") for p in msg.walk()]
    assert f"<{job_notify.LOGO_CID}>" in cids, f"logo 沒掛進信裡：{cids}"
    assert f"<{job_notify.ICON_CID}>" in cids, f"工具圖示沒掛進信裡：{cids}"
    # 純文字版仍在
    assert msg.get_body(preferencelist=("plain",)) is not None


def test_email_without_images_still_sends(auth_off, monkeypatch):
    """圖片產不出來時照樣寄 —— 通知的重點是「你的檔案好了」，不是好不好看。"""
    from app.core import notify_channels as nc
    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def ehlo(self): pass
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): sent["msg"] = msg
        def quit(self): pass
        def close(self): pass

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    nc.send_email({"smtp_host": "smtp.example.test", "email_to": "a@b.test",
                   "smtp_from": "x@y.test", "smtp_tls": "starttls"},
                  "subject", "text", "<p>hi</p>", {})
    assert sent["msg"] is not None


def test_tool_icon_reuses_the_shared_macro():
    """圖示不可以在寄信這條路另外抄一份 SVG —— 抄了就會跟畫面上的不一樣。"""
    import inspect

    from app.core import notify_email_assets as assets
    src = inspect.getsource(assets)
    assert "components/icons.html" in src, "沒有走共用的圖示 macro"
    assert "<path" not in src, "看起來自己抄了 SVG path"


def test_dev_channel_list_is_honest():
    """「開發階段」清單要與實際驗證狀態一致。

    目前只有 Email 對真實服務端到端驗證過（實機 SMTP 寄出、收件者確認收到）；
    其餘管道只有把網路呼叫換掉的單元測試。驗證過一個就從 `DEV_CHANNELS` 拿掉，
    並在 CHANGELOG 註明是用哪個服務驗的 —— 這條測試就是要逼那個動作發生。
    """
    from app.core import notify_channels as nc
    assert "email" not in nc.DEV_CHANNELS, "Email 已實機驗證，不該標開發階段"
    assert nc.DEV_CHANNELS == set(nc.ALL_CHANNELS) - {"email"}, (
        "有管道被驗證過就要從 DEV_CHANNELS 移除；新增管道也要記得加進去")


def test_dev_flag_reaches_both_uis():
    """管理頁與使用者頁都要看得到標示 —— 只標一邊等於另一邊的人不知情。"""
    import pathlib
    admin = pathlib.Path("app/admin/templates/admin_notify.html").read_text(encoding="utf-8")
    user = pathlib.Path("app/web/templates/my_jobs.html").read_text(encoding="utf-8")
    assert "dev_channels" in admin and "開發階段" in admin
    assert "ch.dev" in user and "開發階段" in user


def test_email_corners_do_not_rely_on_overflow_clipping():
    """圓角要畫在標題列 / 頁尾自己身上。

    只在外層表格寫 `border-radius` + `overflow:hidden` 是不夠的 —— 多數讀信軟體
    不支援用 overflow 裁切子元素，實際收到的信會變成「卡片圓角、裡面的紫色標題
    列卻是直角」（使用者回報過）。
    """
    from app.core import notify_email_html as h
    html = h.render(site_name="X", ok=True, tool="T", filename="f.pdf",
                    elapsed="1 秒")
    assert "border-radius:12px 12px 0 0" in html, "標題列沒有自己的上圓角"
    assert "border-radius:0 0 12px 12px" in html, "頁尾沒有自己的下圓角"


def test_workspace_mention_is_a_real_link():
    """「我的工作區」要可以點過去（設了站台網址時）。"""
    from app.core import notify_email_html as h
    html = h.render(site_name="X", ok=True, tool="T", filename="f.pdf",
                    elapsed="1 秒", note_kind="workspace",
                    workspace_url="https://doc.example.test/workspace")
    assert 'href="https://doc.example.test/workspace"' in html
    assert "我的工作區</a>" in html


def test_note_falls_back_to_plain_text_without_site_url():
    """沒設站台網址就不放連結 —— 指向 localhost 的連結點了只會更困惑。"""
    from app.core import notify_email_html as h
    html = h.render(site_name="X", ok=True, tool="T", filename="f.pdf",
                    elapsed="1 秒", note_kind="workspace")
    assert "「我的工作區」" in html
    assert "<a href" not in html


def test_note_is_built_by_the_template_not_the_caller():
    """句子與連結由版型組，呼叫端只給「哪一種情況」。

    呼叫端若能傳現成的 HTML 進來，遲早會有人傳沒跳脫的字串。
    """
    import inspect

    from app.core import job_notify
    src = inspect.getsource(job_notify.build_html)
    assert "note_kind" in src
    assert "<a href" not in src and "<div" not in src


def test_unauthenticated_workspace_link_redirects_to_login(admin_session):
    """信裡的連結給還沒登入的人點 → 導到登入頁，登入後回到工作區。"""
    from fastapi.testclient import TestClient

    import app.main as app_main
    anon = TestClient(app_main.app)
    r = anon.get("/workspace", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers.get("location") == "/login?next=/workspace"
