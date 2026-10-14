"""工作區收大檔：不整份讀進記憶體、容量預留、已存過的不再存一次。

「事務機取件」規格（docs-share/mfp-fetch/，第 3.1、9.3 節）要先補的工作區能力，
其他會產生大檔的工具也用得到：

1. **以路徑存檔**（`save_file_for_key`）：PDF / PNG / 錄音檔只看檔頭認格式、串流寫入，
   `.part` 寫完才原子換名、最後才寫 meta —— meta 出現就代表檔案已經完整就位。
   可以「搬」不必「複製」；跨磁碟 `os.replace` 會失敗，要退回複製；中途失敗原檔要還在。
2. **上傳的 PDF 不整份讀進記憶體**（原本非錄音檔一律 `fobj.read()`）。
3. **作業結果存進工作區不整份讀**（自動保存與「存至工作區」按鈕原本都是 `read_bytes()`）。
4. **容量預留**：兩個同時寫入各自都放得下、加起來超過額度時，只能成功一個 ——
   原本是「先量用量、再慢慢寫」，兩邊量到的都是寫入前的用量。
5. **工具自己已經存進工作區**（`job.meta["workspace"]` 已記 file_id）時，自動保存不再存第二份。
6. 指定 `verify=True` 時 PDF 要真的打得開、至少一頁（加密的照收）；指定 `expect` 時格式要對得上
   —— 設備回一張 HTML 登入頁，不可以被當成純文字收進來。

判準落在**實際的記憶體用量**（`tracemalloc` 的峰值）與**實際的讀取大小**，不是看程式碼長怎樣。
"""
from __future__ import annotations

import errno
import hashlib
import io
import os
import threading
import tracemalloc
from pathlib import Path

import fitz
import pytest

from app.core import workspace as ws

MB = 1024 * 1024


class _State:
    def __init__(self, user):
        self.user = user


class _Req:
    def __init__(self, uid=1):
        self.state = _State({"user_id": uid, "username": f"u{uid}", "source": "local"})


@pytest.fixture
def wsenv(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    monkeypatch.setattr(ws, "_CACHE", None, raising=False)
    monkeypatch.setattr("app.core.auth_settings.is_enabled", lambda: True)
    ws.save_settings({"enabled": True, "per_user_quota_mb": 0, "max_file_mb": 0,
                      "max_audio_mb": 0, "retention_hours": -1})
    return tmp_path


def _real_pdf(pages: int = 2) -> bytes:
    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    out = doc.tobytes()
    doc.close()
    return out


def _big_pdf(path: Path, size: int) -> Path:
    """一份真的打得開的 PDF，後面補到 `size`（PDF 讀取器只看最後的 xref，前面的內容照樣有效）。"""
    body = _real_pdf()
    with path.open("wb") as f:
        f.write(body)
        f.write(b"\n%" + b"x" * (size - len(body) - 2))
    return path


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(MB), b""):
            h.update(chunk)
    return h.hexdigest()


def _peak_mb(fn) -> float:
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1] / MB
    finally:
        tracemalloc.stop()


# --------------------------------------------------------------------- 以路徑存檔

def test_save_file_keeps_the_bytes_and_leaves_no_half_files(wsenv):
    src = _big_pdf(wsenv / "scan.pdf", 3 * MB)
    meta = ws.save_file_for_key("u1", src, "三樓掃描.pdf", source_tool="mfp-fetch")
    assert (meta["ext"], meta["mime"], meta["size"]) == (".pdf", "application/pdf", 3 * MB)
    stored, _ = ws.get_file(_Req(), meta["file_id"])
    assert _sha(stored) == _sha(src)
    assert src.exists(), "沒說要搬就不可以動原檔"
    assert not list(stored.parent.glob("*.part"))


def test_save_file_does_not_load_the_file_into_memory(wsenv):
    src = _big_pdf(wsenv / "big.pdf", 64 * MB)
    peak = _peak_mb(lambda: ws.save_file_for_key("u1", src, "big.pdf"))
    assert peak < 8, f"存一份 64 MB 的 PDF 用掉 {peak:.0f} MB 記憶體（整份讀進來了）"


def test_move_takes_the_file_without_copying(wsenv):
    src = _big_pdf(wsenv / "dl.part.pdf", 2 * MB)
    want = _sha(src)
    meta = ws.save_file_for_key("u1", src, "a.pdf", move=True)
    assert not src.exists()
    assert _sha(ws.get_file(_Req(), meta["file_id"])[0]) == want


