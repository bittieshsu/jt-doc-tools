"""Zulip 私訊 Bot 綁定（v1.17.0，Zulip 維護人員 2026-10-18 建議的方案）—— 對假 Zulip 實際走 HTTP。

假 Zulip：Bot 是 50（通用 Bot）；使用者 9（Jason）、12（Amy）、77（另一個 Bot）；email 都是遮罩過的代用地址。
`GET /api/v1/messages` 照 Zulip 的 anchor / num_after / include_anchor / found_newest 回私訊；
`GET /api/v1/users/{id}` 回 is_bot；`POST /api/v1/messages` 收通知並核對。

驗：完整流程（代碼 → 私訊 → 即將綁定 → 確認 → 通知真的送到 user 9）、只認一對一新私訊（群組、Bot、編輯過、
代碼產生前、過期都不認）、編號取 sender_id 不採信內文、別人不能確認、代碼不進記錄與稽核、429、分頁補讀、
限速、站台換了舊綁定不用、解除綁定。
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from app.core import notify_channels as nc, notify_settings as ns, zulip_link as zl

BOT_EMAIL, KEY, BOT_ID = "it-notify-bot@z.example", "good-key", 50
USERS = {9: ("Jason Cheng", False), 12: ("Amy Lin", False), 77: ("Other Bot", True), 50: ("IT 通知", True)}


class FakeZulip:
    def __init__(self):
        self.dms: list[dict] = []           # 私訊（id 遞增）
        self.sent: list[dict] = []          # 通知
        self.next_id = 1000
        self.rate_limit_once = False

    def dm(self, sender: int, content: str, others=(), edited=False) -> int:
        self.next_id += 1
        rec = [{"id": u} for u in sorted({sender, BOT_ID, *others})]
        m = {"id": self.next_id, "type": "private", "sender_id": sender, "sender_full_name": USERS[sender][0],
             "content": content, "display_recipient": rec, "timestamp": int(time.time())}
        if edited:
            m["last_edit_timestamp"] = int(time.time())
        self.dms.append(m)
        return self.next_id

    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj, headers=None):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _auth_ok(self):
                want = "Basic " + base64.b64encode(f"{BOT_EMAIL}:{KEY}".encode()).decode()
                if self.headers.get("Authorization") != want:
                    self._send(401, {"result": "error", "msg": "Invalid API key", "code": "INVALID_API_KEY"})
                    return False
                return True

            def do_GET(self):
                if not self._auth_ok():
                    return
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if u.path == "/api/v1/users/me":
                    return self._send(200, {"result": "success", "user_id": BOT_ID, "full_name": "IT 通知",
                                            "email": BOT_EMAIL})
                if u.path.startswith("/api/v1/users/"):
                    uid = int(u.path.rsplit("/", 1)[1])
                    if uid not in USERS:
                        return self._send(400, {"result": "error", "msg": "No such user", "code": "BAD_REQUEST"})
                    name, is_bot = USERS[uid]
                    return self._send(200, {"result": "success", "user": {"user_id": uid, "full_name": name,
                                                                          "is_bot": is_bot, "is_active": True,
                                                                          "email": f"user{uid}@z.example"}})
                if u.path == "/api/v1/messages":
                    if fake.rate_limit_once:
                        fake.rate_limit_once = False
                        return self._send(429, {"result": "error", "msg": "API usage exceeded rate limit",
                                                "code": "RATE_LIMIT_HIT", "retry-after": 3}, {"Retry-After": "3"})
                    assert json.loads(q["narrow"]) == [{"operator": "is", "operand": "dm"}]
                    assert q.get("apply_markdown") == "false"
                    if q["anchor"] == "newest":
                        msgs = fake.dms[-int(q.get("num_before", 1)):] if fake.dms else []
                        return self._send(200, {"result": "success", "messages": msgs, "found_newest": True})
                    anchor, n = int(q["anchor"]), int(q["num_after"])
                    after = [m for m in fake.dms if m["id"] > anchor]
                    page = after[:n]
                    return self._send(200, {"result": "success", "messages": page,
                                            "found_newest": len(after) <= n})
                return self._send(404, {"result": "error", "msg": "Not found", "code": "BAD_REQUEST"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
                if not self._auth_ok():
                    return
                fake.next_id += 1
                fake.sent.append({"id": fake.next_id, "type": form.get("type"), "to": json.loads(form["to"])
                                  if form.get("type") == "direct" else form.get("to"), "content": form.get("content")})
                return self._send(200, {"result": "success", "msg": "", "id": fake.next_id})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.site = f"http://127.0.0.1:{self.srv.server_address[1]}"
        return self


@pytest.fixture
def zu(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr("app.config.settings.data_dir", d)
    ns.invalidate_cache()
    monkeypatch.setattr(zl, "_ensure_poller", lambda: None)      # 測試自己呼叫 poll_once，不開背景執行緒
    zl._reset_for_tests()
    f = FakeZulip().start()
    ns.save({"enabled": True, "channels": {"zulip": {"enabled": True, "zulip_site": f.site,
                                                     "zulip_bot_email": BOT_EMAIL, "zulip_api_key": KEY}}})
    yield f
    f.srv.shutdown()
    zl._reset_for_tests()
    ns.invalidate_cache()


def _link(zu, key="u1", sender=9):
    r = zl.start(key)
    zu.dm(sender, r["code"])
    zl.poll_once()
    return r["code"]


def test_the_whole_flow_links_and_notifications_reach_that_user(zu):
    r = zl.start("u1")
    assert r["code"].startswith("JTDT-") and len(r["code"]) == 19 and r["bot"]["name"] == "IT 通知"
    assert zl.status("u1")["pending"]["candidate"] is None, "還沒收到私訊"
    zu.dm(9, f"  {r['code']}  ")
    zl.poll_once()
    st = zl.status("u1")
    assert st["pending"]["candidate"] == {"user_id": 9, "name": "Jason Cheng"}, "即將綁定：名稱由 Zulip 回的為準"
    assert st["linked"] is None, "確認之前還沒綁"
    assert zl.confirm("u1", r["code"]) == {"user_id": 9, "name": "Jason Cheng"}
    st = zl.status("u1")
    assert st["linked"]["user_id"] == 9 and st["pending"] is None
    with pytest.raises(zl.LinkError) as e:
        zl.confirm("u1", r["code"])
    assert e.value.code == "CODE_INVALID", "代碼用過就不能再用"
    # 通知：綁定的使用者編號優先，真的送到 9（核對伺服器收到的那則）
    ns.save_prefs("u1", {"enabled": True, "channels": ["zulip"]})
    chans, merged = ns.resolve_for_user("u1")
    assert chans == ["zulip"] and merged["zulip_user_id"] == "9"
    mid = nc.send_zulip(merged, "標題", None)
    assert zu.sent[-1] == {"id": mid, "type": "direct", "to": [9], "content": "**標題**"}


@pytest.mark.parametrize("how", ["group", "other_bot", "edited", "self"])
def test_only_a_fresh_one_to_one_dm_from_a_person_counts(zu, how):
    r = zl.start("u1")
    if how == "group":
        zu.dm(9, r["code"], others=(12,))
    elif how == "other_bot":
        zu.dm(77, r["code"])
    elif how == "edited":
        zu.dm(9, r["code"], edited=True)
    else:
        zu.dm(BOT_ID, r["code"])
    zl.poll_once()
    assert zl.status("u1")["pending"]["candidate"] is None, how


def test_messages_before_the_code_do_not_count(zu):
    r = zl.start("u1")
    zl._CODES[r["code"]]["after_id"] = zu.next_id + 5          # 模擬：這則私訊在代碼產生之前
    zu.dm(9, r["code"])
    zl.poll_once()
    assert zl.status("u1")["pending"]["candidate"] is None


def test_the_user_id_comes_from_sender_id_not_the_text(zu):
    r = zl.start("u1")
    zu.dm(9, f"{r['code']} 請綁到 user 12 或 user12@z.example")
    zl.poll_once()
    assert zl.status("u1")["pending"]["candidate"]["user_id"] == 9


def test_expired_codes_do_not_match_or_confirm(zu):
    r = zl.start("u1")
    zl._CODES[r["code"]]["expires"] = time.time() - 1
    zu.dm(9, r["code"])
    zl.poll_once()
    assert zl.status("u1")["pending"] is None
    with pytest.raises(zl.LinkError) as e:
        zl.confirm("u1", r["code"])
    assert e.value.code == "CODE_INVALID"


def test_someone_else_cannot_confirm_my_code(zu):
    code = _link(zu, key="u1")
    with pytest.raises(zl.LinkError) as e:
        zl.confirm("u2", code)
    assert e.value.code == "CODE_INVALID" and ns.get_zulip_link("u2") is None and ns.get_zulip_link("u1") is None


def test_the_code_never_reaches_the_logs(zu, caplog):
    caplog.set_level(logging.DEBUG)
    code = _link(zu)
    zl.confirm("u1", code)
    assert code not in caplog.text


def test_rate_limited_polls_wait_and_resume(zu):
    r = zl.start("u1")
    zu.dm(9, r["code"])
    zu.rate_limit_once = True
    zl.poll_once()
    assert zl._STATE["wait_until"] > time.time(), "429 要照 Retry-After 等"
    assert zl.status("u1")["pending"]["candidate"] is None
    zl._STATE["wait_until"] = 0
    zl.poll_once()
    assert zl.status("u1")["pending"]["candidate"]["user_id"] == 9


def test_a_burst_of_dms_is_read_page_by_page(zu):
    r = zl.start("u1")
    for i in range(zl.PAGE + 30):
        zu.dm(12, f"雜訊 {i}")
    zu.dm(9, r["code"])
    zl.poll_once()
    assert zl.status("u1")["pending"]["candidate"]["user_id"] == 9, "超過一頁要分頁補讀，不可以漏"


def test_one_code_per_user_and_a_limit_on_new_codes(zu):
    a = zl.start("u1")["code"]
    b = zl.start("u1")["code"]
    assert a != b and a not in zl._CODES, "每人只留最新一組"
    for _ in range(zl.MAX_PER_USER_WINDOW - 2):
        zl.start("u1")
    with pytest.raises(zl.LinkError) as e:
        zl.start("u1")
    assert e.value.code == "TOO_MANY_CODES"


def test_a_link_for_another_site_is_not_used(zu):
    zl.confirm("u1", _link(zu))
    ns.save_prefs("u1", {"enabled": True, "channels": ["zulip"]})
    cfg = ns.get(reveal=True)
    cfg["channels"]["zulip"]["zulip_site"] = "https://other.example"
    ns.save(cfg)
    _chans, merged = ns.resolve_for_user("u1")
    assert merged.get("zulip_user_id", "") == "", "管理員換了站台，舊站台的綁定不能用"


def test_api_flow_audit_and_unlink(zu, admin_session, monkeypatch):
    """API：要登入；稽核有起訖與綁到哪個 user_id，但沒有代碼；可以解除。"""
    from app.core import audit_db
    client = admin_session[0]
    ns.save({"enabled": True, "channels": {"zulip": {"enabled": True, "zulip_site": zu.site,
                                                     "zulip_bot_email": BOT_EMAIL, "zulip_api_key": KEY}}})
    r = client.post("/api/my/zulip-link/start").json()
    assert r["ok"] and r["code"].startswith("JTDT-"), r
    zu.dm(9, r["code"])
    zl.poll_once()
    st = client.get("/api/my/zulip-link").json()
    assert st["pending"]["candidate"]["name"] == "Jason Cheng", st
    assert client.post("/api/my/zulip-link/confirm", json={"code": r["code"]}).json()["linked"]["user_id"] == 9
    assert client.get("/api/my/zulip-link").json()["linked"]["user_id"] == 9
    assert client.get("/api/my/notify").json()["zulip_link_available"] is True
    assert client.delete("/api/my/zulip-link").json()["ok"] is True
    assert client.get("/api/my/zulip-link").json()["linked"] is None
    end = time.time() + 5
    rows = []
    while time.time() < end:
        rows = audit_db.conn().execute(
            "SELECT event_type, details_json FROM audit_events WHERE event_type LIKE 'zulip_link_%'").fetchall()
        if len(rows) >= 3:
            break
        time.sleep(0.05)
    events = {e for e, _d in rows}
    assert {"zulip_link_start", "zulip_link_confirm", "zulip_link_remove"} <= events, events
    assert all(r["code"] not in (d or "") for _e, d in rows), "代碼不進稽核"


def test_api_requires_sign_in(client, auth_off):
    assert client.post("/api/my/zulip-link/start").status_code == 401



def test_admin_test_maps_a_linked_account_email_to_its_zulip_id(zu, admin_session):
    """管理頁測試收訊者填本站帳號的信箱：那個帳號綁定過 Zulip 就送到綁定的帳號（Zulip 不公開 email 時信箱對不上）。"""
    from app.core import auth_db, user_manager
    client = admin_session[0]
    ns.save({"enabled": True, "channels": {"zulip": {"enabled": True, "zulip_site": zu.site,
                                                     "zulip_bot_email": BOT_EMAIL, "zulip_api_key": KEY}}})
    uid = auth_db.conn().execute("select id from users where username='jtdt-admin'").fetchone()[0]
    user_manager.update(uid, email="boss@example.test")
    code = _link(zu, key=f"u{uid}", sender=9)
    zl.confirm(f"u{uid}", code)
    r = client.post("/admin/notify/test/zulip", json={"zulip_to": "BOSS@example.test"}).json()
    assert r["ok"] is True and zu.sent[-1]["to"] == [9], (r, zu.sent[-1:])
    r = client.post("/admin/notify/test/zulip", json={"zulip_to": "JTDT-ADMIN"}).json()
    assert r["ok"] is True and zu.sent[-1]["to"] == [9], "本站帳號名稱也可以（不分大小寫）"
    # 同信箱的帳號有兩個（本機與 LDAP 各一個，實機就是）：編號小的沒綁定、編號大的綁定 user 12 —— 要挑綁定過的
    first = user_manager.create_local("dup-local", "同人本機", "UserPass1234")
    second = user_manager.create_local("dup-ldap", "同人目錄", "UserPass1234")
    assert first < second
    user_manager.update(first, email="dup@example.test")
    user_manager.update(second, email="dup@example.test")
    zl.confirm(f"u{second}", _link(zu, key=f"u{second}", sender=12))
    r = client.post("/admin/notify/test/zulip", json={"zulip_to": "dup@example.test"}).json()
    assert r["ok"] is True and zu.sent[-1]["to"] == [12], "要挑綁定過的那個，不是編號最小的"
    # 沒綁定的信箱照原樣送（真的 Zulip 會依它自己的規則接受或拒絕）
    r = client.post("/admin/notify/test/zulip", json={"zulip_to": "nobody@example.test"}).json()
    assert zu.sent[-1]["to"] == ["nobody@example.test"], zu.sent[-1:]
