"""會議摘要的「建議替換」：依會議背景找出逐字稿裡可能寫錯的專有名詞（v1.16.39）。

2026-10-02 使用者決定：會議背景的專有名詞要拿來修逐字稿的錯字。這裡驗工具這一側的接線：

* `/suggest-terms` 只建議、不改；
* 勾選的在 `/start` 套用，**原文留在 `orig_text`**；下一次沒勾的要換回原文；
* 換過什麼要跟著結果走：結果頁、匯出的文件、JSON 匯出傳回來都還在；
* 公開 API 做得到同一件事（`/api/term-suggestions` ＋ `/api/meeting-summary` 的 `replacements`）。

比對規則本身在 `tests/test_term_fix.py`。
"""
from __future__ import annotations

import io
import json
import time

import pytest

VTT = """WEBVTT

00:00:01.000 --> 00:00:06.000
<v S1>王曉明今天要報告第四季的預算。

00:00:06.500 --> 00:00:12.000
<v S2>報價我已經寄給 Bianka 了，Bianka 會再確認。

00:00:12.500 --> 00:00:18.000
<v S1>好，那就照原案走，月底前寄給法務。
"""
CTX = "與會者：王小明（財務長）、Bianca（PM）"
BASE = "/tools/meeting-summary"


class FakeClient:
    """假模型 —— 只驗我們自己的程式，回最小、合格的形狀。"""
    def text_query(self, prompt, model=None, **kw):
        if "你是會議記錄整理員" in prompt:
            return json.dumps({"decisions": [{"text": "照原案走", "segment_ids": [3]}],
                               "actions": [], "risks": [], "questions": []},
                              ensure_ascii=False)
        if "切成" in prompt and "章節" in prompt:
            return json.dumps({"chapters": [{"title": "預算", "start_seq": 1, "end_seq": 3}]},
                              ensure_ascii=False)
        if "三到五句" in prompt:
            return json.dumps({"summary": "會議決定照原案走。"}, ensure_ascii=False)
        return json.dumps({"keep": [1], "drop": [], "split": []})


