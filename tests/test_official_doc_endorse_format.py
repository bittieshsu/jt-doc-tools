"""簽辦意見的格式（2026-10-08 使用者看預覽圖問「這樣格式正確嗎」）。

三件事：
* 草稿裡的「今年11月20日」要寫成民國年（「115年11月20日」）—— 模型照原文寫「今年」，
  換算由程式在組好的草稿上做一次，並在檢查結果講出來（跨年才發文會差一年）。
* 結尾已經有「，陳核。」，內文又寫「陳主管核閱後…」是請示兩次 → 提醒（只提醒不改）。
* 回覆來文是「函復」，「後回函」改成「後函復」；名詞的「回函」不動。
"""
from __future__ import annotations

import json
from datetime import date

from app.core import official_doc as od

T = date(2026, 10, 8)


def _fake(replies):
    calls = []

    def ask(prompt):
        calls.append(prompt)
        r = replies[len(calls) - 1]
        return r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
    return ask, calls


# ------------------------------------------------------------------ 今年 → 民國年

def test_relative_years_become_roc_years():
    text, done = od.resolve_relative_years(
        "於今年11月20日前回報；明年1月起實施；去年12月曾辦理；本年 3 月 5 日說明會。", today=T)
    assert "115年11月20日" in text and "116年1月" in text and "114年12月" in text
    assert "115年3月5日" in text
    assert "今年" not in text and "明年" not in text and "去年" not in text
    assert [new for _, new in done] == ["115年11月20日", "116年1月", "114年12月", "115年3月5日"]


def test_only_dates_are_touched():
    # 「今年度」「今年起」不是日期 —— 動了會改掉使用者的意思
    src = "以今年度設備費支應，今年起改為每季檢查。"
    text, done = od.resolve_relative_years(src, today=T)
    assert text == src and done == []


def test_full_width_digits_are_read():
    text, _ = od.resolve_relative_years("今年１１月２０日前", today=T)
    assert text == "115年11月20日前"


def test_the_change_is_reported_as_a_hint_pointing_at_the_new_text():
    _, done = od.resolve_relative_years("今年11月20日前回報", today=T)
    got = od.relative_year_issues(done)
    assert [(i.code, i.severity, i.snippet) for i in got] == [("relative_year", "hint", "115年11月20日")]
    assert "跨年" in got[0].message


def test_run_endorse_writes_the_roc_year_and_the_check_still_finds_it_in_the_source(monkeypatch):
    monkeypatch.setattr(od, "_today", lambda: T)
    src = "上級機關函請本所於今年11月20日前回報辦公場所節能措施及執行情形。"
    ask, _ = _fake([
        {"sender": {"value": "上級機關", "quote": "上級機關"},
         "deadline": {"value": "今年11月20日", "quote": "今年11月20日前"}},
        {"items": ["上級機關要求於今年11月20日前回報辦公場所節能措施及執行情形。",
                   "擬由總務單位彙整後函復"]},
    ])
    d = od.run_endorse(src, "請總務彙整後函復", ask, fmt="list")
    assert "115年11月20日" in d.text and "今年" not in d.text
    codes = [i.code for i in d.issues]
    assert "relative_year" in codes
    # 換成民國年之後，日期仍然找得到依據（原文的「今年」照今天算）
    assert "date_unsupported" not in codes and "omitted_date" not in codes


def test_drafts_of_every_kind_get_the_roc_year(monkeypatch):
    monkeypatch.setattr(od, "_today", lambda: T)
    ask, _ = _fake([{}, {"subject": "請於今年11月20日前回報", "explanation": ["依來文辦理。"],
                         "proposal": ["擬請同意。"]}])
    d = od.run_sign("請於今年11月20日前回報。", ask)
    assert "115年11月20日" in d.text
    ask, _ = _fake([{}, {"subject": "請於今年11月20日前回報", "explanation": ["依來文辦理。"],
                         "measures": []}])
    d = od.run_letter("請於今年11月20日前回報。", ask, relation="down", receiver="嘉禾市立圖書館")
    assert "115年11月20日" in d.text


# ------------------------------------------------------------------ 請示兩次

