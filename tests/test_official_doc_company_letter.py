"""公文撰擬：企業發給政府機關的函（使用者 2026-10-08「公文撰擬內也要加入企業發函的應用」，
範例集 v1.1 第 9～12 點）。

企業的函仍是「函」模式；差別由**發文身分**決定，不讓模型猜：
* 自稱「本公司」、稱對方「貴○ / 貴機關」—— 沒有上下級，不用「鈞」，不套簽的架構；
* 期望語是企業那一組（沒有「鑒核」「核示」「照辦」）；
* 抬頭是公司名稱，沒有機關的檔號、保存年限、密等；地址、統一編號、聯絡人與署名用印沒填就標〔待補〕；
* 請機關做的事寫成請求：「已核准」「驗收合格」「免罰」「不可抗力」這類主張，原文沒有就標出來 ——
  **「不要寫成已經核定」這種寫作指示不算原文有**（改之前它會讓草稿的「已核定」過關）。
"""
from __future__ import annotations

import importlib
import json

import pytest

from app.core import official_doc as od

BASE = "/tools/official-doc"
CO = od.COMPANY_RELATION


def _rt():
    return importlib.import_module("app.tools.official_doc.router")


# ------------------------------------------------------------------ 稱謂、自稱、期望語

@pytest.mark.parametrize("receiver,want", [
    ("嘉禾市資訊局", "貴局"), ("嘉禾市政府", "貴府"), ("經濟部產業發展署", "貴署"),
    ("", "貴機關"), ("某某機關", "貴機關"),
    # 受文者打成公司（不是機關）：不取「公司」當稱謂
    ("○○資訊股份有限公司", "貴機關"),
])
def test_company_salutation_is_gui_never_jun(receiver, want):
    assert od.salutation(receiver, CO) == want


@pytest.mark.parametrize("org,want", [
    ("", "本公司"), ("○○資訊股份有限公司", "本公司"), ("○○會計師事務所", "本所"),
    ("○○工作室", "本公司"),
])
def test_company_self_term(org, want):
    assert od.self_term(org, CO) == want
    # 機關的自稱照舊
    assert od.self_term("嘉禾市資訊局") == "本局"


def test_company_closings_have_no_superior_or_subordinate_wording():
    allowed = od.LETTER_CLOSINGS[CO]
    assert allowed[0] == "請　查照"
    for c in allowed:
        assert not any(w in c for w in ("鑒核", "核示", "鑒察", "照辦", "轉知")), c


# ------------------------------------------------------------------ 組出來的函

def test_company_letter_header_and_placeholders():
    text = od.assemble_letter({"subject": "檢送工作計畫書1份", "explanation": ["說明一"]},
                              relation=CO)
    lines = text.split("\n")
    assert lines[0] == "〔待補：公司名稱〕　函"
    # 機關的檔案管理欄位不出現
    assert not any(l.startswith(("檔", "保存年限", "密等")) for l in lines), text
    # 對方回覆、付款要用的資料沒填就標待補
    for want in ("地址：〔待補：公司地址〕", "統一編號：〔待補：統一編號〕", "聯絡人：〔待補：聯絡人及電話〕",
                 "〔待補：公司及負責人署名與用印〕"):
        assert want in lines, (want, text)
    assert "主旨：檢送工作計畫書1份，請　查照。" in lines


def test_company_letter_uses_the_given_company_data():
    text = od.assemble_letter({"subject": "申請驗收"}, relation=CO, org="○○資訊股份有限公司",
                              contact="地址：○○市○○路1號\n統一編號：12345678",
                              signature="○○資訊股份有限公司　負責人　王○○",
                              closing="請　惠予審查", receiver="嘉禾市資訊局")
    assert text.split("\n")[0] == "○○資訊股份有限公司　函"
    assert "〔待補：公司地址〕" not in text and "〔待補：公司及負責人" not in text
    assert "統一編號：12345678" in text
    assert "主旨：申請驗收，請　惠予審查。" in text
    # 統一編號是抬頭欄位，讀得回來（匯出與檢查都靠它）
    metas = {b["key"] for b in od.parse_text(text) if b["kind"] == "meta"}
    assert {"地址", "統一編號"} <= metas


