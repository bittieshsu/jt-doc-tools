"""下載成品與 API Token 呼叫都要留下稽核記錄（v1.16.71）。

## 由來

合規說明寫著「工具使用會記錄」，查證時發現兩塊沒有：

* **下載**全部是 GET，而 `tool_invoke` 只記 POST / PUT / DELETE ——
  檔案離開系統的那一刻沒有任何一筆。
* **根層級的 `/api/` 工具端點**（`/api/convert-to-pdf`、`/api/llm-review`）
  在 `_auth_gate` 一進來就被當成公開前綴放行，記 `tool_invoke` 那一段走不到。

使用者選擇：下載成品記一筆（同一人同一份五分鐘內只記一次），預覽與縮圖
不記；API Token 的呼叫記成工具使用，帶 Token 名稱與擁有者。

## 判準

* 「我的作業」的下載、工具結果頁的下載、工作區的下載（`?dl=1`）→ 各一筆 `file_download`
* 工作區的預覽（inline）、縮圖 → **不記**（反向對照：只驗「有記」的話，全部都記也會過）
* 同一個人連續下載同一份 → 只有一筆；換一個人 → 另一筆
* 失敗的下載（404）不記；認證關閉時不記（跟 `tool_invoke` 一致）
* 用 API Token 呼叫工具 API → `tool_invoke` 帶 `via=api_token` 與 Token 名稱、帳號是 Token 擁有者
* 根層級 `/api/convert-to-pdf` 用 Token 呼叫 → 補記 `tool_invoke`，目標 `office-to-pdf`、帶檔名
* 稽核員讀歷史檔案已經有 `auditor_view`，不重複記
"""
from __future__ import annotations

import io
import json
import sqlite3
import time

import fitz
import pytest
from fastapi.testclient import TestClient

import app.main as app_main


def _pdf(text: str = "hello") -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _max_id() -> int:
    from app.core import audit_db
    conn = sqlite3.connect(str(audit_db.audit_db_path()), timeout=5.0)
    try:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit_events").fetchone()[0]
    finally:
        conn.close()


def _rows(after: int, event: str, settle: float = 0.6) -> list[dict]:
    """等背景寫入落地後讀回來。先等一下再讀：要驗「沒有寫」的時候，
    太早讀會把「還沒寫」誤當成「沒有寫」。"""
    from app.core import audit_db
    time.sleep(settle)
    conn = sqlite3.connect(str(audit_db.audit_db_path()), timeout=5.0)
    try:
        cur = conn.execute(
            "SELECT username, ip, target, details_json FROM audit_events "
            "WHERE id > ? AND event_type = ? ORDER BY id", (after, event))
        return [{"username": r[0], "ip": r[1], "target": r[2],
                 "details": json.loads(r[3] or "{}")} for r in cur.fetchall()]
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _fresh_dedupe():
    from app.core import access_audit
    access_audit.reset()
    yield
    access_audit.reset()


