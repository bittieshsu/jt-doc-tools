"""認證「開 / 關」兩種模式下的全功能矩陣（發版必跑）。

## 為什麼要有這支

這個專案的每一條路徑幾乎都有「認證開」與「認證關」兩種行為：工作區的儲存鍵、
作業歸屬、通知偏好、權限閘、admin 頁的可見性…… 而**兩邊很容易只顧到一邊**：

* 認證關閉時 `workspace.user_key()` 回共用鍵、開啟時回 `u<id>` —— 任何新功能只要
  用到「這是誰的」就同時踩到兩條路。
* `require_admin` 在認證關閉時全員放行，開啟時只有 admin —— 新的 admin 頁若忘了
  掛 dependency，只有在「認證開啟」的情境才會被發現。
* 「我的作業」在認證關閉時以來源電腦區分、開啟時以帳號區分。

人工把 41 個工具在兩種模式下各點一遍不現實，一定會漏。這裡用同一組斷言跑兩遍，
讓「兩種模式都正常」變成每次 `pytest` 都會驗的事，而不是發版前才想起來的清單。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app.tool_registry import discover_tools

#: 每個使用者都該進得去的共用頁面
_COMMON_PAGES = ["/", "/my-jobs", "/healthz"]

#: 只有管理員該進得去的頁面（認證關閉時全員放行，屬預期）
_ADMIN_PAGES = ["/admin/assets", "/admin/jobs", "/admin/notify",
                "/admin/system-status", "/admin/retention"]

#: 一般使用者也能呼叫的 API
_USER_APIS = ["/api/jobs", "/api/my/notify", "/api/my/inbox"]


def _tool_urls() -> list[str]:
    return [f"/tools/{t.metadata.id}/" for t in discover_tools()]


# ---------------- 認證關閉（單機模式）----------------

def test_all_tool_pages_load_with_auth_off(auth_off):
    c = TestClient(app_main.app)
    bad = [(u, c.get(u).status_code) for u in _tool_urls()
           if c.get(u).status_code != 200]
    assert not bad, f"認證關閉時這些工具頁載不起來：{bad}"


def test_common_pages_with_auth_off(auth_off):
    c = TestClient(app_main.app)
    bad = [(u, c.get(u).status_code) for u in _COMMON_PAGES
           if c.get(u).status_code != 200]
    assert not bad, bad


def test_admin_pages_open_with_auth_off(auth_off):
    """單機模式沒有角色之分 —— admin 頁全員可進（既有設計，不是漏洞）。"""
    c = TestClient(app_main.app)
    bad = [(u, c.get(u).status_code) for u in _ADMIN_PAGES
           if c.get(u).status_code != 200]
    assert not bad, bad


def test_user_apis_with_auth_off(auth_off):
    c = TestClient(app_main.app)
    for u in _USER_APIS:
        r = c.get(u)
        assert r.status_code == 200, (u, r.status_code)


def test_job_scope_is_ip_based_with_auth_off(auth_off):
    from app.core import job_store
    job_store.init()
    c = TestClient(app_main.app, client=("10.0.0.9", 1))
    assert c.get("/api/jobs").json()["scope"] == "ip"


def test_workspace_uses_shared_key_with_auth_off(auth_off):
    from app.core import workspace as ws
    assert ws.key_for_user_id(None) == ws._SINGLE_KEY


# ---------------- 認證開啟 ----------------

def _user_client(username: str):
    from app.core import sessions, user_manager
    uid = user_manager.create_local(username, username, "UserPass1234")
    token, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
    c = TestClient(app_main.app)
    c.cookies.set(sessions.COOKIE_NAME, token)
    return uid, c


def test_all_tool_pages_load_for_admin_with_auth_on(admin_session):
    c, _, _ = admin_session
    bad = [(u, c.get(u).status_code) for u in _tool_urls()
           if c.get(u).status_code != 200]
    assert not bad, f"認證開啟時管理員開不了這些工具頁：{bad}"


def test_common_pages_with_auth_on(admin_session):
    c, _, _ = admin_session
    bad = [(u, c.get(u).status_code) for u in _COMMON_PAGES
           if c.get(u).status_code != 200]
    assert not bad, bad


def test_admin_pages_blocked_for_regular_user(admin_session):
    """**只有認證開啟時才驗得到** —— 新的 admin 頁忘了掛權限 dependency，
    在單機模式下完全看不出來（那時本來就全員放行）。"""
    _, cu = _user_client("matrix-user-a")
    leaked = [u for u in _ADMIN_PAGES if cu.get(u).status_code == 200]
    assert not leaked, f"一般使用者進得去這些 admin 頁：{leaked}"


def test_anonymous_is_redirected_with_auth_on(admin_session):
    anon = TestClient(app_main.app)
    for u in ["/my-jobs", "/"]:
        assert anon.get(u, follow_redirects=False).status_code in (302, 401), u


def test_user_apis_require_login_with_auth_on(admin_session):
    anon = TestClient(app_main.app)
    for u in _USER_APIS:
        assert anon.get(u).status_code in (401, 302), u


def test_job_scope_is_user_based_with_auth_on(admin_session):
    from app.core import job_store
    job_store.init()
    _, cu = _user_client("matrix-user-b")
    assert cu.get("/api/jobs").json()["scope"] == "user"


def test_workspace_key_is_per_user_with_auth_on(admin_session):
    from app.core import workspace as ws
    assert ws.key_for_user_id(7) == "u7"
    with pytest.raises(ws.WorkspaceError):
        ws.key_for_user_id(None)      # 匿名沒有工作區


def test_notify_prefs_are_per_user_with_auth_on(admin_session):
    """兩個人的通知偏好不可互相覆蓋 —— 認證關閉時大家共用一份，開啟後必須分開。

    用 Telegram 的 chat id 當樣本：**Email 的收件位址已不再由使用者自己填**
    （v1.14.6 起只認帳號上的信箱），拿它來測「各自獨立」會恆等於空字串。
    """
    uid_a, ca = _user_client("matrix-user-c")
    uid_b, cb = _user_client("matrix-user-d")
    ca.post("/api/my/notify", json={"enabled": True, "telegram_chat_id": "111"})
    cb.post("/api/my/notify", json={"enabled": True, "telegram_chat_id": "222"})
    assert ca.get("/api/my/notify").json()["prefs"]["telegram_chat_id"] == "111"
    assert cb.get("/api/my/notify").json()["prefs"]["telegram_chat_id"] == "222"


def test_inbox_is_per_user_with_auth_on(admin_session):
    import time

    from app.core import job_store
    from app.core.job_manager import Job
    job_store.init()
    uid_a, ca = _user_client("matrix-user-e")
    uid_b, cb = _user_client("matrix-user-f")
    j = Job(id="f" * 32, tool_id="pdf-merge", status="done")
    j.owner_id = uid_a
    j.updated_at = time.time()
    job_store.upsert(j)
    assert len(ca.get("/api/my/inbox").json()["items"]) == 1
    assert cb.get("/api/my/inbox").json()["items"] == []