@pytest.fixture
def llm(monkeypatch):
    from app.core import llm_settings as ls
    monkeypatch.setattr(ls.llm_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(ls.llm_settings, "make_client", lambda: FakeClient())
    monkeypatch.setattr(ls.llm_settings, "get_model_for", lambda _t: "fake")


def _upload(client) -> str:
    r = client.post(f"{BASE}/upload", files={"file": ("m.vtt", io.BytesIO(VTT.encode()),
                                                       "text/vtt")})
    assert r.status_code == 200, r.text
    return r.json()["upload_id"]


def _suggest(client, uid, ctx=CTX) -> dict:
    r = client.post(f"{BASE}/suggest-terms", json={"upload_id": uid, "context": ctx})
    assert r.status_code == 200, r.text
    return r.json()


def _analyse(client, uid, replacements) -> None:
    r = client.post(f"{BASE}/start", json={"upload_id": uid, "context": CTX,
                                           "with_impacts": "0",
                                           "replacements": replacements})
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    for _ in range(300):
        j = client.get(f"/api/jobs/{jid}").json()
        if j.get("status") in ("done", "error", "cancelled"):
            break
        time.sleep(0.05)
    assert j["status"] == "done", j.get("error")


def _segs(client, uid) -> list[dict]:
    return client.get(f"{BASE}/segments/{uid}").json()["segments"]


def test_suggestions_are_only_suggestions(client, auth_off):
    uid = _upload(client)
    before = _segs(client, uid)
    d = _suggest(client, uid)
    pairs = {(s["from"], s["to"]): s["count"] for s in d["suggestions"]}
    assert pairs == {("Bianka", "Bianca"): 2, ("王曉明", "王小明"): 1}, pairs
    assert d["applied"] == []
    assert _segs(client, uid) == before, "問建議不可以改到逐字稿"


def test_ticked_replacements_are_applied_with_the_original_kept(client, auth_off, llm):
    uid = _upload(client)
    _analyse(client, uid, [{"from": "Bianka", "to": "Bianca"}])
    segs = _segs(client, uid)
    assert segs[1]["text"] == "報價我已經寄給 Bianca 了，Bianca 會再確認。"
    assert segs[1]["orig_text"] == "報價我已經寄給 Bianka 了，Bianka 會再確認。"
    assert "orig_text" not in segs[0], "沒勾的不換"

    res = client.get(f"{BASE}/result/{uid}").json()
    assert res["replacements"] == [{"from": "Bianka", "to": "Bianca", "count": 2}]
    assert "replacements" not in res["source"], "換過什麼要跟著結果走，不混在來源資訊裡"

    md = client.get(f"{BASE}/download/{uid}?fmt=md").text
    assert "逐字稿換過這些寫法：Bianka → Bianca（2 處）" in md

    # 重新打開時預先勾回來；建議清單照原文比，不會因為換過而消失
    d = _suggest(client, uid)
    assert d["applied"] == [{"from": "Bianka", "to": "Bianca", "count": 2}]
    assert ("Bianka", "Bianca") in {(s["from"], s["to"]) for s in d["suggestions"]}


def test_untick_and_rerun_brings_the_original_back(client, auth_off, llm):
    uid = _upload(client)
    original = [s["text"] for s in _segs(client, uid)]
    _analyse(client, uid, [{"from": "Bianka", "to": "Bianca"},
                           {"from": "王曉明", "to": "王小明"}])
    _analyse(client, uid, [])
    segs = _segs(client, uid)
    assert [s["text"] for s in segs] == original
    assert not any("orig_text" in s for s in segs)
    assert "replacements" not in client.get(f"{BASE}/result/{uid}").json()
    assert _suggest(client, uid)["applied"] == []


def test_the_json_export_round_trips_the_replacements(client, auth_off, llm):
    uid = _upload(client)
    _analyse(client, uid, [{"from": "Bianka", "to": "Bianca"}])
    exp = client.get(f"{BASE}/download/{uid}?fmt=json")
    assert exp.status_code == 200
    data = exp.json()
    assert data["replacements"] == [{"from": "Bianka", "to": "Bianca", "count": 2}]
    assert data["segments"][1]["orig_text"].startswith("報價我已經寄給 Bianka")

    r = client.post(f"{BASE}/upload", files={"file": ("x.json", io.BytesIO(exp.content),
                                                      "application/json")})
    assert r.status_code == 200 and r.json().get("imported"), r.text
    new = r.json()["upload_id"]
    assert client.get(f"{BASE}/result/{new}").json()["replacements"] == data["replacements"]
    assert _segs(client, new)[1]["orig_text"] == data["segments"][1]["orig_text"]
    assert _suggest(client, new)["applied"] == data["replacements"]


def test_the_result_page_says_what_was_replaced():
    import pathlib
    src = pathlib.Path("app/tools/meeting_summary/templates/meeting_summary.html").read_text(
        encoding="utf-8")
    assert 'id="msTermFix"' in src and "/suggest-terms" in src
    assert "replacements: manualPairs().concat(checkedReplacements())" in src, "勾選的沒有送給 /start"
    assert "s.orig_text" in src and 'id="msReplNote"' in src, "結果頁沒有講換過哪些、看不到原文"


# ------------------------------------------------------------------ 公開 API

def test_the_api_suggests_without_storing(client, auth_off):
    r = client.post(f"{BASE}/api/term-suggestions",
                    files={"file": ("m.vtt", io.BytesIO(VTT.encode()), "text/vtt")},
                    data={"context": CTX})
    assert r.status_code == 200, r.text
    assert ("Bianka", "Bianca") in {(s["from"], s["to"]) for s in r.json()["suggestions"]}


def test_the_api_applies_replacements_before_the_analysis(client, auth_off, llm):
    r = client.post(f"{BASE}/api/meeting-summary",
                    files={"file": ("m.vtt", io.BytesIO(VTT.encode()), "text/vtt")},
                    data={"context": CTX,
                          "replacements": json.dumps([{"from": "Bianka", "to": "Bianca"}])})
    assert r.status_code == 200, r.text
    assert r.json()["replacements"] == [{"from": "Bianka", "to": "Bianca", "count": 2}]


@pytest.mark.parametrize("bad", ["not json", '{"from": "a", "to": "b"}'])
def test_the_api_refuses_malformed_replacements(client, auth_off, llm, bad):
    """寫錯回 400 —— 安靜地不換的話，呼叫端會以為換過了。"""
    r = client.post(f"{BASE}/api/meeting-summary",
                    files={"file": ("m.vtt", io.BytesIO(VTT.encode()), "text/vtt")},
                    data={"replacements": bad})
    assert r.status_code == 400, r.text


# ------------------------------------------------------------ 自己加替換（v1.16.45）
# 2026-10-03：辨識聽錯、建議抓不到的（Groxmoxity → Proxmox），使用者自己指定。
# `/find-term` 只查不改；勾著的跟建議一起送給 `/start`；自己打的那幾列記住，重新打開時畫回來。

VTT_MANUAL = """WEBVTT

00:00:01.000 --> 00:00:06.000
<v S1>這台 Groxmoxity 叢集要升級，簡報用 POWPOYNT 做。

00:00:06.500 --> 00:00:12.000
<v S2>groxmoxity 的備份也一起做，月底前寄給法務。

00:00:12.500 --> 00:00:18.000
<v S1>好，那就照原案走。
"""


def _upload_manual(client) -> str:
    r = client.post(f"{BASE}/upload", files={"file": ("m.vtt", io.BytesIO(VTT_MANUAL.encode()),
                                                       "text/vtt")})
    assert r.status_code == 200, r.text
    return r.json()["upload_id"]


def test_find_term_only_looks(client, auth_off):
    uid = _upload_manual(client)
    before = _segs(client, uid)
    r = client.post(f"{BASE}/find-term", json={"upload_id": uid, "from": "GROXMOXITY"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["count"] == 2 and set(d["variants"]) == {"Groxmoxity", "groxmoxity"}, d
    assert "Groxmoxity" in d["example"]
    assert client.post(f"{BASE}/find-term",
                       json={"upload_id": uid, "from": "Proxmox"}).json()["count"] == 0
    assert _segs(client, uid) == before, "查詢不可以改到逐字稿"


@pytest.mark.parametrize("uid", ["../../etc", "", "x" * 32])
def test_find_term_validates_the_id(client, auth_off, uid):
    r = client.post(f"{BASE}/find-term", json={"upload_id": uid, "from": "Groxmoxity"})
    assert r.status_code in (400, 404), r.status_code


def test_find_term_is_owner_only(admin_session):
    """查詢會回一段上下文（逐字稿原文）—— 拿別人的上傳編號不可以查得到。"""
    from tests.test_authz_boundaries import _user_client
    from app.core import user_manager
    ua, ca = _user_client("ms_mr_alice")
    ub, cb = _user_client("ms_mr_bob")
    try:
        uid = _upload_manual(ca)
        assert ca.post(f"{BASE}/find-term",
                       json={"upload_id": uid, "from": "Groxmoxity"}).json()["count"] == 2
        r = cb.post(f"{BASE}/find-term", json={"upload_id": uid, "from": "Groxmoxity"})
        assert r.status_code in (403, 404), r.status_code
        assert "Groxmoxity" not in r.text
    finally:
        user_manager.delete(ua)
        user_manager.delete(ub)


def _analyse_manual(client, uid, pairs, rows) -> None:
    r = client.post(f"{BASE}/start", json={"upload_id": uid, "with_impacts": "0",
                                           "replacements": pairs,
                                           "manual_replacements": rows})
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    for _ in range(300):
        j = client.get(f"/api/jobs/{jid}").json()
        if j.get("status") in ("done", "error", "cancelled"):
            break
        time.sleep(0.05)
    assert j["status"] == "done", j.get("error")


def test_manual_rows_are_applied_and_remembered(client, auth_off, llm):
    uid = _upload_manual(client)
    _analyse_manual(client, uid,
                    [{"from": "Groxmoxity", "to": "Proxmox"}, {"from": "groxmoxity", "to": "Proxmox"},
                     {"from": "POWPOYNT", "to": "PowerPoint"}],
                    [{"from": "groxmoxity", "to": "Proxmox"}, {"from": "POWPOYNT", "to": "PowerPoint"}])
    segs = _segs(client, uid)
    assert segs[0]["text"] == "這台 Proxmox 叢集要升級，簡報用 PowerPoint 做。"
    assert segs[0]["orig_text"].startswith("這台 Groxmoxity")
    assert segs[1]["text"].startswith("Proxmox 的備份")

    info = client.get(f"{BASE}/segments/{uid}").json()["info"]
    assert [(r["from"], r["to"]) for r in info["manual_replacements"]] == [
        ("groxmoxity", "Proxmox"), ("POWPOYNT", "PowerPoint")], (
        "自己打的那幾列沒有照加入的順序記住 —— 從「我的作業」打開再按一次分析，會被換回原文")

    md = client.get(f"{BASE}/download/{uid}?fmt=md").text
    assert "逐字稿換過這些寫法：" in md and "POWPOYNT → PowerPoint（1 處）" in md, md[:600]

    # 下一次沒帶 → 換回原文、清單也清掉
    _analyse_manual(client, uid, [], [])
    assert _segs(client, uid)[0]["text"].startswith("這台 Groxmoxity")
    assert client.get(f"{BASE}/segments/{uid}").json()["info"]["manual_replacements"] == []


def test_a_manual_row_wins_over_a_suggestion_for_the_same_spelling(client, auth_off, llm):
    """同一個寫法兩邊都有（建議 Bianka → Bianca、自己打 Bianka → Joan）：自己打的是明確的指定，
    要贏 —— 前端把自己加的排在前面，伺服器取第一個。"""
    uid = _upload(client)
    _analyse(client, uid, [{"from": "Bianka", "to": "Joan"}, {"from": "Bianka", "to": "Bianca"}])
    assert "Joan 了，Joan 會" in _segs(client, uid)[1]["text"]
