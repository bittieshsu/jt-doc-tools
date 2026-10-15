"""機內信箱取件（推測實作）—— 路由與背景作業：列文件、取件、存進工作區。

使用者 2026-10-14：
* 「暫時不能（安排實機）所以你要用推測方式撰寫」
* 「工作區允許 tiff，但是取件回來預設存成 pdf」
* 「取件功能請儘可能在使用流程給 user 越簡單越易懂越好」

驗：
* 預設存 PDF（多頁 TIFF → 一份 PDF、頁數對）；選原始格式就照設備給的存（TIFF 不轉、逐頁的加頁序）。
* 轉 PDF 失敗改存原始檔並註明；工作區停用時留在暫存區，多個檔打包 ZIP 下載。
* 「取件後刪除」的信箱要確認才取；取完再列一次，記下原件還在不在。
* 設備端的錯誤回 422（不是 401，免得畫面以為本站登入過期），附協定紀錄；回應與稽核都不含密碼。
* 只有 FUJIFILM 有這個功能；別人拿作業 id 查不到。
"""
from __future__ import annotations

import importlib
import io
import time
import zipfile

import pytest

from app.core import mfp_client, mfp_output, mfp_queue, mfp_scan, mfp_store
from tests.test_mfp_box import FakeBox, jpeg_bytes, tiff_bytes
from tests.test_mfp_fetch_tool import _fake_report, _user_client, fake_probe  # noqa: F401

R = importlib.import_module("app.tools.mfp_fetch.router")
API = "/tools/mfp-fetch/api"


@pytest.fixture
def dev(monkeypatch, admin_session, fake_probe):
    real = mfp_client.check_address
    monkeypatch.setattr(mfp_client, "check_address", lambda ip: None if ip == "127.0.0.1" else real(ip))
    f = FakeBox()
    srv = f.serve()
    port = srv.server_address[1]
    monkeypatch.setattr(mfp_client, "ALLOWED_PORTS", tuple(mfp_client.ALLOWED_PORTS) + (port,))
    f.port = port
    mfp_queue._reset_for_tests()
    mfp_scan._reset_for_tests()
    R._recent.clear()
    R._busy.clear()
    yield f
    srv.shutdown()
    mfp_queue._reset_for_tests()


def _device(uid, port, vendor="FUJIFILM"):
    rep = _fake_report("127.0.0.1")
    rep["summary"]["reachable_on"] = [f"http:{port}"]
    rep["detection"]["vendor"] = vendor
    c = mfp_store.create(f"u{uid}", uid, "127.0.0.1", port, "http", "三樓", rep)
    return c["id"]


@pytest.fixture
def me(dev):
    a, ua = _user_client(f"box-a-{time.time_ns()}", ["clerk"])
    b, ub = _user_client(f"box-b-{time.time_ns()}", ["clerk"])
    return {"a": a, "ua": ua, "b": b, "ub": ub, "cid": _device(ua, dev.port)}


