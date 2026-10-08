"""公文撰擬的歷史案件管理（2026-10-08 使用者：「公文撰擬 請參考送件前檢核 加入歷史案件管理」）。

原本一個案件是 `data/temp/od_<案件編號>_*` 幾個檔，跟著暫存的保留期走 —— 作業紀錄一過期，
草稿、版本與歸屬紀錄就跟著被清掉，也沒有地方看得到自己寫過哪些。現在：

* 案件存在 `data/official_doc_cases/<案件編號>/`（`app/core/official_doc_cases.py`），
  **暫存區整個清掉也打得開**；擁有者記在案件裡。
* 「我的作業」的「開啟」是 `?case=<案件編號>`（按案件定址），作業紀錄過期之後照樣打得開。
* 歷史案件頁：自己的清單、改名、刪除（軟刪除）；管理員看得到每個人的、已刪除的反灰。
* 舊的暫存案件第一次被打開時搬過來 —— **擁有者照原本的紀錄，不是打開它的人**。
* 保留期在「檔案保留 / 清理」（`official_doc_cases_days`）。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

import pytest

from tests.test_official_doc_tool import (BASE, FakeLLM, _rt, _run, _user_client,  # noqa: F401
                                          _wait_job, fake_llm)


def _cases_mod():
    from app.core import official_doc_cases
    return official_doc_cases


def _case_dir(cid: str) -> Path:
    return _cases_mod().case_dir(cid)


def _wipe_temp(cid: str) -> int:
    from app.config import settings
    n = 0
    for p in settings.temp_dir.glob(f"od_{cid}*"):
        p.unlink()
        n += 1
    owners = settings.temp_dir / ".owners" / f"{cid}.json"
    if owners.exists():
        owners.unlink()
    return n


def _ids(rows) -> list[str]:
    return [r["case_id"] for r in rows]


# ------------------------------------------------------------------ 存在哪裡、活多久

def test_a_new_case_lives_outside_temp_and_survives_a_temp_wipe(client, auth_off, fake_llm):
    job_id, cid, res = _run(client)
    d = _case_dir(cid)
    for name in ("meta.json", "case.json", "result.json", "revisions.json"):
        assert (d / name).is_file(), f"案件的 {name} 不在 {d}"
    from app.config import settings
    assert not list(settings.temp_dir.glob(f"od_{cid}_*.json")), \
        "案件的輸入 / 草稿 / 版本還寫在暫存區 —— 暫存一清就不見了"
    _wipe_temp(cid)                      # 預覽圖、下載用的 .odt 都清掉
    r = client.get(f"{BASE}/result/{cid}")
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["text"] == res["draft"]["text"]
    assert client.get(f"{BASE}/revisions/{cid}").json()["latest"] == 1


def test_the_job_opens_the_case_not_the_job(client, auth_off, fake_llm):
    job_id, cid, _ = _run(client)
    from app.core.job_manager import job_manager
    j = job_manager.get(job_id)
    assert j.meta["case_id"] == cid and "upload_id" not in j.meta
    assert j.meta["view_url"] == f"{BASE}/?case={cid}"
    # 「我的作業」的「開啟」不跟著暫存區消失
    from app.core import job_files
    _wipe_temp(cid)
    row = {"id": job_id, "status": "done", "result_path": None, "meta": dict(j.meta)}
    assert job_files.view_ok(row, job_files.temp_index(), auth_on=True)


def test_case_info_reports_the_result_and_the_last_job(client, auth_off, fake_llm):
    job_id, cid, res = _run(client)
    info = client.get(f"{BASE}/case/{cid}").json()
    assert info["has_result"] is True
    assert info["job"] == {"id": job_id, "status": "done"}
    assert info["title"] == res["title"] and info["mode"] == "sign"
    assert info["latest_rev"] == 1


def test_meta_tracks_title_issues_and_revisions(client, auth_off, fake_llm):
    _, cid, res = _run(client)
    text = res["draft"]["text"] + "\n（補充）"
    r = client.post(f"{BASE}/revisions", json={"case_id": cid, "text": text, "base_rev": 1})
    assert r.status_code in (200, 201), r.text
    meta = _cases_mod().load_meta(cid)
    assert meta["title"] == res["title"]
    assert meta["latest_rev"] == 2 and meta["revisions"] == 2
    assert set(meta["issues"]) >= {"error", "todo", "hint"}


# ------------------------------------------------------------------ 清單

def test_the_list_shows_my_cases_newest_first(client, auth_off, fake_llm):
    _, a, _ = _run(client)
    time.sleep(0.02)
    _, b, _ = _run(client, mode="endorse")
    rows = client.get(f"{BASE}/api/cases").json()["cases"]
    assert _ids(rows)[:2] == [b, a]
    assert rows[0]["mode"] == "endorse" and rows[0]["mode_name"] == "簽辦意見"
    only = client.get(f"{BASE}/api/cases", params={"mode": "endorse"}).json()["cases"]
    assert b in _ids(only) and a not in _ids(only)
    found = client.get(f"{BASE}/api/cases", params={"q": rows[1]["title"][:6]}).json()["cases"]
    assert a in _ids(found)


def test_the_list_is_per_user_and_admin_sees_everyone(admin_session, fake_llm):
    admin, _, _ = admin_session
    alice = _user_client("odc_alice")
    bob = _user_client("odc_bob")
    _, ca, _ = _run(alice)
    _, cb, _ = _run(bob)
    assert ca in _ids(alice.get(f"{BASE}/api/cases").json()["cases"])
    assert cb not in _ids(alice.get(f"{BASE}/api/cases").json()["cases"]), "清單看得到別人的案件"
    assert ca not in _ids(bob.get(f"{BASE}/api/cases").json()["cases"])
    got = admin.get(f"{BASE}/api/cases").json()
    assert {ca, cb} <= set(_ids(got["cases"])) and got["show_owner"] is True
    owners = {r["case_id"]: r["owner"] for r in got["cases"]}
    assert owners[ca].startswith("odc_alice") and owners[cb].startswith("odc_bob")
    # 一般使用者的清單不帶擁有者欄（自己的東西）
    assert "owner" not in alice.get(f"{BASE}/api/cases").json()["cases"][0]


def test_another_user_cannot_open_rename_or_delete_my_case(admin_session, fake_llm):
    alice = _user_client("odc_owner")
    eve = _user_client("odc_eve")
    _, cid, _ = _run(alice)
    for r in (eve.get(f"{BASE}/case/{cid}"),
              eve.get(f"{BASE}/result/{cid}"),
              eve.post(f"{BASE}/case/{cid}/rename", json={"name": "偷改"}),
              eve.delete(f"{BASE}/case/{cid}")):
        assert r.status_code == 404, (r.request.method, r.request.url, r.status_code)
    meta = _cases_mod().load_meta(cid)
    assert meta["name"] == "" and not meta["deleted_at"]
    # 「不是你的」跟「不存在」同一句話 —— 分得出來就等於提供查詢「這個編號存不存在」
    missing = eve.get(f"{BASE}/case/{uuid.uuid4().hex}")
    assert missing.status_code == 404
    assert missing.json() == eve.get(f"{BASE}/case/{cid}").json()


def test_admin_can_read_someone_elses_case_and_it_is_audited(admin_session, fake_llm):
    admin, _, _ = admin_session
    alice = _user_client("odc_audited")
    _, cid, _ = _run(alice)
    assert admin.get(f"{BASE}/result/{cid}").status_code == 200
    from app.core import audit_db
    rows = audit_db.conn().execute(
        "SELECT target FROM audit_events WHERE event_type='admin_file_override'").fetchall()
    assert f"official-doc:{cid}" in [r[0] for r in rows]


# ------------------------------------------------------------------ 改名、刪除

def test_rename_shows_up_in_the_list_and_cleans_the_name(client, auth_off, fake_llm):
    _, cid, res = _run(client)
    r = client.post(f"{BASE}/case/{cid}/rename", json={"name": "  採購\t印表機\n簽  "})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "採購 印表機 簽"
    row = [x for x in client.get(f"{BASE}/api/cases").json()["cases"] if x["case_id"] == cid][0]
    assert row["name"] == "採購 印表機 簽" and row["title"] == res["title"]
    # 清空＝回到用草稿的標題
    assert client.post(f"{BASE}/case/{cid}/rename", json={"name": ""}).json()["name"] == ""


def test_rename_over_the_limit_is_refused_not_truncated(client, auth_off, fake_llm):
    _, cid, _ = _run(client)
    limit = _cases_mod().MAX_NAME_CHARS
    r = client.post(f"{BASE}/case/{cid}/rename", json={"name": "名" * (limit + 1)})
    assert r.status_code == 400 and "上限" in r.json()["detail"]
    assert _cases_mod().load_meta(cid)["name"] == ""
    r = client.post(f"{BASE}/case/{cid}/rename", json={"name": 123})
    assert r.status_code == 400


def test_delete_hides_the_case_from_its_owner(admin_session, fake_llm):
    admin, _, _ = admin_session
    alice = _user_client("odc_deleter")
    _, cid, _ = _run(alice)
    assert alice.delete(f"{BASE}/case/{cid}").status_code == 200
    assert cid not in _ids(alice.get(f"{BASE}/api/cases").json()["cases"])
    assert alice.get(f"{BASE}/result/{cid}").status_code == 404, "刪除之後本人還打得開"
    assert alice.get(f"{BASE}/case/{cid}").status_code == 404
    # 管理員看得到（反灰），寫著誰刪的；也打得開
    row = [x for x in admin.get(f"{BASE}/api/cases").json()["cases"] if x["case_id"] == cid][0]
    assert row["deleted"] is True and row["deleted_by"].startswith("odc_deleter")
    assert admin.get(f"{BASE}/result/{cid}").status_code == 200
    # 刪除有稽核
    from app.core import audit_db
    rows = audit_db.conn().execute(
        "SELECT target FROM audit_events WHERE event_type='official_doc_case_delete'").fetchall()
    assert f"official-doc:{cid}" in [r[0] for r in rows]


def test_with_auth_off_deleted_cases_are_gone_from_the_list_and_the_page(client, auth_off, fake_llm):
    _, cid, _ = _run(client)
    assert client.delete(f"{BASE}/case/{cid}").status_code == 200
    assert cid not in _ids(client.get(f"{BASE}/api/cases").json()["cases"])
    assert client.get(f"{BASE}/result/{cid}").status_code == 404


# ------------------------------------------------------------------ 舊的暫存案件

def _legacy_case(owner_uid=None) -> str:
    """做一份 v1.16.66 以前的案件：只有 `data/temp/od_<編號>_*.json` 與歸屬紀錄。"""
    from app.config import settings
    cid = uuid.uuid4().hex
    out = {"case_id": cid, "mode": "sign", "inputs": {"mode": "sign", "narrative": "x"},
           "title": "舊案件的標題", "draft": {"text": "簽\n主旨：舊的草稿。", "facts": [],
                                              "issues": []},
           "created_at": time.time() - 3600}
    t = settings.temp_dir
    t.mkdir(parents=True, exist_ok=True)
    (t / f"od_{cid}_result.json").write_text(json.dumps(out, ensure_ascii=False), "utf-8")
    (t / f"od_{cid}_case.json").write_text(json.dumps(
        {"case_id": cid, "mode": "sign", "inputs": out["inputs"]}, ensure_ascii=False), "utf-8")
    if owner_uid is not None:
        (t / ".owners").mkdir(exist_ok=True)
        (t / ".owners" / f"{cid}.json").write_text(json.dumps({"user_id": owner_uid}), "utf-8")
    return cid


def test_a_legacy_case_is_moved_over_when_opened(client, auth_off):
    cid = _legacy_case()
    r = client.get(f"{BASE}/result/{cid}")
    assert r.status_code == 200 and r.json()["title"] == "舊案件的標題"
    d = _case_dir(cid)
    assert (d / "meta.json").is_file() and (d / "result.json").is_file() and (d / "case.json").is_file()
    _wipe_temp(cid)
    assert client.get(f"{BASE}/result/{cid}").status_code == 200, "搬過來之後還在讀暫存區"
    assert cid in _ids(client.get(f"{BASE}/api/cases").json()["cases"])


def test_a_legacy_case_keeps_its_owner_when_an_admin_opens_it(admin_session):
    admin, _, _ = admin_session
    from app.core import auth_db
    alice = _user_client("odc_legacy")
    uid = auth_db.conn().execute("SELECT id FROM users WHERE username='odc_legacy'").fetchone()["id"]
    cid = _legacy_case(owner_uid=uid)
    assert admin.get(f"{BASE}/result/{cid}").status_code == 200
    assert _cases_mod().load_meta(cid)["owner_uid"] == uid, "管理員打開別人的舊案件，案件變成管理員的"
    assert cid in _ids(alice.get(f"{BASE}/api/cases").json()["cases"])


def test_a_legacy_case_of_someone_else_stays_closed(admin_session):
    from app.core import auth_db
    _user_client("odc_legacy_owner")
    eve = _user_client("odc_legacy_eve")
    uid = auth_db.conn().execute(
        "SELECT id FROM users WHERE username='odc_legacy_owner'").fetchone()["id"]
    cid = _legacy_case(owner_uid=uid)
    assert eve.get(f"{BASE}/result/{cid}").status_code in (403, 404)
    assert _cases_mod().load_meta(cid) is None, "別人打不開的舊案件被搬過去了"


# ------------------------------------------------------------------ 保留期

def test_retention_purges_old_cases_and_counts_deleted_ones_from_the_deletion(data_dir_cases):
    m = _cases_mod()
    old, fresh, gone = (uuid.uuid4().hex for _ in range(3))
    for cid in (old, fresh, gone):
        m.create(cid, owner_uid=None, mode="sign")
    day = 86400
    _set(old, updated_at=time.time() - 400 * day)
    _set(gone, updated_at=time.time() - 400 * day, deleted_at=time.time() - 10 * day)
    assert m.purge_older_than(-1) == 0 and m.purge_older_than(0) == 0, "永久保留還是刪了"
    assert m.purge_older_than(365) == 1
    assert m.load_meta(old) is None
    assert m.load_meta(fresh) is not None
    assert m.load_meta(gone) is not None, "已刪除的案件要從刪除那天起算"


def _set(cid, **fields):
    m = _cases_mod()
    p = m.file(cid, "meta.json")
    meta = json.loads(p.read_text(encoding="utf-8"))
    meta.update(fields)
    p.write_text(json.dumps(meta), encoding="utf-8")


@pytest.fixture
def data_dir_cases():
    m = _cases_mod()
    import shutil
    shutil.rmtree(m.root(), ignore_errors=True)
    yield
    shutil.rmtree(m.root(), ignore_errors=True)


def test_retention_runs_the_case_purge_and_shows_the_category(data_dir_cases, monkeypatch):
    from app.core import retention
    assert retention.get()["official_doc_cases_days"] == 365
    seen = []
    monkeypatch.setattr(_cases_mod(), "purge_older_than", lambda d: seen.append(d) or 0)
    report = retention.sweep_all()
    assert seen == [365] and report["official_doc_cases"] == 0
    st = retention.collect_stats()["official_doc_cases"]
    assert set(st) >= {"size_mb", "files", "oldest_days"}
    html = Path("app/admin/templates/admin_retention.html").read_text(encoding="utf-8")
    assert "'official_doc_cases_days'" in html, "保留設定頁沒有這一列 —— 管理員改不到"


# ------------------------------------------------------------------ 頁面

def test_cases_page_lists_and_links_by_case(client, auth_off, fake_llm):
    _, cid, res = _run(client)
    html = client.get(f"{BASE}/cases").text
    assert f'href="/tools/official-doc/?case={cid}"' in html
    assert res["title"] in html
    assert 'data-odc-rename' in html and 'data-odc-delete' in html


def test_cases_page_empty_state(client, auth_off, data_dir_cases):
    html = client.get(f"{BASE}/cases").text
    assert "還沒有案件" in html and 'href="/tools/official-doc/"' in html


def test_the_tool_page_links_to_the_history(client, auth_off):
    html = client.get(f"{BASE}/").text
    assert re.search(r'<a [^>]*href="/tools/official-doc/cases"[^>]*id="odCasesLink"', html)
