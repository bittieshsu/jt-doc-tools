"""Zulip 通知對一台假的 Zulip 伺服器實際走 HTTP（v1.17.0，Zulip 維護人員 2026-10-18 的要求）。

假伺服器照 Zulip 12 的行為：
* 成員的 email 是遮罩過的代用地址（`user9@z.example`，email 隱私設定），真正的信箱對不上。
* `POST /api/v1/messages`：Basic 認證（bot email＋API key）；`type=direct` 的 `to` 是 JSON 陣列，
  整數是使用者編號、字串是 email（只認看得到的那個）；`type=stream` 要頻道存在。
* 錯誤回 `{"result": "error", "msg", "code"}`，成功回 `{"result": "success", "id"}`。

驗的是：
* 隱藏 email＋指定使用者編號 → 送到使用者 9，**核對伺服器真的收到那則訊息**與回傳的訊息編號。
* 錯誤的 API key → 錯誤保留 HTTP 401 與 Zulip 的 code / msg，不說成「找不到使用者」。
* 不存在的使用者編號 → 保留 HTTP 400 與 Zulip 的訊息。
* 用真正的信箱私訊（被遮罩）→ 保留狀態碼並補一句原因，email 遮罩後才顯示。
* 原本的頻道通知照舊。
* HTTP 200 但 result 不是 success、或沒有訊息編號 → 不算成功。
* 連不上站台 → 講連不上（不是找不到使用者）。
"""
from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from app.core import notify_channels as nc
from app.core.log_safe import safe_user_error

BOT, KEY = "it-notify-bot@z.example", "good-key"
USERS = {9: "user9@z.example", 12: "user12@z.example"}     # 遮罩過的 email（真正的信箱看不到）
CHANNELS = {"general"}


