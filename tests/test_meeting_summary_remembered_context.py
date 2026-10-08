"""會議摘要：同一份逐字稿再分析時帶入上一次的會議背景 ＋ 作業的結果檔是完整版（2026-10-03 使用者要求）。

① **記住背景**：「同一份」只看說話的內容（發言者名字、時間、斷段方式都不算），
   每個人各存一份，送出分析時記下、背景是空的就清掉、畫面上的「清除」走 `/forget-context`。
   判準落在**上傳的回應真的帶回那一段**，而且**別人上傳同一份拿不到**。
② **完整結果檔**：「我的作業」下載、自動存進工作區的那一份原本不含逐字稿 ——
   傳回來打得開、引用卻點不到原文。現在作業的結果檔就是「下載 JSON」那一份。
   判準走真的路：作業跑完 → 結果檔存進工作區 → 取回 → 傳回會議摘要 → **有逐字稿**。
"""
from __future__ import annotations

import io
import json
import os
import time

import pytest

from app.core import meeting_context_memory as mcm
from app.core import transcript_parse as tp

VTT = """WEBVTT

00:00:01.000 --> 00:00:06.000
<v 王小明>各位早，今天只談一件事：第四季的預算。

00:00:06.500 --> 00:00:12.000
<v 李美華>我看過草案了，行銷那一塊超出去兩百萬。

00:00:12.500 --> 00:00:18.000
<v 王小明>那就照原案走，行銷不加。李美華月底前把修訂版寄給法務。

00:00:18.500 --> 00:00:20.000
<v 李美華>好。
"""

#: 同一場會議，換成轉逐字稿「存至工作區 / 複製純文字」的樣子，而且改過名字。
#: **每位發言者至少說兩次** —— 只出現一次的名字本來就不認成發言者（避免把句子裡的冒號
#: 當名字），素材太小的話純文字那份的第二位會被當成內文，指紋當然對不上。
PLAIN = """[00:01] 財務長：各位早，今天只談一件事：第四季的預算。
[00:06] 行銷經理：我看過草案了，行銷那一塊超出去兩百萬。
[00:12] 財務長：那就照原案走，行銷不加。李美華月底前把修訂版寄給法務。
[00:18] 行銷經理：好。
"""

CTX = "會議主題：第四季預算\n與會者：王小明（財務長）、李美華（行銷經理）"


# ------------------------------------------------------------------ 指紋

def test_the_fingerprint_only_looks_at_what_was_said():
    a = [{"seq": 1, "speaker": "S1", "start_ms": 0, "text": "各位早，今天只談一件事：第四季的預算。"},
         {"seq": 2, "speaker": "S2", "start_ms": 5000, "text": "我看過草案了，行銷那一塊超出去兩百萬。"}]
    renamed = [dict(s, speaker="王小明") for s in a]
    retimed = [dict(s, start_ms=s["start_ms"] + 999) for s in a]
    merged = [{"seq": 1, "speaker": "S1",
               "text": "各位早，今天只談一件事：第四季的預算。\n 我看過草案了，行銷那一塊超出去兩百萬。"}]
    fp = mcm.fingerprint(a)
    assert fp and fp == mcm.fingerprint(renamed) == mcm.fingerprint(retimed) == mcm.fingerprint(merged)
    other = [dict(a[0]), dict(a[1], text="我看過草案了，行銷那一塊超出去三百萬。")]
    assert mcm.fingerprint(other) != fp, "說的話不一樣卻算成同一份"


def test_replaced_terms_are_compared_by_their_original_text():
    """依會議背景替換過錯字的段落帶著 `orig_text` —— 替換是那次分析的選擇，不是另一份逐字稿。"""
    a = [{"seq": 1, "text": "我們這次要上 Proxmocks 的新版本，下週二晚上停機。"}]
    b = [{"seq": 1, "text": "我們這次要上 Proxmox 的新版本，下週二晚上停機。",
          "orig_text": "我們這次要上 Proxmocks 的新版本，下週二晚上停機。"}]
    assert mcm.fingerprint(a) == mcm.fingerprint(b)


def test_a_transcript_too_short_is_not_remembered():
    assert mcm.fingerprint([{"text": "好。"}, {"text": "嗯。"}]) is None


