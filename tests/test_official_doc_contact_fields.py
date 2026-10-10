"""公文撰擬（函）：聯絡資訊改成一格一個欄位、欄位名稱由程式寫（v1.16.71）。

## 由來

原本是一個多行文字框、一行一項，欄位名稱由使用者自己打。實測三種意外：

1. 寫「住址：」或公文慣例的「地　　址：」認不出是地址 → 匯出 DI 檔時地址欄是空的
2. 某一行以「說明：」或「一、」開頭 → 聯絡資訊從那一行斷掉，**後面幾行變成公文的說明段**
3. 按 Enter 把地址折成兩行 → 變成兩筆聯絡資訊

## 判準

* 畫面送 `contact_fields`（每一欄），草稿上的欄位名稱與順序由程式定，沒填的欄不出現
* 舊的 `contact` 文字（API、升級前的案件、瀏覽器裡記的）：認得的行分進各欄；
  **認不得的行不放進草稿**，回傳 `contact_unplaced` 讓呼叫端與畫面講出來
* 「聯絡人 / 承辦人」只能二選一，不收其他字
* 統一編號只有企業的函才有
"""
from __future__ import annotations

import json

import pytest

from app.core import official_doc as od
from tests.test_official_doc_tool import (BASE, LETTER_INPUT, _js, _eval, _until,  # noqa: F401
                                          _run, _start, fake_llm, live)


# ------------------------------------------------------------------ 核心

def test_the_program_writes_the_labels_in_a_fixed_order():
    fields = {"email": "a@example.org", "phone": "(02)1234-5678", "address": "嘉禾市文化路1號",
              "person": "王○○", "fax": ""}
    assert od.contact_text(fields, False).split("\n") == [
        "地址：嘉禾市文化路1號", "聯絡人：王○○", "電話：(02)1234-5678", "電子信箱：a@example.org"]
    assert od.contact_text({}, False) == ""


def test_tax_id_only_on_a_company_letter():
    f = {"address": "甲", "tax_id": "12345678"}
    assert "統一編號" not in od.contact_text(f, False)
    assert od.contact_text(f, True).split("\n") == ["地址：甲", "統一編號：12345678"]
    assert od.clean_contact_fields(f, False)["tax_id"] == ""


def test_person_label_is_one_of_two():
    assert od.contact_text({"person": "王○○", "person_label": "承辦人"}, False) == "承辦人：王○○"
    # 其他字一律不收（那就又是自己打欄位名稱了）
    assert od.contact_text({"person": "王○○", "person_label": "說明"}, False) == "聯絡人：王○○"
    assert od.clean_contact_fields({"person_label": "經理"}, False)["person_label"] == "聯絡人"


def test_each_field_is_one_line_and_repeated_labels_are_dropped():
    f = od.clean_contact_fields({"address": "嘉禾市文化路\n1號", "phone": "電話：(02)1234-5678\next. 12",
                                 "email": "x" * 500, "bogus": "不收"}, False)
    assert f["address"] == "嘉禾市文化路1號"            # 中文折行直接接回去
    assert f["phone"] == "(02)1234-5678 ext. 12"         # 打了欄位名稱也不會變成「電話：電話：」
    assert len(f["email"]) == od.CONTACT_FIELD_MAX["email"]
    assert "bogus" not in f
    assert "\n" not in od.contact_text(f, False).split("\n")[0]


def test_old_text_is_split_into_fields():
    f, unplaced = od.split_contact(
        "地　　址：嘉禾市文化路\n1號\n承辦人：王○○\nE-mail：a@example.org\n傳真：\n電話：02-1\n電話：02-2",
        False)
    assert f["address"] == "嘉禾市文化路1號", f          # 「地　　址」認得、折行接回
    assert (f["person"], f["person_label"]) == ("王○○", "承辦人")
    assert f["email"] == "a@example.org"
    assert f["phone"] == "02-1、02-2"
    assert f["fax"] == ""                               # 只寫了欄位名稱：不算
    assert unplaced == []


