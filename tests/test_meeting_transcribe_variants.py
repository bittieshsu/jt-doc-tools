"""轉逐字稿：已知的錯寫法照表換（JTLW v2.28，`api_revision` 2.9）。

「專有名詞或會議背景」裡寫一行「聽錯的寫法 → 正確寫法」，送件時放進那個詞的 `variants`，
對方在校正**之前**照表換掉 —— 確定性的、不經過模型、任何校正等級都做。拼法差很多的誤聽
（校正認不出來的）靠這個才改得回來。

這裡驗的是：
* 解析：箭頭的幾種寫法、一行好幾個錯寫法、重複的合併；**寫錯的行要講出來**（不可以安靜地當背景），
  對方的三條規則先在我們這邊擋（擋在這裡才講得出是哪一行）；
* 只對 2.9 以上的對方送（舊版的 `GlossaryEntry` 是 `additionalProperties: false`，送了整件被退回），
  送不出去時結果要講出來；
* 補專有名詞重跑校正時**整份**帶過去（對方的 retry 是取代不是累加，漏帶的話前一次寫的錯寫法就不見了）；
* 對方退回時的訊息講得出是哪一條規則。

範例裡的錯寫法與名字都是編的。
"""
from __future__ import annotations

import importlib
import json
import pathlib
import re
import shutil
import subprocess
import time

import pytest
from fastapi import HTTPException

from app.core import jtlw_client
from tests.test_meeting_transcribe import (   # noqa: F401  （`unconfigured` 是 fixture）
    FakeJtlw, _api, _configure, _result, _run, _upload, unconfigured,
)

BASE = "/tools/meeting-transcribe"
_mt = importlib.import_module("app.tools.meeting_transcribe.router")
TEMPLATE = pathlib.Path("app/tools/meeting_transcribe/templates/meeting_transcribe.html")


@pytest.fixture(autouse=True)
def _quick_polls(monkeypatch):
    monkeypatch.setattr(_mt, "_POLL_FIRST", 0.1)
    monkeypatch.setattr(_mt, "_POLL_MAX", 0.3)


