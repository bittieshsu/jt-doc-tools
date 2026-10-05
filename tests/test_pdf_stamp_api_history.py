"""用印 API 補齊跟網頁版一樣的紀錄與用法（2026-10-05）。

1. **API 蓋的章也要存進「用印簽名歷史」**：網頁蓋章會把原檔與蓋好的檔留給稽核員，
   API 原本不會 —— 用印是敏感動作，別的系統替人蓋了章卻查不到原檔與成品。
2. **稽核記錄要帶檔名**：原本只有「誰、從哪裡、呼叫了用印」，看不出蓋的是哪一份。
3. **可以直接用資產庫的章**（`asset_id`）：原本一定要上傳印章圖，呼叫端得自己保管一份
   公司大章的圖檔。沒給位置時用那顆章在資產庫設好的位置。

判準都落在產出上：歷史裡真的有原檔與成品、成品真的蓋上了章；用資產庫的章蓋出來的頁面
跟「上傳同一張圖、給同樣位置」逐像素相同。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from io import BytesIO

import fitz
import pytest
from fastapi.testclient import TestClient

import app.main as app_main
from app.core import asset_manager as _am
from app.core.history_manager import stamp_history

URL = "/tools/pdf-stamp/api/pdf-stamp"


def _pdf(n: int = 2) -> bytes:
    doc = fitz.open()
    for i in range(n):
        pg = doc.new_page(width=595, height=842)
        pg.insert_text((72, 100), f"Page {i + 1}", fontsize=20)
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _seal_png(color=(200, 0, 0, 255)) -> bytes:
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (200, 200), (255, 255, 255, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((5, 5, 195, 195), outline=color, width=10)
    d.line((40, 100, 160, 100), fill=color, width=10)
    buf = BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _page_png(pdf: bytes, page: int = 0) -> bytes:
    with fitz.open(stream=pdf, filetype="pdf") as d:
        return d[page].get_pixmap(dpi=72).tobytes("png")


def _ink(pdf: bytes, page: int = 0) -> int:
    """紅墨的像素數（章是紅的；頁面上的黑字不算）。"""
    with fitz.open(stream=pdf, filetype="pdf") as d:
        pm = d[page].get_pixmap(dpi=60)
    px = pm.samples
    return sum(1 for j in range(0, len(px), pm.n)
               if px[j] > 150 and px[j + 1] < 100 and px[j + 2] < 100)


def _ids() -> set[str]:
    return {m["id"] for m in stamp_history.list_all()}


@pytest.fixture()
def client():
    return TestClient(app_main.app)


@pytest.fixture()
def history_cleanup():
    before = _ids()
    yield
    for hid in _ids() - before:
        stamp_history.delete(hid)


@pytest.fixture()
def stamp_asset():
    a = _am.asset_manager.create_from_bytes("API 測試章", "stamp", _seal_png())
    yield a
    _am.asset_manager.delete(a.id)


# ---------- 1. 用印歷史 ----------

def test_an_api_stamp_is_archived_in_the_stamp_history(client, history_cleanup):
    before = _ids()
    png = _seal_png()
    r = client.post(URL, files={"file": ("contract.pdf", _pdf(), "application/pdf"),
                                "stamp_image": ("chop.png", png, "image/png")},
                    data={"x_mm": "120", "y_mm": "200", "width_mm": "30", "height_mm": "30"})
    assert r.status_code == 200, r.text
    new = _ids() - before
    assert len(new) == 1, f"API 蓋章之後用印歷史多了 {len(new)} 筆（應該是 1 筆）"
    hid = new.pop()
    meta = stamp_history.get(hid)
    assert meta["filename"] == "contract.pdf", meta
    extra = meta.get("extra") or {}
    assert extra.get("source") == "api", f"看不出這筆是 API 蓋的：{extra}"
    assert extra.get("stamp_image_sha256") == hashlib.sha256(png).hexdigest()[:16], (
        f"上傳的印章圖要留指紋，事後才對得出蓋的是哪一張：{extra}")
    orig, out = stamp_history.file(hid, "original"), stamp_history.file(hid, "filled")
    assert orig and orig.exists() and out and out.exists(), "歷史裡沒有原檔或成品"
    assert _ink(orig.read_bytes()) == 0, "原檔不應該有章"
    assert _ink(out.read_bytes()) > 50, "歷史裡的成品沒有蓋上章"
    assert out.read_bytes() == r.content, "歷史裡的成品跟回給呼叫端的不是同一份"


def test_a_rejected_call_leaves_no_history(client, history_cleanup):
    before = _ids()
    r = client.post(URL, files={"file": ("bad.pdf", b"not a pdf", "application/pdf"),
                                "stamp_image": ("chop.png", _seal_png(), "image/png")})
    assert r.status_code == 400
    assert _ids() == before, "被退回的呼叫不應該留下歷史"


# ---------- 3. 用資產庫的章 ----------

def _via_upload(client, png: bytes, **pos) -> bytes:
    r = client.post(URL, files={"file": ("a.pdf", _pdf(), "application/pdf"),
                                "stamp_image": ("s.png", png, "image/png")},
                    data={k: str(v) for k, v in pos.items()})
    assert r.status_code == 200, r.text
    return r.content


def test_asset_id_stamps_the_library_seal_at_its_preset(client, history_cleanup, stamp_asset):
    a = stamp_asset
    before = _ids()
    r = client.post(URL, files={"file": ("a.pdf", _pdf(), "application/pdf")},
                    data={"asset_id": a.id})
    assert r.status_code == 200, r.text
    lib_png = _am.asset_manager.file_path(a).read_bytes()
    want = _via_upload(client, lib_png, x_mm=a.preset.x_mm, y_mm=a.preset.y_mm,
                       width_mm=a.preset.width_mm, height_mm=a.preset.height_mm,
                       rotation_deg=a.preset.rotation_deg)
    assert _ink(r.content) > 50, "用 asset_id 蓋出來的頁面沒有章"
    assert _page_png(r.content) == _page_png(want), (
        "沒給位置時，要用那顆章在資產庫設好的位置 —— 跟上傳同一張圖、給同樣位置的結果不同")
    mine = [stamp_history.get(h) for h in _ids() - before]
    assert any((m.get("extra") or {}).get("asset_id") == a.id for m in mine), (
        f"用印歷史要記下用的是資產庫的哪一顆章：{[m.get('extra') for m in mine]}")


def test_an_explicit_position_overrides_the_preset(client, history_cleanup, stamp_asset):
    a = stamp_asset
    pos = {"x_mm": 30, "y_mm": 60, "width_mm": 25, "height_mm": 25}
    r = client.post(URL, files={"file": ("a.pdf", _pdf(), "application/pdf")},
                    data={"asset_id": a.id, **{k: str(v) for k, v in pos.items()}})
    assert r.status_code == 200, r.text
    lib_png = _am.asset_manager.file_path(a).read_bytes()
    assert _page_png(r.content) == _page_png(_via_upload(client, lib_png, **pos)), (
        "給了位置就要照給的位置蓋")
    preset = _via_upload(client, lib_png, x_mm=a.preset.x_mm, y_mm=a.preset.y_mm,
                         width_mm=a.preset.width_mm, height_mm=a.preset.height_mm)
    assert _page_png(r.content) != _page_png(preset), "給了位置卻還是蓋在預設位置"


def test_an_image_is_required_one_way_or_another(client, history_cleanup):
    r = client.post(URL, files={"file": ("a.pdf", _pdf(), "application/pdf")})
    assert r.status_code == 400, r.text
    assert "stamp_image" in r.text and "asset_id" in r.text, (
        f"訊息要講出兩種給章的方式：{r.text}")


def test_upload_and_asset_id_together_is_ambiguous(client, history_cleanup, stamp_asset):
    r = client.post(URL, files={"file": ("a.pdf", _pdf(), "application/pdf"),
                                "stamp_image": ("s.png", _seal_png(), "image/png")},
                    data={"asset_id": stamp_asset.id})
    assert r.status_code == 400, r.text


@pytest.mark.parametrize("kind", ["watermark", "missing", "sentinel", "traversal"])
def test_asset_id_must_be_a_stamp_kind(client, history_cleanup, kind):
    """不是資產庫裡的印章 / 簽名 / Logo 就 400：浮水印、不存在的 id、網頁內部用的
    「不蓋章」代號（不擋的話會帶著沒有章的狀態走到蓋章那一步）、不是 id 形狀的值。"""
    made = None
    if kind == "watermark":
        made = _am.asset_manager.create_from_bytes("API 測試浮水印", "watermark", _seal_png())
        aid = made.id
    elif kind == "sentinel":
        aid = "__none__"
    elif kind == "traversal":
        aid = "../../assets"
    else:
        aid = "0" * 32
    try:
        r = client.post(URL, files={"file": ("a.pdf", _pdf(), "application/pdf")},
                        data={"asset_id": aid})
        assert r.status_code == 400, r.text
    finally:
        if made:
            _am.asset_manager.delete(made.id)


def test_placements_that_name_their_assets_need_no_upload(client, history_cleanup, stamp_asset):
    pl = [{"page": 0, "x_mm": 40, "y_mm": 250, "width_mm": 20, "height_mm": 20,
           "asset_id": stamp_asset.id},
          {"page": 1, "x_mm": 120, "y_mm": 100, "width_mm": 20, "height_mm": 20,
           "asset_id": stamp_asset.id}]
    r = client.post(URL, files={"file": ("a.pdf", _pdf(), "application/pdf")},
                    data={"placements_json": json.dumps(pl)})
    assert r.status_code == 200, r.text
    assert _ink(r.content, 0) > 20 and _ink(r.content, 1) > 20, "兩頁都應該蓋上章"


def test_the_legacy_call_still_works_unchanged(client, history_cleanup):
    """原本的呼叫方式（上傳圖、不帶位置）結果不變：位置仍是 105 / 250 / 30 / 30。"""
    png = _seal_png()
    r = client.post(URL, files={"file": ("a.pdf", _pdf(), "application/pdf"),
                                "stamp_image": ("s.png", png, "image/png")})
    assert r.status_code == 200, r.text
    want = _via_upload(client, png, x_mm=105, y_mm=250, width_mm=30, height_mm=30)
    assert _page_png(r.content) == _page_png(want)


# ---------- 2. 稽核記錄帶檔名（只有啟用認證時才寫稽核） ----------

@pytest.fixture()
def admin_client():
    from app.core import auth_settings, auth_db, sessions
    if auth_settings.get_backend() == "off":
        auth_settings.enable_local_with_admin(
            admin_username="jtdt-admin", admin_display_name="Admin",
            admin_password="TestAdmin1234", admin_password_confirm="TestAdmin1234",
            actor_ip="127.0.0.1")
    row = auth_db.conn().execute(
        "SELECT id FROM users WHERE username='jtdt-admin'").fetchone()
    tok, _ = sessions.issue(row["id"], remember=False, ip="127.0.0.1", ua="pytest")
    c = TestClient(app_main.app)
    c.cookies.set(sessions.COOKIE_NAME, tok)
    try:
        yield c
    finally:
        try:
            auth_settings.disable_auth(actor="pytest", ip="127.0.0.1")
        except Exception:
            pass


def _latest_stamp_audit(after_id: int, timeout: float = 5.0):
    from app.core import audit_db
    path = str(audit_db.audit_db_path())
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = sqlite3.connect(path, timeout=5.0)
        try:
            r = conn.execute(
                "SELECT id, username, details_json FROM audit_events "
                "WHERE event_type='tool_invoke' AND target='pdf-stamp' AND id > ? "
                "ORDER BY id DESC LIMIT 1", (after_id,)).fetchone()
        finally:
            conn.close()
        if r is not None:
            return r
        time.sleep(0.05)
    return None


def _max_audit_id() -> int:
    from app.core import audit_db
    conn = sqlite3.connect(str(audit_db.audit_db_path()), timeout=5.0)
    try:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit_events").fetchone()[0]
    finally:
        conn.close()


def test_the_audit_row_names_the_stamped_file(admin_client, history_cleanup):
    """稽核記錄的 `filename` 要是**被蓋章的那份文件**。

    多數上傳的檔名是中介層從表單內容抓的第一個檔名 —— 呼叫端先送印章圖、再送 PDF 的話，
    抓到的會是印章圖。所以這裡刻意把印章圖排在前面送。"""
    start = _max_audit_id()
    before = _ids()
    r = admin_client.post(URL, files={"stamp_image": ("chop.png", _seal_png(), "image/png"),
                                      "file": ("採購合約.pdf", _pdf(), "application/pdf")})
    assert r.status_code == 200, r.text
    row = _latest_stamp_audit(start)
    assert row is not None, "啟用認證時，API 用印沒有留下稽核記錄"
    details = json.loads(row[2])
    assert details.get("filename") == "採購合約.pdf", (
        f"稽核記錄看不出蓋的是哪一份檔案：{details}")
    hid = (_ids() - before).pop()
    assert stamp_history.get(hid)["username"].startswith("jtdt-admin"), (
        "用印歷史要記下是誰的帳號呼叫的")