def _ws_upload(c: TestClient, name="報價單.pdf") -> str:
    r = c.post("/workspace/save", files={"file": (name, _pdf(), "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()["file"]["file_id"]


# ---------- 單元：判準本身 ----------

def test_only_successful_get_attachments_count_as_downloads():
    from app.core import access_audit as aa
    att = {"content-disposition": "attachment; filename=\"a.pdf\""}
    assert aa.is_download("GET", 200, att)
    assert aa.is_download("GET", 206, att), "Range 續傳的回應也是下載"
    assert not aa.is_download("GET", 404, att)
    assert not aa.is_download("POST", 200, att), "POST 的回應由 tool_invoke 記"
    assert not aa.is_download("GET", 200, {"content-disposition": "inline; filename=a.pdf"})
    assert not aa.is_download("GET", 200, {})


def test_filename_prefers_the_utf8_form():
    from app.core import access_audit as aa
    from app.core.http_utils import content_disposition
    cd = content_disposition("採購合約.pdf")
    assert aa.filename_from_disposition(cd) == "採購合約.pdf"
    assert aa.filename_from_disposition('attachment; filename="a b.pdf"') == "a b.pdf"
    assert aa.filename_from_disposition("") == ""


def test_dedupe_is_per_person_and_per_file():
    from app.core import access_audit as aa
    t0 = 1_000_000.0
    assert aa._first_time("alice", "/x", t0)
    assert not aa._first_time("alice", "/x", t0 + 10), "五分鐘內同一份只記一筆"
    assert aa._first_time("bob", "/x", t0 + 10), "換一個人要另記一筆"
    assert aa._first_time("alice", "/y", t0 + 10), "換一份檔案要另記一筆"
    assert aa._first_time("alice", "/x", t0 + aa.DEDUPE_SECONDS + 1), "過了去重時間要再記"


def test_target_names_the_tool_or_area():
    from app.core import access_audit as aa
    assert aa.target_for("/tools/pdf-ocr/download/abc") == "pdf-ocr"
    assert aa.target_for("/workspace/file/abc") == "workspace"
    assert aa.target_for("/admin/assets/export") == "admin"
    assert aa.target_for("/api/jobs/nope/download") == "jobs"


# ---------- 整合：真的送請求進去 ----------

def test_workspace_download_is_recorded_but_preview_and_thumbnail_are_not(admin_session):
    c, admin, _ = admin_session
    fid = _ws_upload(c)
    start = _max_id()
    assert c.get(f"/workspace/file/{fid}").status_code == 200        # 預覽（inline）
    assert c.get(f"/workspace/thumb/{fid}").status_code == 200       # 縮圖
    assert not _rows(start, "file_download"), "預覽與縮圖不可以記成下載"

    r = c.get(f"/workspace/file/{fid}?dl=1")
    assert r.status_code == 200
    rows = _rows(start, "file_download")
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["username"] == admin
    assert row["target"] == "workspace"
    assert row["details"]["filename"] == "報價單.pdf"
    assert row["details"]["size_bytes"] > 0
    assert row["ip"], "要記下來源 IP"


def test_downloading_the_same_file_twice_is_one_row(admin_session):
    c, _, _ = admin_session
    fid = _ws_upload(c)
    start = _max_id()
    for _ in range(3):
        assert c.get(f"/workspace/file/{fid}?dl=1").status_code == 200
    assert len(_rows(start, "file_download")) == 1


def _merge_job(c: TestClient) -> str:
    r = c.post("/tools/pdf-merge/submit",
               files=[("file", ("a.pdf", _pdf("a"), "application/pdf")),
                      ("file", ("b.pdf", _pdf("b"), "application/pdf"))])
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    for _ in range(200):
        s = c.get(f"/api/jobs/{jid}").json()
        if s.get("status") in ("done", "error"):
            break
        time.sleep(0.05)
    assert s.get("status") == "done", s
    return jid


def test_job_result_download_names_the_tool(admin_session):
    c, admin, _ = admin_session
    jid = _merge_job(c)
    start = _max_id()
    r = c.get(f"/api/jobs/{jid}/download")
    assert r.status_code == 200
    rows = _rows(start, "file_download")
    assert len(rows) == 1, rows
    assert rows[0]["username"] == admin, "/api/ 底下的下載也要認得出是誰（session）"
    assert rows[0]["target"] == "pdf-merge"
    assert "via" not in rows[0]["details"], "瀏覽器下載不是 API Token 呼叫"


def test_a_failed_download_is_not_recorded(admin_session):
    c, _, _ = admin_session
    start = _max_id()
    assert c.get("/api/jobs/" + "0" * 32 + "/download").status_code == 404
    assert not _rows(start, "file_download")


def test_nothing_is_recorded_when_auth_is_off(admin_session):
    """認證關閉時不記（跟 `tool_invoke` 一致）。

    要留著一個**還查得到的 session** 才驗得到這條：單純的認證關閉根本認不出
    是誰，拿掉判斷照樣不會記（變異驗證時就是這樣綠的）。所以這裡只改設定、
    不走 `disable_auth`（那支會把 session 全部清掉）。"""
    from app.core import auth_settings
    c, _, _ = admin_session
    jid = _merge_job(c)
    s = auth_settings.get()
    s["backend"] = "off"
    auth_settings.save(s)
    assert not auth_settings.is_enabled()
    start = _max_id()
    assert c.get(f"/api/jobs/{jid}/download").status_code == 200
    assert not _rows(start, "file_download")


def test_auditor_history_reads_are_not_counted_twice(admin_session):
    """`/admin/history/` 由 `auditor_view` 記，這裡不能再記一筆。直接拿一個
    附件回應走一次判斷（建歷史資料要整套用印流程，這裡驗的只是排除規則）。"""
    from starlette.requests import Request
    from starlette.responses import Response
    from app.core import access_audit as aa
    c, admin, _ = admin_session
    start = _max_id()

    def _req(path):
        req = Request({"type": "http", "method": "GET", "path": path, "headers": [],
                       "query_string": b"", "client": ("10.0.0.1", 1), "state": {}})
        req.state.user = {"user_id": 1, "username": admin}
        return req
    resp = Response(b"x", headers={"content-disposition": 'attachment; filename="h.pdf"'})
    aa.after_response(_req("/admin/history/fill/" + "a" * 32 + "/file/output"), resp)
    assert not _rows(start, "file_download")
    aa.after_response(_req("/admin/assets/export"), resp)
    assert len(_rows(start, "file_download")) == 1, "同一個判斷換一個路徑要記得到（反向對照）"


# ---------- API Token ----------

@pytest.fixture
def token_for_admin(admin_session):
    from app.core import auth_db
    from app.core.api_tokens import api_tokens
    c, admin, _ = admin_session
    uid = auth_db.conn().execute(
        "SELECT id FROM users WHERE username = ?", (admin,)).fetchone()["id"]
    tok = api_tokens.create("報表排程")
    api_tokens.assign_owner(tok.token, uid)
    try:
        yield tok.token, admin
    finally:
        api_tokens.revoke(tok.token)


def test_tool_api_calls_with_a_token_say_which_token(token_for_admin):
    token, admin = token_for_admin
    api = TestClient(app_main.app)                      # 沒有 session，只有 Token
    start = _max_id()
    r = api.post("/tools/pdf-rotate/api/pdf-rotate",
                 headers={"Authorization": f"Bearer {token}"},
                 files={"file": ("合約.pdf", _pdf(), "application/pdf")},
                 data={"angle": "90"})
    assert r.status_code == 200, r.text
    rows = _rows(start, "tool_invoke")
    assert len(rows) == 1, rows
    d = rows[0]["details"]
    assert rows[0]["username"] == admin, "帳號要是 Token 的擁有者"
    assert rows[0]["target"] == "pdf-rotate"
    assert d.get("via") == "api_token" and d.get("token") == "報表排程", d
    assert d.get("filename") == "合約.pdf"
    assert token not in json.dumps(rows[0], ensure_ascii=False), "不可以把 Token 本身寫進稽核"


def test_web_tool_calls_are_not_marked_as_token_calls(admin_session):
    c, _, _ = admin_session
    start = _max_id()
    r = c.post("/tools/pdf-rotate/api/pdf-rotate",
               files={"file": ("a.pdf", _pdf(), "application/pdf")}, data={"angle": "90"})
    assert r.status_code == 200, r.text
    rows = _rows(start, "tool_invoke")
    assert rows and "via" not in rows[0]["details"]


def test_downloads_with_a_token_say_which_token(token_for_admin, admin_session):
    token, admin = token_for_admin
    c, _, _ = admin_session
    jid = _merge_job(c)
    api = TestClient(app_main.app)
    start = _max_id()
    r = api.get(f"/api/jobs/{jid}/download", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    rows = _rows(start, "file_download")
    assert len(rows) == 1, rows
    assert rows[0]["username"] == admin
    assert rows[0]["details"].get("via") == "api_token"
    assert rows[0]["details"].get("token") == "報表排程"


def test_root_api_tool_endpoints_are_recorded(token_for_admin):
    """`/api/convert-to-pdf` 原本在 `_auth_gate` 就被放行，一筆都沒有。
    轉檔成不成功不影響要不要記（網頁那條路失敗也照記，`status` 欄寫結果）。"""
    token, admin = token_for_admin
    api = TestClient(app_main.app)
    start = _max_id()
    r = api.post("/api/convert-to-pdf", headers={"Authorization": f"Bearer {token}"},
                 files={"file": ("會議紀錄.txt", "第一行\n".encode("utf-8"), "text/plain")})
    rows = _rows(start, "tool_invoke")
    assert len(rows) == 1, (r.status_code, rows)
    assert rows[0]["target"] == "office-to-pdf"
    assert rows[0]["username"] == admin
    d = rows[0]["details"]
    assert d.get("via") == "api_token" and d.get("token") == "報表排程"
    assert d.get("filename") == "會議紀錄.txt", "要抓得到上傳的檔名"
    assert d.get("status") == r.status_code
