"""會議摘要的「自己加替換」送回轉逐字稿那件作業（v1.16.66；JTLW `variants`，`api_revision` 2.9）。

會議摘要裡加的「聽錯的寫法 → 正確寫法」只換得到會議摘要自己那一份逐字稿。這份逐字稿是
「會議錄音轉逐字稿」轉的、那件作業又還在延後 ACK 的保留時間內的話，可以當成已知的錯寫法
送回去、請 JTLW 只重跑校正 —— 轉逐字稿那邊下載與存進工作區的那一份也一起改好。

這裡驗的是那幾條不可以放鬆的（`app/tools/meeting_transcribe/resend.py` 的說明）：

* **整份送**：那件作業原本的專有名詞與錯寫法都要在（JTLW 的 retry 是取代不是累加）；
* **送之前先擋**：寫得不對的替換回 400、講出是哪一條，**一個請求都不送出去**；
* **使用者送來的作業編號只是提示**：別人的作業（管理員也一樣）、已經請 JTLW 刪除的、
  沒有校正的、離刪除不到 15 分鐘的、正在重跑的、對方版本太舊的，一律不送，而且講得出為什麼；
* 送回去之後，會議摘要這一份逐字稿不受影響（原本的「自己加替換」照舊）。

假的 JTLW 照對方的契約（`tests/test_meeting_transcribe.FakeJtlw`）。範例裡的錯寫法與名字都是編的。
"""
from __future__ import annotations

import importlib
import io
import json
import time

import pytest

from app.core import jtlw_ack
from tests.test_meeting_transcribe import (   # noqa: F401  （`unconfigured` 是 fixture）
    FakeJtlw, _configure, _result, _run, _upload, unconfigured,
)

MS = "/tools/meeting-summary"
MT = "/tools/meeting-transcribe"
_mt = importlib.import_module("app.tools.meeting_transcribe.router")
_rs = importlib.import_module("app.tools.meeting_transcribe.resend")

VTT = """WEBVTT

00:00:01.000 --> 00:00:06.000
<v 王小明>各位早，今天只談 Proksmox 叢集。

00:00:06.500 --> 00:00:12.000
<v 李美華>Proksmox 的備份月底前做完。
"""


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    """輪詢縮短；「收不收錯寫法」的快取每條清掉（假服務換測試會重用埠號）。"""
    monkeypatch.setattr(_mt, "_POLL_FIRST", 0.1)
    monkeypatch.setattr(_mt, "_POLL_MAX", 0.3)
    _rs._rev_cache.clear()
    yield
    _rs._rev_cache.clear()


def _wait(client, job_id: str, timeout: float = 60.0) -> dict:
    t0 = time.time()
    j = {}
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j.get("status") in ("done", "error", "cancelled"):
            return j
        time.sleep(0.2)
    raise AssertionError(f"作業沒有結束：{j}")


def _transcribe(client, fake, terms: str = "", **cfg) -> tuple[str, dict]:
    """轉一件，回 (轉逐字稿的 upload_id, 結果頁拿到的那一份 JSON —— 就是轉送會議摘要的那一份)。"""
    _configure(fake, **cfg)
    up = _upload(client)
    j = _run(client, up["upload_id"], **({"terms": terms} if terms else {}))
    assert j["status"] == "done", j.get("error")
    return up["upload_id"], _result(client, up["upload_id"])


def _to_summary(client, data, name: str = "會議-逐字稿.txt") -> dict:
    """轉送會議摘要走工作區中轉，檔名會變成 `.txt` —— 照那個樣子送。"""
    blob = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
    r = client.post(f"{MS}/upload", files={"file": (name, io.BytesIO(blob), "text/plain")})
    assert r.status_code == 200, r.text
    return r.json()


def _state(client, uid: str) -> dict:
    r = client.get(f"{MS}/resend-variants/{uid}")
    assert r.status_code == 200, r.text
    return r.json()


def _send(client, uid: str, rows):
    return client.post(f"{MS}/resend-variants", json={"upload_id": uid, "rows": rows})


# ------------------------------------------------------------------ 送得回去