def test_the_same_meeting_in_another_format_is_the_same_transcript():
    """從轉逐字稿送過來的 JSON、存進工作區的純文字（改過名字）—— 是同一份。
    只比整份檔案的話這兩種一律對不上。"""
    segs_vtt, _ = tp.parse(VTT.encode(), "m.vtt", "auto")
    segs_txt, _ = tp.parse(PLAIN.encode(), "m.txt", "auto")
    assert [s["speaker"] for s in segs_txt][:2] == ["財務長", "行銷經理"], "前提：純文字的發言者要認得出來"
    assert mcm.fingerprint(segs_vtt) == mcm.fingerprint(segs_txt)


# ------------------------------------------------------------------ 存放

def test_remember_recall_forget_and_empty_means_forget():
    fp = "f" * 64
    mcm.forget(None, fp)
    assert mcm.recall(None, fp) is None
    mcm.remember(None, fp, CTX)
    got = mcm.recall(None, fp)
    assert got and got["context"] == CTX and got["saved_at"]
    mcm.remember(None, fp, "   ")
    assert mcm.recall(None, fp) is None, "背景清空再分析，代表不要那一段了"
    # 不只是「查不到」—— 檔案裡也不可以留一筆空的（不然那份逐字稿分析過這件事還記著）
    assert mcm.forget(None, fp) is False, "清空之後檔案裡還留著那一筆"
    mcm.remember(None, fp, CTX)
    assert mcm.forget(None, fp) is True and mcm.recall(None, fp) is None
    assert mcm.forget(None, fp) is False


def test_each_person_has_their_own():
    fp = "e" * 64
    mcm.remember(9001, fp, "甲的背景")
    mcm.remember(9002, fp, "乙的背景")
    try:
        assert mcm.recall(9001, fp)["context"] == "甲的背景"
        assert mcm.recall(9002, fp)["context"] == "乙的背景"
        assert mcm.recall(None, fp) is None or mcm.recall(None, fp)["context"] not in ("甲的背景", "乙的背景")
        mcm.purge_user(9001)
        assert mcm.recall(9001, fp) is None, "刪帳號之後還留著那個人記住的背景"
        assert mcm.recall(9002, fp)["context"] == "乙的背景", "刪一個人把別人的也刪了"
    finally:
        mcm.purge_user(9001)
        mcm.purge_user(9002)


def test_only_the_newest_entries_are_kept(monkeypatch):
    monkeypatch.setattr(mcm, "MAX_ENTRIES", 3)
    try:
        for i in range(5):
            mcm.remember(9003, f"{i:064x}", f"背景 {i}")
            time.sleep(0.01)
        kept = [i for i in range(5) if mcm.recall(9003, f"{i:064x}")]
        assert kept == [2, 3, 4], kept
    finally:
        mcm.purge_user(9003)


@pytest.mark.skipif(os.name != "posix", reason="權限位元是 POSIX 的概念")
def test_the_file_is_private():
    fp = "d" * 64
    mcm.remember(9004, fp, CTX)
    try:
        assert (mcm._file(9004).stat().st_mode & 0o777) == 0o600
    finally:
        mcm.purge_user(9004)


# ------------------------------------------------------------------ 端點

class _FakeClient:
    """假模型 —— 只驗我們自己的程式。"""
    def text_query(self, prompt, model=None, **kw):
        if "你是會議記錄整理員" in prompt:
            return json.dumps({"decisions": [{"text": "第四季行銷預算不加", "segment_ids": [3]}],
                               "actions": [], "risks": [], "questions": []}, ensure_ascii=False)
        if "切成" in prompt and "章節" in prompt:
            return json.dumps({"chapters": [{"title": "第四季預算", "start_seq": 1, "end_seq": 3}]},
                              ensure_ascii=False)
        if "三到五句" in prompt:
            return json.dumps({"summary": "會議確認第四季行銷預算不加。"}, ensure_ascii=False)
        return json.dumps({"keep": [1], "drop": [], "split": []}, ensure_ascii=False)


