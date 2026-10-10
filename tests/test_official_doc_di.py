"""公文撰擬匯出 DI 檔（政府電子公文的文書本文檔，XML）＋ 機關名稱從地址簿挑、帶機關代碼。

2026-10-09 使用者：「規劃一下匯出 DI 檔功能」「匯出功能請一次做到好，旁邊做一個匯出預覽」
「發文機關全銜跟受文者要有一個可以從地址簿選的按鈕…隨打即找，這樣到時才能帶入機關代碼」。

要守住的事：

* **每一份產出都要驗得過官方 104 版 DTD**（跟著程式放在 `app/core/di_dtd/`），而且這條檢查有牙齒：
  元素順序調換就驗不過。DTD 是測試的依據，**缺了不可以 skip**。
* 欄位照文字放：主旨不含「主旨：」、段名帶冒號、條列照層級巢狀、括號半形；發文日期、發文字號、文號
  留空就留空（取號時給），有填就整串照放，不拆、不猜。
* 機關代碼：先用使用者從地址簿挑的，沒有才照名稱查（**名稱完全相同而且只有一筆**）；本機關內部單位
  （「本局…」）不算沒對到；地址簿沒下載、沒對到的都講出來。前端送來的代碼存進案件前要驗過。
* 簽辦意見沒有 DI 檔（400）；找不到主旨也是 400。
* `/export` 的 DI 與 `/di-preview` 是同一支產生器、同一套歸屬檢查。
"""
from __future__ import annotations

import json
import re

import pytest

from app.core import official_doc as od
from app.core import official_doc_di as di
from app.core import official_doc_sources as ods

from tests.test_official_doc_sources import clean  # noqa: F401
from tests.test_official_doc_tool import BASE, _run, case_id, fake_llm  # noqa: F401

BOOK = [
    {"orgId": "Q1000000", "orgName": "嘉禾市政府", "statusCode": "T"},
    {"orgId": "Q20000000B", "orgName": "嘉禾市東湖區公所", "statusCode": "T"},
    {"orgId": "Q30000000C", "orgName": "臺北示範局", "statusCode": "T"},
    # 同名兩個代碼（真的地址簿裡有）：不可以猜
    {"orgId": "Q40000000D", "orgName": "示範同名處", "statusCode": "T"},
    {"orgId": "Q50000000E", "orgName": "示範同名處", "statusCode": "T"},
]
ADDRESS_BOOK = "archives-address-book"


@pytest.fixture
def book(clean):  # noqa: F811
    ods.install_upload(ADDRESS_BOOK, json.dumps(BOOK, ensure_ascii=False).encode("utf-8"), "ab.json")
    return BOOK


def _letter(**kw) -> str:
    content = kw.pop("content", None) or {
        "subject": "檢送本局資訊設備汰換計畫1份",
        "explanation": ["依據　貴府115年10月1日府資字第1150001234號函辦理。", "本局預計汰換電腦20台。"],
        "measures": ["請於10月31日前回復。"],
    }
    args = dict(org="嘉禾市資訊局", receiver="嘉禾市政府", relation="up", closing="請　鑒核",
                copies="", cc="本局資訊安全科", signature="局長　王○○",
                contact="地址：嘉禾市中正路1號\n承辦人：林小明\n電話：02-1234-5678",
                attachments="計畫書1份", speed="普通件", doc_no="")
    args.update(kw)
    return od.assemble_letter(content, **args)


def _sign(**kw) -> str:
    content = kw.pop("content", None) or {
        "subject": "擬辦理本局資訊設備汰換", "explanation": ["本局電腦使用已逾6年。"],
        "proposal": ["奉核後辦理採購。"]}
    args = dict(unit="資訊室", closing="核示", addressee="主任、局長", date_line="中華民國115年10月9日")
    args.update(kw)
    return od.assemble_sign(content, **args)


def _root(data: bytes):
    from lxml import etree
    return etree.fromstring(data, etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False))


def _ok(data: bytes, mode: str) -> None:
    ok, errs = di.validate(data, mode)
    assert ok, (errs, data.decode("utf-8"))


# ------------------------------------------------------------------ DTD 是測試的依據

def test_the_official_dtd_files_ship_with_the_program():
    for name in ("104_2_utf8.dtd", "104_5_utf8.dtd", "104_basic_utf8.ent", "104_exchange_utf8.ent"):
        assert (di.DTD_DIR / name).is_file(), f"{name} 不見了 —— DI 檔沒辦法驗格式"
    # 函的內容模型就是官方那一條（順序固定）
    assert "發文機關+,函類別,地址,聯絡方式+,受文者,發文日期,發文字號+" in (
        di.DTD_DIR / "104_2_utf8.dtd").read_text(encoding="utf-8")