def test_my_replacements_go_back_with_the_whole_glossary(client, unconfigured):
    """原本的專有名詞與錯寫法都在、新的接上去；錯寫法用逐字稿裡**實際的寫法**；
    轉逐字稿那一份換好了、換了幾處講得出來；會議摘要這一份不受影響。"""
    with FakeJtlw(api_revision="2.9", heard="Proksmox PPQ", duration_ms=60000) as fake:
        mt_uid, data = _transcribe(client, fake, terms="嘉禾科技\nProxmux → Proxmox")
        due = jtlw_ack.due_at("job_fake_1")
        up = _to_summary(client, data)
        assert up["from_transcribe"] is True, "轉逐字稿送來的逐字稿沒有認出來"
        st = _state(client, up["upload_id"])
        assert st["available"] is True and st["possible"] is True, st
        assert abs(st["due_at"] - due) < 1

        r = _send(client, up["upload_id"], [{"from": "proksmox", "to": "Proxmox"},
                                            {"from": "PPQ", "to": "PPT"}])
        assert r.status_code == 200, r.text
        j = _wait(client, r.json()["job_id"])
        assert j["status"] == "done", j.get("error")

        assert len(fake.retries) == 1
        assert fake.retries[0]["glossary"]["entries"] == [
            {"source": "嘉禾科技", "mode": "keep"},
            # 原本的錯寫法還在（漏帶的話對方那邊就不見了），新的接在後面；
            # 使用者打的是小寫，送的是逐字稿裡實際的寫法
            {"source": "Proxmox", "mode": "keep", "variants": ["Proxmux", "Proksmox"]},
            {"source": "PPT", "mode": "keep", "variants": ["PPQ"]},
        ], fake.retries[0]
        assert fake.acked == [], "送回去重跑完就 ACK 了 —— 使用者還可以再改"
        assert jtlw_ack.due_at("job_fake_1") == due, "送回去不可以把保留時間往後延"

    after = _result(client, mt_uid)
    texts = [s["text"] for s in after["segments"]]
    assert all("Proxmox" in t and "PPT" in t for t in texts), texts
    assert not any("Proksmox" in t or "PPQ" in t for t in texts), texts
    assert after["variants"] == {"Proxmox": ["Proxmux", "Proksmox"], "PPT": ["PPQ"]}
    assert after["variants_sent"] is True

    st = _state(client, up["upload_id"])
    assert st["last"]["variant_replacements"] == 10, st
    assert st["last"]["variants_sent"] is True and st["last"]["count"] == 1
    # 會議摘要這一份是使用者自己的，**不跟著換**（要換照舊用「自己加替換」）
    segs = client.get(f"{MS}/segments/{up['upload_id']}").json()["segments"]
    assert all("Proksmox" in s["text"] for s in segs), "會議摘要自己的逐字稿被動到了"


def test_the_transcript_came_through_the_workspace_as_txt_and_still_counts(client, unconfigured):
    """轉送是經工作區中轉的，存進去就是 `.txt`、開頭可能有 BOM —— 照樣認得出來。"""
    with FakeJtlw(api_revision="2.9", heard="Proksmox") as fake:
        _, data = _transcribe(client, fake)
        blob = b"\xef\xbb\xbf" + json.dumps(data, ensure_ascii=False).encode()
        up = _to_summary(client, blob, "會議-逐字稿.txt")
        assert up["from_transcribe"] is True
        assert _state(client, up["upload_id"])["possible"] is True


# ------------------------------------------------------------------ 送之前先擋

@pytest.mark.parametrize("rows,says", [
    ([{"from": "PPQ", "to": "PPT"}], "也是清單上的專有名詞"),
    ([{"from": "Groxmoxity", "to": "Proxmox"}], "找不到「Groxmoxity」"),
    ([{"from": "Proksmox", "to": "Proxmox / PVE"}], "只能寫一個正確的寫法"),
    ([{"from": "Prok→smox", "to": "Proxmox"}], "分隔符號、箭頭或句號"),
    ([{"from": "Proksmox、PPQ", "to": "Proxmox"}], "分隔符號、箭頭或句號"),
    ([{"from": "Proksmox", "to": "Proxmox。"}], "箭頭或句號"),
    ([{"from": "Proksmox", "to": "Proksmox"}], "不能送"),
    ([{"from": "Proksmox", "to": "P"}], "至少要兩個字"),
    ([{"from": "Proksmox", "to": "Proxmox"}, {"from": "proksmox", "to": "Proxmux"}],
     "不知道要換成哪一個"),
    ([], "請先在「自己加替換」加一條"),
])
def test_bad_rows_are_refused_before_anything_is_sent(client, unconfigured, rows, says):
    """擋在這裡才講得出是哪一條；送出去才被退回的話，使用者只看到「重跑失敗」。"""
    with FakeJtlw(api_revision="2.9", heard="Proksmox PPQ") as fake:
        _, data = _transcribe(client, fake, terms="PPQ")
        up = _to_summary(client, data)
        r = _send(client, up["upload_id"], rows)
        assert r.status_code == 400, r.text
        assert says in r.json()["detail"], r.json()["detail"]
        assert fake.retries == [], "寫錯的替換還是送出去了"