@pytest.fixture
def fake_llm(monkeypatch):
    from app.core import llm_settings as ls
    monkeypatch.setattr(ls.llm_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(ls.llm_settings, "make_client", lambda *a, **k: _FakeClient())
    monkeypatch.setattr(ls.llm_settings, "get_model_for", lambda _t: "fake")


def _upload(c, data=VTT, name="meeting.vtt"):
    r = c.post("/tools/meeting-summary/upload",
               files={"file": (name, io.BytesIO(data.encode()), "text/plain")})
    assert r.status_code == 200, r.text
    return r.json()


def _start(c, uid, context=""):
    from app.core.job_manager import job_manager
    r = c.post("/tools/meeting-summary/start", json={"upload_id": uid, "context": context})
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    for _ in range(400):
        j = job_manager.get(jid)
        if j and j.status in ("done", "error"):
            break
        time.sleep(0.05)
    j = job_manager.get(jid)
    assert j.status == "done", getattr(j, "error", None)
    return j


def _clear_anon(segs_source=VTT, name="m.vtt"):
    segs, _ = tp.parse(segs_source.encode(), name, "auto")
    mcm.forget(None, mcm.fingerprint(segs))


def test_the_background_comes_back_when_the_same_transcript_is_uploaded(client, auth_off, fake_llm):
    _clear_anon()
    first = _upload(client)
    assert "remembered_context" not in first, "還沒分析過就帶出背景"
    _start(client, first["upload_id"], CTX)

    again = _upload(client)
    assert (again.get("remembered_context") or {}).get("context") == CTX, again.get("remembered_context")
    # 換成純文字、改過名字的同一場會議也認得
    plain = _upload(client, PLAIN, "會議-逐字稿.txt")
    assert (plain.get("remembered_context") or {}).get("context") == CTX
    # 另一場會議不可以帶出這一段
    other = _upload(client, VTT.replace("兩百萬", "五百萬"), "other.vtt")
    assert "remembered_context" not in other, "不同的逐字稿帶出了別場會議的背景"
    _clear_anon()


def test_analysing_with_an_empty_background_forgets_it(client, auth_off, fake_llm):
    _clear_anon()
    _start(client, _upload(client)["upload_id"], CTX)
    _start(client, _upload(client)["upload_id"], "")
    assert "remembered_context" not in _upload(client), "背景清空再分析之後，下一次還是帶出舊的"


def test_the_clear_button_forgets_it(client, auth_off, fake_llm):
    _clear_anon()
    _start(client, _upload(client)["upload_id"], CTX)
    up = _upload(client)
    assert up.get("remembered_context")
    r = client.post("/tools/meeting-summary/forget-context", json={"upload_id": up["upload_id"]})
    assert r.status_code == 200 and r.json()["forgot"] is True, r.text
    assert "remembered_context" not in _upload(client)


def test_the_clear_endpoint_only_takes_an_upload_id(client, auth_off):
    """收指紋的話，任何人送一個指紋就能試探別人分析過什麼 —— 只收上傳編號。"""
    r = client.post("/tools/meeting-summary/forget-context", json={"upload_id": "../../etc"})
    assert r.status_code == 400
    r = client.post("/tools/meeting-summary/forget-context", json={"fingerprint": "a" * 64})
    assert r.status_code == 400


def test_another_person_does_not_get_my_background(admin_session, fake_llm):
    """兩個帳號上傳同一份逐字稿：乙拿不到甲填的背景（裡面常有與會者姓名職稱）。"""
    from tests.test_authz_boundaries import _user_client
    from app.core import user_manager
    ua, ca = _user_client("ms_ctx_alice")
    ub, cb = _user_client("ms_ctx_bob")
    try:
        _start(ca, _upload(ca)["upload_id"], CTX)
        assert (_upload(ca).get("remembered_context") or {}).get("context") == CTX
        assert "remembered_context" not in _upload(cb), "乙拿到了甲填的會議背景"
        # 乙也不可以清掉甲的（拿甲的上傳編號會被歸屬擋下）
        up_a = _upload(ca)["upload_id"]
        r = cb.post("/tools/meeting-summary/forget-context", json={"upload_id": up_a})
        assert r.status_code in (403, 404), r.status_code
        assert (_upload(ca).get("remembered_context") or {}).get("context") == CTX
    finally:
        user_manager.delete(ua)
        user_manager.delete(ub)
    assert mcm.recall(ua, mcm.fingerprint(tp.parse(VTT.encode(), "m.vtt", "auto")[0])) is None, (
        "刪帳號之後還留著那個人記住的背景")


# ------------------------------------------------------------------ 完整結果檔

def test_the_job_result_is_the_full_export(client, auth_off, fake_llm):
    """「我的作業」下載、自動存進工作區的那一份 = 「下載 JSON」那一份（帶逐字稿）。

    走真的路：結果檔存進工作區 → 取回 → 傳回會議摘要 → 引用要有原文。"""
    up = _upload(client)
    j = _start(client, up["upload_id"], CTX)
    data = json.loads(j.result_path.read_text(encoding="utf-8"))
    assert data.get("format") == "jtdt-meeting-summary", "結果檔沒有格式標記，傳回來認不出來"
    assert len(data.get("segments") or []) == 4, "結果檔沒有逐字稿，引用點不到原文"
    assert data.get("context") == CTX
    assert j.result_filename.endswith(".json")

    from tests.test_meeting_summary_workspace_json import _through_the_workspace
    r = _through_the_workspace(client, j.result_path.read_bytes(), j.result_filename,
                               "meeting-summary")
    assert r.status_code == 200, r.text
    got = r.json()
    assert got.get("imported") is True and got.get("has_transcript") is True, got


def test_renaming_a_speaker_rewrites_the_job_result(client, auth_off, fake_llm):
    """改了名字之後，「我的作業」下載到的那一份也要是新名字。"""
    up = _upload(client)
    j = _start(client, up["upload_id"])
    r = client.post(f"/tools/meeting-summary/speakers/{up['upload_id']}",
                    json={"map": {"王小明": "王財務長"}})
    assert r.status_code == 200, r.text
    data = json.loads(j.result_path.read_text(encoding="utf-8"))
    speakers = {s.get("speaker") for s in data.get("segments") or []}
    assert "王財務長" in speakers and "王小明" not in speakers, speakers


# ------------------------------------------------ ③ 轉逐字稿帶來的「專有名詞或會議背景」

#: 轉逐字稿存出來的 JSON 形狀（`segments` ＋ 使用者在「專有名詞或會議背景」寫的原文）
_TRANSCRIBED = {
    "segments": [
        {"seq": 1, "speaker": "S1", "start_ms": 1000, "end_ms": 6000,
         "text": "各位早，今天只談一件事：第四季的預算。"},
        {"seq": 2, "speaker": "S2", "start_ms": 6500, "end_ms": 12000,
         "text": "我看過草案了，行銷那一塊超出去兩百萬。"},
        {"seq": 3, "speaker": "S1", "start_ms": 12500, "end_ms": 18000,
         "text": "那就照原案走，行銷不加。"},
        {"seq": 4, "speaker": "S2", "start_ms": 18500, "end_ms": 20000, "text": "好。"},
    ],
    "context": "第四季預算會議。\n與會者：王小明、李美華",
}


def test_the_text_entered_when_transcribing_comes_along(client, auth_off):
    """轉送會議摘要時，轉逐字稿填的「專有名詞或會議背景」要帶進會議背景（2026-10-03 使用者要求）。"""
    got = _upload(client, json.dumps(_TRANSCRIBED, ensure_ascii=False), "會議-逐字稿.json")
    assert got.get("transcript_context") == _TRANSCRIBED["context"], got.get("transcript_context")
    # 逐字稿本身照常解析 —— 這一欄不可以影響段落
    assert got["segments"] == 4


def test_it_comes_along_through_the_workspace_too(client, auth_off):
    """經工作區中轉時檔名會變成 `.txt` —— 一樣讀得到（看內容不看副檔名）。"""
    from tests.test_meeting_summary_workspace_json import _through_the_workspace
    body = ("﻿" + json.dumps(_TRANSCRIBED, ensure_ascii=False)).encode("utf-8")
    r = _through_the_workspace(client, body, "會議-逐字稿.txt", "meeting-transcribe")
    assert r.status_code == 200, r.text
    assert r.json().get("transcript_context") == _TRANSCRIBED["context"]


@pytest.mark.parametrize("ctx", [None, "", 123, ["x"]])
def test_no_or_odd_context_brings_nothing(client, auth_off, ctx):
    obj = dict(_TRANSCRIBED)
    if ctx is None:
        obj.pop("context")
    else:
        obj["context"] = ctx
    got = _upload(client, json.dumps(obj, ensure_ascii=False), "t.json")
    assert "transcript_context" not in got


def test_control_characters_are_dropped_and_length_is_capped(client, auth_off):
    from app.core import meeting_insight as mi
    obj = dict(_TRANSCRIBED, context="A\x07B\n" + "長" * (mi.MAX_CONTEXT_CHARS + 50))
    got = _upload(client, json.dumps(obj, ensure_ascii=False), "t.json")
    tc = got["transcript_context"]
    assert tc.startswith("AB\n") and len(tc) == mi.MAX_CONTEXT_CHARS
