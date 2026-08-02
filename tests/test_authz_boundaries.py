"""登入後的授權邊界測試（2026-06-27 使用者要求）：

① 垂直越權：已登入的「非 admin」一般使用者不可碰任何 admin 頁 / admin 寫入。
② 工具權限：default-user 沒有的工具（pdf-fill / pdf-stamp）要被擋,有的要能用。
③ 水平越權：A 使用者不可存取 B 使用者的上傳檔 / 工作區檔。

在獨立 TestClient + 臨時資料庫做,不碰生產機。CSRF 在 conftest 以
JTDT_CSRF_DISABLE=1 關閉,故 POST 不需 token（驗的是授權,非 CSRF）。
"""
from __future__ import annotations

from io import BytesIO

import fitz
from fastapi.testclient import TestClient

from app import main as app_main


def _pdf() -> bytes:
    d = fitz.open()
    d.new_page().insert_text((72, 72), "secret")
    b = BytesIO()
    d.save(b)
    d.close()
    return b.getvalue()


def _user_client(username, password="UserPass1234", roles=None):
    """建一般使用者（預設 default-user 角色）+ 回已登入 client。"""
    from app.core import user_manager, sessions
    uid = user_manager.create_local(username, username, password, roles=roles)
    token, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
    c = TestClient(app_main.app)
    c.cookies.set(sessions.COOKIE_NAME, token)
    return uid, c


# ───────────────────────── ① 垂直越權 ─────────────────────────
def test_regular_user_cannot_view_admin_pages(admin_session):
    _, c = _user_client("alice_admin_view")
    for p in ["/admin/", "/admin/users", "/admin/audit", "/admin/auth-settings",
              "/admin/permissions", "/admin/log-forward", "/admin/api-tokens",
              "/admin/retention", "/admin/sso", "/admin/system-status"]:
        r = c.get(p, follow_redirects=False)
        assert r.status_code != 200, f"非 admin 竟然看得到 {p}（status 200）"
        assert r.status_code in (401, 403, 302, 303, 307), f"{p} → {r.status_code}"


def test_regular_user_cannot_call_admin_writes(admin_session):
    _, c = _user_client("alice_admin_write")
    # 改站名
    r = c.post("/admin/branding/site-name", json={"name": "hacked"})
    assert r.status_code in (401, 403, 302, 303), f"改站名 → {r.status_code}"
    # 關閉認證（最敏感）
    r = c.post("/admin/auth-settings/disable")
    assert r.status_code in (401, 403, 302, 303), f"關認證 → {r.status_code}"
    # 列出所有使用者（資料外洩）
    r = c.get("/admin/api/users", follow_redirects=False)
    assert r.status_code in (401, 403, 302, 303, 404), f"列 users → {r.status_code}"


def test_regular_user_cannot_create_admin_token(admin_session):
    _, c = _user_client("alice_token")
    r = c.post("/admin/api-tokens/create", json={"name": "evil"})
    assert r.status_code in (401, 403, 302, 303, 404), f"建 token → {r.status_code}"


# ───────────────────────── ② 工具權限 ─────────────────────────
def test_default_user_blocked_from_unpermitted_tools(admin_session):
    _, c = _user_client("bob_tools")
    # default-user 不含 pdf-fill / pdf-stamp
    for tool in ["pdf-fill", "pdf-stamp"]:
        r = c.get(f"/tools/{tool}/", follow_redirects=False)
        assert r.status_code != 200, f"default-user 竟能開 {tool}"
        assert r.status_code in (401, 403, 302, 303, 307), f"{tool} → {r.status_code}"


def test_default_user_can_use_permitted_tool(admin_session):
    _, c = _user_client("bob_ok")
    # 含 v1.12.41 補進 default-user 的 5 個無害工具
    for tool in ["pdf-merge", "pdf-wordcount", "submission-check",
                 "pdf-annotations", "pdf-annotations-flatten",
                 "pdf-annotations-strip"]:
        r = c.get(f"/tools/{tool}/", follow_redirects=False)
        assert r.status_code == 200, f"default-user 開不了被授權的 {tool} → {r.status_code}"


def test_unpermitted_tool_api_also_blocked(admin_session):
    """工具頁擋了,後端動作端點也要擋（不能只擋 UI）。"""
    _, c = _user_client("bob_api")
    r = c.post("/tools/pdf-fill/detect",
               files={"file": ("a.pdf", _pdf(), "application/pdf")})
    assert r.status_code in (401, 403, 302, 303), f"pdf-fill 動作端點 → {r.status_code}"