def test_lines_that_fit_no_field_are_not_put_in_the_draft():
    f, unplaced = od.split_contact("地址：甲\n說明：請於上班時間來電\n一、上午請撥分機\n統編：12345678", False)
    assert unplaced == ["說明：請於上班時間來電", "一、上午請撥分機", "統編：12345678"], unplaced
    assert od.contact_text(f, False) == "地址：甲"
    # 企業的函收統一編號
    f2, u2 = od.split_contact("統編：12345678", True)
    assert f2["tax_id"] == "12345678" and u2 == []


def test_a_said_line_no_longer_turns_into_the_explanation():
    """意外②：原本「說明：…」那一行把聯絡資訊截斷，後面的電話變成說明段。"""
    fields, _ = od.split_contact("地址：嘉禾市文化路1號\n說明：請於上班時間來電\n電話：02-1", False)
    text = od.assemble_letter({"subject": "請查照", "explanation": ["甲"]}, org="嘉禾市資訊局",
                              receiver="嘉禾市東湖區公所", relation="down", closing="請　查照",
                              contact=od.contact_text(fields, False))
    blocks = od.parse_text(text)
    from app.core.official_doc_odt import _contact_zone
    zone = {blocks[i]["text"] for i in _contact_zone(blocks)}
    assert zone == {"地址：嘉禾市文化路1號", "電話：02-1"}, zone
    assert "請於上班時間來電" not in text


def test_the_di_file_gets_the_address_however_it_was_written():
    """意外①：「地　　址：」原本認不出是地址，DI 檔的地址欄是空的。"""
    from app.core import official_doc_di as di
    fields, _ = od.split_contact("地　　址：嘉禾市文化路1號\n承辦人：王○○", False)
    text = od.assemble_letter({"subject": "請查照", "explanation": ["甲"]}, org="嘉禾市資訊局",
                              receiver="嘉禾市東湖區公所", relation="down", closing="請　查照",
                              contact=od.contact_text(fields, False))
    from tests.test_official_doc_di import _root
    data, _notes = di.build(text, "letter")
    assert _root(data).findtext("地址") == "嘉禾市文化路1號"


# ------------------------------------------------------------------ 端點

def test_fields_go_into_the_draft_with_program_labels(client, auth_off, fake_llm):
    body = {**LETTER_INPUT, "contact": "這一份不看",
            "contact_fields": {"address": "嘉禾市文化路1號", "person_label": "承辦人",
                               "person": "王○○", "phone": "(02)1234-5678", "tax_id": "1"}}
    body.pop("mode", None)
    _jid, _cid, res = _run(client, mode="letter", **body)
    text = res["draft"]["text"]
    lines = text.split("\n")
    i = lines.index("地址：嘉禾市文化路1號")
    assert lines[i:i + 3] == ["地址：嘉禾市文化路1號", "承辦人：王○○", "電話：(02)1234-5678"], lines
    assert "這一份不看" not in text and "統一編號" not in text
    assert res["inputs"]["contact_fields"]["person"] == "王○○"


def test_old_api_text_reports_what_it_could_not_place(client, auth_off, fake_llm):
    body = {k: v for k, v in LETTER_INPUT.items() if k != "mode"}
    body["contact"] = "地址：嘉禾市文化路1號\n說明：請於上班時間來電"
    r = client.post(f"{BASE}/api/official-doc", json={"mode": "letter", **body})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["contact_unplaced"] == ["說明：請於上班時間來電"]
    assert "請於上班時間來電" not in d["text"]
    assert "地址：嘉禾市文化路1號" in d["text"]
    r = _start(client, mode="letter", contact="住址：嘉禾市\n雜項：x")
    assert r.status_code == 200, r.text
    assert r.json()["contact_unplaced"] == ["雜項：x"]


def test_fields_that_are_not_an_object_are_400(client, auth_off, fake_llm):
    r = _start(client, mode="letter", contact_fields="地址：甲")
    assert r.status_code == 400, r.text
    assert not fake_llm.prompts


