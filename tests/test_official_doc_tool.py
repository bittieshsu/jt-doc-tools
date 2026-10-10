"""公文撰擬（official-doc）的端點、背景作業與頁面。

格式與事實檢查的規則在 `app/core/official_doc.py`（另有自己的測試）；這一份驗的是
**接成工具之後**的那一層：欄位白名單、不截斷、背景作業的結果接得回來、
重新檢查與匯出照目前的文字、瀏覽器送回來的資料表要重新判斷狀態、歸屬。

假模型只驗得到我們自己的程式，驗不到「模型會不會照格式回答」—— 那要拿真的模型測。
"""
from __future__ import annotations

import io
import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as app_main
from app.core import official_doc as od

BASE = "/tools/official-doc"
ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "app" / "tools" / "official_doc" / "templates" / "official_doc.html"

NARRATIVE = ("資訊室的兩台印表機已經用了八年，常卡紙，維修廠商說零件停產。"
             "想用今年度的設備費新臺幣6萬元汰換2台雷射印表機，預計11月30日前完成採購。")

SIGN_FACTS = {
    "subject": {"value": "汰換資訊室印表機", "quote": "汰換2台雷射印表機"},
    "purpose": {"value": "印表機老舊、零件停產", "quote": "維修廠商說零件停產"},
    "action": {"value": "以設備費購置雷射印表機", "quote": "想用今年度的設備費"},
    "amount": {"value": "新臺幣6萬元", "quote": "新臺幣6萬元"},
    "quantity": {"value": "雷射印表機2台", "quote": "2台雷射印表機"},
    "budget_source": {"value": None, "quote": ""},
    "schedule": {"value": "11月30日前完成採購", "quote": "預計11月30日前完成採購"},
    "others": [], "conflicts": [],
}
SIGN_DRAFT = {
    "subject": "汰換資訊室雷射印表機2台",
    "explanation": ["資訊室印表機已使用多年，常卡紙，維修廠商表示零件停產。",
                    "擬以今年度設備費支應。"],
    "proposal": ["擬購置雷射印表機2台，預算新臺幣6萬元，於11月30日前完成採購。"],
}

SOURCE = ("臺北市政府115年10月1日府資字第1150012345號函：請各機關於10月20日前"
          "填報資訊設備盤點資料，逾期視同無資料。")
ENDORSE_FACTS = {
    "sender": {"value": "臺北市政府", "quote": "臺北市政府"},
    "doc_ref": {"value": "府資字第1150012345號", "quote": "府資字第1150012345號"},
    "request": {"value": "填報資訊設備盤點資料", "quote": "填報資訊設備盤點資料"},
    "deadline": {"value": "10月20日前", "quote": "10月20日前"},
    "attachments": {"value": None, "quote": ""},
    "others": [], "conflicts": [],
}
ENDORSE_DRAFT = {"text": "臺北市政府函請本局於10月20日前填報資訊設備盤點資料，"
                         "擬照辦，由資訊室填報後函復"}

# 函：上行文（報給上級機關備查）。機關名稱是編的。
LETTER_NARRATIVE = ("本局已完成115年度資訊安全內部稽核，共發現3項缺失，均已於10月15日前改善完成，"
                    "想把稽核結果報給嘉禾市政府備查，附件是稽核報告1份。")
LETTER_FACTS = {
    "subject": {"value": "函報115年度資訊安全內部稽核結果", "quote": "完成115年度資訊安全內部稽核"},
    "purpose": {"value": None, "quote": ""},
    "request": {"value": "備查", "quote": "報給嘉禾市政府備查"},
    "amount": {"value": None, "quote": ""},
    "schedule": {"value": "10月15日前改善完成", "quote": "均已於10月15日前改善完成"},
    "attachments": {"value": "稽核報告1份", "quote": "附件是稽核報告1份"},
    "others": [], "conflicts": [],
}
LETTER_DRAFT = {
    "subject": "檢陳本局115年度資訊安全內部稽核結果",
    "explanation": ["本局已完成115年度資訊安全內部稽核，共發現3項缺失。",
                    "前揭缺失均已於10月15日前改善完成，敬請　鈞府備查。"],
    "measures": [],
}
LETTER_INPUT = {
    "mode": "letter", "narrative": LETTER_NARRATIVE, "org": "嘉禾市資訊局",
    "receiver": "嘉禾市政府", "relation": "up", "closing": "請　核備", "speed": "速件",
    "copies": "", "cc": "本局資訊安全科", "signature": "局長　王○○",
    "contact": "地址：嘉禾市中正路1號\n聯絡人：林○○\n電話：(02)0000-0000",
    "attachments": "稽核報告1份", "length": "normal",
}


def _rewrite_paragraph_in(prompt: str) -> str:
    """改寫提示裡「要改寫的這一段」那一塊（核心 `prompt_rewrite` 的格式）。"""
    m = re.search(r'要改寫的這一段：\n"""\n(.*?)\n"""', prompt, re.S)
    return m.group(1) if m else ""


class FakeLLM:
    """假模型：依提示的開頭認出是哪一步，回固定的 JSON。

    改寫：`rewrite_reply` 是 None 時，精簡＝把那一段的「預算」與逗號拿掉（看得出差異、
    數字一個都沒動），條列＝照逗號拆；其他方式原樣回。要測「冒出新數字」就給一個固定回覆。"""

    def __init__(self, *, sign_facts=None, sign_draft=None,
                 endorse_facts=None, endorse_draft=None,
                 letter_facts=None, letter_draft=None):
        self.rewrite_reply = None
        self.sign_facts = sign_facts if sign_facts is not None else SIGN_FACTS
        self.sign_draft = sign_draft if sign_draft is not None else SIGN_DRAFT
        self.endorse_facts = endorse_facts if endorse_facts is not None else ENDORSE_FACTS
        self.endorse_draft = endorse_draft if endorse_draft is not None else ENDORSE_DRAFT
        self.letter_facts = letter_facts if letter_facts is not None else LETTER_FACTS
        self.letter_draft = letter_draft if letter_draft is not None else LETTER_DRAFT
        self.prompts: list[str] = []

    def text_query(self, prompt, model=None, **kw):
        self.prompts.append(prompt)
        if "要改寫公文草稿裡的" in prompt:
            if self.rewrite_reply is not None:
                return json.dumps(self.rewrite_reply, ensure_ascii=False)
            para = _rewrite_paragraph_in(prompt)
            if '"items": [' in prompt:
                return json.dumps({"items": [x for x in re.split(r"[，,]", para) if x]},
                                  ensure_ascii=False)
            if "精簡：" in prompt:
                return json.dumps({"text": para.replace("預算", "").replace("，", "")},
                                  ensure_ascii=False)
            return json.dumps({"text": para}, ensure_ascii=False)
        if "整理出寫「簽」需要的資料" in prompt:
            return json.dumps(self.sign_facts, ensure_ascii=False)
        if "擬一份「簽」的內容" in prompt:
            return json.dumps(self.sign_draft, ensure_ascii=False)
        if "整理出寫「函」" in prompt:
            return json.dumps(self.letter_facts, ensure_ascii=False)
        if "擬一份「函」的內容" in prompt:
            return json.dumps(self.letter_draft, ensure_ascii=False)
        if "整理出擬「簽辦意見」需要的資料" in prompt:
            return json.dumps(self.endorse_facts, ensure_ascii=False)
        if "擬「簽辦意見」" in prompt:
            return json.dumps(self.endorse_draft, ensure_ascii=False)
        raise AssertionError("沒料到的提示：" + prompt[:80])


