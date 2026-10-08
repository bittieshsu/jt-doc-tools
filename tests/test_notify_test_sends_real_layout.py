"""通知設定的「傳送測試」：Email 寄的是**跟作業完成通知同一個版面**的範例信
（2026-10-08 使用者：測試信原本只有一行純文字，要等一件超過 60 秒的作業才看得到卡片在
Zimbra 網頁版裡的樣子）。

要守住的事：

* Email：HTML 版用的就是真的通知信那支 `notify_email_html.render`（卡片、最大寬度、圖示），
  標題標「[測試]」、純文字版講明是範例；「我的作業」的連結用按測試那個瀏覽器的網址。
* 其他管道（Slack、Telegram…）照舊只收純文字（硬塞 HTML 會變成一堆標籤）。
* 範例信不碰任何真的作業與資料庫（假作業只帶通知用得到的欄位）。
"""
from __future__ import annotations

import pytest

from app.core import job_notify, notify_channels


@pytest.fixture()
def sent(monkeypatch):
    calls: list[tuple] = []

    def fake(*args):
        calls.append(args)

    for ch in ("email", "slack"):
        monkeypatch.setitem(notify_channels._SENDERS, ch, fake)
    return calls


def test_email_test_uses_the_real_notification_layout(admin_session, sent):
    c, _, _ = admin_session
    r = c.post("/admin/notify/test/email", json={"email_to": "someone@example.test"},
               headers={"Origin": "https://doc.example.test"})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert len(sent) == 1
    cfg, subject, text, html, images = sent[0]
    assert cfg.get("email_to") == "someone@example.test"
    assert subject.startswith("[測試] [完成] ") and "測試通知範例" in subject
    assert "測試信" in text and "範例" in text
    # 真的通知信那個版面（不是另外寫一份）
    want = job_notify.build_html(job_notify._SampleJob("https://doc.example.test"))
    assert html == want and "max-width:560px" in html
    assert 'href="https://doc.example.test/my-jobs"' in html, "連結要用按測試那個瀏覽器的網址"
    assert set(images) <= {job_notify.LOGO_CID, job_notify.ICON_CID}


def test_other_channels_still_get_plain_text(admin_session, sent):
    c, _, _ = admin_session
    r = c.post("/admin/notify/test/slack", json={})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert len(sent) == 1 and len(sent[0]) == 3, "Slack 不可以收到 HTML"
    assert "測試通知" in sent[0][2]


def test_the_sample_does_not_touch_real_jobs():
    subject, text, html, _ = job_notify.build_sample("")
    assert subject.startswith("[測試]") and html
    assert "my-jobs" not in html or "href=" not in html.split("my-jobs")[0][-200:], \
        "沒有網址時不可以放指向空白主機的連結"


def test_send_one_passes_html_only_for_email(monkeypatch):
    got = {}
    monkeypatch.setitem(notify_channels._SENDERS, "email", lambda *a: got.setdefault("email", a))
    monkeypatch.setitem(notify_channels._SENDERS, "slack", lambda *a: got.setdefault("slack", a))
    notify_channels.send_one({}, "email", "s", "t", "<b>h</b>", {"x": b"1"})
    notify_channels.send_one({}, "slack", "s", "t", "<b>h</b>", {"x": b"1"})
    assert got["email"] == ({}, "s", "t", "<b>h</b>", {"x": b"1"})
    assert got["slack"] == ({}, "s", "t")