def test_move_across_disks_falls_back_to_copy(wsenv, monkeypatch):
    """暫存區與資料目錄不在同一個檔案系統時 `os.replace` 會丟 EXDEV —— 不可以就此失敗。"""
    src = _big_pdf(wsenv / "other_disk.pdf", 2 * MB)
    want = _sha(src)
    real = os.replace

    def replace(a, b):
        if Path(a) == src:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(a, b)

    monkeypatch.setattr(ws.os, "replace", replace)
    meta = ws.save_file_for_key("u1", src, "a.pdf", move=True)
    assert not src.exists(), "複製成功後原檔要刪掉（語意是「搬」）"
    assert _sha(ws.get_file(_Req(), meta["file_id"])[0]) == want


def test_a_failed_move_puts_the_original_back(wsenv, monkeypatch):
    src = _big_pdf(wsenv / "keep_me.pdf", 2 * MB)
    want = _sha(src)

    def boom(*a, **k):
        raise OSError("disk full while writing meta")

    monkeypatch.setattr(ws, "_write_meta", boom)
    with pytest.raises(OSError):
        ws.save_file_for_key("u1", src, "a.pdf", move=True)
    assert src.exists() and _sha(src) == want, "失敗了原檔卻不見了"
    assert ws.list_files(_Req()) == []
    assert not any((wsenv / "workspace" / "u1").iterdir()), "留下了半筆"


def test_office_and_text_still_go_through_full_validation(wsenv):
    """辦公文件、純文字要看整份才認得出來 —— 走原本那條（有單檔上限擋著）。"""
    p = wsenv / "note.txt"
    p.write_text("會議紀錄\n", encoding="utf-8")
    assert ws.save_file_for_key("u1", p, "note.txt")["ext"] == ".txt"
    junk = wsenv / "x.bin"
    junk.write_bytes(os.urandom(4096).replace(b"%", b"x"))
    with pytest.raises(ws.UnsupportedType):
        ws.save_file_for_key("u1", junk, "x.bin")


def test_size_limit_is_checked_before_anything_is_written(wsenv):
    ws.save_settings({"max_file_mb": 1})
    src = _big_pdf(wsenv / "big.pdf", 2 * MB)
    with pytest.raises(ws.QuotaExceeded) as e:
        ws.save_file_for_key("u1", src, "big.pdf")
    assert "不是格式問題" in str(e.value)
    assert not (wsenv / "workspace" / "u1").exists() or not any((wsenv / "workspace" / "u1").iterdir())


# --------------------------------------------------------------------- 驗證

def test_verify_rejects_a_pdf_that_does_not_open(wsenv):
    bad = wsenv / "bad.pdf"
    bad.write_bytes(b"%PDF-1.7\n" + b"\x00garbage" * 2000)
    with pytest.raises(ws.UnsupportedType) as e:
        ws.save_file_for_key("u1", bad, "bad.pdf", verify=True)
    assert "毀損" in str(e.value)
    # 沒要求驗證時行為不變（既有的上傳不因此改變）
    assert ws.save_file_for_key("u1", bad, "bad.pdf")["ext"] == ".pdf"