def test_asking_up_twice_is_pointed_out():
    text = "一、上級機關要求回報。\n二、擬由總務彙整後，整理完成並陳主管核閱後函復，陳核。"
    got = od.endorse_issues(text, "陳核")
    assert [(i.code, i.snippet) for i in got] == [("endorse_double_closing", "並陳主管核閱")]
    assert "陳核" in got[0].message


def test_the_closing_itself_is_not_counted():
    # 程式加在最後的「，陳核。」就是那一次請示，不可以自己報自己
    assert od.endorse_issues("一、來文要求回報。\n二、擬存查，陳核。", "陳核") == []
    assert od.endorse_issues("擬依來文辦理，陳閱。", "陳閱") == []


def test_no_closing_means_nothing_to_duplicate():
    assert od.endorse_issues("擬簽陳核示後辦理。", "") == []


def test_run_endorse_reports_it_and_recheck_too(monkeypatch):
    monkeypatch.setattr(od, "_today", lambda: T)
    ask, _ = _fake([{}, {"text": "本案上級機關函請回報節能措施，擬由總務彙整並陳主管核閱後函復"}])
    d = od.run_endorse("上級機關函請回報節能措施。", "請總務彙整後函復", ask)
    assert "endorse_double_closing" in [i.code for i in d.issues]
    again = od.recheck("endorse", d.text, d.facts,
                       {"source": "上級機關函請回報節能措施。", "direction": "請總務彙整後函復",
                        "closing": "陳核"})
    assert "endorse_double_closing" in [i.code for i in again]
    # 不加結語時，內文自己寫的請示不算重複
    none = od.recheck("endorse", d.text.replace("，陳核。", "。"), d.facts,
                      {"source": "上級機關函請回報節能措施。", "direction": "請總務彙整後函復",
                       "closing": "none"})
    assert "endorse_double_closing" not in [i.code for i in none]


# ------------------------------------------------------------------ 回函 → 函復

def test_reply_verb_is_written_as_hanfu():
    assert od._clean_item("擬由總務彙整後回函") == "擬由總務彙整後函復"
    assert od._clean_item("核閱後再回函") == "核閱後再函復"


def test_the_noun_huihan_is_left_alone():
    assert od._clean_item("檢附回函一份") == "檢附回函一份"
    assert od._clean_item("收到後回函件請存查") == "收到後回函件請存查"


def test_the_prompt_says_not_to_ask_up_or_write_huihan():
    q = od.prompt_endorse_draft("x", "擬存查", [], fmt="list", length="normal",
                                units="", internal_deadline="", outline=False)
    assert "陳主管核閱" in q and "函復" in q and "回函" in q
    # 大綱那一條接在後面，編號不撞
    q2 = od.prompt_endorse_draft("x", "擬存查", [], fmt="list", length="normal",
                                 units="", internal_deadline="", outline=True)
    assert "\n15. 結尾" in q2 and "\n16. 原文只是大綱" in q2


# ------------------------------------------------------------------ 單獨列印的抬頭與承辦人欄

from app.core import official_doc_odt as odx  # noqa: E402
import io  # noqa: E402
import re  # noqa: E402
import zipfile  # noqa: E402

import pytest  # noqa: E402

BASE = "/tools/official-doc"
ENDORSE_TEXT = "一、臺北市政府函請本局於10月20日前填報資料。\n二、擬由資訊室填報後函復，陳核。"


def _content(data: bytes) -> str:
    return zipfile.ZipFile(io.BytesIO(data)).read("content.xml").decode("utf-8")


def _paras(xml: str) -> list[tuple[str, str]]:
    return re.findall(r'text:style-name="(OD_[A-Za-z0-9]+)">([^<]*)', xml)


def test_the_frame_is_off_unless_asked_for():
    assert odx.page_extras({}).endorse_frame is False
    assert odx.page_extras({"endorse_frame": True}).endorse_frame is True
    plain = _paras(_content(odx.build_odt(ENDORSE_TEXT)))
    assert ("OD_Title", "簽辦意見") not in plain and not any(t.startswith("承辦人") for _, t in plain)