def test_a_row_that_would_silently_drop_out_is_refused(client, unconfigured, monkeypatch):
    """組完之後逐條確認真的在送出去的清單上 —— 規則哪天變了、某一條被當成背景的話，
    要講出來，不可以送一份少了一條的出去（使用者會以為送了）。"""
    with FakeJtlw(api_revision="2.9", heard="Proksmox") as fake:
        _, data = _transcribe(client, fake)
        up = _to_summary(client, data)
        real = _mt.parse_glossary
        monkeypatch.setattr(_mt, "parse_glossary",
                            lambda lines: (real(lines)[0], {}))   # 錯寫法全掉了
        r = _send(client, up["upload_id"], [{"from": "Proksmox", "to": "Proxmox"}])
        assert r.status_code == 400, r.text
        assert "沒辦法照 JTLW 的規則送出" in r.json()["detail"]
        assert fake.retries == []


# ------------------------------------------------------------------ 送不回去的情況

def test_a_transcript_not_from_the_transcribe_tool_has_nothing_to_send_back(client, unconfigured):
    with FakeJtlw(api_revision="2.9") as fake:
        _configure(fake)
        up = _to_summary(client, VTT.encode(), "meeting.vtt")
        assert up["from_transcribe"] is False
        assert _state(client, up["upload_id"]) == {"available": False}
        r = _send(client, up["upload_id"], [{"from": "Proksmox", "to": "Proxmox"}])
        assert r.status_code == 409 and "不是從" in r.json()["detail"], r.text
        assert fake.retries == []


@pytest.mark.parametrize("rid", ["../../etc/passwd", "job 1", "", 42, "j" * 200])
def test_an_odd_job_number_in_the_file_is_not_kept(client, unconfigured, rid):
    """作業編號是使用者上傳的檔案裡寫的 —— 形狀不對就當沒有（只收英數與少數符號）。"""
    up = _to_summary(client, {"remote_job_id": rid, "segments": [
        {"seq": 1, "text": "Proksmox 叢集", "speaker": "S1"}]})
    assert up["from_transcribe"] is False


def test_a_job_number_that_is_not_ours_is_gone(client, unconfigured):
    """檔案裡寫了一個不在延後 ACK 清單上的編號 —— 一律「不能送」，不打給 JTLW。"""
    with FakeJtlw(api_revision="2.9") as fake:
        _configure(fake)
        up = _to_summary(client, {"remote_job_id": "job_made_up_1", "segments": [
            {"seq": 1, "text": "Proksmox 叢集", "speaker": "S1"}]})
        st = _state(client, up["upload_id"])
        assert st["available"] is True and st["possible"] is False and st["reason"] == "gone", st
        r = _send(client, up["upload_id"], [{"from": "Proksmox", "to": "Proxmox"}])
        assert r.status_code == 409, r.text
        assert fake.retries == []


def test_after_done_it_cannot_be_sent(client, unconfigured):
    """轉逐字稿那邊按了「不用再改了」（JTLW 已經刪除它那一份）→ 講出來、不送。"""
    with FakeJtlw(api_revision="2.9", heard="Proksmox") as fake:
        mt_uid, data = _transcribe(client, fake)
        up = _to_summary(client, data)
        assert client.post(f"{MT}/done", json={"upload_id": mt_uid}).json()["acked"] is True
        st = _state(client, up["upload_id"])
        assert st["possible"] is False and st["reason"] == "gone", st
        assert st["message"] == _rs.REASONS["gone"]
        r = _send(client, up["upload_id"], [{"from": "Proksmox", "to": "Proxmox"}])
        assert r.status_code == 409 and r.json()["detail"] == _rs.REASONS["gone"]
        assert fake.retries == []