def test_the_dtd_check_has_teeth():
    """元素順序調換（主旨跑到附件前面）就驗不過 —— 不然「通過 DTD」這句話沒有意義。"""
    data, _ = di.build(_letter(), "letter")
    _ok(data, "letter")
    s = data.decode("utf-8")
    att = re.search(r"  <附件>.*?</附件>\n", s, re.S).group(0)
    subj = re.search(r"  <主旨>.*?</主旨>\n", s, re.S).group(0)
    swapped = s.replace(att, "@@A@@").replace(subj, att).replace("@@A@@", subj)
    assert swapped != s
    ok, errs = di.validate(swapped.encode("utf-8"), "letter")
    assert not ok and errs


# ------------------------------------------------------------------ 函

def test_letter_fields_land_in_the_right_elements():
    data, notes = di.build(_letter(), "letter", org_codes={"嘉禾市政府": "Q1000000"},
                           lookup=lambda n: {"嘉禾市資訊局": "Q13000000"}.get(n, ""))
    _ok(data, "letter")
    r = _root(data)
    assert r.tag == "函" and data.startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE')
    assert r.findtext("發文機關/全銜") == "嘉禾市資訊局"
    assert r.findtext("發文機關/機關代碼") == "Q13000000"
    assert r.find("函類別").get("代碼") == "函"
    assert r.findtext("地址") == "嘉禾市中正路1號"
    assert [e.text for e in r.findall("聯絡方式")] == ["承辦人：林小明", "電話：02-1234-5678"]
    assert r.findtext("受文者/全銜") == "嘉禾市政府" and r.findtext("受文者/機關代碼") == "Q1000000"
    # 發文日期、發文字號留空 —— 公文系統取號時給
    assert not (r.findtext("發文日期/年月日") or "") and r.find("發文字號/文號/年度") is not None
    assert r.find("速別").get("代碼") == "普通件"
    assert r.findtext("附件/文字") == "計畫書1份"
    assert r.findtext("主旨/文字") == "檢送本局資訊設備汰換計畫1份，請　鑒核。", "主旨不含「主旨：」"
    paras = r.findall("段落")
    assert [p.get("段名") for p in paras] == ["說明：", "辦法："]
    assert [i.get("序號") for i in paras[0].findall("條列")] == ["一、", "二、"]
    assert paras[1].findtext("文字") == "請於10月31日前回復。"
    assert [e.text for e in r.findall("正本/全銜")] == ["嘉禾市政府"]
    assert [e.text for e in r.findall("副本/全銜")] == ["本局資訊安全科"]
    assert [e.text for e in r.findall("署名")] == ["局長　王○○"]
    # 本局的內部單位本來就沒有代碼，不算沒對到；兩個機關都對到了 —— 沒有任何注意事項
    assert notes == [], notes


def test_doc_number_and_date_are_kept_whole_when_given():
    text = _letter(doc_no="資字第1150000123號").replace("發文日期：", "發文日期：115年10月9日")
    data, _ = di.build(text, "letter")
    _ok(data, "letter")
    r = _root(data)
    assert r.findtext("發文字號/文字") == "資字第1150000123號", "有填就整串放，不拆成字號"
    assert r.find("發文字號/字") is None
    assert r.findtext("發文日期/年月日") == "中華民國115年10月9日"


def test_nested_items_follow_their_levels():
    content = {"subject": "測試巢狀", "explanation": [], "measures": []}
    text = _letter(content=content) .replace(
        "主旨：測試巢狀", "主旨：測試巢狀") + ""
    text = text.replace("正本：", "說明：\n一、第一項。\n（一）第一項之一。\n1、細項甲。\n（1）更細。\n二、第二項。\n正本：")
    data, _ = di.build(text, "letter")
    _ok(data, "letter")
    r = _root(data)
    p = r.find("段落")
    one = p.findall("條列")
    assert [i.get("序號") for i in one] == ["一、", "二、"]
    sub = one[0].find("條列")
    assert sub.get("序號") == "(一)", "括號照官方範例用半形"
    assert sub.find("條列").get("序號") == "1、"
    assert sub.find("條列/條列").get("序號") == "(1)"
    assert sub.findtext("條列/條列/文字") == "更細。"