# ───────────────────────── ③ 水平越權 ─────────────────────────
def test_cross_user_workspace_file_blocked(admin_session):
    import app.core.workspace as ws
    ws.save_settings({"enabled": True, "per_user_quota_mb": 500,
                      "max_file_mb": 50, "retention_hours": -1})
    _, ca = _user_client("alice_ws")
    _, cb = _user_client("bob_ws")
    # A 存一個工作區檔
    r = ca.post("/workspace/save",
                data={"name": "secret"},
                files={"file": ("s.pdf", _pdf(), "application/pdf")})
    assert r.status_code == 200, r.text
    fid = r.json()["file"]["file_id"]
    # A 自己拿得到
    assert ca.get(f"/workspace/file/{fid}", follow_redirects=False).status_code == 200
    # B 不可下載 A 的檔（水平越權）
    rb = cb.get(f"/workspace/file/{fid}", follow_redirects=False)
    assert rb.status_code in (401, 403, 404), f"B 拿到 A 的工作區檔！→ {rb.status_code}"


def test_cross_user_upload_file_blocked(admin_session):
    """A 用 pdf-editor 上傳產生 upload_id,B 不可用該 id 取原檔（upload_owner ACL）。"""
    _, ca = _user_client("alice_up")
    _, cb = _user_client("bob_up")
    r = ca.post("/tools/pdf-editor/load",
                files={"file": ("a.pdf", _pdf(), "application/pdf")})
    assert r.status_code == 200, r.text
    uid = r.json().get("upload_id")
    assert uid, r.text
    # A 自己拿得到原檔
    assert ca.get(f"/tools/pdf-editor/file/{uid}", follow_redirects=False).status_code == 200
    # B 用 A 的 upload_id 取原檔 → 必須被擋（水平越權）
    rb = cb.get(f"/tools/pdf-editor/file/{uid}", follow_redirects=False)
    assert rb.status_code in (401, 403, 404), f"B 拿到 A 的上傳檔！→ {rb.status_code}"


# ───────── ③-2 水平越權：id 藏在 pydantic 模型欄位裡（v1.14.17） ─────────
def test_nup_preview_does_not_leak_another_users_pdf(admin_session):
    """pdf-nup 的 `/preview` 會把來源 PDF **算成圖**回傳。

    這一條是實際打端點打出來的：B 拿 A 的 `upload_id` 送
    `{"upload_id": "...", "file_count": 1}`，拿回 8 KB 的 PNG —— 那就是 A 文件的
    內容。原因是 id 從 **pydantic 模型欄位**進來（`opts: NupOptions`），處理函式
    連 `Request` 都沒有收，結構上就不可能做歸屬檢查。

    靜態掃描（`test_id_from_body_acl.py`）當時也看不到這個形狀 —— 它只認函式
    參數名與 `body.get("upload_id")`。兩邊都已補上。
    """
    _, a = _user_client("nup_owner")
    _, b = _user_client("nup_other")
    r = a.post("/tools/pdf-nup/load",
               files=[("files", ("a.pdf", _pdf(), "application/pdf"))])
    assert r.status_code == 200, r.text
    uid = r.json()["upload_id"]

    for path in ("/tools/pdf-nup/preview", "/tools/pdf-nup/generate"):
        rb = b.post(path, json={"upload_id": uid, "file_count": 1})
        assert rb.status_code in (403, 404), (
            f"B 竟然能對 A 的上傳呼叫 {path}（{rb.status_code}，"
            f"{len(rb.content)} bytes）")

    # 本人仍然要能用 —— 只擋別人，不要把工具擋死
    ra = a.post("/tools/pdf-nup/preview", json={"upload_id": uid, "file_count": 1})
    assert ra.status_code == 200, f"本人被擋掉了：{ra.status_code} {ra.text[:200]}"
    assert ra.headers["content-type"] == "image/png"


def test_nup_rejects_path_traversal_in_upload_id(admin_session):
    """`upload_id` 原本直接被拼進檔案路徑 —— 順帶擋掉 `../`。"""
    _, a = _user_client("nup_trav")
    r = a.post("/tools/pdf-nup/preview",
               json={"upload_id": "../../etc/passwd", "file_count": 1})
    assert r.status_code in (400, 403, 404), r.status_code


