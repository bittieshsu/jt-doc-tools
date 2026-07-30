"""換掉 job id 能不能看到別人的作業？

job id 是 `uuid4().hex`（128 位元隨機），猜不到也列舉不了 —— 但「猜不到」不能
當成存取控制。這裡把每一種組合都釘住。

**這支的由來**：實測發現「沒有主人的作業」任何登入者都拿得到。原本的註解寫著
「由第一個呼叫者認領」，但那行 `job.owner_id = uid` 改的是 `job_manager.get()`
從 DB 重建出來的**臨時物件**，沒有寫回資料庫 —— 於是每個人查都「認領」一次，
每個人都看得到。註解與實際行為不符是最難發現的一類 bug。
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

import pytest

from app import main as app_main
from app.core import job_store
from app.core.job_manager import Job


@pytest.fixture(autouse=True)
def _isolated_jobs_db(tmp_path, monkeypatch):
    """每個測試用自己的 jobs.sqlite。

    少了這層隔離，這裡的固定 job id（"a"*32 …）會跟其他測試檔的種子資料撞名，
    而 `job_store.upsert` **刻意不更新 owner_id**（擁有者不可被改寫，見
    `test_owner_cannot_be_reassigned_by_upsert`），於是留著上一個測試的擁有者
    → 本測試的擁有者反而拿到 404。症狀是「單獨跑會過、整套跑會失敗」。
    """
    d = tmp_path / "jobs"
    d.mkdir()
    monkeypatch.setattr("app.config.settings.data_dir", d)
    job_store.init()
    yield d


def _seed(jid, owner=None, client_ip="", result: Path | None = None):
    j = Job(id=jid, tool_id="pdf-merge", status="done",
            meta={"filename": "機密報表.pdf"})
    j.owner_id = owner
    j.client_ip = client_ip
    if result is not None:
        j.result_path = result
        j.result_filename = "機密報表.pdf"
    j.updated_at = time.time()
    job_store.upsert(j)
    return j


def _result(tmp_path) -> Path:
    p = tmp_path / "secret.pdf"
    p.write_bytes(b"%PDF-1.4 secret")
    return p


def _user(username):
    from app.core import sessions, user_manager
    uid = user_manager.create_local(username, username, "UserPass1234")
    tok, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
    c = TestClient(app_main.app)
    c.cookies.set(sessions.COOKIE_NAME, tok)
    return uid, c


# ---------------- 認證開啟 ----------------

def test_other_user_cannot_read_or_download(admin_session, tmp_path):
    job_store.init()
    uid_a, ca = _user("acl-owner")
    uid_b, cb = _user("acl-other")
    jid = "a" * 32
    _seed(jid, owner=uid_a, result=_result(tmp_path))
    assert ca.get(f"/api/jobs/{jid}").status_code == 200
    # 同一個 404（不是 403）—— 不對非擁有者確認這個 id 存在
    assert cb.get(f"/api/jobs/{jid}").status_code == 404
    assert cb.get(f"/api/jobs/{jid}/download").status_code == 404


def test_anonymous_cannot_read(admin_session, tmp_path):
    job_store.init()
    uid_a, _ = _user("acl-owner2")
    jid = "b" * 32
    _seed(jid, owner=uid_a, result=_result(tmp_path))
    assert TestClient(app_main.app).get(f"/api/jobs/{jid}").status_code == 404


def test_ownerless_job_is_not_readable_by_any_logged_in_user(admin_session,
                                                             tmp_path):
    """**這是實測抓到的漏洞**：owner_id 為 None 的作業（認證啟用前送出的舊作業）
    原本任何登入者都拿得到 —— 因為「認領」根本沒寫回資料庫，於是每個人查都
    認領一次。無主作業的合法存取者無從判斷，必須 fail-secure。
    """
    job_store.init()
    _uid, cu = _user("acl-nobody")
    jid = "c" * 32
    _seed(jid, owner=None, result=_result(tmp_path))
    assert cu.get(f"/api/jobs/{jid}").status_code == 404
    assert cu.get(f"/api/jobs/{jid}/download").status_code == 404


def test_admin_can_still_reach_ownerless_job(admin_session, tmp_path):
    """管理員要能處理無主作業（支援 / 排除問題），一般使用者不行。"""
    c, _, _ = admin_session
    job_store.init()
    jid = "d" * 32
    _seed(jid, owner=None, result=_result(tmp_path))
    assert c.get(f"/api/jobs/{jid}").status_code == 200


def test_cannot_cancel_someone_elses_job(admin_session):
    job_store.init()
    uid_a, _ = _user("acl-c1")
    _uid_b, cb = _user("acl-c2")
    jid = "e" * 32
    j = _seed(jid, owner=uid_a)
    j.status = "running"
    job_store.upsert(j)
    assert cb.post(f"/api/jobs/{jid}/cancel").status_code == 404


def test_list_never_leaks_other_users(admin_session, tmp_path):
    job_store.init()
    uid_a, _ = _user("acl-l1")
    _uid_b, cb = _user("acl-l2")
    _seed("f" * 32, owner=uid_a, result=_result(tmp_path))
    assert cb.get("/api/jobs").json()["jobs"] == []
    assert cb.get("/api/my/inbox").json()["items"] == []


# ---------------- 認證關閉（單機模式）----------------

def test_auth_off_is_open_by_design(auth_off, tmp_path):
    """認證關閉時**沒有存取控制**（那正是該模式的定義）—— 知道連結的人就拿得到。

    這裡把它釘成明確的預期行為，並確認 UI 有照實說明：使用者不該以為
    「清單按來源電腦分開」等於隔離。
    """
    job_store.init()
    jid = "0" * 32
    _seed(jid, client_ip="10.0.0.1", result=_result(tmp_path))
    other = TestClient(app_main.app, client=("10.0.0.99", 1))
    assert other.get(f"/api/jobs/{jid}").status_code == 200
    # 清單仍按來源電腦分（避免把別人的攤在眼前），但那是體貼不是安全邊界
    assert other.get("/api/jobs").json()["jobs"] == []
    page = other.get("/my-jobs").text
    assert "知道連結" in page, "「我的作業」頁必須照實說明認證關閉時的保護程度"


def test_owner_cannot_be_reassigned_by_upsert(admin_session, tmp_path):
    """擁有者不可被後續的寫入改掉。

    `upsert` 的 ON CONFLICT 子句刻意**不含** owner_id —— 否則任何能觸發狀態更新
    的路徑都可能把作業的歸屬換掉，等於繞過整個存取控制。
    """
    uid_a, ca = _user("acl-immutable-a")
    uid_b, cb = _user("acl-immutable-b")
    jid = "9" * 32
    _seed(jid, owner=uid_a, result=_result(tmp_path))
    # 試圖用 B 的身分覆寫同一筆
    j = Job(id=jid, tool_id="pdf-merge", status="done")
    j.owner_id = uid_b
    job_store.upsert(j)
    assert job_store.get(jid)["owner_id"] == uid_a, "擁有者被改寫了"
    assert cb.get(f"/api/jobs/{jid}").status_code == 404


# ---------- pdf-to-office 的「改善報告」端點（v1.14.6 資安稽核補上） ----------

def _seed_report_job(jid: str, owner_uid: int | None, filename: str):
    """造一個「已完成且有改善報告」的作業紀錄（沿用本檔的 `_seed` 風格）。

    走真的轉檔太慢，而且報告不一定會產生（要看引擎與文件內容）—— 測 ACL 不需要
    那些。
    """
    j = Job(id=jid, tool_id="pdf-to-office", status="done",
            meta={"filename": filename,
                  "summary": {"report": {"pdf_truth": {"pages": 3,
                                                       "language": "zh-Hant"}}}})
    j.owner_id = owner_uid
    j.updated_at = time.time()
    job_store.upsert(j)
    return j


def test_report_endpoint_denies_other_user(admin_session):
    """報告裡有來源檔名、頁數、語言、字型清單 —— 等於別人文件的資訊。

    這個端點就排在**有**做 ACL 的預覽端點下面，原本卻沒驗（只看程式碼很容易
    以為兩個都有）。非擁有者要拿到 404，訊息也不可確認 id 存在。
    """
    job_store.init()
    uid_a, _ca = _user("rep-owner")
    _uid_b, cb = _user("rep-other")
    jid = "c" * 32
    _seed_report_job(jid, uid_a, "alice-secret-payroll.pdf")
    r = cb.get(f"/tools/pdf-to-office/report/{jid}")
    assert r.status_code == 404, f"別人讀到了：{r.status_code}"
    assert "alice-secret-payroll" not in r.text
    assert "zh-Hant" not in r.text


def test_report_endpoint_allows_owner(admin_session):
    """本人要讀得到 —— fail-closed 不可以連自己都擋。"""
    job_store.init()
    uid_a, ca = _user("rep-owner2")
    jid = "d" * 32
    _seed_report_job(jid, uid_a, "alice-secret-payroll.pdf")
    r = ca.get(f"/tools/pdf-to-office/report/{jid}")
    assert r.status_code == 200, f"本人被擋：{r.status_code} {r.text[:150]}"
    assert "alice-secret-payroll" in r.text


def test_report_endpoint_rejects_malformed_id(admin_session):
    """id 格式不合先擋掉（避免拿它去拼路徑）。"""
    job_store.init()
    _uid, c = _user("rep-fmt")
    r = c.get("/tools/pdf-to-office/report/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code in (400, 404), r.status_code


# ---------- 「存到工作區」也是一條取得結果檔的路徑 ----------

def test_workspace_save_denies_other_users_job(admin_session, tmp_path):
    """`/workspace/save?job_id=...` 會把作業結果**複製進呼叫者的工作區** ——
    等於一條下載路徑，ACL 要與 `/api/jobs/*` 一致。"""
    job_store.init()
    uid_a, _ca = _user("ws-owner")
    _uid_b, cb = _user("ws-other")
    jid = "e" * 32
    _seed(jid, owner=uid_a, result=_result(tmp_path))
    r = cb.post("/workspace/save", data={"job_id": jid})
    assert r.status_code in (403, 404), f"別人存走了：{r.status_code}"
    assert "secret" not in r.text


def test_workspace_save_denies_ownerless_job(admin_session, tmp_path):
    """無主作業（認證開啟前產生的）不可以被任何登入者存進自己的工作區。

    這條路徑有自己一份歸屬判斷（`if job.owner_id is not None:`），所以
    `/api/jobs/*` 那邊修好之後它仍然是開的 —— 同一個判斷散在兩個地方就會這樣。
    """
    job_store.init()
    _uid_b, cb = _user("ws-other2")
    jid = "f" * 32
    _seed(jid, owner=None, result=_result(tmp_path))
    r = cb.post("/workspace/save", data={"job_id": jid})
    assert r.status_code in (403, 404), f"無主作業被存走了：{r.status_code}"


def test_workspace_save_allows_owner(admin_session, tmp_path):
    """本人要存得進去（fail-closed 不可以擋到正常流程）。"""
    job_store.init()
    uid_a, ca = _user("ws-owner2")
    jid = "0" * 32
    _seed(jid, owner=uid_a, result=_result(tmp_path))
    r = ca.post("/workspace/save", data={"job_id": jid})
    assert r.status_code in (200, 404), r.status_code   # 404 = 工作區停用
    if r.status_code == 404:
        assert "工作區" in r.text
