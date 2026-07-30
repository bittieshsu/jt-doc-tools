"""API token 閘與 CSRF 豁免的邊界。

## 兩個問題

### 1. `_api_token_gate` 用 `"/api/" in path` 判斷「這是 API」

該中介層自己的註解寫著「never block UI pages, static files, or admin」，但
`"/api/" in path` 會一併命中 **22 個 admin 端點**（`/admin/api/...`、
`/admin/jobs/api/...`）。於是「API token 強制驗證」一打開，管理員用瀏覽器
（session cookie，沒有 Bearer）開管理頁時，那些端點全部回 401 —— 管理區的
作業清單、LLM 設定、系統相依都會壞掉，而且錯誤訊息說「需要有效的 API token」，
看起來像設定壞了而不是這個判斷寫錯。

### 2. CSRF 只要看到 `Authorization: Bearer ` 就整個豁免

不驗那個 token 是真的。目前**不可直接利用** —— 跨站要帶自訂標頭會觸發 CORS
預檢，而本站沒有開放的 CORS 設定，所以瀏覽器根本不會把請求送出。但這條規則的
形狀是錯的：只要哪天加了 CORS，或出現別的能帶標頭的路徑，它就變成一句
「附上一個假標頭即可跳過 CSRF」。

修法不是去 CSRF 層驗 token（那會讓兩層互相依賴），而是：**請求帶著我們的
session cookie 時一律要 CSRF**。真正的 API 客戶端不會帶 session cookie。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as app_main


# ---------- 1. admin 端點不可被 API token 閘擋 ----------

@pytest.fixture
def enforced(admin_session):
    """把「API token 強制驗證」打開，測完關回去。"""
    from app.core.api_tokens import api_tokens
    before = api_tokens.is_enforced()
    api_tokens.set_enforce(True)
    yield admin_session
    api_tokens.set_enforce(before)


ADMIN_API_PATHS = (
    "/admin/api/sys-deps",
    "/admin/api/settings-export/categories",
    "/admin/jobs/api/list",
)


@pytest.mark.parametrize("path", ADMIN_API_PATHS)
def test_admin_api_works_with_session_while_enforce_on(enforced, path):
    """管理員用瀏覽器 session 存取 admin 的 /api/ 端點，不可被 token 閘擋。"""
    c, _, _ = enforced
    r = c.get(path)
    assert r.status_code != 401, (
        f"{path} 被 API token 閘擋掉了（管理區會壞掉）：{r.text[:120]}")
    assert r.status_code == 200, f"{path} → {r.status_code}"


def test_admin_api_still_requires_admin_session(enforced):
    """把 admin 從 token 閘排除，不可以順手變成沒有任何保護。"""
    c, _, _ = enforced
    anon = TestClient(app_main.app)
    r = anon.get("/admin/api/sys-deps", follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403), f"未登入卻拿到 {r.status_code}"


def test_real_api_path_still_enforced(enforced):
    """真正的 `/api/*` 在 enforce 開啟時仍必須要 token。"""
    anon = TestClient(app_main.app)
    r = anon.post("/api/pdf-rotate", files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 401, f"應該要 401，得到 {r.status_code}"


def test_tool_prefixed_api_still_enforced(enforced):
    """`/tools/<x>/api/*` 也仍然要 token（那是對外介面）。"""
    anon = TestClient(app_main.app)
    r = anon.post("/tools/pdf-rotate/api/pdf-rotate",
                  files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 401, f"應該要 401，得到 {r.status_code}"


def test_admin_api_still_reachable_with_bearer_token(enforced):
    """API.md 有記載 `/admin/api/*` 可用 `Authorization: Bearer ADMIN_TOKEN`。

    修「管理區被 token 閘擋掉」時**不可以**改成把 admin 整段排除 —— 那會把這條
    有記載的路徑一起關掉。正確做法是「沒帶 token 落回 session、帶了就驗」。
    """
    from app.core import auth_db
    from app.core.api_tokens import api_tokens
    uid = auth_db.conn().execute(
        "SELECT id FROM users WHERE username='jtdt-admin'").fetchone()["id"]
    tok = api_tokens.create("pytest-admin-token")
    api_tokens.assign_owner(tok.token, uid)
    anon = TestClient(app_main.app)          # 完全沒有 session cookie
    r = anon.get("/admin/api/sys-deps",
                 headers={"Authorization": f"Bearer {tok.token}"})
    assert r.status_code == 200, f"帶 token 的 API 呼叫被擋（{r.status_code}）"


def test_admin_api_rejects_bad_bearer_token(enforced):
    """帶了 token 就要真的驗 —— 假 token 不可以落回 session 路徑蒙混過去。"""
    anon = TestClient(app_main.app)
    r = anon.get("/admin/api/sys-deps",
                 headers={"Authorization": "Bearer definitely-not-valid"})
    assert r.status_code == 401, f"假 token 得到 {r.status_code}"


# ---------- 2. CSRF 豁免不可只看標頭 ----------

def test_csrf_not_exempt_when_session_cookie_present():
    """帶 session cookie 的請求一律要 CSRF —— 假的 Bearer 標頭不可以當免死金牌。

    直接測 middleware 的判斷函式：整體流程在測試套件裡是關掉 CSRF 的
    （`JTDT_CSRF_DISABLE=1`），走 HTTP 測不到這一段。
    """
    from app.core import csrf, sessions
    scope = {
        "type": "http", "method": "POST", "path": "/admin/users/create",
        "headers": [
            (b"authorization", b"Bearer not-a-real-token"),
            (b"cookie", f"{sessions.COOKIE_NAME}=abc123".encode()),
        ],
    }
    assert csrf._is_exempt(scope) is False


def test_csrf_exempt_for_real_api_client_without_cookie():
    """沒有 session cookie 的 API 客戶端仍然豁免（它沒有 CSRF 風險）。"""
    from app.core import csrf
    scope = {
        "type": "http", "method": "POST", "path": "/api/pdf-rotate",
        "headers": [(b"authorization", b"Bearer some-token")],
    }
    assert csrf._is_exempt(scope) is True


def test_csrf_not_exempt_without_authorization_header():
    from app.core import csrf
    scope = {"type": "http", "method": "POST", "path": "/admin/users/create",
             "headers": []}
    assert csrf._is_exempt(scope) is False


def test_csrf_saml_callback_still_exempt():
    """IdP 以 POST 導回的 SAML 回呼不可以被擋（另有簽章 + replay 防護）。"""
    from app.core import csrf
    for p in csrf._EXEMPT_PREFIXES:
        scope = {"type": "http", "method": "POST", "path": p + "/x",
                 "headers": []}
        assert csrf._is_exempt(scope) is True, p
