"""Shared fixtures for the jt-doc-tools test suite.

Provides:
  • ``client``: a TestClient bound to the FastAPI app.
  • ``sample_pdf``: bytes of a freshly-built minimal PDF (one page,
    A4, with a label so PDF Fill can detect at least one field).
  • ``two_page_pdf`` / ``ten_page_pdf``: small multi-page PDFs.
  • ``stamp_png``: bytes for a coloured PNG used as a stamp/watermark.

IMPORTANT: this module **must** set ``JTDT_DATA_DIR`` BEFORE importing
``app.main`` — Settings is a singleton frozen at first import. If we let
the import use the dev's real `data/` dir, tests would write into it.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from io import BytesIO
from pathlib import Path

# ---- CSRF：測試套件預設跳過 double-submit 驗證（仍會設 cookie / state）----
# 真正的 CSRF 邏輯由 tests/test_csrf.py 直接測 middleware（不靠此旗標）。
os.environ.setdefault("JTDT_CSRF_DISABLE", "1")

# ---- Isolate test data dir BEFORE any app.* import ----
# (Module-level: runs once per pytest invocation.)
if "JTDT_DATA_DIR" not in os.environ:
    _TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="jtdt_test_"))
    os.environ["JTDT_DATA_DIR"] = str(_TEST_DATA_DIR)

    # 跑完就刪掉。**這個目錄一次跑下來會長到 500 MB 以上**（字型 / 資產副本 +
    # 各測試寫進去的檔案），原本從來沒有人收 —— 累積四十幾份就把開發機的磁碟
    # 塞爆了（2026-08-27 實際發生）。要留下來看內容時設 `JTDT_KEEP_TEST_DATA=1`。
    if os.environ.get("JTDT_KEEP_TEST_DATA") != "1":
        atexit.register(shutil.rmtree, _TEST_DATA_DIR, True)
    # Seed: copy a few harmless files from real data/ if present (avoids
    # tests that need a default profile / assets failing). We deliberately
    # DO NOT copy api_tokens.json / auth.sqlite / audit.sqlite to keep
    # auth tests on a clean slate.
    _real = Path(__file__).resolve().parent.parent / "data"
    if _real.exists():
        for sub in ("assets", "fonts"):
            src = _real / sub
            if src.is_dir():
                shutil.copytree(src, _TEST_DATA_DIR / sub, dirs_exist_ok=True)
        for f in ("profile.json", "label_synonyms.json",
                  "form_templates.json", "office_paths.json"):
            sf = _real / f
            if sf.is_file():
                shutil.copy2(sf, _TEST_DATA_DIR / f)

import fitz
import time

import pytest
from PIL import Image, ImageDraw
from fastapi.testclient import TestClient

import app.main as app_main


@pytest.fixture(autouse=True)
def _quiet_job_queue():
    """每支測試開始前，等**全域**作業佇列把手上的工作跑完。

    **為什麼**：`job_store` 是在**呼叫當下**才從 `settings.data_dir` 解出路徑的。
    有 20 支測試會 monkeypatch 那個路徑 —— 只要前一支測試留下的 worker 執行緒
    在換路徑之後寫一次進度，它就會寫到**新的那個目錄**上：

    * 新目錄還沒建表 → `no such table: jobs`
    * 剛好撞上 `job_store.init()` 在建表 → **`database is locked`**

    2026-09-18 完整套件實際踩到後者：8,910 支裡就這一支，**而且單跑全綠**。
    看到「單跑綠、合跑紅」就先懷疑共用狀態（本專案第 N 次）。

    **這是測試之間的隔離問題，不是產品缺陷** —— 正式環境只有一個 data_dir，
    不會有人在服務跑著的時候把它換掉。

    **等不到也照樣往下走**（best effort）：有些測試本來就會刻意留著長時間的
    作業，在這裡卡死比偶爾一次競爭更糟。
    """
    from app.core.job_manager import job_manager as _global
    for _ in range(200):                      # 最多 10 秒
        st = _global.stats()
        if not st["running"] and not st["queued"]:
            return
        time.sleep(0.05)


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(app_main.app)


@pytest.fixture
def auth_off():
    """Reset auth to OFF and wipe any users between tests so each test
    starts on a clean canvas. Defensively init schemas — the app's startup
    hook runs lazily under TestClient so tests that touch the DB before
    making any HTTP request would otherwise see "no such table"."""
    from app.core import auth_settings, auth_db, audit_db, db
    auth_db.init()
    audit_db.init()
    s = auth_settings.get()
    s["backend"] = "off"
    auth_settings.save(s)
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM lockouts")
        conn.execute("DELETE FROM subject_perms")
        conn.execute("DELETE FROM subject_roles")
        conn.execute("DELETE FROM group_members")
        conn.execute("DELETE FROM groups")
        conn.execute("DELETE FROM users")
    yield
    # cleanup on teardown too
    s = auth_settings.get()
    s["backend"] = "off"
    auth_settings.save(s)
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM lockouts")
        conn.execute("DELETE FROM users")


@pytest.fixture
def admin_session(auth_off):
    """Bootstrap auth=local with a known admin and return a logged-in
    TestClient. Yields (client, admin_username, admin_password)."""
    from app.core import auth_settings, sessions
    pw = "TestAdmin1234"
    auth_settings.enable_local_with_admin(
        admin_username="jtdt-admin",
        admin_display_name="管理員",
        admin_password=pw,
        admin_password_confirm=pw,
        actor_ip="127.0.0.1",
    )
    # Issue a fresh session directly (skip login flow noise)
    from app.core import auth_db
    uid = auth_db.conn().execute(
        "SELECT id FROM users WHERE username='jtdt-admin'"
    ).fetchone()["id"]
    token, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
    c = TestClient(app_main.app)
    c.cookies.set(sessions.COOKIE_NAME, token)
    yield c, "jtdt-admin", pw


def _make_pdf(pages: int, label: str | None = None) -> bytes:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)  # A4 portrait, pt
        page.insert_text((72, 72), f"Page {i + 1}", fontsize=14)
        if i == 0 and label:
            page.insert_text((72, 120), label, fontsize=12)
    buf = BytesIO()
    doc.save(buf, garbage=3, deflate=True)
    doc.close()
    return buf.getvalue()


@pytest.fixture
def sample_pdf() -> bytes:
    # "公司名稱:" is in our LABEL_MAP so detection produces ≥ 1 field.
    return _make_pdf(1, "公司名稱: ")


@pytest.fixture
def two_page_pdf() -> bytes:
    return _make_pdf(2)


@pytest.fixture
def ten_page_pdf() -> bytes:
    return _make_pdf(10)


@pytest.fixture
def stamp_png() -> bytes:
    im = Image.new("RGBA", (240, 120), (255, 255, 255, 0))
    d = ImageDraw.Draw(im)
    d.rectangle((4, 4, 235, 115), outline=(220, 30, 30, 255), width=4)
    d.text((20, 40), "STAMP", fill=(220, 30, 30, 255))
    buf = BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()
