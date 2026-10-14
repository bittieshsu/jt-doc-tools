"""公文撰擬歷史案件的「文號」欄（使用者 2026-10-14：「加個 文號 欄位」，附歷史案件清單截圖）。

* 清單多一欄「文號」：可以排序、可以在「顯示欄位」藏起來；搜尋框也找得到文號。
* 已經知道的先帶進來：函撰擬時填的發文字號、DI 匯入的函（發文字號）與簽（文號）。
* 可以在清單上補填或改 —— 簽送出去之後在公文系統取了號，回來記在案件上，之後找得到。
* 只是案件的紀錄，**不改草稿**。
"""
from __future__ import annotations

import json

import pytest

from app.core import official_doc_di as di
from tests.test_official_doc_tool import BASE, _run, _user_client, fake_llm  # noqa: F401


def _cases_mod():
    from app.core import official_doc_cases
    return official_doc_cases


def _row(c, cid):
    return next(x for x in c.get(f"{BASE}/api/cases").json()["cases"] if x["case_id"] == cid)


def _set(c, cid, value):
    return c.post(f"{BASE}/case/{cid}/doc-no", json={"doc_no": value})


def test_a_letter_shows_the_doc_no_it_was_written_with(client, auth_off, fake_llm):
    _, cid, _ = _run(client, mode="letter", doc_no="嘉資字第1150012345號")
    assert _row(client, cid)["doc_no"] == "嘉資字第1150012345號"


def test_a_sign_starts_empty_and_can_be_set_cleaned_and_cleared(client, auth_off, fake_llm):
    _, cid, res = _run(client)
    assert _row(client, cid)["doc_no"] == ""
    draft_before = client.get(f"{BASE}/result/{cid}").json()["draft"]["text"]
    r = _set(client, cid, "  1150012345\t號\n ")
    assert r.status_code == 200, r.text
    assert r.json()["doc_no"] == "1150012345 號"
    assert _row(client, cid)["doc_no"] == "1150012345 號"
    assert client.get(f"{BASE}/result/{cid}").json()["draft"]["text"] == draft_before, "改文號不可以動草稿"
    assert _set(client, cid, "").json()["doc_no"] == ""


def test_too_long_or_not_text_is_refused_not_truncated(client, auth_off, fake_llm):
    _, cid, _ = _run(client)
    limit = _cases_mod().MAX_DOC_NO_CHARS
    r = _set(client, cid, "1" * (limit + 1))
    assert r.status_code == 400 and "上限" in r.json()["detail"]
    assert _set(client, cid, 123).status_code == 400
    assert _cases_mod().load_meta(cid).get("doc_no", "") == ""


def test_search_finds_a_case_by_its_doc_no(client, auth_off, fake_llm):
    _, cid, _ = _run(client)
    _set(client, cid, "府資字第1159876543號")
    got = [x["case_id"] for x in client.get(f"{BASE}/api/cases", params={"q": "9876543"}).json()["cases"]]
    assert got == [cid]


def test_an_old_case_picks_up_the_doc_no_from_its_inputs_without_touching_last_modified(client, auth_off, fake_llm):
    _, cid, _ = _run(client, mode="letter", doc_no="嘉資字第1150000001號")
    cs = _cases_mod()
    meta = cs.load_meta(cid)
    meta.pop("doc_no", None)                     # 改版以前的案件沒有這個欄位
    p = cs.file(cid, "meta.json")
    p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    before = meta["updated_at"]
    assert _row(client, cid)["doc_no"] == "嘉資字第1150000001號"
    assert cs.load_meta(cid)["updated_at"] == before, "補一個欄位不是使用者改了案件"


def test_another_user_cannot_set_my_doc_no(admin_session, fake_llm):
    admin, _, _ = admin_session
    a = _user_client("od-docno-a")
    b = _user_client("od-docno-b")
    _, cid, _ = _run(a)
    assert _set(b, cid, "偷改").status_code == 404
    assert _row(a, cid)["doc_no"] == ""
    assert _set(admin, cid, "1150000002").status_code == 200, "管理員可以（同改名，會寫稽核）"


def test_the_page_has_the_column_its_toggle_and_the_value(client, auth_off, fake_llm):
    _, cid, _ = _run(client)
    _set(client, cid, "1150055555")
    html = client.get(f"{BASE}/cases").text
    assert 'data-sort="docno"' in html and 'data-odc-col="docno"' in html
    assert "1150055555" in html and 'data-k-docno="1150055555"' in html
    assert "data-odc-docno" in html, "清單上要能補填文號"


# ------------------------------------------------------------------ DI 匯入

def test_di_reads_the_sign_doc_number():
    from tests.test_official_doc_di import _sign
    data, _ = di.build(_sign(), "sign")
    data = data.replace("<年度 />".encode(), "<年度>115</年度>".encode(), 1) \
               .replace("<流水號 />".encode(), "<流水號>0012345</流水號>".encode(), 1)
    assert di.read(data)["fields"]["doc_no"] == "1150012345"
    plain, _ = di.build(_sign(), "sign")
    assert di.read(plain)["fields"]["doc_no"] == "", "空的文號就是空的，不猜"


def test_imported_cases_carry_their_doc_numbers(client, auth_off):
    from tests.test_official_doc_di import _letter, _sign
    sign, _ = di.build(_sign(), "sign")
    sign = sign.replace("<年度 />".encode(), "<年度>115</年度>".encode(), 1) \
               .replace("<流水號 />".encode(), "<流水號>0012345</流水號>".encode(), 1)
    letter, _ = di.build(_letter(doc_no="嘉資字第1150004321號"), "letter")
    r = client.post(f"{BASE}/cases/import", files=[("files", ("s.di", sign, "application/xml")),
                                                   ("files", ("l.di", letter, "application/xml"))])
    assert r.status_code == 200, r.text
    got = {x["mode"]: x["case_id"] for x in r.json()["imported"]}
    assert _row(client, got["sign"])["doc_no"] == "1150012345"
    assert _row(client, got["letter"])["doc_no"] == "嘉資字第1150004321號"
