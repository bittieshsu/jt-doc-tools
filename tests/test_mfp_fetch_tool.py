"""事務機取件（Beta）：工具頁與 API。

* **擁有者隔離**：A 的事務機 B 查不到、改不到、刪不到、下載不到報告（回「找不到」，不透露是別人的）；
  A 的連線測試 token B 拿去儲存也不行。
* **未啟用登入時只准本機**（傳輸層對端是回送位址、Host 是 localhost、沒有轉送標頭）——
  這支會讓伺服器去連區網設備，規格書要求不可以讓遠端在沒有登入的情況下用。
* **API token**：本專案每支工具都要有對外 API（使用者 2026-10-13：衝突時以本專案為準）；
  token 呼叫時連線歸屬 token 的持有者。
* 次數限制、重複、上限、刪帳號一併清除、所有固定訊息都有英日文。

連線測試本身（真的網路連線）在 `test_mfp_client.py`；這裡把 `probe` 換成假的，只測路由的行為。
"""
from __future__ import annotations

import re

import importlib
import json

import pytest
from fastapi.testclient import TestClient

import app.main as app_main
from app.core import mfp_client, mfp_profiles, mfp_store

R = importlib.import_module("app.tools.mfp_fetch.router")


def _fake_report(host="192.168.1.50", status="first_batch_unverified"):
    return {"tool": "mfp-fetch", "target": {"host": host, "tries": []}, "steps": [], "attempts": [],
            "detection": {"vendor": "FUJIFILM", "family": "Apeos", "model": "C5571", "firmware": None,
                          "status": status, "evidence": [], "message": mfp_profiles.STATUS_MESSAGES[status]},
            "summary": {"reachable": True, "reachable_on": ["https:443"], "tls": {"verified": False},
                        "status": status, "elapsed_ms": 12}}


@pytest.fixture
def fake_probe(monkeypatch):
    calls = []

    def probe(host, port=None, scheme=None):
        calls.append((host, port, scheme))
        h = mfp_client.parse_host(host)
        mfp_client.parse_port_scheme(port, scheme)
        if h.startswith("127.") or h == "169.254.169.254":
            raise mfp_client.PolicyError("ADDR_LOOPBACK" if h.startswith("127.") else "ADDR_LINK_LOCAL",
                                         {"host": h})
        return _fake_report(h)

    monkeypatch.setattr(mfp_client, "probe", probe)
    R._recent.clear()
    R._busy.clear()
    R._detections.clear()
    return calls


def _user_client(username: str, roles: list[str]) -> tuple[TestClient, int]:
    from app.core import sessions, user_manager
    uid = user_manager.create_local(username=username, display_name=username,
                                    password="Passw0rd-1234", roles=roles)
    token, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
    c = TestClient(app_main.app)
    c.cookies.set(sessions.COOKIE_NAME, token)
    return c, uid


def _detect(c, host="192.168.1.50", **kw):
    return c.post("/tools/mfp-fetch/api/detect", json={"host": host, **kw})


# --------------------------------------------------------------------- 未啟用登入

def test_without_sign_in_a_remote_browser_is_refused(client, auth_off, fake_probe):
    r = _detect(client)
    assert r.status_code == 403 and r.json()["code"] == "LOCAL_ONLY"
    assert not fake_probe, "被拒絕了卻還是去連了設備"


def test_without_sign_in_this_machine_itself_can_use_it(auth_off, fake_probe):
    local = TestClient(app_main.app, base_url="http://localhost:8765", client=("127.0.0.1", 50000))
    r = _detect(local)
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    r = local.post("/tools/mfp-fetch/api/connections", json={"token": tok, "display_name": "三樓"})
    assert r.status_code == 200 and r.json()["connection"]["display_name"] == "三樓"
    assert [c["host"] for c in local.get("/tools/mfp-fetch/api/connections").json()["connections"]] == ["192.168.1.50"]


@pytest.mark.parametrize("hdr", [{"X-Forwarded-For": "203.0.113.9"}, {"X-Real-IP": "203.0.113.9"},
                                 {"Forwarded": "for=203.0.113.9"}, {"Host": "doc.example.com"}])
def test_a_reverse_proxy_on_this_machine_does_not_count_as_local(auth_off, fake_probe, hdr):
    """反向代理跟服務在同一台時對端是 127.0.0.1 —— 不可以因此當成本機。"""
    local = TestClient(app_main.app, base_url="http://localhost:8765", client=("127.0.0.1", 50000))
    r = local.post("/tools/mfp-fetch/api/detect", json={"host": "192.168.1.50"}, headers=hdr)
    assert r.status_code == 403 and r.json()["code"] == "LOCAL_ONLY"


