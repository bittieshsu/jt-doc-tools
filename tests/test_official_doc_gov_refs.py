"""公文撰擬 × 知識庫的政府公開資料（v1.16.66）。

* 參考資料的出處（`attribution`）與說明（`notice`，例如「施行日期由行政院定之」）要傳到畫面上 ——
  政府資料開放授權條款要求顯名，匯入時存了、畫面沒畫等於沒做。只給畫面，不進提示。
* 草稿引用了全國法規資料庫裡**已廢止**的法規要標出來；名稱剛好是某個**現行**法規名稱的一部分不算
  （「○○法」廢止了、「○○法施行細則」還在）。沒下載過法規清單就不檢查、也不連外。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core import official_doc as od
from tests.test_official_doc_tool import fake_llm  # noqa: F401 — fixture

ROOT = Path(__file__).resolve().parent.parent


def test_references_keep_attribution_and_notice_for_the_page_only():
    refs = od.normalise_references([{
        "purpose": "business_law", "text": "第 1 條　本法用詞…", "title": "範例管理法",
        "attribution": "資料來源：法務部全國法規資料庫…依政府資料開放授權條款－第1版提供。",
        "notice": "施行日期由行政院定之", "chunk_id": "c1"}])
    assert refs[0]["attribution"].startswith("資料來源：法務部全國法規資料庫")
    assert refs[0]["notice"] == "施行日期由行政院定之"
    block = od.references_block(refs)
    assert "資料來源" not in block and "施行日期由行政院定之" not in block, "出處與說明不進提示"
    long = od.normalise_references([{"purpose": "business_law", "text": "x", "attribution": "字" * 900}])
    assert len(long[0]["attribution"]) == 500


def test_the_page_draws_attribution_and_notice_as_text():
    src = (ROOT / "app/tools/official_doc/templates/official_doc.html").read_text(encoding="utf-8")
    body = src[src.index("function renderRefs"):]
    body = body[:body.index("\n  }\n")]
    assert re.search(r"at\.textContent\s*=\s*r\.attribution", body), "出處沒有畫出來"
    assert re.search(r"nt\.textContent\s*=\s*r\.notice", body)
    assert "innerHTML" not in body


GONE = ["範例管理法", "舊公文處理條例"]
CUR = ["範例管理法施行細則", "中央法規標準法"]


@pytest.mark.parametrize("text,want", [
    ("說明：一、依範例管理法第3條辦理。", ["範例管理法"]),
    ("說明：一、依舊公文處理條例辦理。二、依範例管理法辦理。", ["範例管理法", "舊公文處理條例"]),
    ("說明：一、依範例管理法施行細則第3條辦理。", []),          # 現行法規名稱的一部分
    ("說明：一、依中央法規標準法辦理。", []),
    ("說明：一、依範例管理法施行細則及範例管理法辦理。", ["範例管理法"]),  # 另外單獨引用一次
])
def test_abolished_law_is_flagged(text, want):
    got = od.abolished_law_issues(text, GONE, CUR)
    assert sorted(i.args[0] for i in got) == sorted(want), [(i.code, i.args) for i in got]
    assert all(i.code == "law_abolished" and i.severity == "error" and i.snippet in text for i in got)


def test_nothing_downloaded_means_nothing_checked():
    assert od.abolished_law_issues("說明：一、依範例管理法辦理。", [], CUR) == []


def test_frame_lines_are_not_checked():
    """程式加的框（抬頭、正副本、署名）不是模型寫的，不檢查。"""
    text = "簽　於範例管理法研究室\n主旨：為辦理研習，簽請　核示。"
    assert od.abolished_law_issues(text, GONE, CUR) == []


def test_router_adds_the_warning_to_drafts_and_rechecks(client, auth_off, fake_llm, monkeypatch):
    import importlib
    from tests.test_official_doc_tool import BASE, _run
    rt = importlib.import_module("app.tools.official_doc.router")
    monkeypatch.setattr(rt, "_abolished_law_names", lambda: (GONE, CUR))
    _, case_id, res = _run(client)
    # 放進本文（「敬陳」之後是程式加的框，不檢查）
    text = res["draft"]["text"].replace("主旨：", "主旨：依範例管理法第5條，", 1)
    assert "依範例管理法" in text
    r = client.post(f"{BASE}/check", json={"case_id": case_id, "text": text})
    codes = [i["code"] for i in r.json()["issues"]]
    assert "law_abolished" in codes, codes
    # 沒有法規清單時不檢查
    monkeypatch.setattr(rt, "_abolished_law_names", lambda: ([], []))
    r = client.post(f"{BASE}/check", json={"case_id": case_id, "text": text})
    assert "law_abolished" not in [i["code"] for i in r.json()["issues"]]


def test_abolished_names_come_from_the_downloaded_list(monkeypatch):
    import importlib
    rt = importlib.import_module("app.tools.official_doc.router")
    from app.core.kb import gov
    monkeypatch.setattr(gov, "merged_index", lambda gid: {
        "A1": {"key": "A1", "name": "範例管理法", "abolished": True},
        "A2": {"key": "A2", "name": "範例管理法施行細則", "abolished": False},
        "A3": {"key": "A3", "name": "同名法", "abolished": True},
        "A4": {"key": "A4", "name": "同名法", "abolished": False},   # 同名有現行版本 → 不算廢止
    })
    gone, cur = rt._abolished_law_names()
    assert gone == ["範例管理法"] and "同名法" in cur and "範例管理法施行細則" in cur


def test_a_fresh_draft_carries_the_warning_too(client, auth_off, fake_llm, monkeypatch):
    import importlib
    from tests.test_official_doc_tool import SIGN_DRAFT, _run
    rt = importlib.import_module("app.tools.official_doc.router")
    monkeypatch.setattr(rt, "_abolished_law_names", lambda: (GONE, CUR))
    d = dict(SIGN_DRAFT)
    d["explanation"] = list(d.get("explanation") or []) + ["另依範例管理法辦理。"]
    fake_llm.sign_draft = d
    _, _, res = _run(client)
    assert "law_abolished" in [i["code"] for i in res["draft"]["issues"]], res["draft"]["issues"]