def test_an_agency_closing_is_not_accepted_for_a_company_letter():
    text = od.assemble_letter({"subject": "x"}, relation=CO, closing="請　鑒核")
    assert "請　鑒核" not in text and "，請　查照。" in text


def test_agency_letter_header_is_unchanged():
    text = od.assemble_letter({"subject": "x"}, relation="down")
    lines = text.split("\n")
    assert lines[:3] == ["檔　　號：", "保存年限：", "〔待補：機關全銜〕　函"]
    assert "密等及解密條件或保密期限：" in lines
    assert "統一編號" not in text and "〔待補：公司" not in text


@pytest.mark.parametrize("raw,want", [
    ("請貴機關惠予審查", "請　貴機關惠予審查"),
    ("請求貴機關安排驗收", "請　貴機關安排驗收"),
    ("請求　貴局協助", "請　貴局協助"),
])
def test_nuotai_for_gui_jiguan_and_qingqiu(raw, want):
    assert od._nuotai(raw) == want


# ------------------------------------------------------------------ 檢查

def _codes(issues):
    return [(i.code, i.severity) for i in issues]


def test_company_letter_checks():
    body = "主旨：檢送資料，請　查照。\n說明：\n一、依　鈞局來函辦理。\n二、擬辦：本局將派員。"
    got = od.letter_issues(body, relation=CO, receiver="", org="")
    codes = _codes(got)
    assert ("salutation", "error") in codes                       # 鈞局
    assert sum(1 for c in codes if c == ("company_wording", "warning")) == 2   # 擬辦、本局
    msgs = [i.message for i in got]
    assert od.MESSAGES["missing_company"] in msgs and od.MESSAGES["missing_org"] not in msgs
    # 只有機關的函才問行文關係
    assert "relation_unknown" not in [c for c, _ in codes]


def test_a_person_name_with_jun_is_not_a_salutation():
    got = od.letter_issues("說明：新窗口由林育鈞接任。", relation=CO, receiver="嘉禾市資訊局",
                           org="○○公司")
    assert got == []


def test_ni_banli_is_not_the_ni_ban_section():
    """「擬辦理人員異動」是「打算辦理」，不是簽的「擬辦」段（企業發函範例實測誤報）。"""
    got = od.letter_issues("說明：本公司擬辦理人員異動，並依法辦理。", relation=CO,
                           receiver="嘉禾市資訊局", org="○○公司")
    assert got == []
    got = od.letter_issues("擬辦：本公司將派員。", relation=CO, receiver="嘉禾市資訊局", org="○○公司")
    assert [i.code for i in got] == ["company_wording"]


def test_recheck_uses_the_company_rules():
    inputs = {"narrative": "我們公司要申請驗收", "relation": CO, "receiver": "", "org": ""}
    issues = od.recheck("letter", "主旨：申請驗收，請　鑒核。\n說明：敬陳　鈞長。", [], inputs)
    assert any(i.message == od.MESSAGES["company_no_jun"].format("鈞長") or
               i.args == ("鈞長",) for i in issues)


# ------------------------------------------------------------------ 狀態主張

def test_a_write_instruction_is_not_evidence():
    """「不要寫成計畫已經核定」是寫作指示 —— 改之前它讓草稿的「已核定」過關（機關的簽也一樣）。"""
    src = "幫我寫一份函，請對方審查，不要寫成計畫已經核定。"
    for mode in ("letter", "sign"):
        got = od.check_draft("說明：本計畫已核定。", [src], mode=mode)
        assert ("claim_unsupported", "error") in _codes(got), mode
    # 反向對照：真的寫在原文的事實照樣算
    got = od.check_draft("說明：本計畫已核定。", ["本計畫已經核定，要通知廠商。"], mode="letter")
    assert ("claim_unsupported", "error") not in _codes(got)


def test_strip_prohibitions_keeps_facts():
    s = "不能有空窗期，不得超過3天，報名費不用錢，不要自行引用法條，也不要把初步原因寫成最終調查結果。"
    out = od.strip_prohibitions(s)
    for keep in ("不能有空窗期", "不得超過3天", "不用錢"):
        assert keep in out
    assert "引用法條" not in out and "最終調查結果" not in out