def test_without_correction_there_is_nothing_to_rerun(client, unconfigured):
    with FakeJtlw(api_revision="2.9", heard="Proksmox") as fake:
        _, data = _transcribe(client, fake, tasks=["transcribe", "diarize"])
        up = _to_summary(client, data)
        st = _state(client, up["upload_id"])
        assert st["possible"] is False and st["reason"] == "gone", st
        assert _send(client, up["upload_id"],
                     [{"from": "Proksmox", "to": "Proxmox"}]).status_code == 409
        assert fake.retries == []


def test_too_close_to_the_deletion_time(client, unconfigured):
    with FakeJtlw(api_revision="2.9", heard="Proksmox") as fake:
        mt_uid, data = _transcribe(client, fake)
        # 再過 1 分鐘就到期
        jtlw_ack.defer("job_fake_1", mt_uid, now=time.time() - 24 * 3600 + 60)
        up = _to_summary(client, data)
        st = _state(client, up["upload_id"])
        assert st["possible"] is False and st["reason"] == "too_late", st
        assert _send(client, up["upload_id"],
                     [{"from": "Proksmox", "to": "Proxmox"}]).status_code == 409
        assert fake.retries == []


def test_while_a_rerun_is_running_it_says_so_and_points_at_that_job(client, unconfigured):
    with FakeJtlw(api_revision="2.9", heard="Proksmox", retry_polls=200) as fake:
        mt_uid, data = _transcribe(client, fake)
        up = _to_summary(client, data)
        r = client.post(f"{MT}/retry", json={"upload_id": mt_uid, "terms": "嘉禾科技"})
        assert r.status_code == 200, r.text
        jid = r.json()["job_id"]
        try:
            for _ in range(100):                 # 等那一件真的送到對方（之後才比得出「有沒有多送」）
                if fake.retries:
                    break
                time.sleep(0.1)
            assert len(fake.retries) == 1, "轉逐字稿那邊的重跑沒有送出去"
            st = _state(client, up["upload_id"])
            assert st["possible"] is False and st["reason"] == "running", st
            assert st["job_id"] == jid, "正在重跑時要給那件作業的編號，畫面才接得上進度"
            r2 = _send(client, up["upload_id"], [{"from": "Proksmox", "to": "Proxmox"}])
            assert r2.status_code == 409, r2.text
            assert len(fake.retries) == 1, "正在重跑時又送了一次"
        finally:
            client.post(f"/api/jobs/{jid}/cancel")
            _wait(client, jid)


def test_an_older_voice_service_is_not_sent_anything(client, unconfigured):
    """2.9 以前的 JTLW 不收錯寫法 —— 送了只是白跑一次校正，所以不送、講出來。"""
    with FakeJtlw(api_revision="2.8", heard="Proksmox") as fake:
        _, data = _transcribe(client, fake)
        up = _to_summary(client, data)
        st = _state(client, up["upload_id"])
        assert st["possible"] is False and st["reason"] == "old_version", st
        r = _send(client, up["upload_id"], [{"from": "Proksmox", "to": "Proxmox"}])
        assert r.status_code == 409 and r.json()["detail"] == _rs.REASONS["old_version"]
        assert fake.retries == []


def test_when_the_version_cannot_be_read_it_says_unreachable_not_old(client, unconfigured):
    """連不上與「版本太舊」是兩件事 —— 前者稍後再試就好。"""
    with FakeJtlw(api_revision="2.9", heard="Proksmox") as fake:
        _, data = _transcribe(client, fake)
        up = _to_summary(client, data)
        fake.caps_status = 500
        _mt._last_revision.clear()
        st = _state(client, up["upload_id"])
        assert st["possible"] is False and st["reason"] == "unreachable", st
        r = _send(client, up["upload_id"], [{"from": "Proksmox", "to": "Proxmox"}])
        assert r.status_code == 503, r.text
        assert fake.retries == []


# ------------------------------------------------------------------ 是誰的作業