def test_verify_accepts_real_and_encrypted_pdfs(wsenv):
    ok = wsenv / "ok.pdf"
    ok.write_bytes(_real_pdf(3))
    assert ws.save_file_for_key("u1", ok, "ok.pdf", verify=True)["ext"] == ".pdf"
    doc = fitz.open(stream=_real_pdf(1), filetype="pdf")
    enc = wsenv / "enc.pdf"
    doc.save(str(enc), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    doc.close()
    assert ws.save_file_for_key("u1", enc, "enc.pdf", verify=True)["ext"] == ".pdf", \
        "加密 PDF 要收（內容由使用者之後解密），不可以當成毀損"


def test_expect_rejects_a_login_page_pretending_to_be_a_pdf(wsenv):
    page = wsenv / "doc.pdf"
    page.write_text("<html><body>請先登入</body></html>", encoding="utf-8")
    with pytest.raises(ws.UnsupportedType):
        ws.save_file_for_key("u1", page, "doc.pdf", expect={".pdf"})
    assert ws.list_files(_Req()) == []


def test_origin_is_kept_and_checked(wsenv):
    src = wsenv / "a.pdf"
    src.write_bytes(_real_pdf(1))
    meta = ws.save_file_for_key("u1", src, "a.pdf",
                                origin={"import_item_id": 42, "device": "C5571"})
    assert meta["origin"] == {"import_item_id": 42, "device": "C5571"}
    assert ws.get_file(_Req(), meta["file_id"])[1]["origin"]["import_item_id"] == 42
    for bad in ({"Bad Key": 1}, {"k": {"nested": 1}}, {"k": "x" * 600}):
        with pytest.raises(ValueError):
            ws.save_file_for_key("u1", src, "a.pdf", origin=bad)


def test_find_for_key_only_sees_that_users_files(wsenv):
    src = wsenv / "a.pdf"
    src.write_bytes(_real_pdf(1))
    fid = ws.save_file_for_key("u1", src, "a.pdf")["file_id"]
    assert ws.find_for_key("u1", fid)["file_id"] == fid
    assert ws.find_for_key("u2", fid) is None
    assert ws.find_for_key("u1", "../u2") is None
    assert ws.find_for_key("u1", "0" * 32) is None


# --------------------------------------------------------------------- 上傳

def test_an_uploaded_pdf_is_not_read_in_one_go(wsenv):
    from tests.test_workspace_audio import _NoBigReads
    src = _big_pdf(wsenv / "upload.pdf", 6 * MB)
    _NoBigReads.max_read = 0
    with _NoBigReads(str(src), "rb") as f:
        meta = ws.save_stream(_Req(), f, "掃描.pdf", "手動上傳")
    assert meta["ext"] == ".pdf" and meta["size"] == 6 * MB
    assert _sha(ws.get_file(_Req(), meta["file_id"])[0]) == _sha(src)


def test_an_uploaded_pdf_does_not_cost_its_size_in_memory(wsenv):
    src = _big_pdf(wsenv / "upload.pdf", 64 * MB)

    def run():
        with src.open("rb") as f:
            ws.save_stream(_Req(), f, "big.pdf")

    peak = _peak_mb(run)
    assert peak < 8, f"上傳 64 MB 的 PDF 用掉 {peak:.0f} MB 記憶體"


# --------------------------------------------------------------------- 容量預留

def test_two_writes_cannot_both_squeeze_into_the_quota(wsenv, monkeypatch):
    """A 寫到一半時 B 量到的用量要算進 A 預留的那份 —— 否則兩個都過。"""
    ws.save_settings({"per_user_quota_mb": 3})
    a = _big_pdf(wsenv / "a.pdf", 2 * MB)
    b = _big_pdf(wsenv / "b.pdf", 2 * MB)
    started, go = threading.Event(), threading.Event()
    real = ws._copy_stream

    def slow(fin, fout):
        started.set()
        assert go.wait(10)
        return real(fin, fout)

    monkeypatch.setattr(ws, "_copy_stream", slow)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("a", ws.save_file_for_key("u1", a, "a.pdf")))
    t.start()
    assert started.wait(10)
    monkeypatch.setattr(ws, "_copy_stream", real)
    try:
        with pytest.raises(ws.QuotaExceeded):
            ws.save_file_for_key("u1", b, "b.pdf")
    finally:
        go.set()
        t.join(10)
    assert out["a"]["ext"] == ".pdf"
    assert ws._reserved_bytes("u1") == 0, "寫完要把預留還回去"


def test_a_failed_write_gives_its_reservation_back(wsenv, monkeypatch):
    ws.save_settings({"per_user_quota_mb": 3})
    a = _big_pdf(wsenv / "a.pdf", 2 * MB)

    def boom(fin, fout):
        raise OSError("disk full")

    real = ws._copy_stream
    monkeypatch.setattr(ws, "_copy_stream", boom)
    with pytest.raises(OSError):
        ws.save_file_for_key("u1", a, "a.pdf")
    monkeypatch.setattr(ws, "_copy_stream", real)
    assert ws._reserved_bytes("u1") == 0
    assert ws.save_file_for_key("u1", a, "a.pdf")["ext"] == ".pdf"


def test_bytes_and_stream_paths_share_the_reservation(wsenv, monkeypatch):
    """預留要涵蓋**每一條**寫入工作區的路 —— 只有新函式自己互相擋沒有用。"""
    ws.save_settings({"per_user_quota_mb": 3})
    a = _big_pdf(wsenv / "a.pdf", 2 * MB)
    started, go = threading.Event(), threading.Event()
    real = ws._copy_stream

    def slow(fin, fout):
        started.set()
        assert go.wait(10)
        return real(fin, fout)

    monkeypatch.setattr(ws, "_copy_stream", slow)
    t = threading.Thread(target=lambda: ws.save_file_for_key("u1", a, "a.pdf"))
    t.start()
    assert started.wait(10)
    try:
        with pytest.raises(ws.QuotaExceeded):
            ws.save_bytes(_Req(), _big_pdf(wsenv / "c.pdf", 2 * MB).read_bytes(), "c.pdf")
    finally:
        go.set()
        t.join(10)


