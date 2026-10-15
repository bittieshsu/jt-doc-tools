"""KYOCERA / RICOH 直接取件（SOAP，實驗功能）—— 路由與背景作業（v1.16.95）。

* 清單：KYOCERA 有「取件」（連接埠固定 9091 / 9090，不靠連線測試）；RICOH 要連線測試連得上網頁那組協定與連接埠。
* 列文件：KYOCERA 的位置是文件匣（標號＋名稱、有擁有者的不標共享）、RICOH 是文件伺服器；名稱可以有斜線、中文。
* 取件：檔名用設備上的名稱（不是編碼過的識別碼）；多個部分合成一份 PDF，或存原始格式並加頁序；
  原件還在不在照樣記；錯誤回 422 附協定紀錄，回應裡沒有密碼。
"""
from __future__ import annotations

import time

import pytest

from app.core import mfp_client, mfp_queue, mfp_scan, mfp_settings, mfp_soap, mfp_store
from tests.test_mfp_fetch_tool import _fake_report, _user_client, fake_probe  # noqa: F401
from tests.test_mfp_soap import FakeKyocera, FakeRicoh

import importlib

R = importlib.import_module("app.tools.mfp_fetch.router")
API = "/tools/mfp-fetch/api"


@pytest.fixture
def env(monkeypatch, admin_session, fake_probe):
    real = mfp_client.check_address
    monkeypatch.setattr(mfp_client, "check_address", lambda ip: None if ip == "127.0.0.1" else real(ip))
    monkeypatch.setattr(mfp_soap.time, "sleep", lambda s: None)
    kyo = FakeKyocera().start()
    ricoh = FakeRicoh().start()
    monkeypatch.setattr(mfp_soap, "KYO_PORTS", {"https": kyo.port, "http": kyo.port})
    monkeypatch.setattr(mfp_client, "ALLOWED_PORTS", tuple(mfp_client.ALLOWED_PORTS) + (ricoh.port,))
    mfp_queue._reset_for_tests()
    mfp_scan._reset_for_tests()
    R._recent.clear()
    R._busy.clear()
    a, ua = _user_client(f"soap-a-{time.time_ns()}", ["clerk"])
    b, ub = _user_client(f"soap-b-{time.time_ns()}", ["clerk"])
    yield {"kyo": kyo, "ricoh": ricoh, "a": a, "ua": ua, "b": b, "ub": ub}
    kyo.stop()
    ricoh.stop()
    mfp_queue._reset_for_tests()
    mfp_settings.save({"allow_user_devices": True})


@pytest.fixture
def ws_on(monkeypatch):
    from app.core import workspace
    monkeypatch.setattr(workspace, "is_enabled", lambda: True)
    return workspace


def _device(uid, vendor, port=None, host="127.0.0.1"):
    rep = _fake_report(host)
    rep["detection"]["vendor"] = vendor
    rep["summary"]["reachable_on"] = [f"http:{port}"] if port else []
    return mfp_store.create(f"u{uid}", uid, host, port, "http" if port else None, vendor, rep)["id"]


def _row(c, cid):
    return next(x for x in c.get(f"{API}/connections").json()["connections"] if x["id"] == cid)