def test_someone_elses_transcript_cannot_be_sent_back(admin_session, unconfigured):
    """甲轉的逐字稿檔案給了乙：乙（還有管理員）都不可以拿它重跑甲的作業，
    回的話跟「已經刪除」一樣 —— 不說「那件是別人的」，免得拿編號試探別人的作業。"""
    from app.core import user_manager
    from tests.test_authz_boundaries import _user_client
    admin, _u, _p = admin_session
    ua, ca = _user_client("ms_resend_alice")
    ub, cb = _user_client("ms_resend_bob")
    try:
        with FakeJtlw(api_revision="2.9", heard="Proksmox") as fake:
            _, data = _transcribe(ca, fake)
            mine = _to_summary(ca, data)
            assert _state(ca, mine["upload_id"])["possible"] is True, "甲自己的反而不能送"
            for who in (cb, admin):
                theirs = _to_summary(who, data)
                st = _state(who, theirs["upload_id"])
                assert st["possible"] is False and st["reason"] == "gone", st
                r = _send(who, theirs["upload_id"], [{"from": "Proksmox", "to": "Proxmox"}])
                assert r.status_code == 409, r.text
                assert "last" not in st, "別人的作業的重跑結果被看到了"
            # 乙也不可以拿甲在會議摘要這邊的上傳編號來問
            assert cb.get(f"{MS}/resend-variants/{mine['upload_id']}").status_code in (403, 404)
            r = cb.post(f"{MS}/resend-variants", json={
                "upload_id": mine["upload_id"], "rows": [{"from": "Proksmox", "to": "Proxmox"}]})
            assert r.status_code in (403, 404), r.text
            assert fake.retries == [], "別人的作業被重跑了"
            # 甲自己送得出去（反向對照：上面那幾條擋下來不是因為什麼都擋）
            r = _send(ca, mine["upload_id"], [{"from": "Proksmox", "to": "Proxmox"}])
            assert r.status_code == 200, r.text
            assert _wait(ca, r.json()["job_id"])["status"] == "done"
            assert len(fake.retries) == 1
    finally:
        user_manager.delete(ua)
        user_manager.delete(ub)


def test_without_the_transcribe_permission_it_says_so(admin_session, unconfigured, monkeypatch):
    from app.core import permissions, user_manager
    from tests.test_authz_boundaries import _user_client
    ua, ca = _user_client("ms_resend_carol")
    try:
        with FakeJtlw(api_revision="2.9", heard="Proksmox") as fake:
            _, data = _transcribe(ca, fake)
            up = _to_summary(ca, data)
            real = permissions.user_can_use_tool
            monkeypatch.setattr(permissions, "user_can_use_tool",
                                lambda uid, tool: tool != "meeting-transcribe" and real(uid, tool))
            st = _state(ca, up["upload_id"])
            assert st["possible"] is False and st["reason"] == "no_permission", st
            r = _send(ca, up["upload_id"], [{"from": "Proksmox", "to": "Proxmox"}])
            assert r.status_code == 409, r.text
            assert fake.retries == []
    finally:
        user_manager.delete(ua)


# ------------------------------------------------------------------ 匯出再匯入

def test_an_exported_analysis_dropped_back_in_keeps_the_link(client, unconfigured, monkeypatch):
    """分析完匯出的 JSON 傳回來，那件轉逐字稿作業的編號跟著回來（照樣只是提示）。"""
    from app.core import llm_settings as ls

    class _Fake:
        def text_query(self, prompt, model=None, **kw):
            if "切成" in prompt and "章節" in prompt:
                return json.dumps({"chapters": [{"title": "叢集", "start_seq": 1, "end_seq": 5}]})
            if "三到五句" in prompt:
                return json.dumps({"summary": "談叢集。"}, ensure_ascii=False)
            return json.dumps({"decisions": [], "actions": [], "risks": [], "questions": [],
                               "keep": [], "drop": [], "split": []})

    monkeypatch.setattr(ls.llm_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(ls.llm_settings, "make_client", lambda *a, **k: _Fake())
    monkeypatch.setattr(ls.llm_settings, "get_model_for", lambda _t: "fake")
    with FakeJtlw(api_revision="2.9", heard="Proksmox") as fake:
        _, data = _transcribe(client, fake)
        up = _to_summary(client, data)
        r = client.post(f"{MS}/start", json={"upload_id": up["upload_id"], "with_impacts": "0"})
        assert r.status_code == 200, r.text
        assert _wait(client, r.json()["job_id"])["status"] == "done"
        exp = client.get(f"{MS}/download/{up['upload_id']}?fmt=json").content
        again = _to_summary(client, exp, "會議-會議摘要.json")
        assert again.get("imported") is True and again["from_transcribe"] is True, again
        assert _state(client, again["upload_id"])["possible"] is True
        info = client.get(f"{MS}/segments/{again['upload_id']}").json()["info"]
        assert info["from_transcribe"] is True