def test_submission_check_admin_stats_is_fail_closed(admin_session):
    """送件檢核的儀表板是 admin 限定，但它住在**工具**路由底下。

    原本寫成 `if not _is_admin(user) and user is not None` —— 認不出身分時檢查
    整個被跳過。目前靠「中介層一定會先設好 user」撐著，中介層一改就破。改成直接
    問「有沒有啟用認證」。
    """
    _, c = _user_client("sc_stats_user")
    r = c.get("/tools/submission-check/admin-stats")
    assert r.status_code in (403, 302, 303, 307), \
        f"非 admin 看得到跨案件儀表板（{r.status_code}）"

    admin_c, _, _ = admin_session
    ra = admin_c.get("/tools/submission-check/admin-stats")
    assert ra.status_code == 200, f"admin 反而被擋掉了：{ra.status_code}"


def test_no_fail_open_user_is_none_shape_remains():
    """靜態守門：`if not is_admin(u) and u is not None:` 這種形狀不可以再出現。

    **用 AST 比對，不是用字串比對** —— 第一版是逐行 grep，結果把上面那個函式
    說明裡引用舊寫法的那句話也抓出來了（同樣的誤報在 migration 那份也踩過一次）。
    註解與說明文字不是程式碼，掃描要看真的會執行的東西。
    """
    import ast
    import pathlib

    def _is_admin_call(node) -> bool:
        return (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)
                and isinstance(node.operand, ast.Call)
                and "is_admin" in ast.unparse(node.operand.func))

    def _is_not_none(node) -> bool:
        return (isinstance(node, ast.Compare)
                and len(node.ops) == 1 and isinstance(node.ops[0], ast.IsNot)
                and isinstance(node.comparators[0], ast.Constant)
                and node.comparators[0].value is None)

    bad = []
    for f in pathlib.Path("app").rglob("*.py"):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            t = node.test
            if not (isinstance(t, ast.BoolOp) and isinstance(t.op, ast.And)):
                continue
            if (any(_is_admin_call(v) for v in t.values)
                    and any(_is_not_none(v) for v in t.values)):
                bad.append(f"{f}:{node.lineno}")
    assert not bad, (
        f"fail-open 形狀的權限判斷：{bad}\n"
        "認不出身分時檢查會被整個跳過 —— 改成先問 auth_settings.is_enabled()")


# ───────────────── 公開路徑允許清單（v1.14.17 收斂） ─────────────────
def test_public_exact_is_not_shadowed_by_a_prefix():
    """`_PUBLIC_EXACT` 裡的每一條都不可以同時被某個前綴涵蓋。

    原本 `/login` 兩邊都列，前綴那份先命中 —— 於是整個 EXACT 集合是**死的**，
    而讀的人會以為 `/login-xyz` 不公開（其實公開）。目前沒有那種路由所以沒出事，
    但下一條新路由就會踩到。
    """
    from app.main import _PUBLIC_EXACT, _PUBLIC_PREFIXES
    shadowed = [p for p in _PUBLIC_EXACT
                if any(p.startswith(pre) for pre in _PUBLIC_PREFIXES)]
    assert not shadowed, (
        f"這些路徑同時被前綴涵蓋，EXACT 形同虛設：{shadowed}")


def test_no_public_prefix_swallows_admin():
    from app.main import _PUBLIC_EXACT, _PUBLIC_PREFIXES
    for p in list(_PUBLIC_PREFIXES) + list(_PUBLIC_EXACT):
        assert not "/admin".startswith(p.rstrip("/")) or p in ("/",), \
            f"公開路徑 {p} 會涵蓋 /admin"


def test_login_page_still_public(admin_session):
    """收斂之後登入相關頁面仍然要進得去，否則沒有人登得進來。"""
    c = TestClient(app_main.app)
    for path in ("/login", "/healthz", "/favicon.ico"):
        r = c.get(path, follow_redirects=False)
        # 404 也算通過 —— 代表它進得了路由層（只是沒有那條路由），
        # 重點是**不可以被導回登入頁**（那才是「被認證閘擋住」）。
        assert r.status_code != 302 or "/login" not in r.headers.get("location", ""), \
            f"{path} 被認證閘擋住了（導向 {r.headers.get('location')}）"
        assert r.status_code in (200, 204, 304, 307, 404), f"{path} → {r.status_code}"


def test_error_page_escapes_the_detail():
    """錯誤頁把 `detail` 寫進 HTML —— 一律跳脫。

    目前 401/403 的 detail 都是常數，但只要有一處改成帶使用者輸入的 f-string，
    這裡就是打在**管理員 session** 上的反射型 XSS。
    """
    import inspect
    src = inspect.getsource(app_main._friendly_http_exc)
    assert "escape(" in src, "錯誤頁沒有跳脫 detail"