# --------------------------------------------------------------------- 啟用登入

@pytest.fixture
def two_clerks(admin_session):
    a, ua = _user_client("clerk-a", ["clerk"])
    b, ub = _user_client("clerk-b", ["clerk"])
    return a, b, ua, ub


def test_the_page_renders_with_beta_and_passwords_only_in_the_retrieval_panels(two_clerks):
    """密碼欄位只出現在機內信箱取件（實驗功能，v1.16.89）那一塊，而且不讓瀏覽器自動填入或記住。"""
    a, *_ = two_clerks
    r = a.get("/tools/mfp-fetch/")
    assert r.status_code == 200
    assert "tool-beta" in r.text and "事務機掃描取件" in r.text
    # 共用版面本身有「變更密碼」對話框 —— 只看這支工具自己的模板
    from pathlib import Path
    tpl = Path("app/tools/mfp_fetch/templates/mfp_fetch.html").read_text(encoding="utf-8")
    pw = re.findall(r"<input[^>]*type=\"password\"[^>]*>", tpl)
    assert sorted(re.search(r'id="(\w+)"', x).group(1) for x in pw) == ["mfBoxPw", "mfBoxUserPw", "mfSharePw"]
    assert all('autocomplete="off"' in x for x in pw), "密碼欄不可以讓瀏覽器記住"
    fetch_panels = tpl.split('id="mfBoxPanel"', 1)[1].split('id="mfScansPanel"', 1)[0]
    assert all(x in fetch_panels for x in pw), "密碼欄只在取件那兩塊（信箱取件、檔案分享取件）"


def test_users_without_the_tool_are_refused(admin_session, fake_probe):
    c, _uid = _user_client("plain-user", ["default-user"])
    assert c.get("/tools/mfp-fetch/").status_code in (302, 303, 403)
    r = _detect(c)
    assert r.status_code in (302, 303, 401, 403)
    assert not fake_probe


def test_each_person_only_sees_their_own(two_clerks, fake_probe):
    a, b, *_ = two_clerks
    tok = _detect(a).json()["token"]
    cid = a.post("/tools/mfp-fetch/api/connections", json={"token": tok}).json()["connection"]["id"]
    assert b.get("/tools/mfp-fetch/api/connections").json()["connections"] == []
    for method, path, body in [("GET", f"/connections/{cid}", None),
                               ("GET", f"/connections/{cid}/report", None),
                               ("PATCH", f"/connections/{cid}", {"display_name": "x"}),
                               ("POST", f"/connections/{cid}/test", None),
                               ("DELETE", f"/connections/{cid}", None)]:
        r = b.request(method, "/tools/mfp-fetch/api" + path, json=body)
        assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND", (method, path, r.text)
    assert len(a.get("/tools/mfp-fetch/api/connections").json()["connections"]) == 1


def test_a_detection_token_is_personal(two_clerks, fake_probe):
    a, b, *_ = two_clerks
    tok = _detect(a).json()["token"]
    r = b.post("/tools/mfp-fetch/api/connections", json={"token": tok})
    assert r.status_code == 410 and r.json()["code"] == "DETECTION_EXPIRED"
    # 被 B 試過之後 A 仍然可以用（沒有被 B 消耗掉）
    assert a.post("/tools/mfp-fetch/api/connections", json={"token": tok}).status_code == 200


def test_the_same_ip_can_be_added_by_two_people(two_clerks, fake_probe):
    a, b, *_ = two_clerks
    for c in (a, b):
        tok = _detect(c).json()["token"]
        assert c.post("/tools/mfp-fetch/api/connections", json={"token": tok}).status_code == 200


def test_duplicates_and_limit(two_clerks, fake_probe, monkeypatch):
    a, *_ = two_clerks
    for _ in range(2):
        tok = _detect(a).json()["token"]
        r = a.post("/tools/mfp-fetch/api/connections", json={"token": tok})
    assert r.status_code == 409 and r.json()["code"] == "DUPLICATE"
    monkeypatch.setattr(mfp_store, "MAX_PER_OWNER", 1)
    tok = _detect(a, host="192.168.1.59").json()["token"]
    r = a.post("/tools/mfp-fetch/api/connections", json={"token": tok})
    assert r.status_code == 409 and r.json()["code"] == "LIMIT"