def _wait(c, sid, until=("done", "error", "cancelled"), timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get(f"{API}/scans/{sid}").json()["scan"]
        if s["status"] in until:
            return s
        time.sleep(0.05)
    raise AssertionError(f"作業沒有到 {until}：{s}")


def _fetch(c, cid, docs, **kw):
    body = {"box": 1, "documents": docs, "allow_http": True, **kw}
    return c.post(f"{API}/connections/{cid}/box/fetch", json=body)


@pytest.fixture
def ws_on(monkeypatch):
    from app.core import workspace
    monkeypatch.setattr(workspace, "is_enabled", lambda: True)
    return workspace


# --------------------------------------------------------------------- 列文件

def test_the_list_shows_documents_and_the_protocol(dev, me):
    dev.add_whole(5, name="報價單", Pages="3")
    r = me["a"].post(f"{API}/connections/{me['cid']}/box/list", json={"box": 1, "allow_http": True})
    assert r.status_code == 200, r.text
    j = r.json()
    assert [d["id"] for d in j["documents"]] == ["5"] and j["documents"][0]["name"] == "報價單"
    assert j["via"]["protocol"] == "WebDAV" and j["trace"], "要看得出走什麼協定，也要有協定紀錄"
    row = next(x for x in me["a"].get(f"{API}/connections").json()["connections"] if x["id"] == me["cid"])
    assert row["box"] is True, "FUJIFILM、連得上 → 清單上有「信箱取件」"


def test_device_auth_errors_are_422_with_a_trace_and_no_password(dev, me):
    from app.core import audit_db
    dev.realm = "box-1"
    r = me["a"].post(f"{API}/connections/{me['cid']}/box/list",
                     json={"box": 1, "box_password": "S3cret-Wrong", "allow_http": True})
    assert r.status_code == 422 and r.json()["code"] == "AUTH_FAILED", r.text
    assert r.json()["trace"] and "S3cret" not in r.text
    # 稽核是背景執行緒非同步寫入的：等那一筆寫進去再讀
    end = time.monotonic() + 5
    rows = []
    while time.monotonic() < end and not rows:
        rows = audit_db.conn().execute("SELECT target, details_json FROM audit_events WHERE event_type="
                                       "'mfp_box_list' ORDER BY id DESC LIMIT 5").fetchall()
        time.sleep(0.05)
    assert rows and all("S3cret" not in repr(tuple(x)) for x in rows)
    assert '"code": "AUTH_FAILED"' in rows[0][1]


def test_http_consent_and_missing_password_come_back_as_clear_codes(dev, me):
    r = me["a"].post(f"{API}/connections/{me['cid']}/box/list", json={"box": 1})
    assert r.status_code == 422 and r.json()["code"] == "PLAIN_HTTP_NEEDS_CONSENT" and not dev.requests
    dev.realm = "user-Local"
    r = me["a"].post(f"{API}/connections/{me['cid']}/box/list", json={"box": 1, "allow_http": True})
    assert r.json()["code"] == "AUTH_REQUIRED_USER", "設備要求登入 → 畫面才多問帳號"


def test_only_fujifilm_devices_have_a_mailbox(dev, me):
    rep = _fake_report("127.0.0.2")
    rep["detection"]["vendor"] = "HP"
    c = mfp_store.create(f"u{me['ua']}", me["ua"], "127.0.0.2", 80, "http", "hp", rep)
    cid = c["id"]
    r = me["a"].post(f"{API}/connections/{cid}/box/list", json={"box": 1, "allow_http": True})
    assert r.status_code == 400 and r.json()["code"] == "BOX_NOT_FUJIFILM"
    row = next(x for x in me["a"].get(f"{API}/connections").json()["connections"] if x["id"] == cid)
    assert row["box"] is False


def test_other_people_cannot_use_my_device(dev, me):
    r = me["b"].post(f"{API}/connections/{me['cid']}/box/list", json={"box": 1, "allow_http": True})
    assert r.status_code == 404 and not dev.requests


# --------------------------------------------------------------------- 取件

def test_fetch_saves_a_pdf_into_my_workspace_by_default(dev, me, ws_on):
    import fitz
    dev.add_whole(5, data=tiff_bytes(3), name="報價單")
    r = _fetch(me["a"], me["cid"], ["5"])
    assert r.status_code == 202, r.text
    s = _wait(me["a"], r.json()["scan"]["id"])
    assert s["status"] == "done" and s["kind"] == "box" and s["pages"] == 3, s
    assert [f["name"] for f in s["files"]] == ["報價單.pdf"] and s["files"][0]["workspace_file_id"]
    assert s["items"][0]["source"] == "SOURCE_KEPT" and s["items"][0]["source_message"]
    assert s["settings"]["via"]["protocol"] == "WebDAV" and s["settings"]["output"] == "pdf"
    path, meta = _ws_file(ws_on, me["ua"], s["files"][0]["workspace_file_id"])
    with fitz.open(path) as d:
        assert d.page_count == 3
    assert {r["method"] for r in dev.requests} <= {"PROPFIND", "GET"}
    assert me["b"].get(f"{API}/scans/{s['id']}").status_code == 404


def _ws_file(ws, uid, file_id):
    meta = ws.find_for_key(f"u{uid}", file_id)
    assert meta, "要在送出者自己的工作區"
    return ws._root() / f"u{uid}" / file_id / f"file{meta['ext']}", meta


def test_original_format_keeps_the_device_files(dev, me, ws_on):
    dev.add_whole(5, data=tiff_bytes(2), name="報價單")
    dev.add_pages(6, {"1-1.tif": tiff_bytes(1), "2-1.jpg": jpeg_bytes()}, name="合約")
    sid = _fetch(me["a"], me["cid"], ["5", "6"], output="original").json()["scan"]["id"]
    s = _wait(me["a"], sid)
    assert s["status"] == "done", s
    assert [f["name"] for f in s["files"]] == ["報價單.tif", "合約-p001.tif", "合約-p002.jpg"]
    path, meta = _ws_file(ws_on, me["ua"], s["files"][0]["workspace_file_id"])
    assert meta["ext"] == ".tif" and path.read_bytes() == dev.docs["5"]["whole"], "原始檔一個位元組都不改"


def test_page_folders_become_one_pdf(dev, me, ws_on):
    dev.add_pages(6, {"2-1.jpg": jpeg_bytes(), "1-1.tif": tiff_bytes(1)}, name="合約")
    s = _wait(me["a"], _fetch(me["a"], me["cid"], ["6"]).json()["scan"]["id"])
    assert [f["name"] for f in s["files"]] == ["合約.pdf"] and s["pages"] == 2


def test_a_failed_pdf_conversion_keeps_the_original(dev, me, ws_on, monkeypatch):
    def boom(*a, **k):
        raise mfp_output.OutputError("PDF_FAILED", {"error": "x"})
    monkeypatch.setattr(mfp_output, "to_pdf", boom)
    dev.add_whole(5, data=tiff_bytes(2), name="報價單")
    s = _wait(me["a"], _fetch(me["a"], me["cid"], ["5"]).json()["scan"]["id"])
    assert s["status"] == "done" and [f["name"] for f in s["files"]] == ["報價單.tif"]
    assert s["items"][0]["format_note"] == "PDF_FALLBACK_ORIGINAL" and s["items"][0]["format_message"]


def test_delete_after_get_needs_confirmation_and_is_reported(dev, me, ws_on):
    dev.add_whole(5, name="a", deleteAfterGet="true")
    dev.delete_on_get = True
    s = _wait(me["a"], _fetch(me["a"], me["cid"], ["5"]).json()["scan"]["id"])
    assert s["status"] == "error" and s["items"][0]["error_code"] == "DELETE_AFTER_GET_UNCONFIRMED"
    assert not [r for r in dev.requests if r["method"] == "GET"], "沒確認就不可以取"
    s = _wait(me["a"], _fetch(me["a"], me["cid"], ["5"], confirm_delete_after_get=True).json()["scan"]["id"])
    assert s["status"] == "done" and s["items"][0]["source"] == "SOURCE_GONE"


def test_some_documents_missing_is_a_partial_result(dev, me, ws_on):
    dev.add_whole(5, name="a")
    s = _wait(me["a"], _fetch(me["a"], me["cid"], ["5", "77"]).json()["scan"]["id"])
    assert s["status"] == "done" and s["error_code"] == "BOX_PARTIAL"
    assert [i["status"] for i in s["items"]] == ["ok", "error"] and s["items"][1]["error_code"] == "DOC_NOT_FOUND"


def test_a_wrong_password_stops_the_whole_job(dev, me, ws_on):
    dev.realm = "box-1"
    dev.add_whole(5)
    s = _wait(me["a"], _fetch(me["a"], me["cid"], ["5"], box_password="nope").json()["scan"]["id"])
    assert s["status"] == "error" and s["error_code"] == "AUTH_FAILED"
    assert (s["error_detail"] or {}).get("trace"), "失敗要附協定紀錄"
    assert len([r for r in dev.requests if r["headers"].get("authorization")]) == 1


def test_without_a_workspace_several_files_download_as_a_zip(dev, me, monkeypatch):
    from app.core import workspace
    monkeypatch.setattr(workspace, "is_enabled", lambda: False)
    dev.add_pages(6, {"1-1.tif": tiff_bytes(1), "2-1.tif": tiff_bytes(1)}, name="合約")
    s = _wait(me["a"], _fetch(me["a"], me["cid"], ["6"], output="original").json()["scan"]["id"])
    assert s["status"] == "done" and s["workspace_note"] == "WS_DISABLED" and s["has_file"]
    r = me["a"].get(f"{API}/scans/{s['id']}/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert sorted(z.namelist()) == ["合約-p001.tif", "合約-p002.tif"]
    assert me["b"].get(f"{API}/scans/{s['id']}/download").status_code == 404


@pytest.mark.parametrize("body,code", [
    ({"documents": []}, "NO_DOCUMENTS_SELECTED"),
    ({"documents": ["1"] * 2}, "NO_DOCUMENTS_SELECTED"),
    ({"documents": ["../1"]}, "NO_DOCUMENTS_SELECTED"),
    ({"documents": [str(i) for i in range(21)]}, "TOO_MANY_DOCUMENTS"),
    ({"documents": ["1"], "output": "docx"}, "BOX_OUTPUT_INVALID"),
    ({"documents": ["1"], "box": 0}, "BOX_INVALID"),
    ({"documents": "1"}, "NO_DOCUMENTS_SELECTED"),
])
def test_bad_fetch_requests_are_refused_before_queueing(dev, me, body, code):
    r = me["a"].post(f"{API}/connections/{me['cid']}/box/fetch", json={"box": 1, "allow_http": True, **body})
    assert r.status_code == 422 and r.json()["code"] == code, r.text
    assert not dev.requests and mfp_queue.snapshot("127.0.0.1")["busy"] is False


def test_box_jobs_and_scans_share_one_queue_per_device(dev, me, ws_on, monkeypatch):
    """同一台設備：取件跟掃描排同一條隊伍（不會兩件同時連設備）。"""
    t = mfp_queue.enqueue("127.0.0.1", "someone", "scan")
    try:
        dev.add_whole(5)
        sid = _fetch(me["a"], me["cid"], ["5"]).json()["scan"]["id"]
        st = _wait(me["a"], sid, until=("queued",))
        assert st["ahead"] == 1 and not dev.requests
    finally:
        mfp_queue.done(t)
    assert _wait(me["a"], sid)["status"] == "done"
