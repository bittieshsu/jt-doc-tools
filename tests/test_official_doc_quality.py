"""公文撰擬：2026-10-08 這一輪的品質修正（使用者回報 ＋ 採購簽的審閱意見）。

每一條都是「改回去不會有別的測試變紅、但產出會安靜變差」的那一類：
專有名詞的大小寫、待確認被當成待補、擬辦只列待辦、函的主旨期望語重複、
受文者還沒填就寫〔待確認：受文者稱謂〕、「三場」對「3場」被報成找不到、
日期與星期對不上、改寫一段沒有效果。

素材一律是編的。
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from app.core import official_doc as od


def _fake(replies):
    calls = []

    def ask(prompt):
        calls.append(prompt)
        r = replies[min(len(calls), len(replies)) - 1]
        return r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
    return ask, calls


# ------------------------------------------------------------------ 專有名詞的標準寫法

@pytest.mark.parametrize("raw,want", [
    ("汰換現有 vmware esxi 主機 10 台", "汰換現有 VMware ESXi 主機 10 台"),
    ("升級到 windows 11 與 office 2021", "升級到 Windows 11 與 Office 2021"),
    ("資料庫是 sql server 2019", "資料庫是 SQL Server 2019"),
    ("無線網路 wifi 與 hyper-v 主機", "無線網路 Wi-Fi 與 Hyper-V 主機"),
    ("建立 line 群組", "建立 LINE 群組"),
    ("主機:vmware", "主機:VMware"),
    ("nas 跟 ups 都要換", "NAS 跟 UPS 都要換"),
])
def test_known_proper_nouns_get_their_standard_case(raw, want):
    assert od.canonical_terms(raw) == want


@pytest.mark.parametrize("raw", [
    "Please open the word file before the meeting",      # 英文句子裡的一般字不動
    "the line is long",
    "見 https://example.com/esxi/vmware.html",           # 網址
    "寄到 admin@vmware.example",                         # 電子郵件
    "附件 esxi.pdf",                                     # 檔名
    r"路徑 C:\windows\system32",                         # 路徑
])
def test_urls_emails_files_and_english_sentences_are_left_alone(raw):
    assert od.canonical_terms(raw) == raw


def test_canonical_terms_is_idempotent_and_only_changes_case():
    once = od.canonical_terms("vmware esxi 與 windows server 2022")
    assert od.canonical_terms(once) == once
    assert once.casefold() == "vmware esxi 與 windows server 2022".casefold()


def test_the_model_sees_and_writes_the_standard_spelling():
    """送給模型的原文已經換好；模型自己寫成小寫也會被換回來。"""
    ask, calls = _fake([
        {"subject": {"value": "汰換 vmware esxi 主機", "quote": "汰換 vmware esxi 主機"}},
        {"subject": "為汰換 vmware esxi 主機", "explanation": [], "proposal": ["擬辦理"]},
    ])
    d = od.run_sign("想汰換 vmware esxi 主機 10 台。", ask, unit="資訊室")
    assert "VMware ESXi" in calls[0] and "vmware esxi" not in calls[0]
    assert "VMware ESXi" in d.text and "vmware" not in d.text
    # 大小寫不同不算事實不符
    assert not [i for i in d.issues if i.severity == "error"], [i.message for i in d.issues]


# ------------------------------------------------------------------ 簽的生成規則（審閱意見）

def test_the_sign_prompt_asks_for_approval_scope_follow_up_and_pending_items():
    """擬辦要涵蓋「請核准什麼、核准後做什麼、哪些仍待確認」—— 不能只列待辦。"""
    p = od.prompt_sign_draft("想採購授權。", [], "normal", "")
    assert "擬請同意" in p and "奉核後" in p
    assert "不可以只寫「請某單位確認」" in p
    assert "新臺幣" in p                               # 金額寫法
    assert "①背景與必要性" in p                        # 說明照三個面向合併，不是一句一項


def test_pending_confirmations_are_not_placeholders():
    """原文說「還要請某單位確認」的是後續要辦的事 —— 寫「尚待○○單位確認」，不是〔待補〕。"""
    assert "尚待○○單位確認" in od.prompt_sign_draft("x", [], "normal", "")
    assert "不要填 null —— 那是寫了，只是還沒確定" in od.prompt_sign_facts("x")
    assert "其他地方不要用〔待確認〕" in od._COMMON_RULES


def test_an_amount_that_is_not_known_yet_is_not_an_amount():
    """「目前還不知道要花多少錢」：不提醒缺經費來源；原文明講等估價後另案簽辦，也不提醒缺金額。"""
    facts = [{"key": "amount", "label": "金額", "value": "目前還不知道要花多少錢", "status": "provided"},
             {"key": "budget_source", "label": "經費來源", "value": None, "status": "missing"}]
    narrative = "想請廠商勘查並提出修繕報價，目前還不知道要花多少錢，等金額出來再另外簽辦修繕。"
    got = [i.template for i in od.completeness_issues("sign", facts, narrative)]
    assert od.MESSAGES["missing_budget_source"] not in got
    assert od.MESSAGES["missing_amount"] not in got


def test_a_real_amount_without_a_source_is_still_flagged():
    """反向對照：有金額卻沒寫經費來源，照樣要提醒（不可以整條拿掉）。"""
    facts = [{"key": "amount", "label": "金額", "value": "新臺幣24萬元", "status": "provided"},
             {"key": "budget_source", "label": "經費來源", "value": None, "status": "missing"}]
    got = [i.template for i in od.completeness_issues("sign", facts, "採購授權預估24萬元。")]
    assert od.MESSAGES["missing_budget_source"] in got


# ------------------------------------------------------------------ 函

@pytest.mark.parametrize("subject", [
    "送交成果報告，請〔待確認：受文者稱謂〕查照",
    "送交成果報告，請　貴局查照",
    "送交成果報告，請貴機關查照。",
    "送交成果報告，請　鈞府鑒核",
])
def test_a_closing_the_model_wrote_with_a_salutation_is_removed(subject):
    """模型自己寫的期望語（中間夾了稱謂）也要拿掉 —— 不然程式再加一次，變成「…查照，請　鑒核。」"""
    assert od._clean_subject(subject) == "送交成果報告"


def test_letter_subject_ends_with_one_closing_only():
    text = od.assemble_letter({"subject": "送交成果報告，請〔待確認：受文者稱謂〕查照"},
                              relation="up", receiver="")
    line = next(x for x in text.splitlines() if x.startswith("主旨："))
    assert line == "主旨：送交成果報告，請　鑒核。"


def test_an_empty_receiver_does_not_put_a_salutation_placeholder_in_the_text():
    ask, calls = _fake([{"subject": {"value": "辦理訓練", "quote": "辦理訓練"}},
                        {"subject": "請派員參加訓練", "explanation": [], "measures": []}])
    d = od.run_letter("想請各所屬機關派員參加訓練。", ask, relation="down")
    assert "〔待確認：受文者稱謂〕" not in calls[1]
    assert "不要寫稱謂" in calls[1]
    assert "受文者稱謂" not in d.text


def test_a_named_receiver_still_gets_its_salutation():
    ask, calls = _fake([{"subject": {"value": "辦理訓練", "quote": "辦理訓練"}},
                        {"subject": "請　貴所派員參加訓練", "explanation": [], "measures": []}])
    od.run_letter("想請東湖區公所派員參加訓練。", ask, relation="down", receiver="嘉禾市東湖區公所")
    assert "「貴所」" in calls[1]


def test_the_users_own_self_term_is_kept_when_the_org_is_blank():
    """機關全銜沒填、原文寫「本局三樓會議室」：自稱照原文用「本局」，不是〔待補：機關名稱〕。"""
    ask, calls = _fake([{"subject": {"value": "訓練", "quote": "訓練"}},
                        {"subject": "辦理訓練", "explanation": [], "measures": []}])
    od.run_letter("在本局三樓會議室辦訓練。", ask, relation="down")
    assert "一律寫「本局」" in calls[1]
    assert od._self_term_in("本室備份") == "" and od._self_term_in("本局長") == ""


# ------------------------------------------------------------------ 數量的單位

@pytest.mark.parametrize("draft,source", [
    ("共辦理3場", "共辦三場"),
    ("合計186人次參加", "合計186人次參加"),
    ("分2梯次辦理", "分兩梯次辦理"),
    ("約80箱", "大約80箱"),
])
def test_counted_things_match_across_chinese_and_arabic_numerals(draft, source):
    codes = [i.code for i in od.check_draft(f"主旨：{draft}。\n擬辦：擬辦理。", [source], mode="sign")]
    assert "qty_unsupported" not in codes


def test_a_changed_count_is_still_flagged():
    codes = [i.code for i in od.check_draft("主旨：共辦理4場。\n擬辦：擬辦理。", ["共辦三場"], mode="sign")]
    assert "qty_unsupported" in codes


# ------------------------------------------------------------------ 日期與星期

T = dt.date(2026, 10, 8)        # 星期四


@pytest.mark.parametrize("text", ["今年11月14日星期六", "115年11月14日（六）", "11月14日(週六)",
                                  "2026年11月14日禮拜六"])
def test_a_matching_weekday_is_fine(text):
    assert od.date_issues(text, today=T) == []


def test_a_wrong_weekday_is_reported_and_not_fixed():
    got = od.date_issues("主旨：今年11月15日星期六停機。", today=T)
    assert [i.code for i in got] == ["weekday_mismatch"]
    assert "星期日" in got[0].message and "星期六" in got[0].message
    assert got[0].snippet == "今年11月15日星期六"


def test_a_date_without_a_year_says_which_year_it_assumed():
    got = od.date_issues("草稿", "11月15日（五）要停機", today=T)
    assert got and "以今年（115年）計算" in got[0].message
    assert got[0].snippet == ""          # 草稿裡沒有這一段 → 點不到，但要講


def test_a_passed_deadline_is_a_hint_but_an_old_plan_is_not():
    assert [i.code for i in od.date_issues("請於3月5日前完成。", today=T)] == ["date_past"]
    assert od.date_issues("原本預計今年3月5日前完成。", today=T) == []
    assert od.date_issues("請於11月5日前回覆。", today=T) == []


# ------------------------------------------------------------------ 改寫一段

SRC = ["我們單位有120台電腦，防毒軟體授權到今年11月30日到期，想採購一年期授權，預估含稅24萬元。"]


def test_the_rewrite_prompt_says_not_to_pull_in_other_content():
    """實測：「精簡」把整份需求搬進那一段，字數反而從 30 變 101 —— 提示要講清楚只改這一段。"""
    ask, calls = _fake([{"text": "現有電腦授權今年11月30日到期。"}])
    od.rewrite_paragraph("一、現有120台電腦之防毒軟體授權將於今年11月30日到期。", ask,
                         kind="formal", sources=SRC)
    assert "不要把這一段沒有提到的事搬進來" in calls[0]
    assert "不要自己換成年份" in calls[0]
    assert "不要新增〔待補〕" in calls[0]


def test_a_shorter_rewrite_that_is_not_shorter_is_asked_again_then_reported():
    long = "一、現有120台電腦之防毒軟體授權將於今年11月30日到期，擬採購一年期授權。"
    ask, calls = _fake([{"text": long[2:] + "預估含稅24萬元。"}, {"text": long[2:]}])
    r = od.rewrite_paragraph(long, ask, kind="shorter", sources=SRC)
    assert len(calls) == 2 and "沒有變短" in calls[1]
    assert "rewrite_not_shorter" in [i["code"] for i in r["issues"]]


def test_a_rewrite_that_changes_nothing_says_so():
    para = "一、現有120台電腦之防毒軟體授權將於今年11月30日到期。"
    ask, _ = _fake([{"text": para[2:]}])
    r = od.rewrite_paragraph(para, ask, kind="formal", sources=SRC)
    assert "rewrite_unchanged" in [i["code"] for i in r["issues"]]
    ask, _ = _fake([{"text": "本單位現有電腦120台，其防毒軟體授權將於今年11月30日屆期，擬予續購。"}])
    r = od.rewrite_paragraph(para, ask, kind="formal", sources=SRC)
    assert "rewrite_unchanged" not in [i["code"] for i in r["issues"]]


def test_new_messages_are_in_the_message_table():
    for k in ("weekday_mismatch", "weekday_mismatch_assumed", "date_past",
              "rewrite_unchanged", "rewrite_not_shorter"):
        assert k in od.MESSAGES


def test_an_amount_fact_without_money_is_not_counted_as_an_amount():
    """資料表的「金額」寫的是「要看報價」（沒有金額數字）、原文也沒說另案簽辦：
    那不是金額 —— 不可以因此提醒「有金額但沒有經費來源」。"""
    facts = [{"key": "amount", "label": "金額", "value": "要看廠商報價", "status": "provided"},
             {"key": "budget_source", "label": "經費來源", "value": None, "status": "missing"}]
    got = [i.template for i in od.completeness_issues("sign", facts, "想請廠商勘查漏水的原因。")]
    assert od.MESSAGES["missing_budget_source"] not in got


@pytest.mark.parametrize("raw,want", [
    ("經費來源還沒確定", "經費來源尚未確定"), ("還沒有核准", "尚未核准"),
    ("已經先把文件移走", "已先把文件移走"), ("目前沒辦法核對", "目前無法核對"),
    ("報名費不用錢", "報名費免費"), ("空間快要滿了", "空間即將滿了"),
    ("已經費核定", "已經費核定"),                 # 「已＋經費」不是「已經」
])
def test_safe_plain_words_become_formal_in_the_draft(raw, want):
    assert od._clean_item(raw) == want
    assert od.formalise(raw) == want


# ------------------------------------------------------------------ 36 筆範例實跑抓到的

@pytest.mark.parametrize("text,value,unit", [
    ("預計一個半小時", "1.5", "小時"), ("1個半小時", "1.5", "小時"), ("兩年半", "2.5", "年"),
    ("半天", "0.5", "天"),
])
def test_half_quantities(text, value, unit):
    from decimal import Decimal
    assert [(q.value, q.unit) for q in od.find_quantities(text, lenient=True)] == [(Decimal(value), unit)]


def test_one_and_a_half_hours_supports_1_5_hours():
    codes = [i.code for i in od.check_draft("主旨：預計進行1.5小時。\n擬辦：擬辦理。", ["預計一個半小時"], mode="sign")]
    assert "qty_unsupported" not in codes


def test_counting_classifiers_are_interchangeable_but_real_units_are_not():
    ok = [i.code for i in od.check_draft("主旨：共3項問題。\n擬辦：擬辦理。", ["發現三個問題"], mode="sign")]
    bad = [i.code for i in od.check_draft("主旨：共3台。\n擬辦：擬辦理。", ["發現三個問題"], mode="sign")]
    assert "qty_unsupported" not in ok and "qty_unsupported" in bad


def test_a_date_after_this_year_is_still_a_date():
    """「今年11月6日」原本整個不算日期 → 草稿寫「11月6日」被報成找不到、還被報成漏寫「11月」「6日」。"""
    assert [(d.month, d.day) for d in od.find_dates("在今年11月6日參加")] == [(11, 6)]
    src = "原本通知各所屬機關在今年11月6日參加說明會"
    assert od.check_draft("主旨：原定於11月6日舉行。\n擬辦：擬辦理。", [src], mode="sign") == []
    assert od.omission_issues("主旨：原定於11月6日舉行。", src) == []


@pytest.mark.parametrize("narrative,hint", [
    ("盤點表已經做好，等一下會附上檔案，請依表格填寫。", True),
    ("成果報告跟照片清冊都整理好了，我會一起上傳。", True),
    ("我還沒做好報名表，不要寫成已經有附件。", False),
    ("相關佐證資料我會另外整理，還沒附上。", False),
    ("請各機關派員參加。", False),
])
def test_attachments_mentioned_but_not_filled_in(narrative, hint):
    got = od.attachment_issues("附件：\n主旨：x。", narrative)
    assert bool(got) is hint
    if hint:
        assert got[0].snippet == "附件："
    # 附件欄已經填了就不提醒
    assert od.attachment_issues("附件：盤點表\n主旨：x。", narrative) == []


@pytest.mark.parametrize("narrative,term", [
    ("請廠商在11月8日前回覆改善計畫。", "貴公司"),
    ("有位民眾申請借用活動場地，資料缺少時間表。", "台端"),
])
def test_people_salutation_without_a_receiver(narrative, term):
    ask, calls = _fake([{"subject": {"value": "x", "quote": "x"}},
                        {"subject": "請補送資料", "explanation": [], "measures": []}])
    od.run_letter(narrative, ask, relation="people")
    assert f"一律寫「{term}」" in calls[1]


def test_ban_ge_zi_is_read_as_personal_data():
    """「辦個資保護教育訓練」被模型讀成「辦個」＋「資保護」。"""
    assert od.disambiguate("在本局辦個資保護教育訓練") == "在本局辦理個資保護教育訓練"
    assert od.disambiguate("想辦個活動") == "想辦個活動"
    ask, calls = _fake([{"subject": {"value": "x", "quote": "x"}},
                        {"subject": "辦理個資保護教育訓練", "explanation": [], "measures": []}])
    od.run_letter("在本局辦個資保護教育訓練。", ask, relation="down")
    assert "辦理個資保護" in calls[0]


@pytest.mark.parametrize("subject,want", [
    ("請主管同意試辦服務櫃檯動線調整", "為試辦服務櫃檯動線調整"),
    ("簽請主管同意公文附件儲存系統更新維護安排", "公文附件儲存系統更新維護安排"),
    ("為辦理採購", "為辦理採購"),
])
def test_sign_subject_does_not_ask_for_approval_twice(subject, want):
    line = od.assemble_sign({"subject": subject, "proposal": ["擬辦理"]}).splitlines()[1]
    assert line == f"主旨：{want}，簽請　核示。"


def test_duplicated_approval_is_collapsed():
    assert od._clean_item("擬請同意原則同意規劃宣導活動") == "擬請原則同意規劃宣導活動"


def test_amount_clause_goes_after_the_case_and_verbs_get_wei():
    line = od.assemble_sign({"subject": "辦理兩場資安教育訓練，預估所需經費新臺幣3萬元（含稅）一案",
                             "proposal": ["擬辦理"]}).splitlines()[1]
    assert line == "主旨：為辦理兩場資安教育訓練一案，預估所需經費新臺幣3萬元（含稅），簽請　核示。"


@pytest.mark.parametrize("raw,want", [
    ("日期、講師及〔待補：預算來源〕尚待確認", "日期、講師及預算來源尚待確認"),
    ("補件期限尚待〔待補：承辦單位〕確認", "補件期限尚待確認"),
    ("經費由〔待補：經費來源〕支應", "經費由〔待補：經費來源〕支應"),     # 真的缺的照留
])
def test_a_placeholder_inside_a_pending_clause_is_dropped(raw, want):
    assert od._clean_item(raw) == want


def test_formal_wording_in_the_draft_still_matches_the_users_plain_words():
    """草稿把「跟」寫成「與」，「依支出跟單位規定」不可以因此被當成沒依據的法規。"""
    issues = od.check_draft("主旨：實際依支出與單位規定辦理。\n擬辦：擬辦理。",
                            ["交通費實際依支出跟單位規定辦理"], mode="sign")
    assert [i.code for i in issues if i.severity == "error"] == []


def test_this_year_in_the_source_pins_the_year(monkeypatch):
    """原文「今年11月18日」，草稿寫成「111年11月18日」：年份錯了要抓到（原本沒有年份的日期支持任何年份）。"""
    monkeypatch.setattr(od, "_today", lambda: dt.date(2026, 10, 8))
    src = ["時間暫訂今年11月18日上午10點"]
    wrong = od.check_draft("主旨：暫訂111年11月18日。\n擬辦：擬辦理。", src, mode="sign")
    assert [i.code for i in wrong] == ["date_unsupported"]
    for ok in ("暫訂115年11月18日", "暫訂11月18日", "暫訂2026年11月18日"):
        assert od.check_draft(f"主旨：{ok}。\n擬辦：擬辦理。", src, mode="sign") == [], ok
    # 原文沒寫年份的（「11月18日」）照舊：草稿寫哪一年都不算錯
    assert od.check_draft("主旨：暫訂111年11月18日。\n擬辦：擬辦理。", ["暫訂11月18日"], mode="sign") == []


def test_the_attachment_hint_quotes_the_clause_not_the_whole_sentence():
    got = od.attachment_issues("附件：\n", "已經完成活動，共辦三場，成果報告都整理好了，我會一起上傳。")
    assert got[0].args == ("我會一起上傳",)


@pytest.mark.parametrize("text,flag", [
    ("簽　　於x\n主旨：為辦理。\n擬辦：擬依規定比價。", True),
    ("簽　　於x\n主旨：為辦理。\n擬辦：\n一、擬依規定比價。\n二、奉核後辦理。", True),
    ("簽　　於x\n主旨：為辦理。\n擬辦：\n一、擬請同意辦理。\n二、奉核後比價。", False),
    ("簽　　於x\n主旨：為辦理。\n擬辦：擬陳閱後存查。", False),
    ("簽　　於x\n主旨：為辦理。\n擬辦：擬請原則同意規劃。\n敬陳\n局長", False),
])
def test_a_proposal_must_say_what_is_being_approved(text, flag):
    assert bool(od.proposal_issues(text)) is flag


def test_a_user_given_article_left_out_of_the_draft_is_hinted():
    src = "擬依政府採購法第49條規定邀請3家以上廠商比價"
    got = od.omission_issues("主旨：擬邀請3家以上廠商比價。", src)
    assert [i.args for i in got] == [("第49條",)]
    assert od.omission_issues("主旨：擬依政府採購法第49條規定邀請3家以上廠商比價。", src) == []
    assert "原文自己寫的法規名稱與條號" in od.prompt_sign_draft("x", [], "normal", "")


# ------------------------------------------------------------------ 照格式能力較差的模型（TAIDE 實測）

def test_a_truncated_reply_keeps_the_fields_that_were_finished():
    """TAIDE 把「金額」寫成一長串重複的跳脫字元、一路寫到輸出上限 → 整個回答讀不出來，
    連問兩次都一樣，整份草稿失敗。前面寫完的欄位要救回來。"""
    raw = ('{"subject": {"value": "辦理採購", "quote": ""},\n "purpose": {"value": "設備老舊", "quote": ""},\n'
           ' "amount": {"value": "' + "\\u514d\\u6570" * 300)
    assert od.parse_json_reply(raw) == {"subject": {"value": "辦理採購", "quote": ""},
                                        "purpose": {"value": "設備老舊", "quote": ""}}
    # 寫到一半的那一句整段拿掉（補上引號的話半句話會原樣進草稿），前面寫完的照收
    assert od.parse_json_reply('{"subject": "為辦理", "explanation": ["甲", "乙寫到一') == \
        {"subject": "為辦理", "explanation": ["甲"]}
    assert od.parse_json_reply("不是 JSON") is None


def test_runaway_repetition_is_not_content():
    got = od.parse_json_reply(json.dumps({"a": "正常", "b": "免数台磣" * 20,
                                          "c": ["好的", "台蒂" * 30]}, ensure_ascii=False))
    assert got == {"a": "正常", "b": None, "c": ["好的"]}
    # 一般的重複寫法不算（佔位的〇、刪節號）
    assert od.parse_json_reply('{"t": "〇〇〇〇年〇月〇日……"}') == {"t": "〇〇〇〇年〇月〇日……"}


def test_a_salvaged_reply_still_produces_a_draft():
    ask, calls = _fake([
        '{"subject": {"value": "辦理採購", "quote": "辦理採購"},\n "amount": {"value": "' + "\\u514d" * 500,
        {"subject": "為辦理採購", "explanation": ["甲"], "proposal": ["擬請同意辦理採購"]},
    ])
    d = od.run_sign("想辦理採購。", ask, unit="資訊室")
    assert len(calls) == 2 and "為辦理採購" in d.text


def test_trailing_junk_after_a_complete_object_keeps_every_field():
    """多一個「}」（TAIDE 實測）：原本整段讀不出來、退回救援，最後一個欄位（擬辦）被丟掉。"""
    got = od.parse_json_reply('{"subject": "甲", "explanation": ["乙"], "proposal": ["丙"]}}')
    assert got == {"subject": "甲", "explanation": ["乙"], "proposal": ["丙"]}


def test_chinese_keys_are_mapped_back():
    got = od.parse_json_reply('{"主旨": "甲", "說明": ["乙"], "擬辦": ["丙"]}')
    assert got == {"subject": "甲", "explanation": ["乙"], "proposal": ["丙"]}
    facts = od.parse_json_reply('{"事由": {"value": "x", "quote": ""}, "金額": {"value": "10萬元", "quote": ""}}')
    assert set(facts) == {"subject", "amount"}


def test_is_runaway_spots_repetition_but_not_normal_text():
    assert od.is_runaway('{"amount": {"value": "' + "\\u6210\\u6163\\u53f0" * 40)
    assert od.is_runaway("免数台磣" * 80)
    normal = json.dumps({"subject": "為辦理採購一案", "explanation": ["甲" * 3 + "乙丙丁戊己庚" * 3] * 5},
                        ensure_ascii=False)
    assert not od.is_runaway(normal * 3)


def test_the_stream_stops_early_when_the_model_runs_away():
    """模型打轉時串流提早停 —— 不必等到輸出上限（TAIDE 實測每次白等 150 秒）。"""
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from app.core.llm_client import LLMClient

    sent = {"n": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                head = json.dumps({"choices": [{"delta": {"content": '{"subject": "甲", "amount": "'}}]})
                self.wfile.write(f"data: {head}\n\n".encode())
                # TAIDE 實測的樣子：重複的單位有 48 個字元（8 個 \\u 跳脫），而且一個字一個字送來 ——
                # 只看最後 256 段的話永遠湊不滿 6 次重複，停不下來
                unit = "\\u676b\\u6280\\u6976\\u6280\\u6709\\u62b3\\u80cd\\u624f"
                for ch in unit * 300:
                    c = json.dumps({"choices": [{"delta": {"content": ch}}]})
                    self.wfile.write(f"data: {c}\n\n".encode())
                    sent["n"] += 1
                self.wfile.write(b"data: [DONE]\n\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = LLMClient(base_url=f"http://127.0.0.1:{port}/v1", timeout=30)
        out = client.text_query("x", model="m", stop_when=od.is_runaway)
        assert len(out) < 4000, len(out)                      # 沒有讀完 14400 段
        assert client.last_stats.get("stopped_early")
        assert od.parse_json_reply(out) == {"subject": "甲"}  # 救回寫完的欄位
        # 沒給 stop_when 照舊讀完（反向對照）
        full = client.text_query("x", model="m")
        assert len(full) > 14000
    finally:
        srv.shutdown()


def test_a_reply_missing_only_its_closing_brackets_keeps_every_field():
    """TAIDE 實測：回答最後少了「]}」。原本只能退回到最後一個逗號，「擬辦」整段不見。"""
    raw = '{"subject": "甲", "explanation": [\n"乙",\n"丙"\n], "proposal": [\n"丁",\n"戊"\n]'
    assert od.parse_json_reply(raw) == {"subject": "甲", "explanation": ["乙", "丙"], "proposal": ["丁", "戊"]}


def test_items_written_as_objects_are_flattened():
    """TAIDE 實測：說明每一項寫成 {'背景與必要性': '…'}，字典的寫法原樣印進了草稿。"""
    ask, _ = _fake([{"subject": {"value": "x", "quote": "x"}},
                    {"subject": "為辦理勘查", "explanation": [{"背景與必要性": "檔案室漏水。"}],
                     "proposal": [{"請主管同意的事": "擬請同意辦理勘查。"}]}])
    d = od.run_sign("檔案室漏水，想請廠商勘查。", ask, unit="總務課")
    assert "{" not in d.text and "背景與必要性" not in d.text
    assert "檔案室漏水。" in d.text and "擬請同意辦理勘查。" in d.text


def test_the_facts_prompt_has_no_example_value_to_copy():
    """「預計由某預算支應」這個例子被 TAIDE 原樣抄進草稿 —— 提示裡不放具體的例子值。"""
    assert "某預算" not in od.prompt_sign_facts("x")


def test_a_retry_uses_the_callers_retry_hook():
    """格式不對而重問時用 `ask.retry`（呼叫端換一點溫度）；第一次一律用 `ask`。"""
    seen = []

    def ask(p):
        seen.append("ask")
        return "不是 JSON"

    def retry(p):
        seen.append("retry")
        return json.dumps({"subject": "為辦理"}, ensure_ascii=False)

    ask.retry = retry
    got, n = od._ask_json(ask, "x", ("subject",))
    assert seen == ["ask", "retry"] and got == {"subject": "為辦理"} and n == 2


@pytest.mark.parametrize("raw,want", [
    ("背景與必要性：因應櫃檯排隊過長。", "因應櫃檯排隊過長。"),
    ("①請主管同意的事：擬請同意試辦。", "擬請同意試辦。"),
    ("擬辦理背景與必要性之說明。", "擬辦理背景與必要性之說明。"),
])
def test_echoed_facet_labels_are_removed(raw, want):
    assert od._clean_item(raw) == want


def test_markdown_escapes_do_not_reach_the_draft():
    assert od._clean_item(r"感謝\[　台端\]提供資訊") == "感謝[　台端]提供資訊"


def test_prompts_carry_no_concrete_agency_name_to_copy():
    """「嘉禾市政府」這個例子被 TAIDE 照抄成「嘉禾市政府教育局及嘉禾市警察局」—— 原文根本沒有的機關。"""
    for p in (od._COMMON_RULES, od.prompt_sign_draft("x", [], "normal", ""),
              od.prompt_letter_draft("x", [], "normal", term="貴所", self_name="本所", relation="peer")):
        assert "嘉禾" not in p


def test_tax_qualifiers_the_user_never_wrote_are_flagged():
    """「（含稅）」是提示裡的例子，被照抄進原文沒寫含稅的草稿。"""
    bad = od.check_draft("主旨：預估所需經費新臺幣180萬元（含稅）。\n擬辦：擬辦理。", ["預算大概180萬元"], mode="sign")
    assert [i.code for i in bad] == ["word_unsupported"] and bad[0].snippet == "含稅"
    ok = od.check_draft("主旨：預估所需經費新臺幣24萬元（含稅）。\n擬辦：擬辦理。", ["預估總共含稅24萬元"], mode="sign")
    assert ok == []
    p = od.prompt_sign_draft("x", [], "normal", "")
    assert "原文沒寫的不要加" in p and "元（含稅）」）" not in p


def test_facts_that_echo_the_prompt_schema_are_missing():
    """TAIDE 把提示裡範例 JSON 的說明字原樣當成值（「來文期限：來文要求的期限」）。"""
    f = od.normalise_facts({"sender": {"value": "來文機關（待確認）", "quote": ""},
                            "deadline": {"value": "來文要求的期限", "quote": ""},
                            "request": {"value": "借會議室", "quote": "借會議室"}},
                           # 原文裡也有這些字 —— 只靠「跟原文對不上」那一道擋不下來，要靠認得範例的說明字
                           "來文機關與來文要求的期限都沒寫，只說想借會議室", od.ENDORSE_FACT_LABELS)
    by = {x["key"]: x for x in f}
    assert by["sender"]["value"] is None and by["deadline"]["value"] is None
    assert by["request"]["value"] == "借會議室"


def test_every_json_prompt_ends_with_the_plain_chinese_line():
    """TAIDE 實測：少了這一句，資料表提示 3/3 用 \\u 跳脫寫中文、然後打轉到輸出上限；加了 3/3 正常。"""
    ask, calls = _fake([{"subject": {"value": "x", "quote": "x"}},
                        {"subject": "為辦理", "explanation": [], "proposal": ["擬請同意辦理"]}])
    od.run_sign("想辦理採購。", ask)
    assert calls and all(c.endswith(od.JSON_TAIL) for c in calls)


def test_a_garbled_value_unrelated_to_the_source_is_dropped():
    f = od.normalise_facts({"sender": {"value": "决斉有息", "quote": ""},
                            "request": {"value": "請本所查明施工噪音", "quote": ""}},
                           "民眾陳情附近施工晚上很吵，希望我們處理。", od.ENDORSE_FACT_LABELS)
    by = {x["key"]: x for x in f}
    assert by["sender"]["value"] is None
    assert by["request"]["value"] == "請本所查明施工噪音"      # 改寫過但跟原文共用「施工」—— 留著


def test_a_runaway_gets_one_more_retry():
    seen = []

    def ask(p):
        seen.append("ask")
        return '{"text": "' + "\\n" * 300

    def retry(p):
        seen.append("retry")
        return '{"text": "' + "\\n" * 300 if seen.count("retry") == 1 else '{"text": "擬存查"}'

    ask.retry = retry
    got, n = od._ask_json(ask, "x", ("text",))
    assert got == {"text": "擬存查"} and seen == ["ask", "retry", "retry"] and n == 3