def test_policy_errors_come_back_with_a_code_and_never_connect(two_clerks, fake_probe):
    a, *_ = two_clerks
    r = _detect(a, host="127.0.0.1")
    assert r.status_code == 400 and r.json()["code"] == "ADDR_LOOPBACK"
    r = _detect(a, host="http://192.168.1.50/x")
    assert r.status_code == 400 and r.json()["code"] == "HOST_INVALID"
    r = _detect(a, port=22)
    assert r.status_code == 400 and r.json()["code"] == "PORT_NOT_ALLOWED"


def test_tests_are_rate_limited(two_clerks, fake_probe, monkeypatch):
    a, b, *_ = two_clerks
    monkeypatch.setattr(R, "RATE_LIMIT", 3)
    codes = [_detect(a).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert _detect(b).status_code == 200, "次數是每個人各自算的"


def test_rename_retest_report_and_delete(two_clerks, fake_probe):
    a, *_ = two_clerks
    tok = _detect(a, scheme="https", port=8443).json()["token"]
    c = a.post("/tools/mfp-fetch/api/connections", json={"token": tok, "display_name": "  "}).json()["connection"]
    assert c["display_name"] == "192.168.1.50" and (c["port"], c["scheme"]) == (8443, "https")
    assert c["model"] == "C5571" and c["detect_status"] == "first_batch_unverified"
    cid = c["id"]
    r = a.patch(f"/tools/mfp-fetch/api/connections/{cid}", json={"display_name": "三樓行政區"})
    assert r.json()["connection"]["display_name"] == "三樓行政區"
    r = a.post(f"/tools/mfp-fetch/api/connections/{cid}/test")
    assert r.status_code == 200 and fake_probe[-1] == ("192.168.1.50", 8443, "https"), \
        "重新測試要用存下來的位址與連線方式"
    r = a.get(f"/tools/mfp-fetch/api/connections/{cid}/report")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert json.loads(r.content)["detection"]["model"] == "C5571"
    assert a.delete(f"/tools/mfp-fetch/api/connections/{cid}").json()["ok"] is True
    assert a.get(f"/tools/mfp-fetch/api/connections/{cid}").status_code == 404


@pytest.mark.parametrize("fmt,ctype,magic", [
    ("json", "application/json", b"{"), ("txt", "text/plain", "事務機連線測試診斷報告".encode()),
    ("md", "text/markdown", "# 事務機連線測試診斷報告".encode()), ("html", "text/html", b"<!doctype html>"),
    ("pdf", "application/pdf", b"%PDF"),
])
def test_saved_report_downloads_in_every_format(two_clerks, fake_probe, fmt, ctype, magic):
    """診斷報告可選 txt / md / json / html / pdf（使用者 2026-10-15）；不給 format 照舊是 JSON。"""
    a, b, *_ = two_clerks
    tok = _detect(a).json()["token"]
    cid = a.post("/tools/mfp-fetch/api/connections", json={"token": tok}).json()["connection"]["id"]
    r = a.get(f"/tools/mfp-fetch/api/connections/{cid}/report", params={"format": fmt})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith(ctype) and r.content.startswith(magic)
    cd = r.headers["content-disposition"]
    assert "attachment" in cd and re.search(rf'mfp-diagnostic-192\.168\.1\.50-\d{{8}}-\d{{6}}\.{fmt}', cd), cd
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    if fmt == "json":
        d = a.get(f"/tools/mfp-fetch/api/connections/{cid}/report")
        assert d.content == r.content, "不給 format 要跟以前一樣是 JSON"
    assert b.get(f"/tools/mfp-fetch/api/connections/{cid}/report", params={"format": fmt}).status_code == 404


def test_report_format_is_checked_and_follows_the_interface_language(two_clerks, fake_probe):
    a, *_ = two_clerks
    tok = _detect(a).json()["token"]
    cid = a.post("/tools/mfp-fetch/api/connections", json={"token": tok}).json()["connection"]["id"]
    for bad in ("docx", "../x", "PDF ", ""):
        r = a.get(f"/tools/mfp-fetch/api/connections/{cid}/report", params={"format": bad})
        if bad.strip().lower() in ("pdf", ""):
            continue        # 大小寫與空白容忍；空字串＝預設 JSON
        assert r.status_code == 422 and r.json()["code"] == "REPORT_FORMAT_INVALID", (bad, r.text)
    a.cookies.set("jtdt_locale", "en")
    try:
        r = a.get(f"/tools/mfp-fetch/api/connections/{cid}/report", params={"format": "txt"})
        assert "MFP connection test diagnostic report" in r.text and "事務機" not in r.text
    finally:
        a.cookies.delete("jtdt_locale")


def test_unsaved_test_result_report_needs_the_owners_token_and_does_not_use_it_up(two_clerks, fake_probe):
    a, b, *_ = two_clerks
    tok = _detect(a).json()["token"]
    r = a.post("/tools/mfp-fetch/api/detect/report", json={"token": tok, "format": "pdf"})
    assert r.status_code == 200 and r.content.startswith(b"%PDF"), r.text
    assert ".pdf" in r.headers["content-disposition"]
    r = b.post("/tools/mfp-fetch/api/detect/report", json={"token": tok, "format": "txt"})
    assert r.status_code == 404 and r.json()["code"] == "REPORT_EXPIRED", "別人拿到 token 也不能下載"
    for body in ({"token": "nope", "format": "txt"}, {"format": "txt"}, {}):
        assert a.post("/tools/mfp-fetch/api/detect/report", json=body).status_code == 404
    r = a.post("/tools/mfp-fetch/api/detect/report", json={"token": tok, "format": "docx"})
    assert r.status_code == 422 and r.json()["code"] == "REPORT_FORMAT_INVALID"
    # 下載過後照樣可以儲存
    assert a.post("/tools/mfp-fetch/api/connections", json={"token": tok}).status_code == 200
    r = a.post("/tools/mfp-fetch/api/detect/report", json={"token": tok, "format": "txt"})
    assert r.status_code == 404, "儲存之後 token 就用掉了（報告改從清單的詳細下載）"


def test_deleting_an_account_removes_their_mfps(two_clerks, fake_probe):
    a, b, ua, ub = two_clerks
    tok = _detect(a).json()["token"]
    a.post("/tools/mfp-fetch/api/connections", json={"token": tok})
    tok = _detect(b).json()["token"]
    b.post("/tools/mfp-fetch/api/connections", json={"token": tok})
    from app.core import user_manager
    user_manager.delete(ua)
    assert mfp_store.list_for(f"u{ua}") == []
    assert len(mfp_store.list_for(f"u{ub}")) == 1, "只清那個人的"


def test_actions_are_audited_without_secrets(two_clerks, fake_probe):
    a, *_ = two_clerks
    tok = _detect(a).json()["token"]
    a.post("/tools/mfp-fetch/api/connections", json={"token": tok})
    import time
    from app.core import audit_db
    rows, deadline = [], time.time() + 5    # 稽核是背景寫入，等它寫進去
    while time.time() < deadline:
        rows = audit_db.conn().execute(
            "SELECT event_type, target, details_json FROM audit_events WHERE event_type LIKE 'mfp_%'"
            " ORDER BY id").fetchall()
        if {"mfp_detect", "mfp_connection_create"} <= {r["event_type"] for r in rows}:
            break
        time.sleep(0.1)
    mine = [r for r in rows if r["target"] == "192.168.1.50"]
    kinds = [r["event_type"] for r in mine]
    assert "mfp_detect" in kinds and "mfp_connection_create" in kinds
    assert tok not in json.dumps([dict(r) for r in rows], ensure_ascii=False)


# --------------------------------------------------------------------- API token

def test_an_api_token_acts_as_its_owner(two_clerks, fake_probe):
    """本專案每支工具都要有對外 API；token 呼叫時連線歸屬 token 的持有者。"""
    a, b, ua, ub = two_clerks
    from app.core.api_tokens import api_tokens
    raw = api_tokens.create("mfp-test").token
    assert api_tokens.assign_owner(raw, ua)
    api = TestClient(app_main.app)
    r = api.post("/tools/mfp-fetch/api/detect", json={"host": "192.168.1.50"},
                 headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200, r.text
    r = api.post("/tools/mfp-fetch/api/connections", json={"token": r.json()["token"]},
                 headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200
    assert len(a.get("/tools/mfp-fetch/api/connections").json()["connections"]) == 1
    assert b.get("/tools/mfp-fetch/api/connections").json()["connections"] == []


# --------------------------------------------------------------------- 文字

def test_every_fixed_message_is_translated():
    en = json.load(open("app/i18n/en.json", encoding="utf-8"))
    ja = json.load(open("app/i18n/ja.json", encoding="utf-8"))
    msgs = (list(mfp_client.MESSAGES.values()) + list(mfp_profiles.STATUS_MESSAGES.values())
            + list(R.MESSAGES.values()))
    missing = [m for m in msgs if m not in en or m not in ja]
    assert not missing, f"這些訊息沒有英日文（畫面上 tr() 翻不到）：{missing}"