def _wait(c, sid, until=("done", "error", "cancelled"), timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get(f"{API}/scans/{sid}").json()["scan"]
        if s["status"] in until:
            return s
        time.sleep(0.05)
    raise AssertionError(f"作業沒有到 {until}：{s}")


def _pages(ws, uid, fid):
    import fitz
    meta = ws.find_for_key(f"u{uid}", fid)
    with fitz.open(ws._root() / f"u{uid}" / fid / f"file{meta['ext']}") as d:
        return d.page_count


KYO = {"route": "kyocera_soap_http", "allow_http": True, "user": "clerk", "password": "pw-1"}
RIC = {"route": "ricoh_soap", "allow_http": True, "user": "clerk", "password": "pw-2"}


def test_which_devices_get_direct_retrieval(env):
    e = env
    kyo = _device(e["ua"], "Kyocera", host="127.0.0.2")
    ricoh = _device(e["ua"], "Ricoh", e["ricoh"].port)
    ricoh_no_web = _device(e["ua"], "Ricoh", host="127.0.0.3")
    assert _row(e["a"], kyo)["share"] == {"routes": ["kyocera_soap", "kyocera_soap_http"]}, \
        "KYOCERA 的連接埠是固定的，不靠連線測試"
    assert _row(e["a"], ricoh)["share"] == {"routes": ["ricoh_soap"]}
    assert _row(e["a"], ricoh_no_web)["share"] is None, "RICOH 走網頁那組協定與連接埠：連線測試沒連上就沒有"
    r = e["a"].post(f"{API}/connections/{kyo}/share/list", json=dict(RIC))
    assert r.status_code == 400 and r.json()["code"] == "SHARE_ROUTE_INVALID"


def test_kyocera_list_boxes_and_fetch_into_workspace(env, ws_on):
    e = env
    cid = _device(e["ua"], "Kyocera")
    r = e["a"].post(f"{API}/connections/{cid}/share/list", json=KYO)
    assert r.status_code == 200, r.text
    j = r.json()
    assert [(x["id"], x["label"], x["shared"]) for x in j["roots"]] == [("1", "1 會計", True), ("2", "2 個人", False)]
    assert j["root"] == "1" and j["via"] == {"protocol": "SOAP", "scheme": "http", "port": e["kyo"].port}
    assert [x["label"] for x in j["entries"]] == ["合約", "報價 / 2026"]
    assert "pw-1" not in r.text
    files = [[x["name"]] for x in j["entries"]]
    r = e["a"].post(f"{API}/connections/{cid}/share/fetch", json=dict(KYO, root="1", files=files))
    assert r.status_code == 202, r.text
    assert r.json()["scan"]["settings"]["via"]["protocol"] == "SOAP"
    s = _wait(e["a"], r.json()["scan"]["id"])
    assert s["status"] == "done", s
    assert [f["name"] for f in s["files"]] == ["合約.pdf", "報價 2026.pdf"], "檔名用設備上的名稱（斜線換掉）"
    assert [i["name"] for i in s["items"]] == ["合約", "報價 / 2026"]
    assert all(i["source"] == "SOURCE_KEPT" for i in s["items"])
    assert _pages(ws_on, e["ua"], s["files"][0]["workspace_file_id"]) == 2
    assert e["kyo"].logged_out, "取完要登出"
    assert e["b"].get(f"{API}/scans/{s['id']}").status_code == 404


def test_kyocera_several_parts_become_one_pdf_or_numbered_originals(env, ws_on):
    e = env
    e["kyo"].parts_per_doc, e["kyo"].final_with_data = 2, False
    cid = _device(e["ua"], "Kyocera")
    name = mfp_soap.enc_name("合約")
    r = e["a"].post(f"{API}/connections/{cid}/share/fetch", json=dict(KYO, root="1", files=[[name]]))
    s = _wait(e["a"], r.json()["scan"]["id"])
    assert s["status"] == "done" and [f["name"] for f in s["files"]] == ["合約.pdf"], s
    assert _pages(ws_on, e["ua"], s["files"][0]["workspace_file_id"]) == 4, "兩個部分各 2 頁，照順序合成"
    r = e["a"].post(f"{API}/connections/{cid}/share/fetch", json=dict(KYO, root="1", files=[[name]], output="original"))
    s = _wait(e["a"], r.json()["scan"]["id"])
    assert [f["name"] for f in s["files"]] == ["合約-p001.tif", "合約-p002.tif"], s


def test_kyocera_box_password_and_wrong_account(env):
    e = env
    cid = _device(e["ua"], "Kyocera")
    r = e["a"].post(f"{API}/connections/{cid}/share/list", json=dict(KYO, root="2"))
    assert r.status_code == 422 and r.json()["code"] == "BOX_PASSWORD_FAILED"
    r = e["a"].post(f"{API}/connections/{cid}/share/list", json=dict(KYO, root="2", box_password="1234"))
    assert r.status_code == 200 and [x["label"] for x in r.json()["entries"]] == ["私人"]
    r = e["a"].post(f"{API}/connections/{cid}/share/list", json=dict(KYO, password="wrong-secret"))
    assert r.status_code == 422 and r.json()["code"] == "AUTH_FAILED"
    assert r.json()["trace"] and "wrong-secret" not in r.text


def test_ricoh_list_and_fetch_original_documents(env, ws_on):
    e = env
    cid = _device(e["ua"], "Ricoh", e["ricoh"].port)
    r = e["a"].post(f"{API}/connections/{cid}/share/list", json=RIC)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["root"] == "documents" and [x["id"] for x in j["roots"]] == ["documents"]
    by = {x["label"]: x for x in j["entries"]}
    assert set(by) == {"合約", "報價", "會議紀錄"} and by["會議紀錄"]["locked"] is True
    r = e["a"].post(f"{API}/connections/{cid}/share/fetch", json=dict(
        RIC, root="documents", files=[[by["合約"]["name"]], [by["報價"]["name"]]]))
    s = _wait(e["a"], r.json()["scan"]["id"])
    assert s["status"] == "done", s
    assert [f["name"] for f in s["files"]] == ["合約.pdf", "報價.pdf"]
    assert _pages(ws_on, e["ua"], s["files"][0]["workspace_file_id"]) == 2, "兩個內容元素（各一頁 TIFF）合成一份"
    assert all(i["source"] == "SOURCE_KEPT" for i in s["items"])
    assert e["ricoh"].terminated
    assert "SECRETURLTOKEN" not in str(s), "作業紀錄裡不可以有下載網址的權杖"


def test_ricoh_locked_document_needs_the_document_password(env, ws_on):
    e = env
    cid = _device(e["ua"], "Ricoh", e["ricoh"].port)
    name = mfp_soap.enc_name("doc-3")
    r = e["a"].post(f"{API}/connections/{cid}/share/fetch", json=dict(RIC, root="documents", files=[[name]]))
    s = _wait(e["a"], r.json()["scan"]["id"])
    assert s["status"] == "error" and s["items"][0]["error_code"] == "DOC_PASSWORD_REQUIRED"
    r = e["a"].post(f"{API}/connections/{cid}/share/fetch", json=dict(RIC, root="documents", files=[[name]],
                                                                       box_password="9999"))
    s = _wait(e["a"], r.json()["scan"]["id"])
    assert s["status"] == "done" and [f["name"] for f in s["files"]] == ["會議紀錄.pdf"], s
    assert "9999" not in str(s) and "pw-2" not in str(s)
