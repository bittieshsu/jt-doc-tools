"""稽核 / 上傳記錄的時間解讀必須與畫面一致（GitHub issue #48）。

客戶回報：後端跑在 UTC、使用者在台北時，
  ① CSV 匯出的時間欄與畫面差 8 小時；
  ② 日期範圍篩選**無聲地**篩掉不該排除的資料。

畫面顯示是對的（epoch 交給瀏覽器轉），錯的是後端自己解讀 `datetime-local`
字串時用了**伺服器行程的時區**。修法：前端把瀏覽器的 UTC 偏移一起送上來，
後端只做明確換算；沒帶偏移（curl / 排程）才退回伺服器時區，且 CSV 一律
輸出**帶偏移**的 ISO-8601。

測試清單：
  1. helper：帶偏移解析 / 帶偏移輸出 / 字串自帶偏移優先 / 亂數偏移被擋
  2. 篩選：伺服器 UTC + 使用者 +08:00 → 該筆資料要留下來（修前會被篩掉）
  3. 匯出：時間欄帶偏移，且與畫面看到的時間一致
  4. 上傳記錄頁：同一個 bug，同樣要修好（回報者沒提到）
  5. 靜態守門：管理區不可再出現「不帶時區」的時間解析
  6. 正負號守門：前端必須送 `-getTimezoneOffset()`（JS 的正負號是反的）
"""
from __future__ import annotations

import ast
import datetime as dt
import os
import time
from pathlib import Path

import pytest

from app.core import audit_db, timeutil as tu

ROOT = Path(__file__).resolve().parent.parent
TPE = 480          # 台北：UTC+8（東為正）
#: 事件時間：台北 2026-08-21 09:30 == UTC 01:30
EVENT_TS = dt.datetime(2026, 8, 21, 1, 30, tzinfo=dt.timezone.utc).timestamp()


@pytest.fixture
def server_in_utc(monkeypatch):
    """**強制行程時區為 UTC** —— 這個 bug 只在「伺服器時區 ≠ 使用者時區」時
    現形。不固定的話，在剛好設成 Asia/Taipei 的機器上，修正前的程式也會過。"""
    old = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


# ------------------------------------------------------------------ 1
def test_offset_is_honoured_when_parsing(server_in_utc):
    """台北使用者選「09:00」→ 必須是 UTC 01:00，不是 UTC 09:00。"""
    ts = tu.local_input_to_epoch("2026-08-21T09:00", TPE)
    assert dt.datetime.fromtimestamp(ts, dt.timezone.utc) == \
        dt.datetime(2026, 8, 21, 1, 0, tzinfo=dt.timezone.utc)


def test_output_carries_the_offset(server_in_utc):
    out = tu.epoch_to_iso(EVENT_TS, TPE)
    assert out.startswith("2026-08-21 09:30:00")
    assert out.endswith("+08:00"), "時間字串沒帶偏移，正是對錯 8 小時看不出原因的來源"


def test_explicit_offset_in_string_wins(server_in_utc):
    """字串自帶偏移時以字串為準（API 呼叫端可以自己講清楚）。"""
    ts = tu.local_input_to_epoch("2026-08-21T09:00+00:00", TPE)
    assert dt.datetime.fromtimestamp(ts, dt.timezone.utc).hour == 9


def test_bogus_offsets_are_ignored(server_in_utc):
    assert tu.clean_offset("abc") is None
    assert tu.clean_offset(99999) is None      # 超出 UTC-12..+14
    assert tu.clean_offset("480") == 480
    # 擋不下來時會靜靜地算出離譜的時間，所以退回「沒給」而不是丟例外
    assert tu.local_input_to_epoch("2026-08-21T09:00", 99999) is not None


def test_unparsable_input_means_no_filter():
    assert tu.local_input_to_epoch("not-a-date", TPE) is None
    assert tu.local_input_to_epoch("", TPE) is None


# ---------------------------------------------------------------- 2, 3
def _seed_event():
    audit_db.init()
    c = audit_db.conn()
    c.execute("INSERT INTO audit_events(ts, username, ip, event_type, target, "
              "details_json) VALUES (?,?,?,?,?,?)",
              (EVENT_TS, "tz-tester", "127.0.0.1", "login", "t", "{}"))
    c.commit()