def test_half_written_parts_do_not_count_twice(wsenv):
    d = wsenv / "workspace" / "u1" / ("a" * 32)
    d.mkdir(parents=True)
    (d / "file.pdf.part").write_bytes(b"x" * MB)
    assert ws.usage_for_key("u1")["used_bytes"] == 0, \
        "寫到一半的 .part 已經算在預留裡，再算一次就重複了"


# --------------------------------------------------------------------- 作業結果

class _Job:
    def __init__(self, path: Path, owner=1, meta=None):
        self.id = "j" * 32
        self.status = "done"
        self.result_path = path
        self.result_filename = path.name
        self.tool_id = "pdf-merge"
        self.owner_id = owner
        self.owner_label = "u1"
        self.meta = meta or {}


@pytest.fixture
def not_watched(monkeypatch):
    from app.core.job_manager import job_manager
    monkeypatch.setattr(job_manager, "is_being_watched", lambda _id: False)


def test_autosave_does_not_read_the_result_into_memory(wsenv, not_watched):
    from app.core import job_autosave
    src = _big_pdf(wsenv / "result.pdf", 64 * MB)
    res = {}
    peak = _peak_mb(lambda: res.update(job_autosave.on_job_finished(_Job(src))))
    assert res.get("saved") is True, res
    assert peak < 8, f"自動保存 64 MB 的結果用掉 {peak:.0f} MB 記憶體"
    assert src.exists(), "自動保存是複製，作業結果要留著給下載"


def test_autosave_reuses_what_the_tool_already_saved(wsenv, not_watched):
    from app.core import job_autosave
    src = wsenv / "a.pdf"
    src.write_bytes(_real_pdf(1))
    fid = ws.save_file_for_key("u1", src, "已匯入.pdf")["file_id"]
    job = _Job(src, meta={"workspace": {"saved": True, "file_id": fid, "name": "已匯入.pdf"}})
    res = job_autosave.on_job_finished(job)
    assert res["saved"] is True and res["file_id"] == fid
    assert len(ws.list_files(_Req())) == 1, "工具已經存過，自動保存又存了一份"


def test_autosave_reuse_needs_the_file_to_still_be_there(wsenv, not_watched):
    """記錄說存過、檔案卻已經不在（過期 / 被刪）→ 不可以回傳一個不存在的 file_id。"""
    from app.core import job_autosave
    src = wsenv / "a.pdf"
    src.write_bytes(_real_pdf(1))
    job = _Job(src, meta={"workspace": {"saved": True, "file_id": "f" * 32, "name": "x.pdf"}})
    res = job_autosave.on_job_finished(job)
    assert res["saved"] is True and res["file_id"] != "f" * 32
    assert ws.find_for_key("u1", res["file_id"]) is not None


def test_autosave_reuse_does_not_trust_another_users_file(wsenv, not_watched):
    from app.core import job_autosave
    src = wsenv / "a.pdf"
    src.write_bytes(_real_pdf(1))
    other = ws.save_file_for_key("u2", src, "別人的.pdf")["file_id"]
    job = _Job(src, owner=1, meta={"workspace": {"saved": True, "file_id": other}})
    res = job_autosave.on_job_finished(job)
    assert res["file_id"] != other, "記錄裡的 file_id 是別人的，卻被當成自己的"


def test_the_save_button_for_a_job_result_does_not_read_it_whole(client, monkeypatch):
    """「存至工作區」（帶 job_id）原本 `read_bytes()` 整份讀 —— 實際打端點，讀整份就紅。"""
    import uuid
    from pathlib import Path as _P
    from app.config import settings
    from app.core.job_manager import Job, job_manager
    ws.save_settings({"enabled": True, "per_user_quota_mb": 0, "max_file_mb": 0,
                      "retention_hours": -1})
    jid = uuid.uuid4().hex
    p = _big_pdf(settings.temp_dir / f"{jid}_out.pdf", 3 * MB)
    job = Job(id=jid, tool_id="pdf-merge")
    job.result_path, job.result_filename, job.status = p, "merged.pdf", "done"
    job_manager._jobs[jid] = job
    real = _P.read_bytes

    def guarded(self):
        assert self != p, "作業結果被整份讀進記憶體"
        return real(self)

    monkeypatch.setattr(_P, "read_bytes", guarded)
    try:
        r = client.post("/workspace/save", data={"job_id": jid})
    finally:
        job_manager._jobs.pop(jid, None)
    assert r.status_code == 200, r.text
    meta = r.json()["file"]
    assert (meta["name"], meta["size"], meta["source_tool"]) == ("merged.pdf", 3 * MB, "pdf-merge")
    assert p.exists()