class FakeZulip:
    def __init__(self):
        self.messages: list[dict] = []
        self.next_id = 100
        self.force: dict | None = None          # 強制回應（status, body）

    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
                if fake.force:
                    return self._send(*fake.force)
                want = "Basic " + base64.b64encode(f"{BOT}:{KEY}".encode()).decode()
                if self.headers.get("Authorization") != want:
                    return self._send(401, {"result": "error", "msg": "Invalid API key", "code": "INVALID_API_KEY"})
                if self.path != "/api/v1/messages":
                    return self._send(404, {"result": "error", "msg": "Not found", "code": "BAD_REQUEST"})
                if form.get("type") == "direct":
                    to = json.loads(form["to"])
                    ids = []
                    for t in to:
                        if isinstance(t, int):
                            if t not in USERS:
                                return self._send(400, {"result": "error", "msg": f"Invalid user ID {t}",
                                                        "code": "BAD_REQUEST"})
                            ids.append(t)
                        else:
                            hit = [u for u, e in USERS.items() if e == t]
                            if not hit:
                                return self._send(400, {"result": "error", "msg": f"Invalid email '{t}'",
                                                        "code": "BAD_REQUEST"})
                            ids += hit
                    fake.next_id += 1
                    fake.messages.append({"id": fake.next_id, "type": "direct", "to": ids, "content": form["content"]})
                    return self._send(200, {"result": "success", "msg": "", "id": fake.next_id})
                if form.get("type") == "stream":
                    if form["to"] not in CHANNELS:
                        return self._send(400, {"result": "error", "msg": f"Channel '{form['to']}' does not exist",
                                                "code": "STREAM_DOES_NOT_EXIST"})
                    fake.next_id += 1
                    fake.messages.append({"id": fake.next_id, "type": "stream", "to": form["to"],
                                          "topic": form.get("topic"), "content": form["content"]})
                    return self._send(200, {"result": "success", "msg": "", "id": fake.next_id})
                return self._send(400, {"result": "error", "msg": "Invalid message type", "code": "BAD_REQUEST"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.site = f"http://127.0.0.1:{self.srv.server_address[1]}"
        return self

    def stop(self):
        self.srv.shutdown()


@pytest.fixture
def zulip():
    f = FakeZulip().start()
    yield f
    f.stop()


def _cfg(z, **kw):
    return {"zulip_site": z.site, "zulip_bot_email": BOT, "zulip_api_key": KEY, **kw}


def _err(fn) -> str:
    with pytest.raises(RuntimeError) as e:
        fn()
    return safe_user_error(e.value, "傳送失敗")


def test_hidden_email_with_a_user_id_reaches_that_user(zulip):
    mid = nc.send_zulip(_cfg(zulip, zulip_to="jason@example.test", zulip_user_id="9"), "標題", "內文")
    got = zulip.messages[-1]
    assert got["id"] == mid and got["type"] == "direct" and got["to"] == [9], "使用者編號優先，不用被遮罩的 email"
    assert "標題" in got["content"] and "內文" in got["content"]


def test_a_numeric_test_recipient_is_a_user_id(zulip):
    mid = nc.send_zulip(_cfg(zulip, zulip_to="12"), "標題", None)
    assert zulip.messages[-1] == {"id": mid, "type": "direct", "to": [12], "content": "**標題**"}


def test_wrong_api_key_keeps_status_and_code(zulip):
    shown = _err(lambda: nc.send_zulip(_cfg(zulip, zulip_api_key="bad-key", zulip_user_id="9"), "標題", None))
    assert "HTTP 401" in shown and "INVALID_API_KEY" in shown and "Invalid API key" in shown, shown
    assert "找不到" not in shown and "bad-key" not in shown, "API key 錯不可以說成找不到使用者，也不可以帶出 key"
    assert not zulip.messages


def test_an_unknown_user_id_keeps_zulips_message(zulip):
    shown = _err(lambda: nc.send_zulip(_cfg(zulip, zulip_user_id="999"), "標題", None))
    assert "HTTP 400" in shown and "Invalid user ID 999" in shown, shown
    assert not zulip.messages


def test_a_masked_email_is_explained_and_the_address_is_masked(zulip):
    shown = _err(lambda: nc.send_zulip(_cfg(zulip, zulip_to="jason@example.test"), "標題", None))
    assert "HTTP 400" in shown and "BAD_REQUEST" in shown and "Invalid email" in shown, shown
    assert "使用者編號" in shown, "email 被拒時要說原因"
    assert "jason@example.test" not in shown and "j***@example.test" in shown, "email 要遮罩"


def test_the_team_channel_still_works(zulip):
    mid = nc.send_zulip(_cfg(zulip, zulip_stream="general", zulip_topic="jt"), "標題", None)
    assert zulip.messages[-1] == {"id": mid, "type": "stream", "to": "general", "topic": "jt", "content": "**標題**"}
    shown = _err(lambda: nc.send_zulip(_cfg(zulip, zulip_stream="nope"), "標題", None))
    assert "HTTP 400" in shown and "STREAM_DOES_NOT_EXIST" in shown, shown


@pytest.mark.parametrize("status,body", [
    (200, {"result": "error", "msg": "Rate limited", "code": "RATE_LIMIT_HIT"}),
    (200, {"result": "success", "msg": ""}),                    # 沒有訊息編號
])
def test_a_200_that_is_not_a_sent_message_is_not_success(zulip, status, body):
    zulip.force = (status, body)
    with pytest.raises(RuntimeError):
        nc.send_zulip(_cfg(zulip, zulip_user_id="9"), "標題", None)


def test_an_unreachable_site_says_so():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    shown = _err(lambda: nc.send_zulip({"zulip_site": f"http://127.0.0.1:{port}", "zulip_bot_email": BOT,
                                        "zulip_api_key": KEY, "zulip_user_id": "9"}, "標題", None))
    assert "連不上 Zulip" in shown and "找不到" not in shown, shown


def test_admin_test_uses_the_channel_or_a_test_recipient(zulip, admin_session):
    """管理頁「傳送測試」：有團隊頻道就送頻道；沒有就送給管理頁填的測試收訊者，沒填就說要填。"""
    from app.core import notify_settings as ns
    client = admin_session[0]

    def setup(stream):
        ns.save({"enabled": True, "channels": {"zulip": {"enabled": True, **_cfg(zulip, zulip_stream=stream)}}})
    setup("")
    r = client.post("/admin/notify/test/zulip", json={}).json()
    assert r["ok"] is False and "測試收訊者" in r["error"] and not zulip.messages
    r = client.post("/admin/notify/test/zulip", json={"zulip_to": "9"}).json()
    assert r["ok"] is True and zulip.messages[-1]["to"] == [9], r
    r = client.post("/admin/notify/test/zulip", json={"zulip_to": "999"}).json()
    assert r["ok"] is False and "HTTP 400" in r["error"] and "Invalid user ID" in r["error"], r
    setup("general")
    r = client.post("/admin/notify/test/zulip", json={"zulip_to": "9"}).json()
    assert r["ok"] is True and zulip.messages[-1]["type"] == "stream" and zulip.messages[-1]["to"] == "general"