def _wait(client, job_id: str, timeout: float = 60.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j.get("status") in ("done", "error", "cancelled"):
            return j
        time.sleep(0.2)
    raise AssertionError(f"作業沒有結束：{j}")


# ------------------------------------------------------------------ 解析

@pytest.mark.parametrize("raw,terms,variants", [
    # 一行好幾個錯寫法；詞照出現的順序
    ("王小明\nProksmox、Proxmux → Proxmox\nPVE",
     ["王小明", "Proxmox", "PVE"], {"Proxmox": ["Proksmox", "Proxmux"]}),
    # 箭頭的幾種寫法
    ("Proksmox -> Proxmox", ["Proxmox"], {"Proxmox": ["Proksmox"]}),
    ("Proksmox=>Proxmox", ["Proxmox"], {"Proxmox": ["Proksmox"]}),
    ("Proksmox ⇒ Proxmox", ["Proxmox"], {"Proxmox": ["Proksmox"]}),
    # 詞已經在清單上：不重複送、照第一次的寫法
    ("Proxmox\nProksmox → proxmox", ["Proxmox"], {"Proxmox": ["Proksmox"]}),
    # 同一個詞分兩行寫：合併、不分大小寫去重
    ("Proksmox → Proxmox\nproksmox、Proxmux → Proxmox",
     ["Proxmox"], {"Proxmox": ["Proksmox", "Proxmux"]}),
    # 中文的錯寫法照字面
    ("王曉明 → 王小明", ["王小明"], {"王小明": ["王曉明"]}),
    # 斜線沒有空白的是一個詞（TCP/IP）
    ("TCP-IP → TCP/IP", ["TCP/IP"], {"TCP/IP": ["TCP-IP"]}),
    # 兩個以上箭頭是流程 —— 背景，不可以整行當成一個詞
    ("上傳 → 轉檔 → 下載\nPVE", ["PVE"], {}),
    # `#` 標題與句子裡的箭頭也是背景
    ("# Proksmox → Proxmox\nPVE", ["PVE"], {}),
    ("今天把 Proksmox → Proxmox 改好了。\nPVE", ["PVE"], {}),
    # 沒有箭頭：跟原本一樣
    ("Bianca、Acme-Kevin", ["Bianca", "Acme-Kevin"], {}),
])
def test_the_arrow_lines_become_variants(raw, terms, variants):
    assert _mt.parse_glossary(raw) == (terms, variants)


@pytest.mark.parametrize("raw,word", [
    ("→ Proxmox", "兩邊都要有"),
    ("Proksmox →", "兩邊都要有"),
    # 右邊寫了好幾個（對方的 `variants_need_single_term`）
    ("Proksmox → Proxmox、PVE", "只能寫一個"),
    ("Proksmox → Proxmox VE / PVE", "只能寫一個"),
    # 太短（對方每個錯寫法 2~200 字）
    ("X → Proxmox", "2～200"),
    # 錯寫法剛好是清單上的詞（或就是自己）—— 照表換會把寫對的換掉（`variant_is_a_glossary_term`）
    ("PVE\nPVE → Proxmox", "清單上的專有名詞"),
    ("Proxmox → proxmox", "清單上的專有名詞"),
    # 同一個錯寫法對到兩個詞（`ambiguous_variant`）
    ("Proksmox → Proxmox\nProksmox → Bianca", "不知道要換成哪一個"),
    # 一個詞最多 20 個
    ("、".join(f"Prox{i:02d}" for i in range(21)) + " → Proxmox", "最多 20"),
])
def test_a_wrong_arrow_line_is_refused_and_named(raw, word):
    """**寫錯的行不可以安靜地當背景** —— 使用者明明寫了一條替換，結果什麼都沒換，
    畫面上卻看不出來。要回 400 並講出問題在哪。"""
    with pytest.raises(HTTPException) as ei:
        _mt.parse_glossary(raw)
    assert ei.value.status_code == 400
    assert word in ei.value.detail, ei.value.detail


def test_parse_terms_still_returns_only_the_terms():
    assert _mt.parse_terms("Proksmox → Proxmox\nPVE") == ["Proxmox", "PVE"]


def test_only_the_correct_spelling_goes_into_the_context():
    """錯寫法進了會議背景的話，會議摘要的替換建議會把它當成「背景就是這樣寫的」而不再提醒。"""
    raw = "今天談備份。\nProksmox、Proxmux → Proxmox\n上傳 → 轉檔 → 下載"
    assert _mt.context_text(raw) == "今天談備份。\nProxmox\n上傳 → 轉檔 → 下載"


def test_the_start_endpoint_refuses_before_anything_is_sent(client, unconfigured):
    with FakeJtlw(api_revision="2.9") as fake:
        _configure(fake)
        up = _upload(client)
        r = client.post(f"{BASE}/start",
                        json={"upload_id": up["upload_id"], "terms": "PVE\nPVE → Proxmox"})
        assert r.status_code == 400, r.text
        assert "body" not in fake.seen, "寫錯的行還是送出去了"


# ------------------------------------------------------------------ 送件

def test_variants_are_sent_to_29_and_replaced(client, unconfigured):
    with FakeJtlw(api_revision="2.9", heard="Proksmox", duration_ms=60000) as fake:
        _configure(fake)
        up = _upload(client)
        j = _run(client, up["upload_id"], terms="Proksmox → Proxmox\nPVE")
        assert j["status"] == "done", j.get("error")
        sent = fake.seen["body"]["glossary"]
    assert sent == {"entries": [
        {"source": "Proxmox", "mode": "keep", "variants": ["Proksmox"]},
        {"source": "PVE", "mode": "keep"}]}
    res = _result(client, up["upload_id"])
    assert res["variants"] == {"Proxmox": ["Proksmox"]}
    assert res["variants_sent"] is True
    assert res["terms"] == ["Proxmox", "PVE"]
    assert res["summary"]["correction"]["variant_replacements"] == 5
    assert res["glossary"]["variants"] == 1
    assert all("Proksmox" not in s["text"] and "Proxmox" in s["text"]
               for s in res["segments"]), [s["text"] for s in res["segments"]]


@pytest.mark.parametrize("cfg", [{"api_revision": "2.8"}, {"caps_status": 500}])
def test_an_older_service_gets_no_variants_and_the_result_says_so(client, unconfigured, cfg):
    """舊版的對方收到 `variants` 整件 400 —— 不送，但**要記下沒送**（結果頁講出來），
    不然使用者寫了替換、逐字稿卻沒變，分不出是沒送還是沒找到。問不到版本當舊版。"""
    with FakeJtlw(heard="Proksmox", **cfg) as fake:
        _configure(fake)
        up = _upload(client)
        j = _run(client, up["upload_id"], terms="Proksmox → Proxmox")
        assert j["status"] == "done", j.get("error")
        assert fake.seen["body"]["glossary"] == {"entries": [
            {"source": "Proxmox", "mode": "keep"}]}
    res = _result(client, up["upload_id"])
    assert res["variants"] == {"Proxmox": ["Proksmox"]}, "沒送也要記著（重跑時要帶回去）"
    assert res["variants_sent"] is False
    assert all("Proksmox" in s["text"] for s in res["segments"])


def test_no_variants_means_no_extra_field(client, unconfigured):
    """反向對照：沒寫箭頭的話送出去的形狀跟原本一模一樣。"""
    with FakeJtlw(api_revision="2.9") as fake:
        _configure(fake)
        up = _upload(client)
        assert _run(client, up["upload_id"], terms="Proxmox")["status"] == "done"
        assert fake.seen["body"]["glossary"] == {"entries": [
            {"source": "Proxmox", "mode": "keep"}]}
    res = _result(client, up["upload_id"])
    assert res["variants"] == {} and res["variants_sent"] is False


def test_without_diarization_the_revision_is_still_asked(client, unconfigured):
    """版本原本只在要分離發言者時才問 —— 台語模式不分離，錯寫法也要照版本決定送不送。"""
    with FakeJtlw(api_revision="2.9", heard="Proksmox") as fake:
        _configure(fake, profile_id="transcribe.taiwanese")
        up = _upload(client)
        j = _run(client, up["upload_id"], terms="Proksmox → Proxmox")
        assert j["status"] == "done", j.get("error")
        assert "diarize" not in fake.seen["body"]["tasks"]
        assert fake.seen["body"]["glossary"]["entries"][0]["variants"] == ["Proksmox"]


# ------------------------------------------------------------------ 補專有名詞、重跑校正

def _transcribe(client, fake, terms: str = "") -> str:
    _configure(fake)
    up = _upload(client)
    j = _run(client, up["upload_id"], terms=terms)
    assert j["status"] == "done", j.get("error")
    return up["upload_id"]


def test_a_retry_carries_the_variants(client, unconfigured):
    with FakeJtlw(api_revision="2.9", heard="Proksmox") as fake:
        uid = _transcribe(client, fake)
        assert all("Proksmox" in s["text"] for s in _result(client, uid)["segments"])
        r = client.post(f"{BASE}/retry",
                        json={"upload_id": uid, "terms": "Proksmox → Proxmox\nPVE"})
        assert r.status_code == 200, r.text
        assert _wait(client, r.json()["job_id"])["status"] == "done"
        assert fake.retries == [{"glossary": {"entries": [
            {"source": "Proxmox", "mode": "keep", "variants": ["Proksmox"]},
            {"source": "PVE", "mode": "keep"}]}}]
    out = _result(client, uid)
    assert out["variants"] == {"Proxmox": ["Proksmox"]} and out["variants_sent"] is True
    assert all("Proksmox" not in s["text"] for s in out["segments"]), "重跑之後沒有換掉"


def test_a_retry_to_an_older_service_drops_them_and_says_so(client, unconfigured):
    with FakeJtlw(api_revision="2.8", heard="Proksmox") as fake:
        uid = _transcribe(client, fake)
        r = client.post(f"{BASE}/retry", json={"upload_id": uid, "terms": "Proksmox → Proxmox"})
        assert r.status_code == 200, r.text
        assert _wait(client, r.json()["job_id"])["status"] == "done"
        assert fake.retries == [{"glossary": {"entries": [
            {"source": "Proxmox", "mode": "keep"}]}}]
    out = _result(client, uid)
    assert out["variants"] == {"Proxmox": ["Proksmox"]} and out["variants_sent"] is False


def test_a_wrong_arrow_line_in_a_retry_is_refused_before_asking_jtlw(client, unconfigured):
    with FakeJtlw(api_revision="2.9") as fake:
        uid = _transcribe(client, fake)
        r = client.post(f"{BASE}/retry",
                        json={"upload_id": uid, "terms": "Proksmox → Proxmox、PVE"})
        assert r.status_code == 400, r.text
        assert fake.retries == []


# ------------------------------------------------------------------ 對方退回

@pytest.mark.parametrize("reason", ["variants_need_single_term", "variant_is_a_glossary_term",
                                    "ambiguous_variant"])
def test_the_sync_api_says_which_rule_was_broken(client, unconfigured, reason):
    """對方的判準跟我們不一定完全一樣（他們拆詞的規則多幾種）—— 退回時要講得出是哪一條，
    而且是**呼叫端**要改的參數（400，不是 502）。"""
    with FakeJtlw(api_revision="2.9", variant_reject=reason) as fake:
        _configure(fake)
        r = _api(client, terms="Proksmox → Proxmox")
        assert fake.seen.get("rejected_variants") == reason
    assert r.status_code == 400, r.text[:300]
    assert r.json()["detail"] == jtlw_client.MESSAGES[reason]


def test_the_rejection_only_applies_when_variants_are_sent(client, unconfigured):
    """反向對照：同一台會退回錯寫法的對方，沒寫箭頭時照常成功。"""
    with FakeJtlw(api_revision="2.9", variant_reject="ambiguous_variant") as fake:
        _configure(fake)
        r = _api(client, terms="Proxmox")
    assert r.status_code == 200, r.text[:300]


def test_describe_error_maps_the_reasons_only_for_glossary_fields():
    f = "glossary.entries[2].variants"
    for reason in ("variants_need_single_term", "variant_is_a_glossary_term",
                   "ambiguous_variant"):
        assert jtlw_client.describe_error("invalid_request", field=f, reason=reason) == \
            jtlw_client.MESSAGES[reason]
    # 別的欄位、或不認得的代碼 → 照原本的說法
    assert jtlw_client.describe_error("invalid_request", field="language",
                                      reason="ambiguous_variant") == \
        jtlw_client.MESSAGES["language_rejected"]
    other = jtlw_client.describe_error("invalid_request", field=f, reason="something_new")
    assert other not in {jtlw_client.MESSAGES[r] for r in jtlw_client._VARIANT_REASONS}


# ------------------------------------------------------------------ 畫面

def _js_function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1


@pytest.mark.skipif(not shutil.which("node"), reason="沒有 node")
def test_the_retry_box_brings_the_arrow_lines_back():
    """補專有名詞的框要帶回**整份**清單 —— 對方的 retry 是取代不是累加，
    錯寫法沒寫回框裡的話，補一個詞按下去，前一次的錯寫法就不見了。真的把那支函式跑一次。"""
    fn = _js_function(TEMPLATE.read_text(encoding="utf-8"), "termsText")
    data = {"terms": ["王小明", "Proxmox", "PVE"],
            "variants": {"Proxmox": ["Proksmox", "Proxmux"]}}
    js = fn + "\nprocess.stdout.write(termsText(" + json.dumps(data, ensure_ascii=False) + "));"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout == "王小明\nProksmox、Proxmux → Proxmox\nPVE"
    # 帶回去的那一份要解析得回同一組（不然重跑會被自己的檢查擋下來）
    assert _mt.parse_glossary(out.stdout) == (data["terms"], data["variants"])


def test_the_page_explains_the_arrow_and_reports_the_result():
    src = TEMPLATE.read_text(encoding="utf-8")
    shown = re.sub(r"\{#.*?#\}", "", src, flags=re.S)
    assert "聽錯的寫法 → 正確寫法" in shown, "說明沒有寫箭頭的寫法"
    assert "termsText(data)" in src, "補專有名詞的框沒有帶回錯寫法"
    assert "data.variants_sent === false" in src, "對方版本較舊、沒送出時畫面不會講"
    assert "variant_replacements" in src, "結果頁沒有講換了幾處"