def test_copies_with_attachments_and_secrecy_value():
    text = _letter(copies="嘉禾市政府、嘉禾市東湖區公所", cc="臺北示範局（含附件）").replace(
        "密等及解密條件或保密期限：", "密等及解密條件或保密期限：密")
    data, _ = di.build(text, "letter")
    _ok(data, "letter")
    r = _root(data)
    assert [e.text for e in r.findall("正本/全銜")] == ["嘉禾市政府", "嘉禾市東湖區公所"]
    assert [e.text for e in r.findall("副本/全銜")] == ["臺北示範局"]
    assert r.findtext("副本/含附件") == "含附件"
    assert r.find("密等及解密條件或保密期限/密等").get("代碼") == "密"


def test_company_letter_has_no_agency_only_fields_and_keeps_placeholders():
    text = _letter(relation=od.COMPANY_RELATION, closing="請　查照", org="示範資訊股份有限公司",
                   contact="", signature="")
    data, notes = di.build(text, "letter")
    _ok(data, "letter")
    r = _root(data)
    assert r.find("密等及解密條件或保密期限") is None, "企業的函沒有密等"
    assert "〔待補" in data.decode("utf-8"), "〔待補〕要原樣留著，匯入後看得到要補什麼"
    codes = [n["code"] for n in notes]
    assert "placeholders" in codes
    n = next(x for x in notes if x["code"] == "placeholders")
    assert int(n["args"][0]) == len(od.PLACEHOLDER_RE.findall(text))


def test_unrecognised_lines_are_merged_not_dropped():
    text = _letter().replace("二、本局預計汰換電腦20台。", "二、本局預計汰換電腦20台。\n共分兩批交貨。")
    data, notes = di.build(text, "letter")
    _ok(data, "letter")
    assert "本局預計汰換電腦20台。共分兩批交貨。" in data.decode("utf-8")
    assert {"code": "unplaced", "args": ["1"]} in notes


def test_text_is_escaped_and_control_chars_dropped():
    content = {"subject": "A&B <公司> 合作\x07案", "explanation": [], "measures": []}
    data, _ = di.build(_letter(content=content), "letter")
    _ok(data, "letter")
    assert _root(data).findtext("主旨/文字").startswith("A&B <公司> 合作案")


# ------------------------------------------------------------------ 簽

def test_sign_fields():
    data, notes = di.build(_sign(), "sign")
    _ok(data, "sign")
    r = _root(data)
    assert r.tag == "簽"
    assert r.findtext("發文機關/全銜") == "資訊室"
    assert r.find("文號/流水號") is not None and not (r.findtext("文號/流水號") or "")
    assert r.findtext("主旨/文字") == "擬辦理本局資訊設備汰換，簽請　核示。"
    assert [p.get("段名") for p in r.findall("段落")] == ["說明：", "擬辦："]
    assert [e.text for e in r.findall("敬陳/職稱")] == ["主任", "局長"]
    assert r.find("署名") is not None
    assert r.findtext("年月日") == "中華民國115年10月9日"
    assert notes == []


def test_sign_without_date_or_addressee_is_still_valid():
    data, _ = di.build(_sign(addressee="", date_line=""), "sign")
    _ok(data, "sign")
    r = _root(data)
    assert r.find("敬陳") is None and r.find("年月日") is not None


@pytest.mark.parametrize("mode, text", [
    ("letter", None), ("sign", None),
])
def test_many_shapes_all_validate(mode, text):
    """各種寫法（多段說明、只有一項、有附件、空副本、長主旨）都要驗得過。"""
    variants = []
    if mode == "letter":
        for rel in ("up", "down", "flat", "people", "unknown", od.COMPANY_RELATION):
            variants.append(_letter(relation=rel, closing=(od.LETTER_CLOSINGS.get(rel) or ("",))[0]))
        variants.append(_letter(content={"subject": "只有主旨", "explanation": [], "measures": []},
                                cc="", attachments="", contact=""))
        variants.append(_letter(content={"subject": "長" * 200, "explanation": ["單一項說明"], "measures": []}))
    else:
        variants.append(_sign(content={"subject": "只有主旨", "explanation": [], "proposal": []}))
        variants.append(_sign(content={"subject": "多項", "explanation": ["甲", "乙", "丙"],
                                       "proposal": ["一", "二"]}, unit=""))
    for v in variants:
        data, _ = di.build(v, mode)
        _ok(data, mode)


# ------------------------------------------------------------------ 出不了 DI 的

def test_endorse_and_missing_subject_are_refused():
    with pytest.raises(di.DiNotApplicable, match="簽辦意見"):
        di.build("擬依來文辦理，陳核。", "endorse")
    with pytest.raises(di.DiNotApplicable, match="主旨"):
        di.build("嘉禾市資訊局　函\n說明：沒有主旨。", "letter")