@pytest.mark.parametrize("draft,src,flag", [
    # 原文只有「不要寫成已經驗收合格」→ 草稿寫驗收合格要標
    ("說明：本案業經　貴機關驗收合格。", "這是申請驗收，不要寫成機關已經驗收合格。", True),
    # 原文真的收到驗收合格通知 → 草稿怎麼寫都是對的（只比核心那幾個字）
    ("說明：本案業經　貴機關驗收合格。", "已經收到機關的驗收合格通知，要請款。", False),
    # 條件與計畫不是主張
    ("說明：如經　貴機關同意展延，本公司將分批交貨；俟驗收合格後請款。", "申請展延。", False),
    ("說明：本次延遲係屬不可抗力，依約免罰。", "原廠延後到貨，不要直接宣稱免罰或屬於不可抗力。", True),
    ("說明：是否免罰仍請　貴機關審認。", "不要直接宣稱免罰。", False),
    ("說明：本公司已同意更換型號。", "在獲得同意以前還不會自行換貨。", True),
])
def test_stage_claims(draft, src, flag):
    got = od.check_draft(draft, [src], mode="letter")
    assert (("claim_unsupported", "error") in _codes(got)) is flag, [i.args for i in got]


def test_numbers_inside_write_instructions_are_not_omission_hints():
    got = od.omission_issues("主旨：請款", "要請領尾款，不要自行寫成收到函後七天內一定要付款。")
    assert got == []
    got = od.omission_issues("主旨：x", "原本的窗口調職，改由另一位同仁接手。")
    assert got == []


# ------------------------------------------------------------------ 提示與流程（假模型）

def _fake_ask(record):
    def ask(prompt):
        record.append(prompt)
        if '"explanation"' in prompt:
            return json.dumps({"subject": "申請驗收", "explanation": ["本公司已完成安裝。"],
                               "measures": ["請貴機關安排驗收。"]}, ensure_ascii=False)
        return json.dumps({"subject": {"value": "申請驗收", "quote": "申請驗收"}}, ensure_ascii=False)
    return ask


def test_run_letter_in_company_mode():
    seen: list[str] = []
    d = od.run_letter("我們公司已完成安裝，想申請驗收。", _fake_ask(seen), relation=CO)
    facts_prompt, draft_prompt = seen[0], seen[-1]
    assert "企業的文書承辦助理" in facts_prompt and "政府機關的文書承辦助理" not in facts_prompt
    for want in ("提到受文機關一律寫「貴機關」", "提到本公司一律寫「本公司」", "沒有上下級關係",
                 "不要寫「簽於」", "一律寫成請求", "不同階段"):
        assert want in draft_prompt, want
    assert "行文關係" not in draft_prompt          # 機關那一份提示沒有被拿來用
    assert d.text.split("\n")[0] == "〔待補：公司名稱〕　函"
    assert "請　貴機關安排驗收" in d.text            # 挪抬由程式補
    msgs = [i.message for i in d.issues]
    assert od.MESSAGES["missing_company"] in msgs


def test_run_letter_agency_prompt_is_untouched():
    seen: list[str] = []
    od.run_letter("請各區公所派員參加研習。", _fake_ask(seen), relation="down", receiver="東湖區公所")
    assert "政府機關的文書承辦助理" in seen[0]
    assert "行文關係：下行" in seen[-1] and "本公司" not in seen[-1]


# ------------------------------------------------------------------ 端點

def test_parse_letter_issuer_company_fixes_the_relation():
    p = _rt()._parse_inputs({"mode": "letter", "issuer": "company", "relation": "up",
                             "narrative": "申請驗收"})
    assert p["issuer"] == "company" and p["relation"] == CO
    assert p["closing"] == od.LETTER_CLOSINGS[CO][0]
    # 舊的呼叫端只送了行文關係 company
    p = _rt()._parse_inputs({"mode": "letter", "relation": CO, "narrative": "申請驗收"})
    assert p["issuer"] == "company" and p["relation"] == CO