def test_reopening_an_old_case_splits_its_contact(client, auth_off, fake_llm):
    """升級前的案件只有一行一項的文字：重新開啟時表單要拿到分好的欄位與對不到的行。"""
    import importlib
    r = importlib.import_module("app.tools.official_doc.router")
    _jid, cid, _res = _run(client, mode="letter")
    p = r._result_path(cid)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["inputs"].pop("contact_fields", None)
    data["inputs"].pop("contact_unplaced", None)
    data["inputs"]["contact"] = "地　址：嘉禾市\n承辦人：林○○\n一、上午請撥分機"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    got = client.get(f"{BASE}/result/{cid}").json()["inputs"]
    assert got["contact_fields"]["address"] == "嘉禾市"
    assert (got["contact_fields"]["person"], got["contact_fields"]["person_label"]) == ("林○○", "承辦人")
    assert got["contact_unplaced"] == ["一、上午請撥分機"]


# ------------------------------------------------------------------ 畫面

def test_the_page_uses_separate_fields_and_migrates_old_saved_text(live):
    port, send, errs, _txt = live
    page = f"http://127.0.0.1:{port}/tools/official-doc/"
    send("Page.navigate", {"url": page})
    assert _until(send, "document.readyState === 'complete' && !!document.getElementById('odGo')", 30)
    # 升級前記在瀏覽器裡的一行一項文字
    old = "地　　址：嘉禾市文化路1號\n承辦人：王○○\n說明：請於上班時間來電"
    _eval(send, "localStorage.setItem('jtdt.officialDoc.issuer','agency'),"
                "localStorage.setItem('jtdt.officialDoc.letter', JSON.stringify({org:'嘉禾市資訊局',"
                "contact:%s})), 1" % _js(old))
    send("Page.navigate", {"url": page})
    assert _until(send, "document.readyState === 'complete' && !!document.getElementById('odGo')", 30)
    _eval(send, "document.querySelector('input[name=odMode][value=letter]').click(), 1")
    got = _eval(send, "[document.getElementById('odCtAddress').value,"
                      "document.getElementById('odCtPerson').value,"
                      "document.getElementById('odCtPersonLabel').value,"
                      "document.getElementById('odCtUnplaced').hidden,"
                      "document.getElementById('odCtUnplacedList').textContent,"
                      "document.getElementById('odCtTaxIdRow').hidden]")
    assert got == ["嘉禾市文化路1號", "王○○", "承辦人", False, "說明：請於上班時間來電", True], got
    # 企業：統一編號那一格出現
    _eval(send, "(function(){var s=document.getElementById('odIssuer'); s.value='company';"
                "s.dispatchEvent(new Event('change')); return 1;})()")
    assert _eval(send, "!document.getElementById('odCtTaxIdRow').hidden")

    # ---- 重新開啟一件用舊寫法（一行一項）送出的函：各欄從案件填回來，不是瀏覽器記的值 ----
    body = {**LETTER_INPUT, "contact": "地址：甲路1號\n承辦人：林○○\n一、雜項"}
    cid = _eval(send, """(async function(){
        var r = await fetch('/tools/official-doc/start', {method:'POST',
          headers:{'Content-Type':'application/json'}, body: JSON.stringify(%s)});
        var d = await r.json();
        for (var i = 0; i < 200; i++) {
          var j = await (await fetch('/api/jobs/' + d.job_id)).json();
          if (j.status === 'done') return d.case_id;
          if (j.status === 'error') return 'error:' + j.error;
          await new Promise(function(ok){ setTimeout(ok, 200); });
        }
        return 'timeout';
      })()""" % _js(body))
    assert cid and len(cid) == 32, cid
    _eval(send, "localStorage.removeItem('jtdt.officialDoc.letter'),"
                "localStorage.removeItem('jtdt.officialDoc.letter.company'), 1")
    send("Page.navigate", {"url": f"{page}?case={cid}"})
    assert _until(send, "!document.getElementById('odResult').hidden && "
                        "document.getElementById('odCtAddress').value === '甲路1號'", 60), \
        "重新開啟案件，地址沒有填回那一格"
    got = _eval(send, "[document.getElementById('odCtPerson').value,"
                      "document.getElementById('odCtPersonLabel').value,"
                      "document.getElementById('odCtUnplacedList').textContent]")
    # 送出當下伺服器就把對不到的那一行擋掉了，案件裡記著 → 重新開啟時照樣講出來
    assert got == ["林○○", "承辦人", "一、雜項"], got
    assert not errs, errs