# ------------------------------------------------------------------ 機關代碼

def test_codes_from_the_address_book(book):
    assert ods.exact_org_code("嘉禾市政府") == "Q1000000"
    assert ods.exact_org_code("台北示範局") == "Q30000000C", "台臺視為相同"
    assert ods.exact_org_code("嘉禾市") == "", "名稱的一部分不算"
    assert ods.exact_org_code("示範同名處") == "", "同名好幾個代碼不可以猜"
    assert ods.org_code_matches("嘉禾市政府", "Q1000000")
    assert ods.org_code_matches("嘉禾市政府", "q1000000"), "代碼大小寫不分"
    assert not ods.org_code_matches("嘉禾市政府", "Q20000000B"), "代碼是別的機關的"
    info = ods.address_book_info()
    assert info["installed"] and info["count"] == len(BOOK) and info["updated_at"]


def test_no_address_book_means_no_codes(clean):  # noqa: F811
    assert ods.exact_org_code("嘉禾市政府") == ""
    assert not ods.org_code_matches("嘉禾市政府", "Q1000000")
    assert ods.address_book_info() == {"installed": False, "updated_at": None, "count": 0}
    data, notes = di.build(_letter(), "letter", book_available=False)
    _ok(data, "letter")
    assert notes[0]["code"] == "book_missing"


def test_missing_codes_are_named(book):
    data, notes = di.build(_letter(org="不存在的局", receiver="嘉禾市東湖區公所"), "letter",
                           lookup=ods.exact_org_code)
    r = _root(data)
    assert r.findtext("受文者/機關代碼") == "Q20000000B"
    assert not (r.findtext("發文機關/機關代碼") or "")
    assert {"code": "codes_missing", "args": ["不存在的局"]} in notes


def test_several_receivers_leave_the_code_empty(book):
    data, notes = di.build(_letter(receiver="嘉禾市政府、嘉禾市東湖區公所"), "letter",
                           lookup=ods.exact_org_code)
    _ok(data, "letter")
    assert not (_root(data).findtext("受文者/機關代碼") or "")
    assert any(n["code"] == "receiver_many" for n in notes)


def test_orgs_endpoint_reports_the_exact_match(client, auth_off, book):
    d = client.get(f"{BASE}/orgs", params={"q": "嘉禾"}).json()
    assert {"name": "嘉禾市政府", "id": "Q1000000", "marks": [[0, 2]]} in d["orgs"] and d["exact"] == ""
    assert client.get(f"{BASE}/orgs", params={"q": "嘉禾市政府"}).json()["exact"] == "Q1000000"
    assert client.get(f"{BASE}/orgs", params={"q": "示範同名處"}).json()["exact"] == ""
    assert len(client.get(f"{BASE}/orgs", params={"q": "示範", "limit": 1}).json()["orgs"]) == 1


def test_chosen_codes_are_verified_before_they_are_stored(book):
    import importlib
    from fastapi import HTTPException
    # 套件的 `router` 是 APIRouter 物件（同名子模組被遮住），要用 importlib 拿模組
    r = importlib.import_module("app.tools.official_doc.router")
    inputs = {"org": "嘉禾市資訊局", "receiver": "嘉禾市政府", "copies": "嘉禾市政府、嘉禾市東湖區公所", "cc": ""}
    got = r._org_codes({"org_codes": {"嘉禾市政府": "Q1000000", "嘉禾市東湖區公所": "Q1000000",
                                      "沒寫在函裡的局": "Q30000000C"}}, inputs)
    assert got == {"嘉禾市政府": "Q1000000"}, "代碼對不上名稱、名稱不在函裡的都不收"
    for bad in ("Q1000000", ["Q1000000"], {"嘉禾市政府": 123}, {"嘉禾市政府": "Q-1 000"},
                {f"機關{i}": "Q1000000" for i in range(r.MAX_ORG_CODES + 1)}):
        with pytest.raises(HTTPException) as e:
            r._org_codes({"org_codes": bad}, inputs)
        assert e.value.status_code == 400
    assert r._org_codes({}, inputs) == {}


# ------------------------------------------------------------------ 端點

LETTER_BODY = {"mode": "letter", "narrative": "請受文者於期限內回復資訊設備盤點結果。",
               "relation": "down", "closing": "請　查照", "receiver": "嘉禾市東湖區公所", "org": "嘉禾市政府",
               "org_codes": {"嘉禾市東湖區公所": "Q20000000B"}}


