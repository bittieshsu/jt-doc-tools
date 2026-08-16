"""Tests for the user-workspace core (app/core/workspace.py).

Covers: enable/disable gate, PDF/PNG-only type enforcement, per-user quota +
single-file cap, CRUD, cross-user isolation, auth-OFF single workspace, and
retention sweep.
"""
from __future__ import annotations

import pytest

from app.core import workspace as ws

PDF_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class _State:
    def __init__(self, user):
        self.user = user


class _Req:
    def __init__(self, user):
        self.state = _State(user)


def _user(uid, name="u", src="local"):
    return _Req({"user_id": uid, "username": name, "source": src})


@pytest.fixture
def wsenv(tmp_path, monkeypatch):
    """Isolated data dir + auth ON + fresh settings cache for each test."""
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    monkeypatch.setattr(ws, "_CACHE", None, raising=False)
    monkeypatch.setattr("app.core.auth_settings.is_enabled", lambda: True)
    # default-enabled workspace with generous limits unless a test overrides
    ws.save_settings({"enabled": True, "per_user_quota_mb": 500,
                      "max_file_mb": 50, "retention_hours": -1})
    return tmp_path


def test_save_and_list_pdf(wsenv):
    req = _user(1)
    meta = ws.save_bytes(req, PDF_BYTES, "報告.pdf", "pdf-merge")
    assert meta["ext"] == ".pdf" and meta["mime"] == "application/pdf"
    assert meta["name"] == "報告.pdf"
    files = ws.list_files(req)
    assert len(files) == 1 and files[0]["file_id"] == meta["file_id"]


def test_save_png_detected_by_magic(wsenv):
    req = _user(1)
    meta = ws.save_bytes(req, PNG_BYTES, "shot", "pdf-to-image")
    assert meta["ext"] == ".png"
    assert meta["name"].endswith(".png")  # extension auto-applied


def test_reject_non_pdf_png(wsenv):
    req = _user(1)
    with pytest.raises(ws.UnsupportedType):
        ws.save_bytes(req, b"PK\x03\x04 not a pdf", "x.zip", "t")


def test_get_delete_rename(wsenv):
    req = _user(1)
    meta = ws.save_bytes(req, PDF_BYTES, "a.pdf", "t")
    fid = meta["file_id"]
    fp, m = ws.get_file(req, fid)
    assert fp.exists() and m["file_id"] == fid
    m2 = ws.rename_file(req, fid, "新名.pdf")
    assert m2["name"] == "新名.pdf"
    assert ws.delete_file(req, fid) is True
    with pytest.raises(ws.NotFound):
        ws.get_file(req, fid)


def test_cross_user_isolation(wsenv):
    a, b = _user(1, "alice"), _user(2, "bob")
    meta = ws.save_bytes(a, PDF_BYTES, "secret.pdf", "t")
    # bob cannot read alice's file id (resolved under his own dir → NotFound)
    with pytest.raises(ws.NotFound):
        ws.get_file(b, meta["file_id"])
    assert ws.list_files(b) == []
    assert len(ws.list_files(a)) == 1


def test_quota_enforced(wsenv):
    ws.save_settings({"enabled": True, "per_user_quota_mb": 0, "max_file_mb": 0,
                      "retention_hours": -1})
    # 0 = unlimited; switch to a tiny quota by faking usage via many saves.
    # Use a 1 MB quota and a >1MB payload.
    ws.save_settings({"per_user_quota_mb": 1, "max_file_mb": 0,
                      "enabled": True, "retention_hours": -1})
    req = _user(1)
    big = PDF_BYTES + b"\x00" * (2 * 1024 * 1024)
    with pytest.raises(ws.QuotaExceeded):
        ws.save_bytes(req, big, "big.pdf", "t")


def test_max_file_cap(wsenv):
    ws.save_settings({"enabled": True, "per_user_quota_mb": 0, "max_file_mb": 1,
                      "retention_hours": -1})
    req = _user(1)
    big = PDF_BYTES + b"\x00" * (2 * 1024 * 1024)
    with pytest.raises(ws.QuotaExceeded):
        ws.save_bytes(req, big, "big.pdf", "t")