@pytest.fixture
def fake_llm(monkeypatch):
    from app.core import llm_settings as ls
    fake = FakeLLM()
    monkeypatch.setattr(ls.llm_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(ls.llm_settings, "make_client", lambda *a, **k: fake)
    monkeypatch.setattr(ls.llm_settings, "get_model_for", lambda _t: "fake-model")
    return fake


def _wait_job(job_id: str, timeout: float = 20.0):
    from app.core.job_manager import job_manager
    end = time.time() + timeout
    while time.time() < end:
        j = job_manager.get(job_id)
        if j and j.status in ("done", "error", "cancelled", "interrupted"):
            return j
        time.sleep(0.05)
    return job_manager.get(job_id)


def _start(c, **body):
    if body.get("mode") == "endorse":
        payload = {"mode": "endorse", "source": SOURCE, "direction": "擬照辦"}
    elif body.get("mode") == "letter":
        payload = dict(LETTER_INPUT)
    else:
        payload = {"mode": "sign", "narrative": NARRATIVE, "unit": "資訊室",
                   "addressee": "主任秘書\n局長", "closing": "核示", "length": "normal"}
    payload.update(body)
    return c.post(f"{BASE}/start", json=payload)


def _run(c, **body) -> tuple[str, str, dict]:
    """送出、等作業跑完，回 (job_id, case_id, result)。"""
    r = _start(c, **body)
    assert r.status_code == 200, r.text
    d = r.json()
    j = _wait_job(d["job_id"])
    assert j.status == "done", getattr(j, "error", None)
    res = c.get(f"{BASE}/result/{d['case_id']}")
    assert res.status_code == 200, res.text
    return d["job_id"], d["case_id"], res.json()


# ------------------------------------------------------------------ 頁面

def test_page_renders_both_modes(client, auth_off):
    r = client.get(f"{BASE}/")
    assert r.status_code == 200, r.text
    html = r.text
    for el_id in ("odNarrative", "odUnit", "odAddressee", "odClosing", "odLength",
                  "odWithDate", "odSource", "odDirection", "odUnits", "odDeadline",
                  "odFmt", "odLength2", "odEndClosing", "odGo", "odDraft", "odFacts",
                  "odIssues", "odRecheck", "odRegen"):
        assert f'id="{el_id}"' in html, f"頁面少了 #{el_id}"
    # 共用進度列要是**完整的元件**（只放空 <div> 的話整段腳本停住、按鈕接不上）
    assert "job-bar-inner" in html and "job-reset" in html


def test_page_renders_all_three_modes(client, auth_off):
    """三種模式的卡片與欄位都在 —— 函的欄位少一個，`params()` 讀到 null 整段腳本就停了。"""
    html = client.get(f"{BASE}/").text
    modes = set(re.findall(r'<input type="radio" name="odMode" value="([a-z]+)"', html))
    assert modes == set(od.MODES), modes
    for el_id in ("odLetter", "odLetterNarrative", "odLetterNarrativeUp",
                  "odLetterNarrativeUp-input", "odLetterNarrativeCount", "odOrg", "odReceiver",
                  "odRelation", "odRelationHint", "odLetterClosing", "odSpeed", "odLength3",
                  "odCopies", "odCc", "odAttachments", "odSignature", "odContact",
                  "odCtAddress", "odCtTaxId", "odCtTaxIdRow", "odCtPersonLabel", "odCtPerson",
                  "odCtPhone", "odCtFax", "odCtEmail", "odCtUnplaced", "odCtUnplacedList",
                  "odSalute", "odSaluteTerm", "odSaluteSelf"):
        assert f'id="{el_id}"' in html, f"頁面少了 #{el_id}"
    # 函的區塊一開始是藏起來的（預設是簽）
    assert re.search(r'<div id="odLetter" hidden', html)


def _letter_closings_on_page(html: str) -> dict:
    m = re.search(r"data-closings='([^']*)'", html)
    assert m, "函的期望語清單沒有送進頁面（data-closings）"
    import html as _h
    return json.loads(_h.unescape(m.group(1)))


def test_letter_choices_come_from_the_core(client, auth_off, monkeypatch):
    """行文關係、速別、期望語**只有核心那一份** —— 改核心的清單，頁面要跟著變
    （前端自己再寫一份的話，兩邊一定會漂，使用者選得到一個必然被 400 擋掉的期望語）。"""
    html = client.get(f"{BASE}/").text
    # 「企業發給政府機關」由發文身分決定，不在行文關係的下拉裡
    assert _select_values(html, "odRelation") == set(od.RELATIONS) - {od.COMPANY_RELATION}
    assert _select_values(html, "odIssuer") == set(od.ISSUERS)
    assert _select_values(html, "odSpeed") == set(od.LETTER_SPEEDS)
    got = _letter_closings_on_page(html)
    assert got == {k: list(v) for k, v in od.LETTER_CLOSINGS.items()}

    # 反向對照：核心換一份，頁面跟著換（證明不是樣板裡寫死了一份剛好一樣的）
    fake = {"up": ("請　測試甲",), "peer": ("請　測試乙", "請　測試丙"), "down": (),
            "people": ("請　測試丁",), "unknown": ()}
    monkeypatch.setattr(od, "LETTER_CLOSINGS", fake)
    monkeypatch.setattr(od, "LETTER_SPEEDS", ("普通件", "測試件"))
    html2 = client.get(f"{BASE}/").text
    assert _letter_closings_on_page(html2) == {k: list(v) for k, v in fake.items()}
    assert _select_values(html2, "odSpeed") == {"普通件", "測試件"}
    # 樣板裡不可以另外寫死期望語（只有 JS 依 data-closings 畫）
    tpl = TEMPLATE.read_text(encoding="utf-8")
    # （「簽請　鑒核」是簽的結語，那是另一份清單 —— 這裡挑函才有的）
    for c in ("請　查照辦理", "請　惠允見復", "請　確實辦理"):
        assert c not in tpl, f"樣板裡寫死了期望語「{c}」—— 清單要從核心送來"


def test_salutation_preview_uses_the_core_rules(client, auth_off):
    """畫面上「草稿會稱對方」的那一行由伺服器算（核心的 `salutation` / `self_term`）。"""
    r = client.get(f"{BASE}/salutation",
                   params={"receiver": "嘉禾市政府", "relation": "up", "org": "嘉禾市資訊局"})
    assert r.status_code == 200, r.text
    assert r.json() == {"term": od.salutation("嘉禾市政府", "up"),
                        "self": od.self_term("嘉禾市資訊局")}
    assert r.json()["term"] == "鈞府" and r.json()["self"] == "本局"
    r = client.get(f"{BASE}/salutation", params={"receiver": "東湖區公所", "relation": "peer"})
    assert r.json()["term"] == "貴所"
    assert client.get(f"{BASE}/salutation", params={"relation": "sideways"}).status_code == 400
    assert client.get(f"{BASE}/salutation",
                      params={"relation": "up",
                              "receiver": "府" * (od.MAX_FIELD_CHARS + 1)}).status_code == 400


def _select_values(html: str, sel_id: str) -> set[str]:
    m = re.search(rf'<select id="{sel_id}"[^>]*>(.*?)</select>', html, re.S)
    assert m, f"找不到 #{sel_id}"
    return set(re.findall(r'<option value="([^"]+)"', m.group(1)))


def test_template_choices_match_the_core_whitelists():
    """下拉的值寫在樣板裡、白名單在核心模組 —— 兩邊對不上的話，使用者選得到一個
    按下去必然被 400 擋掉的選項（同一份清單寫兩個地方一定會漂）。"""
    html = TEMPLATE.read_text(encoding="utf-8")
    assert _select_values(html, "odClosing") == set(od.SUBJECT_CLOSINGS)
    assert _select_values(html, "odEndClosing") == set(od.ENDORSE_CLOSINGS)
    assert _select_values(html, "odLength") == set(od.LENGTHS)
    assert _select_values(html, "odLength2") == set(od.LENGTHS)
    import importlib
    rt = importlib.import_module("app.tools.official_doc.router")
    assert _select_values(html, "odFmt") == set(rt.ENDORSE_FORMATS)


# ------------------------------------------------------------------ 簽：端到端

def test_sign_runs_end_to_end_and_the_job_has_a_real_result(client, auth_off, fake_llm):
    job_id, case_id, res = _run(client)
    text = res["draft"]["text"]
    assert text.startswith("簽　　於資訊室"), text
    assert "主旨：" in text and "簽請　核示" in text
    assert "說明：" in text and "擬辦：" in text
    assert text.rstrip().endswith("局長"), "陳核對象要接在敬陳後面"
    assert res["mode"] == "sign" and res["case_id"] == case_id
    assert res["title"].startswith("汰換資訊室雷射印表機")

    from app.core.job_manager import job_manager
    j = job_manager.get(job_id)
    # **`result_path` 要是 Path**（字串的話 `/api/jobs/{id}` 會 500，作業本身卻完全正常）
    assert isinstance(j.result_path, Path)
    assert j.result_path.exists()
    assert j.result_path.read_bytes()[:2] == b"PK", "作業結果要是 ODT（zip）"
    assert j.result_filename.endswith("-草稿.odt")
    assert j.meta["case_id"] == case_id
    # 案件自己保存，「開啟」按案件定址；**不可以放 `upload_id`** —— 放了的話「我的作業」
    # 把「開啟」當成要從暫存區讀回來的那一種，暫存清掉就把鈕藏起來（案件明明還在）
    assert "upload_id" not in j.meta
    assert j.meta["view_url"] == f"{BASE}/?case={case_id}"

    r = client.get(f"/api/jobs/{job_id}")
    assert r.status_code == 200, r.text
    assert r.json()["has_result"] is True, "「我的作業」不會有下載鈕"


def test_with_date_puts_todays_roc_date_under_the_heading(client, auth_off, fake_llm):
    import datetime as dt
    _, _, res = _run(client, with_date=True)
    d = dt.date.today()
    assert ("中華民國" + od.roc_date(d.year, d.month, d.day)) in res["draft"]["text"]


def test_a_fabricated_law_is_flagged(client, auth_off, fake_llm):
    """模型自己加了一條使用者沒提的法規 —— 要標出來，不可以安靜放過。"""
    fake_llm.sign_draft = dict(SIGN_DRAFT, explanation=[
        "依政府採購法第22條辦理。", "擬以今年度設備費支應。"])
    _, _, res = _run(client)
    codes = {i["code"] for i in res["draft"]["issues"]}
    assert "law_unsupported" in codes, res["draft"]["issues"]
    assert "article_unsupported" in codes


def test_recheck_catches_an_edited_in_claim(client, auth_off, fake_llm):
    """使用者自己在草稿裡加「業經核准」—— 重新檢查要抓得到（不呼叫模型）。"""
    _, case_id, res = _run(client)
    n_calls = len(fake_llm.prompts)
    text = res["draft"]["text"].replace("擬購置", "本案業經核准，擬購置")
    r = client.post(f"{BASE}/check", json={"case_id": case_id, "text": text})
    assert r.status_code == 200, r.text
    codes = {i["code"] for i in r.json()["issues"]}
    assert "claim_unsupported" in codes
    assert len(fake_llm.prompts) == n_calls, "重新檢查不可以呼叫模型"
    # 反向對照：原文照送回去不會冒出這一條
    r2 = client.post(f"{BASE}/check", json={"case_id": case_id, "text": res["draft"]["text"]})
    assert "claim_unsupported" not in {i["code"] for i in r2.json()["issues"]}


def test_returned_facts_are_resanitised(client, auth_off, fake_llm):
    """瀏覽器送回來的資料表是不可信的輸入：把一個原文沒有的數字標成「原文有」，
    伺服器要降成「推論」—— 不然送一個假的「原文有」就能讓檢查放過任何數字。"""
    forged = [
        {"key": "amount", "label": "金額", "value": "新臺幣99萬元",
         "quote": "新臺幣99萬元", "status": "provided"},
        {"key": "quantity", "label": "品項與數量", "value": "雷射印表機2台",
         "quote": "2台雷射印表機", "status": "provided"},
    ]
    fake_llm.sign_draft = dict(SIGN_DRAFT, proposal=["擬以新臺幣99萬元購置雷射印表機2台。"])
    _, _, res = _run(client, facts=forged)
    by_key = {f["key"]: f for f in res["draft"]["facts"]}
    assert by_key["amount"]["status"] == "inferred", by_key["amount"]
    assert by_key["quantity"]["status"] == "provided", "原文真的有的那一列要留著"
    codes = {i["code"] for i in res["draft"]["issues"]}
    assert "qty_unsupported" in codes, "假的「原文有」不可以讓 99 萬元過關"
    # 帶了資料表就只剩撰寫那一步（不再叫模型整理資料）
    assert not any("整理出寫「簽」需要的資料" in p for p in fake_llm.prompts)


def test_overrides_become_confirmed_and_are_trusted(client, auth_off, fake_llm):
    """使用者在資料表上改的值，狀態是「已確認」，檢查拿它當依據。"""
    fake_llm.sign_draft = dict(SIGN_DRAFT, explanation=["經費來源為115年度設備費。"])
    _, _, res = _run(client, overrides={"budget_source": "115年度設備費"})
    by_key = {f["key"]: f for f in res["draft"]["facts"]}
    assert by_key["budget_source"]["status"] == "confirmed"
    assert by_key["budget_source"]["value"] == "115年度設備費"


# ------------------------------------------------------------------ 簽辦意見

def test_endorse_runs_end_to_end(client, auth_off, fake_llm):
    _, _, res = _run(client, mode="endorse", direction="擬照辦，填報後函復。")
    text = res["draft"]["text"]
    assert text.endswith("，陳核。"), text
    assert res["title"] == "簽辦意見"


def test_endorse_without_direction_is_400(client, auth_off, fake_llm):
    r = _start(client, mode="endorse", source=SOURCE, direction="   ")
    assert r.status_code == 400
    assert "辦理方向" in r.json()["detail"]
    assert not fake_llm.prompts, "沒有辦理方向就不該叫模型"


# ------------------------------------------------------------------ 函

def test_letter_runs_end_to_end(client, auth_off, fake_llm):
    job_id, case_id, res = _run(client, mode="letter")
    text = res["draft"]["text"]
    lines = text.split("\n")
    assert lines[2] == "嘉禾市資訊局　函", lines[:4]
    # 聯絡資訊原樣放在機關名稱下面
    assert lines[3:6] == ["地址：嘉禾市中正路1號", "聯絡人：林○○", "電話：(02)0000-0000"], lines
    assert "受文者：嘉禾市政府" in lines
    assert "速別：速件" in lines
    assert "附件：稽核報告1份" in lines
    # 使用者選的期望語（屬於上行文的那一組）
    assert "主旨：檢陳本局115年度資訊安全內部稽核結果，請　核備。" in lines, text
    assert "正本：嘉禾市政府" in lines, "正本留空時要同受文者"
    assert "副本：本局資訊安全科" in lines
    assert lines[-1] == "局長　王○○", "署名要在最後"
    # 稱謂由程式依行文關係決定，送給模型的提示要帶著它（上行＋市政府 → 鈞府）
    draft_prompts = [p for p in fake_llm.prompts if "擬一份「函」的內容" in p]
    assert draft_prompts and "「鈞府」" in draft_prompts[-1] and "「本局」" in draft_prompts[-1]
    assert not {i["code"] for i in res["draft"]["issues"]} & {"salutation", "relation_unknown"}

    assert res["mode"] == "letter"
    assert res["title"].startswith("檢陳本局115年度"), res["title"]
    assert "請" not in res["title"], "標題要去掉期望語"
    # 存下來的 inputs 要用核心 `recheck()` 讀的鍵名
    inp = res["inputs"]
    assert (inp["relation"], inp["receiver"], inp["org"]) == ("up", "嘉禾市政府", "嘉禾市資訊局")
    assert inp["closing"] == "請　核備" and inp["speed"] == "速件"

    from app.core.job_manager import job_manager
    j = job_manager.get(job_id)
    assert isinstance(j.result_path, Path) and j.result_path.exists()
    assert j.meta["case_id"] == case_id and j.meta["mode"] == "letter"
    assert j.meta["filename"] == "函"


def test_letter_default_closing_and_unknown_relation(client, auth_off, fake_llm):
    # 期望語留空 → 那個行文關係的預設（清單第一個）
    _, _, res = _run(client, mode="letter", relation="peer", receiver="嘉禾市東湖區公所",
                      closing="")
    assert res["inputs"]["closing"] == od.LETTER_CLOSINGS["peer"][0]
    assert f"，{od.LETTER_CLOSINGS['peer'][0]}。" in res["draft"]["text"]
    # 不確定 → 期望語標〔待確認〕，送了期望語也不收；檢查要提醒
    _, _, res = _run(client, mode="letter", relation="unknown", closing="")
    assert "〔待確認：期望語〕" in res["draft"]["text"]
    assert res["inputs"]["closing"] == ""
    assert "relation_unknown" in {i["code"] for i in res["draft"]["issues"]}
    assert res["title"].startswith("檢陳本局"), "〔待確認：期望語〕也要從標題拿掉"


def test_letter_relation_defaults_to_unknown_for_api_callers(client, auth_off, fake_llm):
    body = dict(LETTER_INPUT)
    body.pop("relation")
    body.pop("closing")
    r = client.post(f"{BASE}/api/official-doc", json=body)
    assert r.status_code == 200, r.text
    assert "〔待確認：期望語〕" in r.json()["text"]


@pytest.mark.parametrize("body", [
    {"relation": "sideways"},                              # 不在白名單
    {"relation": "down", "closing": "請　鑒核"},            # 上行的期望語用在下行文
    {"relation": "up", "closing": "請　查照"},              # 平行的期望語用在上行文
    {"relation": "up", "closing": "請鑒核"},                # 少了挪抬的全形空白
    {"relation": "up", "closing": ["請　鑒核"]},
    {"speed": "特急件"},
    {"narrative": "   "},
    {"narrative": "字" * (od.MAX_NARRATIVE_CHARS + 1)},
    {"receiver": "府" * (od.MAX_FIELD_CHARS + 1)},
    {"contact": "電話" * 300},
    {"signature": 123},
])
def test_letter_fields_outside_the_whitelist_are_400(client, auth_off, fake_llm, body):
    r = _start(client, mode="letter", **body)
    assert r.status_code == 400, r.text
    assert not fake_llm.prompts, "擋下來的請求不可以叫模型"


def test_letter_closing_error_names_the_allowed_ones(client, auth_off, fake_llm):
    r = _start(client, mode="letter", relation="down", closing="請　鑒核")
    detail = r.json()["detail"]
    assert "期望語" in detail and od.LETTER_CLOSINGS["down"][0] in detail, detail


def test_letter_recheck_catches_a_wrong_salutation(client, auth_off, fake_llm):
    """上行文卻把受文者稱作「貴府」—— 使用者自己改草稿時最常犯的錯，重新檢查要抓到。
    依據是**存下來的 inputs**（relation / receiver / org），所以鍵名對不上的話這條會漏。"""
    _, case_id, res = _run(client, mode="letter")
    text = res["draft"]["text"]
    assert "鈞府" in text
    n_calls = len(fake_llm.prompts)
    bad = text.replace("鈞府", "貴府")
    r = client.post(f"{BASE}/check", json={"case_id": case_id, "text": bad})
    assert r.status_code == 200, r.text
    hits = [i for i in r.json()["issues"] if i["code"] == "salutation"]
    assert hits and hits[0]["snippet"] == "貴府", r.json()["issues"]
    assert len(fake_llm.prompts) == n_calls, "重新檢查不可以呼叫模型"
    # 反向對照：原文照送回去不會有這一條
    r2 = client.post(f"{BASE}/check", json={"case_id": case_id, "text": text})
    assert "salutation" not in {i["code"] for i in r2.json()["issues"]}


def test_letter_regenerate_with_edited_facts(client, auth_off, fake_llm):
    """「依修改後的資料重新產生」：函的原文也是需求敘述（不是來文）。"""
    _, _, res = _run(client, mode="letter")
    facts = res["draft"]["facts"]
    fake_llm.prompts.clear()
    _, _, res2 = _run(client, mode="letter", facts=facts,
                      overrides={"purpose": "依年度稽核計畫辦理"})
    by_key = {f["key"]: f for f in res2["draft"]["facts"]}
    assert by_key["purpose"]["status"] == "confirmed"
    assert by_key["attachments"]["status"] == "provided", "原文真的有的那一列要留著"
    assert not any("整理出寫「函」" in p for p in fake_llm.prompts), "帶了資料表就不再整理"


# ------------------------------------------------------------------ 白名單與上限

def test_too_long_input_is_400_with_the_limit_and_never_truncated(client, auth_off, fake_llm):
    r = _start(client, narrative="字" * (od.MAX_NARRATIVE_CHARS + 1))
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert str(od.MAX_NARRATIVE_CHARS) in detail, "訊息要講出上限是多少字"
    assert not fake_llm.prompts, "超過上限要直接擋，不可以截斷之後照送"
    r = _start(client, mode="endorse", source="字" * (od.MAX_SOURCE_CHARS + 1),
               direction="擬照辦")
    assert r.status_code == 400 and str(od.MAX_SOURCE_CHARS) in r.json()["detail"]


@pytest.mark.parametrize("body", [
    {"mode": "fax"},
    {"closing": "請核示"},
    {"length": "huge"},
    {"mode": "endorse", "source": SOURCE, "direction": "擬照辦", "fmt": "table"},
    {"mode": "endorse", "source": SOURCE, "direction": "擬照辦", "closing": "核示"},
    {"unit": "單" * (od.MAX_FIELD_CHARS + 1)},
    {"narrative": ["不是", "文字"]},
    {"facts": "not-a-list"},
    {"overrides": ["x"]},
])
def test_values_outside_the_whitelist_are_400(client, auth_off, fake_llm, body):
    r = _start(client, **body)
    assert r.status_code == 400, r.text
    assert not fake_llm.prompts


def test_start_without_llm_is_503_not_500(client, auth_off, monkeypatch):
    from app.core import llm_settings as ls
    monkeypatch.setattr(ls.llm_settings, "is_enabled", lambda: False)
    r = _start(client)
    assert r.status_code == 503, r.text


def test_model_failure_does_not_leak_the_exception_text(client, auth_off, monkeypatch):
    """模型連不上時，作業失敗的訊息是固定的一句 —— 例外字串常帶著 LLM 伺服器的內部位址。"""
    from app.core import llm_settings as ls

    class Boom:
        def text_query(self, prompt, **kw):
            raise RuntimeError("connect to http://10.9.8.7:11434 refused")

    monkeypatch.setattr(ls.llm_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(ls.llm_settings, "make_client", lambda *a, **k: Boom())
    monkeypatch.setattr(ls.llm_settings, "get_model_for", lambda _t: "fake")
    r = _start(client)
    j = _wait_job(r.json()["job_id"])
    assert j.status == "error"
    assert "10.9.8.7" not in (j.error or "")
    assert "LLM" in (j.error or "")


def test_a_model_that_ignores_the_format_fails_with_its_own_message(client, auth_off, fake_llm):
    fake_llm.sign_draft = {"note": "我不照格式"}
    r = _start(client)
    j = _wait_job(r.json()["job_id"])
    assert j.status == "error"
    assert "模型" in (j.error or ""), j.error


# ------------------------------------------------------------------ 匯出

@pytest.fixture
def case_id(client, auth_off, fake_llm) -> str:
    """一件跑完的案件 —— 匯出一律綁在案件上（驗歸屬），不收沒有案件的匯出。"""
    return _run(client)[1]


def test_export_without_a_case_is_refused(client, auth_off):
    """不帶案件編號的匯出一律擋下 —— 歸屬檢查不可以「只有 JSON 才做」。"""
    for fmt in ("txt", "odt", "json"):
        r = client.post(f"{BASE}/export", json={"text": "主旨：測試。", "fmt": fmt})
        assert r.status_code == 400, (fmt, r.text)


def test_txt_export_is_exactly_the_text(client, case_id):
    text = "簽　　於資訊室\n主旨：測試，簽請　核示。\n"
    r = client.post(f"{BASE}/export", json={"text": text, "fmt": "txt", "title": "測試主旨",
                                             "case_id": case_id})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain")
    assert r.content.decode("utf-8") == text, "純文字只有草稿本身，不夾任何說明"
    cd = r.headers["content-disposition"]
    assert "filename*=UTF-8''" in cd
    from urllib.parse import quote
    assert quote("測試主旨-草稿.txt") in cd


def test_odt_docx_pdf_go_through_the_export_module(client, case_id, monkeypatch):
    from app.core import official_doc_odt as odx
    seen = []

    def fake_export(text, fmt, *, title="", draft_mark=True, extras=None):
        seen.append((fmt, title, draft_mark))
        return b"FAKE-" + fmt.encode(), "application/x-test"

    monkeypatch.setattr(odx, "export", fake_export)
    for fmt in ("odt", "docx", "pdf"):
        r = client.post(f"{BASE}/export", json={
            "text": "主旨：測試。", "fmt": fmt, "title": "a/b:c", "draft_mark": False,
            "case_id": case_id})
        assert r.status_code == 200, r.text
        assert r.content == b"FAKE-" + fmt.encode()
        cd = r.headers["content-disposition"]
        assert f"-%E8%8D%89%E7%A8%BF.{fmt}" in cd      # 「-草稿.<ext>」
        assert "a%2Fb" not in cd and "abc" in cd, "檔名不能用的字元要拿掉"
    assert seen == [("odt", "abc", False), ("docx", "abc", False), ("pdf", "abc", False)]


def test_real_odt_export_is_a_zip(client, case_id):
    r = client.post(f"{BASE}/export", json={"text": "主旨：測試。", "fmt": "odt",
                                             "case_id": case_id})
    assert r.status_code == 200, r.text
    assert r.content[:2] == b"PK"


def test_missing_office_engine_is_503(client, case_id, monkeypatch):
    from app.core import office_convert, official_doc_odt as odx

    def no_office(*a, **kw):
        raise office_convert.OfficeUnavailableError("找不到 LibreOffice")

    monkeypatch.setattr(odx, "export", no_office)
    r = client.post(f"{BASE}/export", json={"text": "主旨：測試。", "fmt": "pdf",
                                             "case_id": case_id})
    assert r.status_code == 503, r.text


@pytest.mark.parametrize("body", [
    {"text": "x", "fmt": "exe"},
    {"text": "", "fmt": "txt"},
    {"text": "字" * 30001, "fmt": "txt"},
    {"text": "x", "fmt": "json", "case_id": "../../etc"},
])
def test_bad_export_requests_are_400(client, case_id, body):
    body = {"case_id": case_id, **body}
    r = client.post(f"{BASE}/export", json=body)
    assert r.status_code == 400, r.text


def test_json_export_uses_the_current_text_and_rechecks(client, auth_off, fake_llm):
    _, case_id, res = _run(client)
    # 加在本文裡（加在「敬陳」之後的話那是結尾，本來就不檢查）
    text = res["draft"]["text"].replace("擬購置", "本案業經核准，擬購置")
    r = client.post(f"{BASE}/export", json={"text": text, "fmt": "json", "case_id": case_id,
                                             "title": res["title"]})
    assert r.status_code == 200, r.text
    d = json.loads(r.content)
    assert d["format"] == "jtdt-official-doc"
    assert d["draft"]["text"] == text
    assert "claim_unsupported" in {i["code"] for i in d["draft"]["issues"]}


# ------------------------------------------------------------------ 從檔案帶入文字

def test_extract_text_from_a_txt(client, auth_off):
    r = client.post(f"{BASE}/extract-text",
                    files={"file": ("a.txt", io.BytesIO(NARRATIVE.encode()), "text/plain")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["text"] == NARRATIVE and d["chars"] == len(NARRATIVE) and d["filename"] == "a.txt"


def test_extract_text_rejects_other_types(client, auth_off):
    r = client.post(f"{BASE}/extract-text",
                    files={"file": ("a.exe", io.BytesIO(b"MZ"), "application/octet-stream")})
    assert r.status_code == 400
    r = client.post(f"{BASE}/extract-text",
                    files={"file": ("a.txt", io.BytesIO(b""), "text/plain")})
    assert r.status_code == 400


# ------------------------------------------------------------------ 同步 API

def test_sync_api_returns_text_and_issues(client, auth_off, fake_llm):
    r = client.post(f"{BASE}/api/official-doc", json={
        "mode": "sign", "narrative": NARRATIVE, "unit": "資訊室", "closing": "鑒核"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d) == {"mode", "text", "facts", "issues", "llm_calls", "references", "kb_note",
                      "history_note", "contact_unplaced"}
    assert "簽請　鑒核" in d["text"]
    assert d["llm_calls"] == 2
    assert isinstance(d["issues"], list)


def test_sync_api_validates_like_start(client, auth_off, fake_llm):
    r = client.post(f"{BASE}/api/official-doc", json={"mode": "endorse", "source": SOURCE})
    assert r.status_code == 400


# ------------------------------------------------------------------ 歸屬

def _user_client(username: str) -> TestClient:
    from app.core import permissions, roles, sessions, user_manager
    roles.seed_builtin_roles()
    uid = user_manager.create_local(username, username, "UserPass1234")
    # 前提：一般使用者真的拿得到這支工具 —— 不然中介層先擋成 403，
    # 下面的「B 拿不到」會因為錯的原因而成立
    assert permissions.user_can_use_tool(uid, "official-doc")
    token, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
    c = TestClient(app_main.app)
    c.cookies.set(sessions.COOKIE_NAME, token)
    return c


def test_another_user_cannot_read_check_or_export_my_case(admin_session, fake_llm):
    owner = _user_client("od_owner")
    other = _user_client("od_other")
    _, case_id, res = _run(owner)
    text = res["draft"]["text"]

    assert owner.get(f"{BASE}/result/{case_id}").status_code == 200
    assert owner.post(f"{BASE}/check", json={"case_id": case_id, "text": text}).status_code == 200
    assert owner.post(f"{BASE}/export", json={"case_id": case_id, "text": text,
                                              "fmt": "json"}).status_code == 200

    r = other.get(f"{BASE}/result/{case_id}", follow_redirects=False)
    assert r.status_code in (403, 404), r.status_code
    assert "主旨" not in r.text
    r = other.post(f"{BASE}/check", json={"case_id": case_id, "text": text})
    assert r.status_code in (403, 404), r.status_code
    r = other.post(f"{BASE}/export", json={"case_id": case_id, "text": text, "fmt": "json"})
    assert r.status_code in (403, 404), r.status_code
    assert b"jtdt-official-doc" not in r.content


# ------------------------------------------------------------------ 逐段改寫

def _rt():
    import importlib
    return importlib.import_module("app.tools.official_doc.router")


PARA = "擬購置雷射印表機2台，預算新臺幣6萬元，於11月30日前完成採購。"


@pytest.fixture
def rw_case(client, auth_off, fake_llm) -> str:
    return _run(client)[1]


def _rw(c, case_id, **body):
    payload = {"case_id": case_id, "paragraph": PARA, "kind": "shorter"}
    payload.update(body)
    return c.post(f"{BASE}/rewrite", json=payload)


@pytest.mark.parametrize("kind", [k for k in od.REWRITE_KINDS if k != "custom"])
def test_rewrite_each_kind(client, rw_case, fake_llm, kind):
    n = len(fake_llm.prompts)
    r = _rw(client, rw_case, kind=kind)
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d) == {"text", "issues"}, d
    assert d["text"].strip()
    assert len(fake_llm.prompts) == n + 1, "一次改寫只呼叫一次模型"
    prompt = fake_llm.prompts[-1]
    assert PARA in prompt
    assert od.REWRITE_KINDS[kind] in prompt, "改寫方式沒有送進提示"
    # 依據跟整份草稿一樣：使用者的需求敘述、確認過的資料（不是只有那一段）
    assert "零件停產" in prompt and "新臺幣6萬元" in prompt
    if kind == "list":
        # 要點的項次由程式加（這一段沒有項次 → 「一、」「二、」）
        assert d["text"].split("\n") == ["一、擬購置雷射印表機2台", "二、預算新臺幣6萬元",
                                         "三、於11月30日前完成採購。"], d["text"]
    if kind == "shorter":
        assert "預算" not in d["text"] and "6萬元" in d["text"]
    assert all(set(i) >= {"code", "severity", "template", "args", "snippet"} for i in d["issues"])


def _tools_area(home: str) -> str:
    """首頁去掉側欄的「設定」那一組（管理頁）—— 那一組另有自己的 Beta（公文知識庫，
    `nav_settings` 的 `beta`），不算在工具裡。"""
    tools_only = re.sub(r'<details class="sb-group"[^>]*data-sbkey="設定".*?</details>', "", home,
                        flags=re.S)
    assert tools_only != home, "找不到側欄的「設定」那一組（樣板改了？）"
    return tools_only


def test_the_tool_is_no_longer_marked_beta(client, auth_off):
    """2026-10-07 起標 Beta，2026-10-10 使用者指示拿掉：側欄、首頁卡片、工具頁標題都不再標。
    （頁面上「參考公文知識庫」旁的 Beta 是知識庫的，留著。）"""
    from app.tools.official_doc import metadata
    assert metadata.beta is False
    home = client.get("/").text
    assert 'class="tool-beta"' not in _tools_area(home)
    page = client.get(f"{BASE}/").text
    h1 = re.search(r"<h1>(.*?)</h1>", page, re.S).group(1)
    assert "Beta" not in h1


def test_a_tool_marked_beta_still_shows_it_in_the_sidebar_and_on_its_card(client, auth_off, monkeypatch):
    """標示的機制本身留著（之後別的工具試用時用得到）：只有標了 `beta=True` 的工具有，
    別的工具不可以被一起標上。"""
    import app.main as main_mod
    # 側欄與首頁的工具清單是啟動時從 `ToolMetadata.beta` 算好的，這裡直接標那一列
    item = next(t for t in main_mod._nav_tool_items if t["id"] == "official-doc")
    monkeypatch.setitem(item, "beta", True)
    home = client.get("/").text
    for m in re.finditer(r'data-tool-id="([a-z0-9-]+)"(.*?)</a>', home, re.S):
        has = 'class="tool-beta"' in m.group(2)
        assert has == (m.group(1) == "official-doc"), m.group(1)
    assert _tools_area(home).count('class="tool-beta"') == 2, "側欄與首頁卡片各一個"


def test_rewrite_endpoint_keeps_the_item_number(client, rw_case, fake_llm):
    """畫面選了「一、…」送來：回來的那一段開頭仍是「一、」（使用者 2026-10-07 回報）。"""
    r = _rw(client, rw_case, paragraph="一、" + PARA)
    assert r.status_code == 200, r.text
    assert r.json()["text"].startswith("一、擬購置"), r.json()["text"]
    assert "一、" not in _rewrite_paragraph_in(fake_llm.prompts[-1])


def test_rewrite_endpoint_refuses_several_items_at_once(client, rw_case, fake_llm):
    n = len(fake_llm.prompts)
    r = _rw(client, rw_case, paragraph="一、甲。\n二、乙。")
    assert r.status_code == 400 and "一次改寫一項" in r.json()["detail"]
    assert len(fake_llm.prompts) == n


def test_rewrite_custom_needs_an_instruction_and_sends_it(client, rw_case, fake_llm):
    n = len(fake_llm.prompts)
    r = _rw(client, rw_case, kind="custom", instruction="  ")
    assert r.status_code == 400, r.text
    assert "怎麼改" in r.json()["detail"]
    assert len(fake_llm.prompts) == n, "沒寫要求就不可以叫模型"
    r = _rw(client, rw_case, kind="custom", instruction="語氣委婉一點，期限不要動")
    assert r.status_code == 200, r.text
    assert "語氣委婉一點，期限不要動" in fake_llm.prompts[-1]


@pytest.mark.parametrize("field,limit", [("paragraph", od.MAX_REWRITE_CHARS),
                                         ("instruction", od.MAX_INSTRUCTION_CHARS)])
def test_rewrite_too_long_is_400_and_never_truncated(client, rw_case, fake_llm, field, limit):
    """**不截斷**：核心對「要求」是安靜截斷的 —— 上限一定要在端點先擋，講出上限與目前字數。"""
    n = len(fake_llm.prompts)
    body = {"kind": "custom", "instruction": "請精簡", field: "字" * (limit + 1)}
    r = _rw(client, rw_case, **body)
    assert r.status_code == 400, r.text
    assert str(limit) in r.json()["detail"] and str(limit + 1) in r.json()["detail"]
    assert len(fake_llm.prompts) == n


@pytest.mark.parametrize("body", [
    {"kind": "sideways"},
    {"paragraph": ""},
    {"paragraph": ["一段"]},
    {"instruction": 123, "kind": "custom"},
])
def test_bad_rewrite_requests_are_400(client, rw_case, fake_llm, body):
    n = len(fake_llm.prompts)
    r = _rw(client, rw_case, **body)
    assert r.status_code == 400, r.text
    assert len(fake_llm.prompts) == n


def test_rewrite_that_invents_a_number_is_flagged(client, rw_case, fake_llm):
    fake_llm.rewrite_reply = {"text": "擬購置雷射印表機2台，預算新臺幣9萬元，於11月30日前完成採購。"}
    d = _rw(client, rw_case).json()
    bad = [i for i in d["issues"] if i["code"] == "qty_unsupported"]
    assert bad and any("9萬元" in i["message"] for i in bad), d["issues"]
    # 反向對照：照抄原本那一段（數字都對得上）就不會有這一條 —— 不然上面那條什麼都沒驗到
    fake_llm.rewrite_reply = {"text": PARA}
    d = _rw(client, rw_case).json()
    assert not [i for i in d["issues"] if i["code"] == "qty_unsupported"], d["issues"]


def test_rewrite_model_failure_is_502_without_the_exception_text(client, rw_case, monkeypatch):
    from app.core import llm_settings as ls

    class Boom:
        def text_query(self, prompt, **kw):
            raise RuntimeError("connect to http://10.9.8.7:11434 refused")

    monkeypatch.setattr(ls.llm_settings, "make_client", lambda *a, **k: Boom())
    r = _rw(client, rw_case)
    assert r.status_code == 502, r.text
    assert "10.9.8.7" not in r.text and "11434" not in r.text
    assert "LLM" in r.json()["detail"]


def test_rewrite_unreadable_reply_is_502_with_a_fixed_message(client, rw_case, fake_llm):
    fake_llm.rewrite_reply = {"note": "我不照格式"}
    r = _rw(client, rw_case)
    assert r.status_code == 502, r.text
    assert r.json()["detail"] == _rt()._DRAFT_FAILED


def test_rewrite_without_llm_is_503(client, rw_case, monkeypatch):
    from app.core import llm_settings as ls
    monkeypatch.setattr(ls.llm_settings, "is_enabled", lambda: False)
    r = _rw(client, rw_case)
    assert r.status_code == 503, r.text


def test_rewrite_of_an_expired_case_is_410(client, rw_case, fake_llm):
    _rt()._result_path(rw_case).unlink()
    n = len(fake_llm.prompts)
    assert _rw(client, rw_case).status_code == 410
    assert len(fake_llm.prompts) == n


def test_source_kind_is_a_pair_of_cards(client, auth_off):
    """「這是完整來文 / 這是我自己整理的大綱」做成大一點的二選一卡片（2026-10-08 使用者：「做成大塊一點的切換」）。"""
    html = client.get(f"{BASE}/").text
    m = re.search(r'<div class="option-cards od-src-cards"[^>]*>(.*?)</div>\s*</div>', html, re.S)
    assert m, "找不到來文種類的那一組卡片"
    cards = re.findall(r'<label class="option-card">(.*?)</label>', m.group(1), re.S)
    assert len(cards) == 2
    assert 'name="odSrcKind" value="full" checked' in cards[0] and 'value="outline"' in cards[1], "預設是完整來文"
    assert all('class="opt-desc"' in c and "<svg" in c for c in cards), "每張要有圖示與一句說明"
    assert 'class="od-radios"' not in html


def test_rewrite_kinds_on_the_page_come_from_the_core(client, auth_off):
    """改寫方式是一排卡片（2026-10-08 使用者：跟「版面加註」同一種樣式），清單只有核心那一份。"""
    html = client.get(f"{BASE}/").text
    m = re.search(r'<div class="option-cards od-rw-cards" id="odRwKinds"[^>]*>(.*?)</div>\s*<div class="od-rw-row">',
                  html, re.S)
    assert m, "找不到改寫方式的那一排卡片"
    assert set(re.findall(r'name="odRwKind" value="([^"]+)"', m.group(1))) == set(od.REWRITE_KINDS)
    assert m.group(1).count('class="opt-desc"') == len(od.REWRITE_KINDS), "每張卡片都要有一句說明"
    assert len(re.findall(r'name="odRwKind" value="[^"]+" checked', m.group(1))) == 1, "要預設選一種"
    assert '<select id="odRwKind"' not in html
    assert set(_rt().REWRITE_LABELS) == set(od.REWRITE_KINDS) == set(_rt().REWRITE_CARDS)


def test_segment_rules_compile_in_js_and_agree_with_parse_text():
    """前端找「游標所在那一段」用的正規式是**核心那一份**（以字串送進頁面）——
    在 JS 裡要編得起來，而且對同一行的判斷要跟 Python 一樣（全形空白、全形冒號都算）。"""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("沒有 node")
    rules = _rt()._segment_rules()
    pats = [rules["label"], *rules["items"], rules["head"], rules["meta"],
            rules["title"], rules["date"]]
    lines = ["主旨：汰換資訊室雷射印表機2台，簽請　核示。", "說明：", "擬辦:　擬照辦。",
             "一、資訊室印表機已使用多年。", "（二）擬以設備費支應。", "3. 第三項", "3.5億元的預算",
             "（12）第十二點", "簽　　於資訊室", "受文者：嘉禾市政府", "檔　　號：",
             "密等及解密條件或保密期限：", "嘉禾市資訊局　函", "中華民國115年10月7日",
             "115 年 10 月 7 日", "敬陳", "局長　王○○", "一般的一段話。"]
    script = ("const p=" + json.dumps(pats) + ",L=" + json.dumps(lines, ensure_ascii=False) +
              ";console.log(JSON.stringify(p.map(x=>{const r=new RegExp(x);"
              "return L.map(l=>{const m=r.exec(l);return m?[true,m[2]===undefined?null:m[2]]:[false,null];});})));")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    for pi, p in enumerate(pats):
        r = re.compile(p)
        for li, line in enumerate(lines):
            m = r.match(line)
            want = [bool(m), (m.group(2) if m and r.groups >= 2 else None)]
            assert got[pi][li] == want, (p, line, got[pi][li], want)
    # 結語清單：簽的結語與函的期望語都在（「只有句號」不算）
    for tail in ("，簽請　核示。", "，請　查照。", "，陳核。", "，〔待確認：期望語〕。"):
        assert tail in rules["tails"], tail
    assert "。" not in rules["tails"]


# ------------------------------------------------------------------ 版本記錄

def _revs(c, cid):
    r = c.get(f"{BASE}/revisions/{cid}")
    assert r.status_code == 200, r.text
    return r.json()


def _save(c, cid, text, base, **kw):
    body = {"case_id": cid, "text": text, "base_rev": base, "source": "edit", "note": ""}
    body.update(kw)
    return c.post(f"{BASE}/revisions", json=body)


def test_first_revision_is_what_the_model_wrote(client, auth_off, fake_llm):
    _, cid, res = _run(client)
    # 寫在作業完成時 —— 不是第一次讀清單時才補
    assert _rt()._revisions_path(cid).exists()
    d = _revs(client, cid)
    assert d["latest"] == 1 and d["max"] == _rt().MAX_REVISIONS
    (r1,) = d["revisions"]
    assert r1["rev"] == 1 and r1["parent"] is None and r1["source"] == "ai"
    assert "text" not in r1, "清單不帶全文"
    assert r1["chars"] == len(res["draft"]["text"])
    one = client.get(f"{BASE}/revisions/{cid}/1").json()
    assert one["text"] == res["draft"]["text"]
    assert client.get(f"{BASE}/revisions/{cid}/2").status_code == 404


def test_old_cases_get_their_first_revision_backfilled(client, auth_off, fake_llm):
    _, cid, res = _run(client)
    path = _rt()._revisions_path(cid)
    path.unlink()
    d = _revs(client, cid)
    assert [r["source"] for r in d["revisions"]] == ["ai"]
    assert client.get(f"{BASE}/revisions/{cid}/1").json()["text"] == res["draft"]["text"]
    assert path.exists()


def test_a_broken_revision_file_is_not_overwritten(client, auth_off, fake_llm):
    """清單壞掉時**不可以補一份新的蓋上去** —— 那等於刪掉使用者存過的版本。"""
    _, cid, res = _run(client)
    path = _rt()._revisions_path(cid)
    path.write_text("{broken", encoding="utf-8")
    assert client.get(f"{BASE}/revisions/{cid}").status_code == 500
    r = _save(client, cid, res["draft"]["text"] + "x", 1)
    assert r.status_code == 500
    assert path.read_text(encoding="utf-8") == "{broken"


def test_saving_a_new_revision(client, auth_off, fake_llm):
    _, cid, res = _run(client)
    t2 = res["draft"]["text"] + "\n（承辦人補充一句。）"
    r = _save(client, cid, t2, 1)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["rev"] == 2 and d["latest"] == 2 and d["unchanged"] is False
    assert d["revision"]["parent"] == 1 and d["revision"]["source"] == "edit"
    assert [x["rev"] for x in d["revisions"]] == [1, 2]
    assert client.get(f"{BASE}/revisions/{cid}/2").json()["text"] == t2
    # 內容跟最新版一樣 → 不另存
    d = _save(client, cid, t2, 2).json()
    assert d["unchanged"] is True and d["rev"] == 2 and d["latest"] == 2
    # 逐段改寫的版本記著改寫方式；自訂要求放在說明
    d = _save(client, cid, t2 + "改", 2, source="rewrite", kind="custom",
              note="語氣委婉一點").json()
    assert d["revision"]["source"] == "rewrite" and d["revision"]["kind"] == "custom"
    assert d["revision"]["note"] == "語氣委婉一點"


def test_a_stale_base_rev_is_409_and_nothing_is_overwritten(client, auth_off, fake_llm):
    """原規格 E02：兩個分頁同時改，後存的那一邊不可以安靜地蓋掉先存的。"""
    _, cid, res = _run(client)
    base = res["draft"]["text"]
    t_a, t_b = base + "\n（分頁 A）", base + "\n（分頁 B）"
    assert _save(client, cid, t_a, 1).json()["rev"] == 2          # 分頁 A 先存
    r = _save(client, cid, t_b, 1)                                 # 分頁 B 還以為是第 1 版
    assert r.status_code == 409, r.text
    body = r.json()
    assert "另一個分頁" in body["detail"]
    assert body["latest"]["rev"] == 2 and body["latest"]["text"] == t_a
    assert [x["rev"] for x in body["revisions"]] == [1, 2]
    d = _revs(client, cid)
    assert d["latest"] == 2, "被 409 擋下的那一份不可以存進去"
    assert client.get(f"{BASE}/revisions/{cid}/2").json()["text"] == t_a
    # 「以最新版為基礎另存」：兩份都留得住
    r = _save(client, cid, t_b, 2)
    assert r.status_code == 200 and r.json()["rev"] == 3
    assert client.get(f"{BASE}/revisions/{cid}/2").json()["text"] == t_a
    assert client.get(f"{BASE}/revisions/{cid}/3").json()["text"] == t_b


def test_restore_is_a_new_revision_and_keeps_the_later_ones(client, auth_off, fake_llm):
    _, cid, res = _run(client)
    t1 = res["draft"]["text"]
    _save(client, cid, t1 + "\n二", 1)
    _save(client, cid, t1 + "\n三", 2)
    r = _save(client, cid, t1, 3, restored_from=1)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["rev"] == 4 and d["revision"]["restored_from"] == 1
    assert d["revision"]["source"] == "edit" and d["revision"]["parent"] == 3
    assert [x["rev"] for x in _revs(client, cid)["revisions"]] == [1, 2, 3, 4]
    assert client.get(f"{BASE}/revisions/{cid}/4").json()["text"] == t1
    assert client.get(f"{BASE}/revisions/{cid}/3").json()["text"] == t1 + "\n三"
    r = _save(client, cid, t1 + "\n五", 4, restored_from=99)
    assert r.status_code == 400 and "99" in r.json()["detail"]


def test_at_most_50_revisions_and_the_first_one_stays(client, auth_off, fake_llm):
    rt = _rt()
    _, cid, res = _run(client)
    t1 = res["draft"]["text"]
    base = 1
    for i in range(rt.MAX_REVISIONS + 10):
        r = _save(client, cid, f"{t1}\n第{i}次", base)
        assert r.status_code == 200, r.text
        base = r.json()["rev"]
    d = _revs(client, cid)
    revs = [x["rev"] for x in d["revisions"]]
    assert len(revs) == rt.MAX_REVISIONS
    assert revs[0] == 1 and d["revisions"][0]["source"] == "ai", "第一版一定要留著"
    assert revs[-1] == d["latest"] == rt.MAX_REVISIONS + 11
    assert revs[1] == d["latest"] - rt.MAX_REVISIONS + 2, "刪的是最舊的那幾版"
    assert client.get(f"{BASE}/revisions/{cid}/1").json()["text"] == t1
    assert client.get(f"{BASE}/revisions/{cid}/2").status_code == 404


@pytest.mark.parametrize("patch", [
    {"source": "ai"},                       # 「模型產生」只有伺服器寫
    {"source": "magic"},
    {"base_rev": None},
    {"base_rev": "1"},
    {"base_rev": True},
    {"base_rev": 0},
    {"text": ""},
    {"text": "   \n "},
    {"text": "字" * 30_001},
    {"source": "rewrite"},                  # 逐段改寫要寫明方式
    {"source": "rewrite", "kind": "bogus"},
    {"note": "字" * (od.MAX_INSTRUCTION_CHARS + 1)},
    {"restored_from": "1"},
])
def test_bad_revision_requests_are_400(client, auth_off, fake_llm, patch):
    _, cid, res = _run(client)
    body = {"case_id": cid, "text": res["draft"]["text"] + "x", "base_rev": 1, "source": "edit"}
    body.update(patch)
    r = client.post(f"{BASE}/revisions", json=body)
    assert r.status_code == 400, (patch, r.text)
    assert _revs(client, cid)["latest"] == 1


def test_json_export_carries_the_revisions(client, auth_off, fake_llm):
    _, cid, res = _run(client)
    t2 = res["draft"]["text"] + "\n補"
    _save(client, cid, t2, 1)
    r = client.post(f"{BASE}/export", json={"case_id": cid, "text": t2, "fmt": "json"})
    assert r.status_code == 200, r.text
    exp = json.loads(r.content)
    assert [x["rev"] for x in exp["revisions"]] == [1, 2]
    assert exp["revisions"][0]["source"] == "ai" and exp["revisions"][1]["text"] == t2


def test_another_user_cannot_rewrite_or_touch_my_revisions(admin_session, fake_llm):
    owner = _user_client("od_rv_owner")
    other = _user_client("od_rv_other")
    _, cid, res = _run(owner)
    text = res["draft"]["text"]
    assert owner.get(f"{BASE}/revisions/{cid}").status_code == 200
    assert _rw(owner, cid).status_code == 200

    n = len(fake_llm.prompts)
    r = _rw(other, cid)
    assert r.status_code in (403, 404), r.status_code
    assert len(fake_llm.prompts) == n, "別人的案件不可以拿去叫模型"
    for path in (f"{BASE}/revisions/{cid}", f"{BASE}/revisions/{cid}/1"):
        r = other.get(path, follow_redirects=False)
        assert r.status_code in (403, 404), (path, r.status_code)
        assert "主旨" not in r.text
    r = _save(other, cid, text + "\n別人寫的", 1)
    assert r.status_code in (403, 404), r.status_code
    assert _revs(owner, cid)["latest"] == 1


# ------------------------------------------------------------------ 真的在瀏覽器裡跑一次
#
# 結果頁幾乎全部是 JS 產生的（資料表、檢查清單、點一條就選取草稿裡那一段、匯出鈕）。
# 本專案一整個家族的 bug 都長成「元素都在、沒有 JS 例外、按了沒反應」——
# 只有真的跑一次 JS 才看得到。LLM 指向一支假的 OpenAI 相容伺服器：
# 驗得到我們的接線，驗不到「模型會不會照做」。

def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _fake_llm_server(port: int):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    fake = FakeLLM()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            prompt = "\n".join(str(m.get("content") or "") for m in body.get("messages") or [])
            try:
                content = fake.text_query(prompt)
            except AssertionError:
                content = "{}"
            # **一定要用 SSE 回** —— `llm_client` 一律送 `stream: true`
            chunk = json.dumps({"choices": [{"delta": {"content": content}}]})
            out = (f"data: {chunk}\n\n" + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            self.send_response(404)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture(scope="module")
def live():
    import os
    import subprocess
    import sys
    import tempfile
    import urllib.request

    sys.path.insert(0, str(ROOT))
    from tools import browser_probe

    br_path = browser_probe.browser()
    if not br_path:
        pytest.skip("沒有 chromium")
    try:
        import websockets.sync.client as wsc
    except ImportError:
        pytest.skip("沒有 websockets")

    data = tempfile.mkdtemp(prefix="ode2e-")
    port, cdp, llm_port = _free_port(), _free_port(), _free_port()
    llm = _fake_llm_server(llm_port)
    # **要明寫「認證關閉」** —— 資料庫裡一有使用者，fail-secure 會自動改用本機認證
    (Path(data) / "auth_settings.json").write_text(json.dumps({"backend": "off"}),
                                                   encoding="utf-8")
    (Path(data) / "llm_settings.json").write_text(json.dumps({
        "enabled": True, "base_url": f"http://127.0.0.1:{llm_port}/v1",
        "model": "fake", "timeout_seconds": 60}), encoding="utf-8")
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [br_path, "--headless=new", "--no-sandbox", "--disable-gpu",
         browser_probe.profile_arg(), f"--remote-debugging-port={cdp}", "--remote-allow-origins=*", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws = None
    try:
        for _ in range(160):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.skip("實例或瀏覽器起不來")
        req = urllib.request.Request(f"http://127.0.0.1:{cdp}/json/new?about:blank",
                                     method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            tab = json.loads(r.read())
        ws = wsc.connect(tab["webSocketDebuggerUrl"], max_size=None, open_timeout=10)
        n = [0]
        errs: list[str] = []

        def send(method, params=None):
            n[0] += 1
            i = n[0]
            ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
            while True:
                m = json.loads(ws.recv(timeout=180))
                if m.get("method") == "Runtime.exceptionThrown":
                    d = m["params"]["exceptionDetails"]
                    errs.append((d.get("exception", {}).get("description")
                                 or d.get("text", "?"))[:300])
                if m.get("id") == i:
                    return m

        txt = Path(browser_probe.uploadable_dir()) / "ode2e-需求.txt"
        txt.write_text(NARRATIVE, encoding="utf-8")
        yield port, send, errs, str(txt)
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        br.terminate()
        srv.terminate()
        llm.shutdown()


def _eval(send, expr):
    r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                  "awaitPromise": True})
    return (r.get("result", {}).get("result", {}) or {}).get("value")


def _until(send, expr, secs=60):
    end = time.time() + secs
    while time.time() < end:
        if _eval(send, expr):
            return True
        time.sleep(0.3)
    return False


def _set_file(send, selector, path):
    # 整棵樹要抓（depth -1），而且要重試 —— 節點表可能剛好被作廢（會議摘要的 e2e 記過）。
    # 頁面要整頁載完才塞，塞完確認 input 真的拿到檔案（會議摘要 e2e 2026-10-10 查到：
    # 太早塞的話上傳元件還沒接上，input 裡 0 個檔案、一筆上傳都沒送出）。
    _until(send, "document.readyState === 'complete'", 30)
    has = ("(function(){var i=document.querySelector(%s);if(!i)return false;"
           "var r=i.closest('.file-upload'),n=r&&r.querySelector('.drop-zone-filename');"
           "return !!((i.files&&i.files.length)||(n&&n.textContent.trim()));})()" % json.dumps(selector))
    last = None
    for _ in range(8):
        doc = send("DOM.getDocument", {"depth": -1})
        if "result" not in doc:
            last = doc
            time.sleep(0.5)
            continue
        node = send("DOM.querySelector", {"nodeId": doc["result"]["root"]["nodeId"],
                                          "selector": selector})
        nid = node.get("result", {}).get("nodeId")
        if nid:
            send("DOM.setFileInputFiles", {"files": [path], "nodeId": nid})
            if _until(send, has, 3):
                return
            last = "塞了檔案但 input 裡沒有"
            continue
        last = node
        time.sleep(0.5)
    raise AssertionError(f"找不到 {selector}：{last}")


_FIND_DONE = """fetch('/api/jobs', {headers:{Accept:'application/json'}})
  .then(function(r){ return r.json(); })
  .then(function(d){
    var list = Array.isArray(d) ? d : (d.jobs || d.items || []);
    for (var i = 0; i < list.length; i++) {
      var j = list[i];
      if ((j.tool === 'official-doc' || j.tool_id === 'official-doc') && j.status === 'done')
        return j.id || j.job_id;
    }
    return null;
  })"""


DOCX_TEXT = "嘉禾市政府函請本局於10月20日前填報資訊設備盤點資料"
#: 「另一個分頁存了新版之後，這邊仍然另存一版」那一份草稿裡的記號
_FORCED_MARK = "（這一份是在另一個分頁存過之後才保存的）"


def _make_docx() -> str:
    """一份真的 Word 檔（python-docx 產生），放在瀏覽器讀得到的目錄。"""
    import sys
    sys.path.insert(0, str(ROOT))
    from tools import browser_probe
    import docx
    d = docx.Document()
    d.add_paragraph(DOCX_TEXT + "，逾期視同無資料。")
    path = Path(browser_probe.uploadable_dir()) / "ode2e-來文.docx"
    d.save(str(path))
    return str(path)


_CASE_ID_JS = """fetch('/api/jobs', {headers:{Accept:'application/json'}})
  .then(function(r){ return r.json(); })
  .then(function(d){
    var list = Array.isArray(d) ? d : (d.jobs || d.items || []);
    var j = list.find(function(x){ return (x.tool === 'official-doc' || x.tool_id === 'official-doc')
                                         && x.status === 'done'; });
    return j ? fetch('/api/jobs/' + encodeURIComponent(j.id || j.job_id)).then(function(r){ return r.json(); }) : null;
  })
  .then(function(j){ return j && j.meta ? j.meta.case_id : null; })"""


def _rewrite_and_revisions_flow(send):
    """逐段改寫（游標所在那一段 → 精簡 → 對照 → 採用 → 存成新的一版）與版本（兩個分頁同時存）。"""
    assert _until(send, "document.querySelectorAll('#odRevs li').length === 1 && "
                        "!!document.querySelector('#odRevs li .od-src-ai')", 15), \
        "版本清單沒有第一版（模型產生的）"

    def caret(expr_index):
        _eval(send, "(function(){var t=document.getElementById('odDraft'); var i=%s;"
                    "t.focus(); t.setSelectionRange(i,i); t.dispatchEvent(new Event('keyup'));"
                    "return 1;})()" % expr_index)

    # 游標在抬頭（格式欄位）→ 講出不能改寫
    caret("1")
    assert _until(send, "document.getElementById('odRwTarget').classList.contains('is-warn')", 10), \
        "游標放在抬頭上，卻沒有說這一行不能改寫"
    # 游標在主旨 → 結語（使用者選的）不算在要改寫的範圍裡
    caret("t.value.indexOf('主旨：') + 4")
    tgt = _until(send, "document.getElementById('odRwTarget').textContent.indexOf('汰換') >= 0", 10)
    assert tgt, _eval(send, "document.getElementById('odRwTarget').textContent")
    assert "簽請" not in _eval(send, "document.getElementById('odRwTarget').textContent"), \
        "主旨的結語（簽請　核示）也被算進要改寫的範圍"
    # 選取幾個字 → 改寫選取的那幾個字
    _eval(send, "(function(){var t=document.getElementById('odDraft'); var i=t.value.indexOf('常卡紙');"
                "t.focus(); t.setSelectionRange(i, i+3); t.dispatchEvent(new Event('select')); return 1;})()")
    assert _until(send, "document.getElementById('odRwTarget').textContent.indexOf('常卡紙') >= 0", 10)
    # 選取整行「一、資訊室…」→ 項次留在草稿裡，不算在要改寫的範圍（不然改完「一、」會不見）
    _eval(send, "(function(){var t=document.getElementById('odDraft'); var i=t.value.indexOf('一、資訊室');"
                "var e=t.value.indexOf('\\n', i); t.focus(); t.setSelectionRange(i, e);"
                "t.dispatchEvent(new Event('select')); return 1;})()")
    assert _until(send, "document.getElementById('odRwTarget').textContent.indexOf('資訊室') >= 0", 10)
    tgt = _eval(send, "document.getElementById('odRwTarget').textContent")
    assert "一、" not in tgt, tgt
    # 一次選了兩項 → 講出要一次改寫一項
    _eval(send, "(function(){var t=document.getElementById('odDraft'); var i=t.value.indexOf('一、資訊室');"
                "var e=t.value.indexOf('設備費支應'); t.focus(); t.setSelectionRange(i, e);"
                "t.dispatchEvent(new Event('select')); return 1;})()")
    assert _until(send, "(function(){var b=document.getElementById('odRwTarget');"
                        "return b.classList.contains('is-warn') && b.textContent.indexOf('一次改寫一項') >= 0;})()",
                  10), _eval(send, "document.getElementById('odRwTarget').textContent")

    # 游標在「擬辦：」那一行 → 精簡
    caret("t.value.indexOf('擬辦：') + 6")
    assert _until(send, "(function(){var x=document.getElementById('odRwTarget').textContent;"
                        "return x.indexOf('本案業經核准') >= 0 && x.indexOf('擬辦：') < 0;})()", 10), \
        "游標所在那一段沒有認對（要是「擬辦：」後面的內文）"
    before = _eval(send, "document.getElementById('odDraft').value")
    # 選了改寫方式之後「改寫」按鈕要變成主要按鈕、閃一下、寫出選的是哪一種，旁邊講出還要按它
    # （2026-10-09 使用者：讓使用者一看就知道還要按下「改寫」才會動作）
    go = ("(function(){var b=document.getElementById('odRwGo'), h=document.getElementById('odRwReady');"
          "var cs=getComputedStyle(b);"
          "return {primary:b.classList.contains('btn-primary'), ready:b.classList.contains('is-ready'),"
          "label:document.getElementById('odRwGoLabel').textContent, hint:!h.hidden && h.offsetWidth>0,"
          "anim:cs.animationName, bg:cs.backgroundColor};})()")
    g0 = _eval(send, go)
    assert not g0["primary"] and not g0["hint"], f"還沒選之前按鈕維持次要樣式：{g0}"
    assert g0["label"] == "改寫：精簡", g0
    _eval(send, "document.querySelector('input[name=odRwKind][value=formal]').click(), 1")
    g1 = _eval(send, go)
    assert g1["primary"] and g1["ready"] and g1["hint"], f"選了方式之後按鈕要變醒目、旁邊要提示：{g1}"
    assert g1["anim"] == "odRwReady", g1
    assert g1["label"] == "改寫：更正式", g1
    assert g1["bg"] != g0["bg"], f"按鈕顏色要真的換掉：{g0['bg']} → {g1['bg']}"
    _eval(send, "document.querySelector('input[name=odRwKind][value=shorter]').click(), 1")
    assert _eval(send, go)["label"] == "改寫：精簡"
    _eval(send, "document.getElementById('odRwGo').click(), 1")
    # 改寫中按鈕換成轉圈（標籤暫時不在畫面上），所以這裡只看提示與閃動收起來了沒
    g2 = _eval(send, "(function(){var b=document.getElementById('odRwGo'), h=document.getElementById('odRwReady');"
                     "return {ready:b.classList.contains('is-ready'), hint:!h.hidden};})()")
    assert not g2["hint"] and not g2["ready"], f"按下去之後提示要收起來：{g2}"
    assert _until(send, "!document.getElementById('odRwOut').hidden && "
                        "document.querySelectorAll('#odRwBefore del').length > 0", 30), \
        "改寫之後沒有顯示前後對照（刪掉的字要劃線）"
    assert "預算" in _eval(send, "Array.from(document.querySelectorAll('#odRwBefore del'))"
                                 ".map(function(x){return x.textContent;}).join('')")
    assert _eval(send, "document.getElementById('odDraft').value") == before, \
        "還沒按「採用」就改了草稿"
    _eval(send, "document.getElementById('odRwAccept').click(), 1")
    assert _until(send, "document.getElementById('odDraft').value.indexOf("
                        "'擬辦：本案業經核准擬購置雷射印表機2台新臺幣6萬元於11月30日前完成採購。') >= 0", 10), \
        _eval(send, "document.getElementById('odDraft').value")
    after = _eval(send, "document.getElementById('odDraft').value")
    assert after.split("\n")[:2] == before.split("\n")[:2], "採用時動到了別的段落"
    assert not _eval(send, "document.getElementById('odDirty').hidden"), "採用之後沒有標成「改過了」"
    assert _until(send, "document.querySelectorAll('#odRevs li').length === 2 && "
                        "!!document.querySelector('#odRevs li .od-src-rewrite')", 15), \
        "採用之後沒有存成新的一版"
    cid = _eval(send, _CASE_ID_JS)
    assert cid, "撈不到案件編號"
    revs = _eval(send, "fetch('/tools/official-doc/revisions/%s').then(function(r){return r.json();})" % cid)
    assert revs["latest"] == 2 and revs["revisions"][-1]["source"] == "rewrite" \
        and revs["revisions"][-1]["kind"] == "shorter", revs

    # ---- 另一個分頁先存了一版 → 這邊保存被擋下（不會蓋掉）→ 以最新版為基礎另存 ----
    st = _eval(send, "fetch('/tools/official-doc/revisions', {method:'POST',"
                     "headers:{'Content-Type':'application/json'},"
                     "body: JSON.stringify({case_id:%s, text:'另一個分頁的版本', base_rev:2,"
                     "source:'edit'})}).then(function(r){return r.status;})" % _js(cid))
    assert st == 200, st
    _eval(send, "(function(){var t=document.getElementById('odDraft'); t.value += %s;"
                "t.dispatchEvent(new Event('input')); return 1;})()" % _js("\n" + _FORCED_MARK))
    _eval(send, "document.getElementById('odRevSave').click(), 1")
    assert _until(send, "!document.getElementById('odRevConflict').hidden", 15), \
        "另一個分頁已經存過新版，這邊保存卻沒有被擋下"
    assert "3" in _eval(send, "document.getElementById('odRevConflictMsg').textContent")
    _eval(send, "document.getElementById('odRevForce').click(), 1")
    assert _until(send, "document.getElementById('odRevConflict').hidden && "
                        "document.querySelectorAll('#odRevs li').length === 4", 15), \
        "「仍然保存為新版本」沒有另存"
    revs = _eval(send, "fetch('/tools/official-doc/revisions/%s').then(function(r){return r.json();})" % cid)
    assert [r["rev"] for r in revs["revisions"]] == [1, 2, 3, 4], revs
    third = _eval(send, "fetch('/tools/official-doc/revisions/%s/3').then(function(r){return r.json();})" % cid)
    assert third["text"] == "另一個分頁的版本", "另一個分頁存的那一版被蓋掉了"

    # ---- 看某一版 → 還原（還原也是新的一版，之後重新打開時再驗）----
    _eval(send, "document.querySelector('#odRevs [data-od-rev-view=\"1\"]').click(), 1")
    assert _until(send, "!document.getElementById('odRevView').hidden && "
                        "document.getElementById('odRevViewText').textContent.indexOf('預算') >= 0", 15), \
        "「看這一版」沒有顯示第一版的全文"
    _eval(send, "document.getElementById('odRevViewClose').click(), 1")


def test_form_controls_in_a_row_line_up(live):
    """同一列的輸入框與下拉要**一樣高、頂端對齊**（2026-10-07 使用者：「這邊的框要整齊」）。
    三種模式各量一次；判準是瀏覽器量出來的位置，不是 CSS 寫了什麼。"""
    port, send, errs, _txt = live
    send("Emulation.setDeviceMetricsOverride",
         {"width": 1366, "height": 900, "deviceScaleFactor": 1, "mobile": False})
    try:
        send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/official-doc/"})
        assert _until(send, "document.readyState === 'complete' && "
                            "!!document.getElementById('odGo')", 30)
        measure = """(function(){
          var out = [];
          document.querySelectorAll('.od-grid').forEach(function(g){
            if (!g.offsetParent) return;
            var rows = {};
            Array.from(g.children).forEach(function(cell){
              // 下拉是本站樣式（jt-select）：原生的藏起來了，量**畫面上那一顆**（id 取原生的）
              var c = cell.querySelector(':scope > input.field, :scope > select.field, :scope > textarea.field, '
                                         + ':scope > .jt-select-wrap > .jt-select-trigger');
              if (!c) return;
              var top = Math.round(cell.getBoundingClientRect().top);
              var r = c.getBoundingClientRect();
              var nat = c.classList.contains('jt-select-trigger') ? c.parentElement.querySelector('select') : c;
              (rows[top] = rows[top] || []).push({id: nat.id, tag: nat.tagName, top: r.top, h: r.height});
            });
            Object.keys(rows).forEach(function(k){ out.push(rows[k]); });
          });
          return out;
        })()"""
        for mode in ("sign", "endorse", "letter"):
            _eval(send, "document.querySelector('input[name=odMode][value=%s]').click(), 1" % mode)
            rows = _eval(send, measure)
            multi = [r for r in rows if len(r) >= 2]
            assert multi, (mode, rows)
            for row in multi:
                tops = [c["top"] for c in row]
                hs = [c["h"] for c in row]
                assert max(tops) - min(tops) <= 1, (mode, "頂端沒對齊", row)
                assert max(hs) - min(hs) <= 1, (mode, "高度不一樣", row)
                assert not [c for c in row if c["tag"] == "TEXTAREA"], \
                    (mode, "多行的欄位不可以跟單行欄位擠在同一列", row)
            if mode == "sign":
                ids = {c["id"] for r in multi for c in r}
                assert {"odUnit", "odAddressee", "odClosing", "odLength"} <= ids, \
                    ("簽的那一列沒有量到", rows)
    finally:
        send("Emulation.clearDeviceMetricsOverride")


def test_the_whole_flow_works_in_a_real_browser(live):
    port, send, errs, txt = live
    send("Page.enable")
    send("Runtime.enable")
    send("DOM.enable")
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/official-doc/"})
    assert _until(send, "document.readyState === 'complete' && "
                        "!!document.getElementById('odGo')", 30), "頁面沒開起來"

    # ---- 切換模式：兩組欄位各自出現 ----
    _eval(send, "document.querySelector('input[name=odMode][value=endorse]').click(), 1")
    assert _eval(send, "!document.getElementById('odEndorse').hidden && "
                       "document.getElementById('odSign').hidden")
    # 上傳區是共用元件：拖曳區與「從工作區載入」都在（工作區預設開著）
    assert _eval(send, "!!document.querySelector('#odSourceUp .drop-zone') && "
                       "!!document.querySelector('#odSourceUp .ws-load-btn') && "
                       "!document.querySelector('#odSourceUp .ws-load-btn').hidden"), \
        "來文內容的上傳區不是共用元件（或少了「從工作區載入」）"
    # ---- 來文內容：拖一份 Word 檔進來（抽文字走 Office 引擎）----
    from app.core import office_convert
    if office_convert.find_soffice():
        _set_file(send, "#odSourceUp-input", _make_docx())
        assert _until(send, "document.getElementById('odSource').value.indexOf(%s) >= 0"
                            % _js(DOCX_TEXT), 90), \
            "拖進 .docx 之後，來文內容沒有被填進去"
    _eval(send, "document.querySelector('input[name=odMode][value=sign]').click(), 1")
    assert _eval(send, "!document.getElementById('odSign').hidden")

    # ---- 從檔案帶入文字 ----
    _set_file(send, "#odNarrativeUp-input", txt)
    assert _until(send, "document.getElementById('odNarrative').value.length > 20", 30), \
        "選了檔案，需求敘述框卻沒有被填進去 —— 「從檔案帶入文字」沒接上"
    assert "/" in (_eval(send, "document.getElementById('odNarrativeCount').textContent") or "")

    # ---- 框裡已經有字：再帶一份進來要**先問**（取代 / 接在後面），不可以安靜蓋掉 ----
    other = Path(txt).with_name("ode2e-補充.txt")
    other.write_text("補充：另請總務科協助驗收。", encoding="utf-8")
    _eval(send, "(function(){var t=document.getElementById('odNarrative'); t.value='我先打的字。';"
                "t.dispatchEvent(new Event('input')); return 1;})()")
    _set_file(send, "#odNarrativeUp-input", str(other))
    assert _until(send, "!!document.querySelector('.modal-overlay .modal-cancel')", 20), \
        "框裡已經有字，帶入檔案之前沒有先問"
    assert _eval(send, "document.getElementById('odNarrative').value") == "我先打的字。", \
        "還沒回答就把框裡的字換掉了"
    _eval(send, "document.querySelector('.modal-overlay .modal-cancel').click(), 1")   # 接在後面
    assert _until(send, "(function(){var v=document.getElementById('odNarrative').value;"
                        "return v.indexOf('我先打的字。')===0 && v.indexOf('總務科協助驗收')>0;})()", 20), \
        "選「接在後面」，原本的字卻不見了"
    assert _until(send, "!document.querySelector('.modal-overlay')", 10)
    _set_file(send, "#odNarrativeUp-input", txt)
    assert _until(send, "!!document.querySelector('.modal-overlay .modal-ok')", 20)
    _eval(send, "document.querySelector('.modal-overlay .modal-ok').click(), 1")       # 取代
    assert _until(send, "document.getElementById('odNarrative').value === %s" % _js(NARRATIVE), 20), \
        "選「取代」，框裡的字沒有換成檔案的內容"
    assert _until(send, "!document.querySelector('.modal-overlay')", 10)

    # ---- 產生草稿 ----
    _eval(send, "document.getElementById('odUnit').value = '資訊室', 1")
    _eval(send, "document.getElementById('odGo').click(), 1")
    assert _until(send, "!document.getElementById('odResult').hidden && "
                        "document.getElementById('odDraft').value.indexOf('主旨：') >= 0", 90), \
        "作業跑完了，草稿卻沒有出現在畫面上"
    assert _eval(send, "document.querySelectorAll('#odFacts .od-fr').length") >= 5
    assert _eval(send, "document.getElementById('odIssueSum').textContent.length") > 0
    # 「推論」「原文有」的徽章要畫出來，而且原文有的那一列滑上去看得到原文
    assert _eval(send, "!!document.querySelector('#odFacts .od-b-provided[title]')")

    # ---- 改草稿 → 重新檢查 → 點一條就選取那一段 ----
    _eval(send, """(function(){var t=document.getElementById('odDraft');
      t.value = t.value.replace('擬購置', '本案業經核准，擬購置');
      t.dispatchEvent(new Event('input')); return 1;})()""")
    assert _eval(send, "!document.getElementById('odDirty').hidden"), "改了草稿卻沒提醒要重新檢查"
    _eval(send, "document.getElementById('odRecheck').click(), 1")
    assert _until(send, "Array.from(document.querySelectorAll('#odIssues li'))"
                        ".some(function(li){return li.textContent.indexOf('業經核准') >= 0;})", 20), \
        "重新檢查沒有抓到使用者自己加的「業經核准」"
    _eval(send, """(function(){var b = Array.from(document.querySelectorAll('#odIssues .od-iss-btn'))
      .find(function(x){return x.textContent.indexOf('業經核准') >= 0;}); b.click(); return 1;})()""")
    sel = _eval(send, """(function(){var t=document.getElementById('odDraft');
      return t.value.slice(t.selectionStart, t.selectionEnd);})()""")
    assert sel and "業經" in sel, f"點了檢查結果卻沒有選取草稿裡那一段：{sel!r}"

    _rewrite_and_revisions_flow(send)

    # ---- 匯出：按鈕真的拿到檔案 ----
    _eval(send, """(function(){window.__dl = [];
      var o = URL.createObjectURL.bind(URL);
      URL.createObjectURL = function(b){ window.__dl.push(b.size); return o(b); };
      return 1;})()""")
    _eval(send, "document.querySelector('[data-od-dl=txt]').click(), 1")
    assert _until(send, "window.__dl.length >= 1 && window.__dl[0] > 20", 20), "純文字下載沒有拿到檔案"
    _eval(send, "document.querySelector('[data-od-dl=odt]').click(), 1")
    assert _until(send, "window.__dl.length >= 2", 20), "ODT 下載沒有拿到檔案"

    # ---- 從「我的作業」開啟：結果與當初填的內容都要接得回來 ----
    jid = _eval(send, _FIND_DONE)
    assert jid, "撈不到已完成的公文撰擬作業"
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/official-doc/?job={jid}"})
    assert _until(send, "!!document.getElementById('odResult') && "
                        "!document.getElementById('odResult').hidden && "
                        "document.getElementById('odDraft').value.indexOf('主旨：') >= 0", 30), \
        "從「我的作業」開啟之後畫面是空的"
    assert _eval(send, "document.getElementById('odNarrative').value.length > 20"), \
        "當初填的需求沒有放回表單 —— 「依修改後的資料重新產生」會送一份空的表單"
    # 存過的版本：重新打開時草稿是**最新那一版**，不是模型原本那一份
    assert _until(send, "document.getElementById('odDraft').value.indexOf(%s) >= 0 && "
                        "document.querySelectorAll('#odRevs li').length === 4"
                        % _js(_FORCED_MARK), 20), \
        "從「我的作業」開啟，草稿沒有顯示最後存的那一版"

    # ---- 從「歷史案件」打開（`?case=`）：清單上有這一件，點下去是同一份、最新那一版 ----
    cid = _eval(send, "fetch('/api/jobs/' + %s).then(function(r){ return r.json(); })"
                      ".then(function(j){ return j.meta.case_id; })" % _js(jid))
    assert cid, "作業沒有記案件編號"
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/official-doc/cases"})
    assert _until(send, "!!document.querySelector('tr[data-case-id=%s] a[data-odc-title]')"
                        % _js(cid), 20), "歷史案件清單上沒有這一件"
    assert _eval(send, "!!document.querySelector('.odc-search .jt-select-wrap')"), \
        "歷史案件的「文別」下拉不是本站樣式（頁面沒載 custom_select.js）"
    href = _eval(send, "document.querySelector('tr[data-case-id=%s] a[data-odc-title]').href"
                       % _js(cid))
    assert href.endswith(f"/tools/official-doc/?case={cid}"), href
    send("Page.navigate", {"url": href})
    assert _until(send, "!document.getElementById('odResult').hidden && "
                        "document.getElementById('odDraft').value.indexOf(%s) >= 0 && "
                        "document.querySelectorAll('#odRevs li').length === 4 && "
                        "document.getElementById('odNarrative').value.length > 20"
                        % _js(_FORCED_MARK), 30), \
        "從歷史案件打開之後草稿、版本或當初填的需求沒有接回來"

    _letter_flow(port, send)

    assert not errs, "主控台有 JS 例外：\n  " + "\n  ".join(errs)


def _js(v) -> str:
    """Python 值 → JS 字面值（中文、全形空白、換行都安全）。"""
    return json.dumps(v)


def _letter_flow(port, send):
    """函：行文關係決定期望語與稱謂、產生草稿、記住發文機關、從「我的作業」放回表單。"""
    page = f"http://127.0.0.1:{port}/tools/official-doc/"
    send("Page.navigate", {"url": page})
    assert _until(send, "document.readyState === 'complete' && "
                        "!!document.getElementById('odGo')", 30)
    _eval(send, "document.querySelector('input[name=odMode][value=letter]').click(), 1")
    assert _eval(send, "!document.getElementById('odLetter').hidden && "
                       "document.getElementById('odSign').hidden && "
                       "document.getElementById('odEndorse').hidden"), "切到函，欄位沒有換過來"
    assert _eval(send, "document.getElementById('odLetterClosing').disabled"), \
        "還沒選行文關係，期望語不應該能選"

    def pick(rel):
        _eval(send, "(function(){var s=document.getElementById('odRelation'); s.value=%s;"
                    "s.dispatchEvent(new Event('change')); return 1;})()" % _js(rel))

    def closings():
        return _eval(send, "Array.from(document.getElementById('odLetterClosing').options)"
                           ".map(function(o){return o.value;})")

    # ---- 期望語跟著行文關係變（清單是核心那一份） ----
    pick("up")
    assert closings() == list(od.LETTER_CLOSINGS["up"]), closings()
    assert not _eval(send, "document.getElementById('odLetterClosing').disabled")
    pick("down")
    assert closings() == list(od.LETTER_CLOSINGS["down"]), "換了行文關係，期望語下拉沒跟著換"
    pick("unknown")
    assert _eval(send, "document.getElementById('odLetterClosing').disabled && "
                       "!document.getElementById('odRelationHint').hidden"), \
        "選「不確定」要提示草稿會標待確認"
    pick("up")
    assert _eval(send, "document.getElementById('odRelationHint').hidden")

    # ---- 填欄位；稱謂即時顯示（伺服器算的） ----
    for el_id, v in (("odLetterNarrative", LETTER_NARRATIVE), ("odOrg", "嘉禾市資訊局"),
                     ("odReceiver", "嘉禾市政府"), ("odSignature", "局長　王○○"),
                     ("odCc", "本局資訊安全科"), ("odCtAddress", "嘉禾市中正路1號")):
        _eval(send, "(function(){var e=document.getElementById(%s); e.value=%s;"
                    "e.dispatchEvent(new Event('input')); e.dispatchEvent(new Event('change'));"
                    "return 1;})()" % (_js(el_id), _js(v)))
    assert _until(send, "!document.getElementById('odSalute').hidden && "
                        "document.getElementById('odSaluteTerm').textContent === '鈞府' && "
                        "document.getElementById('odSaluteSelf').textContent === '本局'", 15), \
        "畫面沒有顯示草稿會用的稱謂（上行＋市政府要是鈞府、自稱本局）"
    _eval(send, "document.getElementById('odLetterClosing').value = %s, 1" % _js("請　核備"))

    # ---- 產生草稿：第三行是「〇〇　函」 ----
    _eval(send, "document.getElementById('odGo').click(), 1")
    assert _until(send, "!document.getElementById('odResult').hidden && "
                        "document.getElementById('odDraft').value.split('\\n')[2] === %s"
                        % _js("嘉禾市資訊局　函"), 90), \
        "函的草稿沒有出現（或第三行不是「機關全銜　函」）"
    text = _eval(send, "document.getElementById('odDraft').value")
    assert "主旨：檢陳本局115年度資訊安全內部稽核結果，請　核備。" in text, text
    assert "受文者：嘉禾市政府" in text and text.rstrip().endswith("局長　王○○")

    # ---- 發文機關、署名、副本、聯絡資訊：重新開頁要自動帶入 ----
    send("Page.navigate", {"url": page})
    assert _until(send, "document.readyState === 'complete' && "
                        "!!document.getElementById('odOrg')", 30)
    remembered = _eval(send, "[document.getElementById('odOrg').value,"
                             "document.getElementById('odSignature').value,"
                             "document.getElementById('odCc').value,"
                             "document.getElementById('odCtAddress').value]")
    assert remembered == ["嘉禾市資訊局", "局長　王○○", "本局資訊安全科",
                          "嘉禾市中正路1號"], remembered
    assert _eval(send, "document.getElementById('odReceiver').value") == "", \
        "受文者每一份都不同，不可以記住"

    # ---- 從「我的作業」開啟函：欄位放回、期望語保留 ----
    # 清單不帶 meta；作業名稱是模式名稱（`meta.filename`＝「函」）
    jid = _eval(send, _FIND_DONE.replace("j.status === 'done'",
                                         "j.status === 'done' && j.filename === %s" % _js("函")))
    assert jid, "撈不到已完成的函"
    send("Page.navigate", {"url": f"{page}?job={jid}"})
    assert _until(send, "!document.getElementById('odResult').hidden && "
                        "document.getElementById('odDraft').value.indexOf('　函') >= 0", 30), \
        "從「我的作業」開啟函之後畫面是空的"
    got = _eval(send, "[document.querySelector('input[name=odMode]:checked').value,"
                      "document.getElementById('odRelation').value,"
                      "document.getElementById('odLetterClosing').value,"
                      "document.getElementById('odReceiver').value,"
                      "document.getElementById('odLetter').hidden]")
    assert got == ["letter", "up", "請　核備", "嘉禾市政府", False], got


def test_every_model_call_caps_its_output(monkeypatch, client, auth_off):
    """停不下來的模型會讓整件作業卡到逾時（評估 TAIDE 時一件生成了十分鐘）——
    兩條路（背景作業、同步 API）每一次呼叫都要帶輸出上限。"""
    import importlib
    from app.core import official_doc as od
    r = importlib.import_module("app.tools.official_doc.router")
    seen = []

    class _C:
        def text_query(self, prompt, model, think=False, max_tokens=None, **kw):
            seen.append(max_tokens)
            return "{}"

    monkeypatch.setattr(r.llm_settings, "make_client", lambda *a, **k: _C())
    r._plain_ask()("x")

    class _J:
        cancelled = False
    r._ask_for(_J())("x")
    assert seen == [od.MAX_OUTPUT_TOKENS, od.MAX_OUTPUT_TOKENS]