def _letter_case(client, book_ok=True):
    _, cid, res = _run(client, **LETTER_BODY)
    return cid, res


def test_export_and_preview_share_one_generator(client, auth_off, fake_llm, book):  # noqa: F811
    cid, res = _letter_case(client)
    assert res["inputs"]["org_codes"] == {"嘉禾市東湖區公所": "Q20000000B"}, "從地址簿挑的代碼要跟著案件存"
    text = res["draft"]["text"]
    r = client.post(f"{BASE}/export", json={"case_id": cid, "fmt": "di", "text": text, "title": "盤點"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/xml")
    assert ".di" in r.headers["content-disposition"]
    assert r.headers["x-jtdt-di-valid"] == "1"
    _ok(r.content, "letter")
    assert _root(r.content).findtext("受文者/機關代碼") == "Q20000000B"
    assert _root(r.content).findtext("發文機關/機關代碼") == "Q1000000", "沒挑的照名稱查地址簿"
    p = client.post(f"{BASE}/di-preview", json={"case_id": cid, "text": text, "title": "盤點"})
    assert p.status_code == 200, p.text
    d = p.json()
    assert d["xml"].encode("utf-8") == r.content, "預覽與下載要是同一份"
    assert d["valid"] is True and d["mode"] == "letter" and d["dtd"] == "104_2_utf8.dtd"
    assert d["filename"].endswith(".di") and isinstance(d["notes"], list)


def test_preview_and_export_refuse_what_has_no_di(client, auth_off, fake_llm):  # noqa: F811
    _, cid, res = _run(client, mode="endorse", source="來文：請各單位於期限內回報。",
                       direction="照辦，通知各單位回報")
    for path, extra in (("/export", {"fmt": "di"}), ("/di-preview", {})):
        r = client.post(f"{BASE}{path}", json={"case_id": cid, "text": res["draft"]["text"], **extra})
        assert r.status_code == 400 and "簽辦意見" in r.json()["detail"], r.text
    _, sid, _ = _run(client)
    r = client.post(f"{BASE}/di-preview", json={"case_id": sid, "text": "說明：沒有主旨。"})
    assert r.status_code == 400 and "主旨" in r.json()["detail"]


def test_preview_checks_ownership_like_export(client, auth_off):
    """不帶案件、或案件不存在：跟匯出一樣擋（不會變成「任意文字轉 DI」的服務）。"""
    assert client.post(f"{BASE}/di-preview", json={"text": "主旨：測試。"}).status_code in (400, 404)
    gone = client.post(f"{BASE}/export", json={"text": "主旨：測試。", "fmt": "di", "case_id": "0" * 32})
    r = client.post(f"{BASE}/di-preview", json={"text": "主旨：測試。", "case_id": "0" * 32})
    assert r.status_code == gone.status_code and r.status_code >= 400, (r.status_code, gone.status_code)


def test_page_has_the_picker_and_the_viewer(client, auth_off, book):
    html = client.get(f"{BASE}/").text
    assert 'id="odOrgList"' not in html and 'list="odOrgList"' not in html, "原生 datalist 要拿掉"
    for el_id in ("odOrg", "odReceiver"):
        assert re.search(rf'id="{el_id}"[^>]*data-org-pick="single"', html, re.S), el_id
    for el_id in ("odCopies", "odCc"):
        assert re.search(rf'id="{el_id}"[^>]*data-org-pick="multi"', html, re.S), el_id
    assert "/static/js/org_picker.js" in html
    cfg = re.search(r'<div id="orgPickerCfg"[^>]*>', html, re.S).group(0)
    assert 'data-installed="1"' in cfg and 'data-stale="0"' in cfg and 'data-admin="1"' in cfg
    assert 'data-od-dl="di"' in html and 'id="odDiPreview"' in html and 'id="odDi"' in html


def test_page_reminds_when_the_book_is_missing_or_stale(client, auth_off, clean, monkeypatch):  # noqa: F811
    html = client.get(f"{BASE}/").text
    assert 'id="odOrgMissing"' in html and 'data-installed="0"' in html
    assert 'href="/admin/official-doc"' in html.split('id="odOrgMissing"')[1][:600], "管理員要有設定頁的連結"
    import time
    monkeypatch.setattr(ods, "address_book_info",
                        lambda: {"installed": True, "updated_at": time.time() - 45 * 86400, "count": 9})
    html = client.get(f"{BASE}/").text
    assert 'id="odOrgStale"' in html and 'data-stale="1"' in html and 'data-days="45"' in html