def test_the_frame_puts_the_heading_on_top_and_the_sign_off_at_the_bottom():
    ex = odx.PageExtras(endorse_frame=True, endorse_source="臺北市政府 府資字第1150012345號")
    paras = _paras(_content(odx.build_odt(ENDORSE_TEXT, extras=ex)))
    assert paras[0] == ("OD_Title", "簽辦意見")
    assert paras[1] == ("OD_Para", "來文：臺北市政府 府資字第1150012345號")
    assert paras[-2] == ("OD_Sign", "承辦人：")
    assert paras[-1][0] == "OD_SignLine" and paras[-1][1].startswith("日期：")
    # 本文照原樣、在中間
    assert ("OD_Item1", "二、擬由資訊室填報後函復，陳核。") in paras


def test_the_source_line_is_part_of_the_preview_cache_key():
    a = odx.PageExtras(endorse_frame=True, endorse_source="甲")
    b = odx.PageExtras(endorse_frame=True, endorse_source="乙")
    assert a.cache_key() != b.cache_key() != odx.NO_EXTRAS.cache_key()


@pytest.fixture
def endorse_case(client, auth_off, monkeypatch):
    from tests.test_official_doc_tool import FakeLLM, _run
    from app.core import llm_settings as ls
    fake = FakeLLM()
    monkeypatch.setattr(ls.llm_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(ls.llm_settings, "make_client", lambda *a, **k: fake)
    monkeypatch.setattr(ls.llm_settings, "get_model_for", lambda _t: "fake-model")
    return _run(client, mode="endorse", direction="擬照辦，填報後函復。")[1]


@pytest.fixture
def sign_case(client, auth_off, monkeypatch):
    from tests.test_official_doc_tool import FakeLLM, _run
    from app.core import llm_settings as ls
    fake = FakeLLM()
    monkeypatch.setattr(ls.llm_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(ls.llm_settings, "make_client", lambda *a, **k: fake)
    monkeypatch.setattr(ls.llm_settings, "get_model_for", lambda _t: "fake-model")
    return _run(client)[1]


def test_endorse_export_takes_the_source_from_the_case_not_the_request(client, endorse_case):
    r = client.post(f"{BASE}/export", json={
        "text": ENDORSE_TEXT, "fmt": "odt", "title": "簽辦意見", "case_id": endorse_case,
        # 畫面送來的「來文」不收 —— 印在文件上的那一行要跟草稿同一份依據
        "extras": {"endorse_frame": True, "endorse_source": "偽造的來文"}})
    assert r.status_code == 200, r.text
    paras = _paras(_content(r.content))
    assert ("OD_Para", "來文：臺北市政府 府資字第1150012345號") in paras
    assert not any("偽造" in t for _, t in paras)
    assert paras[-2] == ("OD_Sign", "承辦人：")


def test_endorse_export_without_the_option_has_no_frame(client, endorse_case):
    r = client.post(f"{BASE}/export", json={"text": ENDORSE_TEXT, "fmt": "odt", "title": "x",
                                             "case_id": endorse_case, "extras": {"page_numbers": True}})
    assert r.status_code == 200, r.text
    assert ("OD_Title", "簽辦意見") not in _paras(_content(r.content))


def test_sign_ignores_the_endorse_frame(client, sign_case):
    r = client.post(f"{BASE}/export", json={"text": "簽　　於資訊室\n主旨：測試，簽請　核示。",
                                             "fmt": "odt", "title": "x", "case_id": sign_case,
                                             "extras": {"endorse_frame": True}})
    assert r.status_code == 200, r.text
    assert not any(t.startswith("承辦人") or t == "簽辦意見" for _, t in _paras(_content(r.content)))


def test_the_page_has_the_card_and_it_is_off_by_default(client, auth_off):
    html = client.get(f"{BASE}/").text
    m = re.search(r'<div class="od-extra-letter" id="odExtrasEndorse" hidden>(.*?)</div>', html, re.S)
    assert m, "簽辦意見的「抬頭與承辦人欄」卡片不見了"
    assert 'id="odEndorseFrame"' in m.group(1) and "checked" not in m.group(1)
