"""API 手冊教人用 token 呼叫的工具路徑，在**啟用認證**的機器上也要通（v1.16.26）。

## 由來

做 issue #53（辦公文件轉圖片加 WebP）時，在正式機上照 API 手冊的兩步驟實跑：
第 1 步 `POST /tools/pdf-to-image/convert` 通，第 2 步
`GET /tools/pdf-to-image/download/{id}` 帶 `Authorization: Bearer` **拿到 302 登入頁**。

`_api_token_gate` 只在路徑含 `/api/` 或結尾是 `/convert` 時才看 token；其餘工具路徑
token 根本沒被看，交給 `_auth_gate` 以「沒有 session」導去登入。手冊裡同一種的還有
`doc-diff/page-image`、`translate-doc/start` / `job`、`office-convert/formats`。
**手冊的逐條實跑一直是綠的** —— 那支稽核跑在認證關閉的實例上，302 永遠不會出現。

四個方向（缺一條就沒有牙齒）：
①帶有效 token → 通 ②什麼都沒帶 → 仍然導去登入（網頁那條路不變）
③帶錯的 token → 401 ④token 的持有者**沒有那支工具的權限** → 擋下（不是 token 就通行無阻）
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app import main as app_main

fitz = pytest.importorskip("fitz")


def _pdf() -> bytes:
    doc = fitz.open()
    doc.new_page(width=960, height=540).insert_text((40, 80), "Slide", fontsize=24)
    out = doc.tobytes()
    doc.close()
    return out


def _token_for(username: str) -> str:
    from app.core import auth_db
    from app.core.api_tokens import api_tokens
    uid = auth_db.conn().execute(
        "SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
    tok = api_tokens.create(f"pytest-{username}")
    api_tokens.assign_owner(tok.token, uid)
    return tok.token


@pytest.fixture
def admin_token(admin_session):
    return _token_for("jtdt-admin")


def _convert(client, token: str):
    return client.post(
        "/tools/pdf-to-image/convert",
        files={"file": ("deck.pdf", io.BytesIO(_pdf()), "application/pdf")},
        data={"format": "webp", "width": "480"},
        headers={"Authorization": f"Bearer {token}"})


def test_the_documented_two_step_flow_works_with_a_token(admin_token):
    anon = TestClient(app_main.app)          # 沒有 session cookie，就是 curl
    r = _convert(anon, admin_token)
    assert r.status_code == 200, r.text
    uid = r.json()["upload_id"]
    d = anon.get(f"/tools/pdf-to-image/download/{uid}",
                 headers={"Authorization": f"Bearer {admin_token}"},
                 follow_redirects=False)
    assert d.status_code == 200, (
        f"帶 token 下載得到 {d.status_code}（{d.headers.get('location')}）—— "
        "手冊第 2 步在啟用認證的機器上走不通")
    assert d.headers["content-type"] == "image/webp"


def test_other_documented_tool_paths_accept_the_token(admin_token):
    anon = TestClient(app_main.app)
    r = anon.get("/tools/office-convert/formats",
                 headers={"Authorization": f"Bearer {admin_token}"},
                 follow_redirects=False)
    assert r.status_code == 200, r.status_code
    assert r.headers["content-type"].startswith("application/json")


def test_without_credentials_the_web_path_is_unchanged(admin_token):
    """反向對照：沒帶 token 的請求照舊交給登入那一關（導去登入頁）。"""
    anon = TestClient(app_main.app)
    r = anon.get("/tools/pdf-to-image/download/0123456789abcdef0123456789abcdef",
                 follow_redirects=False)
    assert r.status_code in (302, 303, 307), r.status_code
    assert "/login" in r.headers.get("location", "")


def test_a_wrong_token_is_rejected_not_redirected(admin_token):
    anon = TestClient(app_main.app)
    r = anon.get("/tools/pdf-to-image/download/0123456789abcdef0123456789abcdef",
                 headers={"Authorization": "Bearer definitely-not-valid"},
                 follow_redirects=False)
    assert r.status_code == 401, r.status_code


def test_a_token_does_not_bypass_tool_permissions(admin_session):
    """token 持有者沒有這支工具的權限時要擋下 —— 驗 token 只是認人，不是放行。"""
    from app.core import user_manager
    user_manager.create_local("no-tools", "沒有工具", "NoTools12345", roles=[])
    tok = _token_for("no-tools")
    anon = TestClient(app_main.app)
    r = anon.get("/tools/office-convert/formats",
                 headers={"Authorization": f"Bearer {tok}"},
                 follow_redirects=False)
    assert r.status_code in (401, 403), (
        f"沒有權限的 token 持有者得到 {r.status_code}")