def test_filter_keeps_rows_the_naive_parse_would_have_dropped(admin_session,
                                                              server_in_utc):
    """台北 09:00 起算的篩選，要留下台北 09:30 的那筆。

    修正前：`09:00` 被當成 UTC → 門檻變成台北 17:00 → 這筆被無聲篩掉。
    """
    c, _, _ = admin_session
    _seed_event()
    r = c.get("/admin/audit", params={"q_user": "tz-tester",
                                      "q_from": "2026-08-21T09:00",
                                      "q_to": "2026-08-21T10:00",
                                      "tz_offset": TPE})
    assert r.status_code == 200
    # **判準要挑「只有真的列出那一筆才會出現」的訊號**。
    # 用帳號名當判準是不夠的 —— 它同時出現在篩選下拉的使用者清單裡，
    # 所以就算資料被篩掉也照樣「找得到」（變異驗證當場抓到這個盲點）。
    marker = 'data-ts="%d"' % int(EVENT_TS * 1000)
    assert marker in r.text, "台北時區的篩選把自己時區內的資料篩掉了"


def test_export_time_column_matches_what_the_user_sees(admin_session,
                                                       server_in_utc):
    c, _, _ = admin_session
    _seed_event()
    r = c.get("/admin/audit/export.csv", params={"q_user": "tz-tester",
                                                 "tz_offset": TPE})
    assert r.status_code == 200
    body = r.text
    assert "2026-08-21 09:30:00+08:00" in body, \
        f"CSV 時間欄不是使用者看到的時間（或沒帶偏移）：{body[:300]}"


def test_export_without_offset_still_states_its_timezone(admin_session,
                                                         server_in_utc):
    """沒帶偏移（curl / 排程）→ 退回伺服器時區，但**必須寫明是哪個時區**。"""
    c, _, _ = admin_session
    _seed_event()
    r = c.get("/admin/audit/export.csv", params={"q_user": "tz-tester"})
    assert "2026-08-21 01:30:00+00:00" in r.text


# ------------------------------------------------------------------ 4
def test_uploads_page_filter_uses_the_same_conversion(admin_session,
                                                      server_in_utc):
    """`/admin/uploads` 是**稽核員專屬**頁（管理員刻意被擋 403，職責分離），
    所以這裡要用稽核員身分開，不是管理員。"""
    from fastapi.testclient import TestClient
    from app.core import permissions, sessions, user_manager
    import app.main as app_main
    uid = user_manager.create_local("tz-auditor", "稽核員", "AuditPass1234")
    permissions.set_subject_roles("user", str(uid), ["auditor"])
    tok, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
    c = TestClient(app_main.app)
    c.cookies.set(sessions.COOKIE_NAME, tok)
    audit_db.init()
    conn = audit_db.conn()
    conn.execute("INSERT INTO audit_events(ts, username, ip, event_type, target,"
                 " details_json) VALUES (?,?,?,?,?,?)",
                 (EVENT_TS, "tz-uploader", "127.0.0.1", "tool_invoke",
                  "pdf-merge", '{"filename": "tz-sample.pdf"}'))
    conn.commit()
    r = c.get("/admin/uploads", params={"q_from": "2026-08-21T09:00",
                                        "q_to": "2026-08-21T10:00",
                                        "tz_offset": TPE})
    assert r.status_code == 200
    assert "tz-sample.pdf" in r.text, "上傳記錄頁的篩選仍在用伺服器時區"


# ------------------------------------------------------------------ 5
def test_no_naive_time_parsing_left_in_admin_routes():
    """管理區不可再出現「不帶時區」的 `fromisoformat().timestamp()` /
    `fromtimestamp()`。

    用 `ast` 找呼叫節點，不做字串比對 —— 說明文字裡本來就會提到這兩個名字
    （本專案踩過「掃描連註解一起掃」兩次）。
    """
    bad = []
    for f in (ROOT / "app" / "admin").rglob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else ""
            if name == "fromtimestamp" and not any(
                    k.arg == "tz" for k in node.keywords):
                bad.append(f"{f.name}:{node.lineno} fromtimestamp() 沒帶 tz")
            if name == "fromisoformat":
                bad.append(f"{f.name}:{node.lineno} 直接 fromisoformat()"
                           "（請走 timeutil.local_input_to_epoch）")
    assert not bad, "管理區還有依賴伺服器時區的時間解析：" + "; ".join(bad)


# ------------------------------------------------------------------ 6
@pytest.mark.parametrize("tpl", ["admin_audit.html", "admin_uploads.html"])
def test_frontend_sends_inverted_offset(tpl):
    """JS 的 `getTimezoneOffset()` 正負號與慣例相反（台北回 -480）。

    送錯號不會壞掉，只會**偏兩倍時差**，而且看起來「有處理時區」——
    比完全沒處理更難發現，所以這裡守住那個負號。
    """
    t = (ROOT / "app" / "admin" / "templates" / tpl).read_text(encoding="utf-8")
    assert "-new Date().getTimezoneOffset()" in t
    assert 'name="tz_offset"' in t
