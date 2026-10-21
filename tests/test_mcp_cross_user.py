"""MCP：一個人的 Token 拿不到另一個人的檔案與作業（v1.17.6）。

站台開了認證時，每張 Token 綁一個使用者，工作區與作業都依使用者分開（`ctx.ws_key`）。
這裡兩個人各拿一張 Token：A 上傳檔案、跑一件作業；B 拿著 A 的檔案編號與作業編號去叫
`jtdt_run`、`job_status`、`workspace_download_url`、`workspace_read_text`、`workspace_list`，一律要失敗，
而且錯誤訊息跟「編號不存在」一樣（不透露那個編號是別人的）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from tests.conftest import _make_pdf
from tests.test_mcp_server import SITE, _call, _upload_inline


@pytest.fixture
def two_users(admin_session):
    from app.core import api_tokens as at, notify_settings, user_manager, workspace as ws
    from app.mcp import runner, server, settings as ms, tickets
    ms.save({"enabled": True, "tools": None, "rate_per_minute": 600, "max_concurrent": 4,
             "ticket_bind_ip": True, "ticket_allow_http": False})
    notify_settings.save({"site_url": SITE})
    ws.save_settings({"enabled": True})
    runner._reset_for_tests()
    server._calls.clear()
    tickets._reset_for_tests()
    toks = []
    for name in ("mcp-alice", "mcp-bob"):
        uid = user_manager.create_local(name, name, "UserPass1234")
        tok = at.api_tokens.create(name)
        at.api_tokens.set_mcp(tok.token, True)
        at.api_tokens.assign_owner(tok.token, uid)
        toks.append(tok.token)
    with TestClient(app_main.app) as c:
        yield c, toks[0], toks[1]
    for t in toks:
        at.api_tokens.revoke(t)
    ms.save({"enabled": False, "tools": None})
    notify_settings.save({"site_url": ""})


def test_another_users_files_and_jobs_are_out_of_reach(two_users):
    c, alice, bob = two_users
    a1 = _upload_inline(c, alice, _make_pdf(1), "a1.pdf")
    a2 = _upload_inline(c, alice, _make_pdf(2), "a2.pdf")
    note = _upload_inline(c, alice, "機密備忘".encode("utf-8"), "note.txt")
    err, res = _call(c, alice, "jtdt_run", {"tool": "pdf_merge", "files": [a1, a2]})
    assert not err, res
    job = res["job_id"]
    out = res["files"][0]["file_id"]

    # 拿 A 的檔案去跑工具
    err, msg = _call(c, bob, "jtdt_run", {"tool": "pdf_merge", "files": [a1, a2]})
    assert err, "B 不能拿 A 的檔案跑工具"
    b1 = _upload_inline(c, bob, _make_pdf(1), "b1.pdf")
    err, msg_mixed = _call(c, bob, "jtdt_run", {"tool": "pdf_merge", "files": [b1, a1]})
    assert err, "混一個自己的也不行"
    err, msg_fake = _call(c, bob, "jtdt_run", {"tool": "pdf_merge", "files": [b1, "f" * 32]})
    assert err
    assert msg_mixed.replace(a1, "<id>") == msg_fake.replace("f" * 32, "<id>"), \
        "別人的編號跟不存在的編號要回一樣的錯誤（不透露那是別人的）"

    # 查 A 的作業
    err, msg = _call(c, bob, "job_status", {"job_id": job, "wait_seconds": 0})
    assert err and "not found" in msg
    err, res = _call(c, alice, "job_status", {"job_id": job, "wait_seconds": 0})
    assert not err and res["status"] == "done", "A 自己查得到"

    # 下載、讀 A 的檔案
    for fid in (out, a1, note):
        err, _ = _call(c, bob, "workspace_download_url", {"file_id": fid})
        assert err, f"B 不能拿到 A 的檔案的下載票證：{fid}"
    err, _ = _call(c, bob, "workspace_read_text", {"file_id": note})
    assert err
    err, res = _call(c, alice, "workspace_read_text", {"file_id": note})
    assert not err and "機密備忘" in str(res)

    # 清單只有自己的
    err, res = _call(c, bob, "workspace_list", {})
    assert not err
    ids = {f["file_id"] for f in res["files"]}
    assert ids == {b1}, ids
    err, res = _call(c, alice, "workspace_list", {})
    assert {a1, a2, note, out} <= {f["file_id"] for f in res["files"]}


def test_a_download_ticket_only_works_for_the_file_it_was_issued_for(two_users):
    """下載票證綁檔案與工作區：B 用自己的票證換不到 A 的檔案。"""
    c, alice, bob = two_users
    a1 = _upload_inline(c, alice, _make_pdf(3), "a1.pdf")
    b1 = _upload_inline(c, bob, _make_pdf(1), "b1.pdf")
    err, res = _call(c, bob, "workspace_download_url", {"file_id": b1})
    assert not err, res
    import re
    m = re.search(r"X-JTDT-Ticket: ([A-Za-z0-9_\-\.]+)", str(res))
    assert m, res
    r = c.get("/mcp/files/download", params={"file_id": a1}, headers={"X-JTDT-Ticket": m.group(1)})
    import fitz
    assert r.status_code == 200, r.text
    assert r.headers.get("X-Content-SHA256") == res["sha256"]
    assert fitz.open(stream=r.content, filetype="pdf").page_count == 1, "網址帶了 A 的檔案編號，換到的仍只是 B 自己那份"
    # 下載票證可以用幾次（續傳、重試），用完就失效
    from app.mcp import tickets
    codes = [c.get("/mcp/files/download", headers={"X-JTDT-Ticket": m.group(1)}).status_code
             for _ in range(tickets.DOWNLOAD_MAX_USES)]
    assert codes[:tickets.DOWNLOAD_MAX_USES - 1] == [200] * (tickets.DOWNLOAD_MAX_USES - 1) and codes[-1] == 403, codes
