"""預覽端點的 ACL 不可以「認不出 upload_id 就放行」。

## 原本的寫法

    uid = upload_owner.extract_upload_id(name)
    if uid:                       # ← 認不出來就整個跳過 ACL
        upload_owner.require(uid, request)

`extract_upload_id` 只看**第一段**（`split("_")[0]`），所以任何不是「uuid 開頭」
的檔名都會讓 ACL 變成 no-op。pdf-watermark 的註解自己就寫過踩到這件事
（檔名是 `wm_<uid>_...`，第一段是 `wm`），當時的修法是在呼叫端先把 `wm_` 切掉
—— 那是**個案補救**，下一個用別的前綴的工具還是會再中一次。

臨時目錄裡確實存在不以 uuid 開頭的檔案（例如使用者上傳的浮水印圖
`wm_temp_<uuid>.png`），所以這不是理論上的漏洞。

## 修法方向

ACL 的預設必須是**拒絕**：認不出 upload_id 就不給（管理員例外，未啟用認證時直通）。
另外 `extract_upload_id` 改為掃描所有以 `_` 分隔的片段，這樣 `wm_` 這類前綴不必
每個呼叫端各自處理一次。

回 404 而不是 403 —— 403 等於告訴對方「這個檔案存在，只是你不能看」。
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

import app.main as app_main
from app.config import settings


TOOLS = ("pdf-fill", "pdf-stamp", "pdf-watermark")

#: 另一組同樣形狀的預覽端點：檔名帶固定前綴，程式用 `filename[N:]` 切出 id。
#: (工具 id, 檔名前綴)
PREFIX_TOOLS = (
    ("doc-deident", "did_"),
    ("pdf-editor", "pe_"),
    ("pdf-to-image", "p2i_"),
)


@pytest.fixture
def two_users(auth_off):
    """建立 alice / bob 兩個一般使用者，回兩個已登入的 client。"""
    from app.core import auth_settings, roles, sessions, user_manager
    pw = "TestAdmin1234"
    auth_settings.enable_local_with_admin(
        admin_username="jtdt-admin", admin_display_name="管理員",
        admin_password=pw, admin_password_confirm=pw, actor_ip="127.0.0.1")
    roles.seed_builtin_roles()
    out = []
    for name in ("alice", "bob"):
        uid = user_manager.create_local(name, name, "UserPass1234")
        # 預設角色 default-user **不含** pdf-fill / pdf-stamp（那兩個要另外授權），
        # 不給權限的話這裡收到的 403 是工具權限閘擋的，測不到 ACL。
        from app.core import permissions
        permissions.set_subject_roles("user", str(uid), ["finance"])
        tok, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
        c = TestClient(app_main.app)
        c.cookies.set(sessions.COOKIE_NAME, tok)
        out.append((uid, c))
    return out


def _write_temp_png(name: str) -> None:
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    # 最小 PNG（內容不重要，重點是端點會不會給）
    (settings.temp_dir / name).write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)


# ---------- 1. 認不出 upload_id → 必須拒絕 ----------

@pytest.mark.parametrize("tool", TOOLS)
def test_unrecognised_filename_is_denied(two_users, tool):
    """檔名裡沒有任何 uuid → 一般使用者不可以拿到（原本會直接回 200）。"""
    (_, alice) = two_users[0]
    name = "nouuidhere_p1.png"
    _write_temp_png(name)
    r = alice.get(f"/tools/{tool}/preview/{name}")
    assert r.status_code == 404, (
        f"{tool}：認不出 upload_id 卻放行了（{r.status_code}）")


@pytest.mark.parametrize("tool", TOOLS)
def test_wm_temp_style_name_is_denied(two_users, tool):
    """`wm_temp_<uuid>.png` 這種真實存在的檔名不可以任人取用。

    這是使用者上傳的浮水印圖（簽名 / 公司標誌），沒有 owner 記錄。
    """
    (_, alice) = two_users[0]
    name = f"wm_temp_{uuid.uuid4().hex}.png"
    _write_temp_png(name)
    r = alice.get(f"/tools/{tool}/preview/{name}")
    assert r.status_code == 404, f"{tool}：回了 {r.status_code}"


# ---------- 2. 正常路徑不可以被弄壞 ----------

@pytest.mark.parametrize("tool", TOOLS)
def test_owner_can_still_read_own_preview(two_users, tool):
    """修成 fail-closed 之後，本人還是要看得到自己的預覽。"""
    from app.core import upload_owner
    (alice_uid, alice) = two_users[0]
    uid_hex = uuid.uuid4().hex
    prefix = "wm_" if tool == "pdf-watermark" else ""
    name = f"{prefix}{uid_hex}_p1.png"
    _write_temp_png(name)
    upload_owner.record_uid(uid_hex, alice_uid)
    r = alice.get(f"/tools/{tool}/preview/{name}")
    assert r.status_code == 200, f"{tool}：本人被擋（{r.status_code}）"


@pytest.mark.parametrize("tool", TOOLS)
def test_other_user_cannot_read_someone_elses_preview(two_users, tool):
    from app.core import upload_owner
    (alice_uid, _) = two_users[0]
    (_, bob) = two_users[1]
    uid_hex = uuid.uuid4().hex
    prefix = "wm_" if tool == "pdf-watermark" else ""
    name = f"{prefix}{uid_hex}_p1.png"
    _write_temp_png(name)
    upload_owner.record_uid(uid_hex, alice_uid)
    r = bob.get(f"/tools/{tool}/preview/{name}")
    assert r.status_code in (403, 404), f"{tool}：別人讀到了（{r.status_code}）"


# ---------- 3. extract_upload_id 掃全部片段 ----------

def test_extract_scans_all_segments():
    """前綴不必由每個呼叫端各自處理 —— 那種個案補救遲早會漏。"""
    from app.core import upload_owner as uo
    h = uuid.uuid4().hex
    assert uo.extract_upload_id(f"{h}_p1.png") == h
    assert uo.extract_upload_id(f"wm_{h}_p1.png") == h
    assert uo.extract_upload_id(f"wm_api_{h}_out.pdf") == h
    assert uo.extract_upload_id(f"anything_{h}.png") == h
    assert uo.extract_upload_id("nouuid_p1.png") == ""
    assert uo.extract_upload_id("") == ""
    # 不是 32 位 hex 的長字串不可以被當成 id
    assert uo.extract_upload_id("z" * 32 + "_p1.png") == ""


def test_admin_can_read_unrecognised_name(two_users, admin_of):
    """管理員仍可存取（支援 / 檢視用），否則出問題時沒人查得到。"""
    name = "nouuidhere_p1.png"
    _write_temp_png(name)
    r = admin_of.get(f"/tools/pdf-fill/preview/{name}")
    assert r.status_code == 200


@pytest.fixture
def admin_of(two_users):
    from app.core import auth_db, sessions
    uid = auth_db.conn().execute(
        "SELECT id FROM users WHERE username='jtdt-admin'").fetchone()["id"]
    tok, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
    c = TestClient(app_main.app)
    c.cookies.set(sessions.COOKIE_NAME, tok)
    return c


# ---------- 4. 未啟用認證時不可以擋 ----------

@pytest.mark.parametrize("tool", TOOLS)
def test_auth_off_is_not_blocked(auth_off, tool):
    """單人模式沒有隔離的必要，改成 fail-closed 不可以連這個一起擋掉。"""
    c = TestClient(app_main.app)
    name = "nouuidhere_p1.png"
    _write_temp_png(name)
    r = c.get(f"/tools/{tool}/preview/{name}")
    assert r.status_code == 200, f"{tool}：未啟用認證卻被擋（{r.status_code}）"


# ---------- 5. 前綴切割型的預覽端點（同一個 fail-open 形狀） ----------

@pytest.mark.parametrize("tool,prefix", PREFIX_TOOLS)
def test_prefix_tools_deny_when_id_segment_is_empty(two_users, tool, prefix):
    """`rest = filename[N:].split("_", 1)[0]` 切出空字串時不可以放行。

    這三支用的是「切掉固定前綴再取第一段」，寫法是 `if rest: require(...)` ——
    `did__x.png` 這種檔名切出來是空字串，於是**完全不檢查**。與 pdf-fill /
    pdf-stamp / pdf-watermark 是同一個形狀，只是前綴不同；那三支修好之後這三支
    仍然留著，正好說明「逐個呼叫端各自處理前綴」為什麼不可靠。
    """
    (_uid, alice) = two_users[0]
    name = f"{prefix}_evil.png"          # 前綴後面直接接底線 → 切出空字串
    _write_temp_png(name)
    r = alice.get(f"/tools/{tool}/preview/{name}")
    assert r.status_code == 404, f"{tool}：切出空 id 卻放行了（{r.status_code}）"


@pytest.mark.parametrize("tool,prefix", PREFIX_TOOLS)
def test_prefix_tools_owner_still_works(two_users, tool, prefix):
    """本人仍要看得到自己的預覽（fail-closed 不可以擋到正常流程）。"""
    from app.core import upload_owner
    (alice_uid, alice) = two_users[0]
    uid_hex = uuid.uuid4().hex
    name = f"{prefix}{uid_hex}_p1.png"
    _write_temp_png(name)
    upload_owner.record_uid(uid_hex, alice_uid)
    r = alice.get(f"/tools/{tool}/preview/{name}")
    assert r.status_code == 200, f"{tool}：本人被擋（{r.status_code}）"


@pytest.mark.parametrize("tool,prefix", PREFIX_TOOLS)
def test_prefix_tools_deny_other_user(two_users, tool, prefix):
    from app.core import upload_owner
    (alice_uid, _) = two_users[0]
    (_, bob) = two_users[1]
    uid_hex = uuid.uuid4().hex
    name = f"{prefix}{uid_hex}_p1.png"
    _write_temp_png(name)
    upload_owner.record_uid(uid_hex, alice_uid)
    r = bob.get(f"/tools/{tool}/preview/{name}")
    assert r.status_code in (403, 404), f"{tool}：別人讀到了（{r.status_code}）"
