"""「我的工作」/ 管理區工作監控的 API 與權限邊界。

重點在**水平越權**：工作清單是新的一條資料外洩管道 —— 在這之前 job_id 只活在
送出者那個分頁的 JS 變數裡，別人根本無從得知；現在有了列表端點，如果沒有正確
依歸屬過濾，任何登入者都能看到（甚至下載）別人轉的檔案。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import main as app_main
from app.core import job_store
from app.core.job_manager import Job


def _seed(job_id: str, owner_id=None, client_ip="", tool="pdf-merge",
          status="done", filename="機密報表.pdf") -> Job:
    j = Job(id=job_id, tool_id=tool, status=status,
            meta={"filename": filename})
    j.owner_id = owner_id
    j.client_ip = client_ip
    job_store.upsert(j)
    return j


def _user_client(username, password="UserPass1234", roles=None):
    from app.core import sessions, user_manager
    uid = user_manager.create_local(username, username, password, roles=roles)
    token, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
    c = TestClient(app_main.app)
    c.cookies.set(sessions.COOKIE_NAME, token)
    return uid, c


# ---------------- 認證關閉（單機模式）----------------

def test_auth_off_scopes_by_source_ip(auth_off):
    """沒有帳號可用時以來源 IP 區分 —— 至少不同電腦看不到彼此的工作。

    用兩個不同來源位址的 client 實測，不寫死 IP 字串（TestClient 預設的來源
    不是 127.0.0.1，寫死會測到假的東西）。
    """
    job_store.init()
    ca = TestClient(app_main.app, client=("192.168.50.7", 1111))
    cb = TestClient(app_main.app, client=("192.168.50.8", 2222))
    ip_a = ca.get("/api/jobs").json()
    assert ip_a["scope"] == "ip"
    _seed("a" * 32, client_ip="192.168.50.7", filename="我的.pdf")
    _seed("b" * 32, client_ip="192.168.50.8", filename="別台電腦的.pdf")

    names_a = [j["filename"] for j in ca.get("/api/jobs").json()["jobs"]]
    names_b = [j["filename"] for j in cb.get("/api/jobs").json()["jobs"]]
    assert names_a == ["我的.pdf"], names_a
    assert names_b == ["別台電腦的.pdf"], names_b


def test_auth_off_page_renders(auth_off):
    c = TestClient(app_main.app)
    assert c.get("/my-jobs").status_code == 200


# ---------------- 認證開啟 ----------------

def test_requires_login_when_auth_on(admin_session):
    """未登入時不可列工作清單（否則等於公開所有人的檔名）。"""
    anon = TestClient(app_main.app)
    assert anon.get("/api/jobs").status_code in (401, 302)


def test_user_sees_only_own_jobs(admin_session):
    """**水平越權**：A 不可看到 B 的工作。"""
    job_store.init()
    uid_a, ca = _user_client("jobs-alice")
    uid_b, cb = _user_client("jobs-bob")
    _seed("c" * 32, owner_id=uid_a, filename="alice.pdf")
    _seed("d" * 32, owner_id=uid_b, filename="bob.pdf")

    names_a = [j["filename"] for j in ca.get("/api/jobs").json()["jobs"]]
    names_b = [j["filename"] for j in cb.get("/api/jobs").json()["jobs"]]
    assert "alice.pdf" in names_a and "bob.pdf" not in names_a
    assert "bob.pdf" in names_b and "alice.pdf" not in names_b


def test_ip_scope_not_used_when_auth_on(admin_session):
    """認證開啟後就必須用帳號歸屬 —— 不可因為同一個 IP 就看到別人的工作
    （辦公室裡大家都在同一個網段，那等於沒有隔離）。"""
    job_store.init()
    uid_a, ca = _user_client("jobs-carol")
    _seed("e" * 32, owner_id=None, client_ip="127.0.0.1", filename="無主.pdf")
    d = ca.get("/api/jobs").json()
    assert d["scope"] == "user"
    assert "無主.pdf" not in [j["filename"] for j in d["jobs"]]


def test_cannot_cancel_another_users_job(admin_session):
    job_store.init()
    uid_a, ca = _user_client("jobs-dave")
    uid_b, cb = _user_client("jobs-erin")
    _seed("f" * 32, owner_id=uid_b, status="running")
    r = ca.post("/api/jobs/" + "f" * 32 + "/cancel")
    assert r.status_code == 404, "不可取消別人的工作，且不該確認它存在"


# ---------------- 管理區 ----------------

def test_admin_sees_all_jobs(admin_session):
    c, _, _ = admin_session
    job_store.init()
    uid_a, _ = _user_client("jobs-frank")
    _seed("0" * 32, owner_id=uid_a, filename="frank.pdf")
    _seed("1" * 32, client_ip="10.0.0.5", filename="anon.pdf")
    d = c.get("/admin/jobs/api/list").json()
    names = [j["filename"] for j in d["jobs"]]
    assert "frank.pdf" in names and "anon.pdf" in names
    assert "runtime" in d and "office" in d and "memory" in d


def test_admin_list_blocked_for_regular_user(admin_session):
    _, cu = _user_client("jobs-grace")
    assert cu.get("/admin/jobs/api/list").status_code == 403
    assert cu.get("/admin/jobs").status_code == 403


def test_admin_concurrency_blocked_for_regular_user(admin_session):
    _, cu = _user_client("jobs-heidi")
    r = cu.post("/admin/jobs/api/concurrency",
                json={"max_concurrent_jobs": 16})
    assert r.status_code == 403


def test_admin_can_pause_and_resume(admin_session):
    c, _, _ = admin_session
    try:
        assert c.post("/admin/jobs/api/pause",
                      json={"paused": True}).json()["paused"] is True
        assert c.get("/admin/jobs/api/list").json()["runtime"]["paused"] is True
    finally:
        c.post("/admin/jobs/api/pause", json={"paused": False})


def test_admin_concurrency_is_clamped(admin_session):
    """管理員填一個誇張的數字不可以真的生效 —— 那是直接把機器打到 OOM 的捷徑。"""
    c, _, _ = admin_session
    from app.core import concurrency_settings as cs
    try:
        d = c.post("/admin/jobs/api/concurrency",
                   json={"max_concurrent_jobs": 9999,
                         "max_office_concurrent": 9999,
                         "reserve_mb": 999999}).json()["conc"]
        assert d["max_concurrent_jobs"] <= cs.hard_max_jobs()
        assert d["max_office_concurrent"] <= cs.hard_max_office()
        assert d["reserve_mb"] <= 8192
    finally:
        cs.save({"max_concurrent_jobs": 2, "max_office_concurrent": 1,
                 "reserve_mb": 768})


def test_macos_forces_single_office_conversion(monkeypatch):
    """macOS 上多個 soffice 會在 Aqua bootstrap 競爭 → 硬上限必須是 1，
    而且 UI 要說明原因（不是留一個沒反應的欄位讓人猜）。"""
    from app.core import concurrency_settings as cs
    monkeypatch.setattr(cs, "is_macos", lambda: True)
    assert cs.hard_max_office() == 1
    assert cs.describe()["office_locked_reason"]


def test_non_macos_allows_more_than_one(monkeypatch):
    from app.core import concurrency_settings as cs
    monkeypatch.setattr(cs, "is_macos", lambda: False)
    monkeypatch.setattr(cs, "total_mb", lambda: 16384)
    assert cs.hard_max_office() >= 2
    assert cs.describe()["office_locked_reason"] == ""


def test_office_concurrency_setting_reaches_the_semaphore():
    """設定要真的傳到 office_convert 的號誌 —— 只存進 JSON 但沒套用等於沒做。"""
    from app.core import concurrency_settings as cs, office_convert
    try:
        cs.save({"max_office_concurrent": 2})
        if cs.hard_max_office() >= 2:
            assert office_convert.office_concurrency()["limit"] == 2
    finally:
        cs.save({"max_office_concurrent": 1})
        assert office_convert.office_concurrency()["limit"] == 1