def test_parse_letter_rejects_mismatched_choices():
    from fastapi import HTTPException
    rt = _rt()
    with pytest.raises(HTTPException) as e:      # 機關的期望語不可以用在企業的函
        rt._parse_inputs({"mode": "letter", "issuer": "company", "closing": "請　鑒核",
                          "narrative": "x"})
    assert e.value.status_code == 400
    with pytest.raises(HTTPException):           # 機關不可以選企業那個關係
        rt._parse_inputs({"mode": "letter", "issuer": "agency", "relation": CO, "narrative": "x"})
    with pytest.raises(HTTPException):
        rt._parse_inputs({"mode": "letter", "issuer": "government", "narrative": "x"})


def test_salutation_endpoint_for_company(client, auth_off):
    r = client.get(f"{BASE}/salutation", params={"relation": CO, "receiver": "嘉禾市資訊局"})
    assert r.status_code == 200 and r.json() == {"term": "貴局", "self": "本公司"}


def test_page_has_the_issuer_switch(client, auth_off):
    html = client.get(f"{BASE}/").text
    assert 'id="odIssuer"' in html and 'id="odRelationRow"' in html
    # 企業那一組的期望語在 data-closings 裡（前端依它畫），行文關係下拉不列 company
    rel = html[html.index('id="odRelation"'):]
    rel = rel[:rel.index("</select>")]
    assert f'value="{CO}"' not in rel


# ------------------------------------------------------------------ 實際產出對出來的（2026-10-08 使用者給的兩份 ODT）

def test_receiver_placeholder_becomes_the_salutation_when_the_receiver_is_known():
    """受文者填了「○○有限公司」，說明卻寫「檢查〔待補：廠商名稱〕交付之網站」——
    受文者是誰已經知道，內文用稱謂（貴公司）。"""
    c = {"subject": "請貴公司提出改善計畫", "explanation": ["檢查〔待補：廠商名稱〕交付之網站發現3項問題"],
         "measures": []}
    got = od._fill_receiver_placeholders(c, "貴公司", receiver="○○有限公司", relation="people")
    assert got["explanation"] == ["檢查貴公司交付之網站發現3項問題"]
    # 受文者沒填（機關的函）：真的不知道是誰，留著〔待補〕
    same = od._fill_receiver_placeholders(c, "台端", receiver="", relation="people")
    assert same["explanation"] == c["explanation"]
    # 企業發函：受文機關的名稱一律是「貴機關」；別的〔待補〕不動
    c2 = {"subject": "", "explanation": ["本公司承接〔待補：機關名稱〕〔待補：案名〕"], "measures": []}
    got = od._fill_receiver_placeholders(c2, "貴機關", receiver="", relation=CO)
    assert got["explanation"] == ["本公司承接貴機關〔待補：案名〕"]


@pytest.mark.parametrize("raw,want", [
    ("預估總共含稅新臺幣24萬元", "預估共計新臺幣24萬元（含稅）"),
    ("未稅新臺幣1,200元", "新臺幣1,200元（未稅）"),
    ("新臺幣24萬元（含稅）", "新臺幣24萬元（含稅）"),          # 已經是對的不動
    ("含稅價格另行報價", "含稅價格另行報價"),                  # 沒有金額不動
])
def test_amount_tax_wording(raw, want):
    assert od.formalise(raw) == want


def test_the_export_does_not_hang_punctuation_past_the_margin():
    """句末標點不懸掛到版心外 —— 懸掛的「。」在 Writer 畫面上常看不到，像是少了句號。"""
    import io
    import zipfile
    from app.core import official_doc_odt as odx
    data, _media = odx.export("主旨：測試，請　查照。", "odt", title="t")
    styles = zipfile.ZipFile(io.BytesIO(data)).read("styles.xml").decode()
    assert 'style:punctuation-wrap="simple"' in styles
    assert 'punctuation-wrap="hanging"' not in styles


def test_title_does_not_start_with_the_request():
    rt = _rt()
    d = od.Draft("letter", "主旨：請　貴公司針對網站發現之問題提出改善計畫，請　查照。", [], [], {}, 0, [])
    assert rt._title_for("letter", d) == "針對網站發現之問題提出改善計畫"