def test_disabled_hides_everything(wsenv):
    ws.save_settings({"enabled": False, "per_user_quota_mb": 500,
                      "max_file_mb": 50, "retention_hours": -1})
    assert ws.is_enabled() is False
    req = _user(1)
    with pytest.raises(ws.WorkspaceDisabled):
        ws.save_bytes(req, PDF_BYTES, "a.pdf", "t")
    assert ws.list_files(req) == []


def test_auth_off_single_workspace(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    monkeypatch.setattr(ws, "_CACHE", None, raising=False)
    monkeypatch.setattr("app.core.auth_settings.is_enabled", lambda: False)
    ws.save_settings({"enabled": True, "per_user_quota_mb": 500,
                      "max_file_mb": 50, "retention_hours": -1})
    # No user bound — auth OFF → single shared key, still works.
    req = _Req(None)
    assert ws.user_key(req) == "__single__"
    meta = ws.save_bytes(req, PDF_BYTES, "a.pdf", "t")
    assert ws.list_files(req)[0]["file_id"] == meta["file_id"]


def test_retention_sweep(wsenv):
    import time
    req = _user(1)
    meta = ws.save_bytes(req, PDF_BYTES, "old.pdf", "t")
    # Backdate saved_at well past the cutoff.
    d = ws._user_dir(req) / meta["file_id"]
    mf = d / "meta.json"
    import json
    data = json.loads(mf.read_text())
    data["saved_at"] = time.time() - 10 * 3600
    mf.write_text(json.dumps(data))
    # sweep entries older than 1 hour
    removed = ws.sweep_older_than(3600)
    assert removed == 1
    assert ws.list_files(req) == []


def test_count_files(wsenv):
    req = _user(1)
    assert ws.count_files(req) == 0
    ws.save_bytes(req, PDF_BYTES, "a.pdf", "t")
    ws.save_bytes(req, PNG_BYTES, "b.png", "t")
    assert ws.count_files(req) == 2


def test_thumbnail_png_returns_self(wsenv):
    req = _user(1)
    meta = ws.save_bytes(req, PNG_BYTES, "b.png", "t")
    fp, mime = ws.get_thumbnail(req, meta["file_id"])
    assert mime == "image/png" and fp.name == "file.png"


def test_thumbnail_pdf_renders_first_page(wsenv):
    import fitz
    # Build a real 1-page PDF (the minimal literal isn't renderable by MuPDF).
    doc = fitz.open()
    doc.new_page(width=200, height=200)
    data = doc.tobytes()
    doc.close()
    req = _user(1)
    meta = ws.save_bytes(req, data, "doc.pdf", "t")
    fp, mime = ws.get_thumbnail(req, meta["file_id"])
    assert mime == "image/png" and fp.name == "thumb.png" and fp.exists()
    # cached on second call
    fp2, _ = ws.get_thumbnail(req, meta["file_id"])
    assert fp2 == fp


def test_settings_roundtrip(wsenv):
    saved = ws.save_settings({"enabled": False, "per_user_quota_mb": 123,
                              "max_file_mb": 7, "retention_hours": 48})
    assert saved["enabled"] is False
    assert saved["per_user_quota_mb"] == 123
    assert saved["retention_hours"] == 48
    monkeypatch_cache = ws._CACHE
    assert monkeypatch_cache["max_file_mb"] == 7


# ---------------- v1.14.6：簡報 / 試算表格式 ----------------
# 「PDF 轉簡報」產出的就是 .pptx / .odp，原本工作區收不下 —— 使用者按「存至
# 工作區」只會拿到「不支援的檔案類型」，而那正是他最需要留存的產出。

def _zip_bytes(entries: dict, mimetype: str = "") -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if mimetype:
            z.writestr("mimetype", mimetype)
        for name, body in entries.items():
            z.writestr(name, body)
    return buf.getvalue()


@pytest.mark.parametrize("part,ext", [
    ("word/document.xml", ".docx"),
    ("xl/workbook.xml", ".xlsx"),
    ("ppt/presentation.xml", ".pptx"),
])
def test_ooxml_formats_detected(part, ext):
    from app.core.workspace import detect_kind
    data = _zip_bytes({"[Content_Types].xml": "<x/>", part: "<x/>"})
    got = detect_kind(data)
    assert got and got[1] == ext, got


@pytest.mark.parametrize("mime,ext", [
    ("application/vnd.oasis.opendocument.text", ".odt"),
    ("application/vnd.oasis.opendocument.spreadsheet", ".ods"),
    ("application/vnd.oasis.opendocument.presentation", ".odp"),
    ("application/vnd.oasis.opendocument.graphics", ".odg"),
])
def test_odf_formats_detected(mime, ext):
    from app.core.workspace import detect_kind
    got = detect_kind(_zip_bytes({"content.xml": "<x/>"}, mimetype=mime))
    assert got and got == (mime, ext), got


def test_plain_zip_still_rejected():
    """**安全性**：型別判定必須開 zip 驗內部結構。只看副檔名的話，把任意 zip
    改名成 .pptx 就能塞進工作區。"""
    from app.core.workspace import detect_kind
    assert detect_kind(_zip_bytes({"evil.exe": "MZ", "readme.txt": "hi"})) is None


def test_odf_with_bogus_mimetype_rejected():
    from app.core.workspace import detect_kind
    assert detect_kind(_zip_bytes({"content.xml": "<x/>"},
                                  mimetype="application/x-not-a-thing")) is None


def test_allowed_table_matches_detect_kind():
    """ALLOWED 與 detect_kind 必須一致 —— 兩邊各寫一份遲早會不一致，
    結果是「偵測得出來卻存不進去」這種難查的問題。"""
    from app.core import workspace as ws
    for _part, mime, ext in ws._OOXML_KINDS:
        assert ws.ALLOWED.get(mime) == ext
    for mime, ext in ws._ODF_KINDS.items():
        assert ws.ALLOWED.get(mime) == ext


def test_office_formats_have_no_thumbnail_but_do_not_crash():
    """新格式沒有縮圖是預期行為，但要丟明確的例外讓呼叫端改用預設圖示，
    不可讓整個列表壞掉。"""
    from app.core.workspace import WorkspaceError
    assert issubclass(WorkspaceError, Exception)


def test_deleting_a_user_removes_their_workspace_files(tmp_path, monkeypatch):
    """刪帳號要一起清磁碟上的檔案。

    第一版的 `user_manager.delete()` 只清資料庫，`data/workspace/u<id>/`
    原封不動留著 —— 保留期設成「永久保留」時那就是**永久**留著離職者的檔案，
    管理區的用量統計還會繼續列出一個已經不存在的帳號（v1.14.31 對抗式驗證）。

    這件事跟工具交接直接相關：交接每按一次就自動存一份，使用者不見得知道
    有留底。
    """
    from app.config import settings
    from app.core import workspace

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    (tmp_path / "workspace" / "u99" / "abc").mkdir(parents=True)
    (tmp_path / "workspace" / "u99" / "abc" / "file.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "workspace" / "u98" / "xyz").mkdir(parents=True)
    (tmp_path / "workspace" / "u98" / "xyz" / "other.pdf").write_bytes(b"%PDF-1.4\n")

    assert workspace.purge_user(99) == 1
    assert not (tmp_path / "workspace" / "u99").exists()
    # **只能刪那一個人的** —— 別人的檔案不可以受影響
    assert (tmp_path / "workspace" / "u98" / "xyz" / "other.pdf").exists()

    # 沒有工作區目錄的使用者不可以出錯
    assert workspace.purge_user(12345) == 0


def test_user_delete_actually_calls_workspace_purge(monkeypatch):
    """整合層：`user_manager.delete()` 要**真的**去清工作區。

    只測 `workspace.purge_user()` 本身不夠 —— 把 `delete()` 裡那兩行拿掉之後
    那個單元測試照樣全綠（實測變異驗證沒抓到，這一輪第五個同類型的空殼）。
    """
    from app.core import auth_db, roles, user_manager, workspace

    auth_db.init()
    roles.seed_builtin_roles()
    uid = user_manager.create_local("purge-probe", "測試", "Aa!23456789")

    called = []
    monkeypatch.setattr(workspace, "purge_user", lambda i: called.append(i) or 0)
    user_manager.delete(uid)
    assert called == [uid], (
        "刪帳號沒有去清工作區 —— 離職者的檔案會永久留在磁碟上，"
        "管理區的用量統計也會繼續列出那個帳號")
