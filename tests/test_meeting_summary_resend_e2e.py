"""會議摘要「自己加替換」送回轉逐字稿 —— **真的在瀏覽器裡按一次**（v1.16.66）。

伺服器端的判斷另有 `tests/test_meeting_summary_resend_variants.py`；這一支驗的是頁面：
那一塊在對的時候出現、不能送時**不畫按鈕**只講原因、沒有勾著的替換時按鈕不能按、
按下去之後接上作業進度、做完講出「換了幾處」—— 這些只有真的跑一次 JS 才看得到
（本專案一整個家族的 bug 都是「元素都在、沒有例外，但那段程式根本沒跑」）。

實例裡的語音服務指向一台**假的 JTLW**（`tests/test_meeting_transcribe.FakeJtlw`，在這個
測試行程裡跑）；轉逐字稿那件作業直接種好（已完成、還在延後 ACK 的保留時間內）。
範例裡的錯寫法都是編的。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import browser_probe  # noqa: E402

pytestmark = pytest.mark.skipif(
    browser_probe.browser() is None
    or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")

_UID = "a1" * 16

TRANSCRIPT = {
    "source": {"filename": "會議.m4a", "size_bytes": 5000},
    "remote_job_id": "job_seed_1",
    "status": "succeeded",
    "tasks": ["transcribe", "diarize", "correct"],
    "terms": ["嘉禾科技"],
    "variants": {},
    "summary": {"duration_ms": 60000, "correction_level": "punctuation_only"},
    "speaker_names": {}, "speaker_overrides": {},
    "context": "",
    "segments": [{"seq": i, "text": f"第 {i} 段：Proksmox 叢集這週要升級。",
                  "speaker": "S1" if i % 2 else "S2",
                  "start_ms": i * 4000, "end_ms": i * 4000 + 3500} for i in range(1, 6)],
}

_SEED = r"""
import json, sys
from app.config import settings
from app.core import jtlw_ack, jtlw_settings
jtlw_settings.save({"enabled": True, "base_url": sys.argv[1], "api_key_enc": "seeded",
                    "audio_base_url": "http://127.0.0.1:1", "profile_id": "meeting.balanced",
                    "retry_window_hours": 24, "tasks": ["transcribe", "diarize", "correct"]})
settings.temp_dir.mkdir(parents=True, exist_ok=True)
(settings.temp_dir / ("mt_%s_transcript.json" % sys.argv[2])).write_text(sys.argv[3], encoding="utf-8")
jtlw_ack.defer("job_seed_1", sys.argv[2])
"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def live():
    from tests.test_meeting_transcribe import FakeJtlw
    fake = FakeJtlw(api_revision="2.9", heard="Proksmox", duration_ms=60000).__enter__()
    # 種好的那件「送件時要求了這些處理」—— 沒有 `correct` 的話對方的 retry 回 409
    fake.requested_tasks = ("transcribe", "diarize", "correct")
    data = tempfile.mkdtemp(prefix="msresend-")
    (Path(data) / "auth_settings.json").write_text(json.dumps({"backend": "off"}),
                                                   encoding="utf-8")
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    subprocess.run([sys.executable, "-c", _SEED, fake.base, _UID,
                    json.dumps(TRANSCRIPT, ensure_ascii=False)],
                   cwd=ROOT, env=env, check=True, capture_output=True, timeout=120)
    port, cdp = _free_port(), _free_port()
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [browser_probe.browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
         f"--remote-debugging-port={cdp}", "--remote-allow-origins=*", "about:blank"],
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
        import websockets.sync.client as wsc
        req = urllib.request.Request(f"http://127.0.0.1:{cdp}/json/new?about:blank",
                                     method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            tab = json.loads(r.read())
        ws = wsc.connect(tab["webSocketDebuggerUrl"], max_size=None, open_timeout=10)
        n = [0]

        def send(method, params=None):
            n[0] += 1
            i = n[0]
            ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
            while True:
                m = json.loads(ws.recv(timeout=180))
                if m.get("id") == i:
                    return m

        updir = Path(browser_probe.uploadable_dir())
        tx = updir / "msresend-逐字稿.txt"          # 經工作區中轉時就是 .txt
        tx.write_text(json.dumps(TRANSCRIPT, ensure_ascii=False), encoding="utf-8")
        vtt = updir / "msresend-plain.vtt"
        vtt.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n<v 甲>Proksmox 要升級。\n\n"
                       "00:00:06.000 --> 00:00:09.000\n<v 乙>Proksmox 的備份。\n", encoding="utf-8")
        yield port, send, str(tx), str(vtt), fake
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        br.terminate()
        srv.terminate()
        try:
            br.wait(timeout=5)
            srv.wait(timeout=5)
        except Exception:
            br.kill()
            srv.kill()
        fake.__exit__(None, None, None)
        shutil.rmtree(data, ignore_errors=True)


def _eval(send, expr):
    r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                  "awaitPromise": True})
    res = r.get("result", {})
    assert "exceptionDetails" not in res, res.get("exceptionDetails")
    return (res.get("result", {}) or {}).get("value")


def _wait(send, expr, secs=60):
    end = time.time() + secs
    while time.time() < end:
        if _eval(send, expr):
            return True
        time.sleep(0.3)
    return False


def _upload(port, send, path):
    from tests.test_meeting_summary_e2e import _set_file
    send("Page.enable"); send("Runtime.enable"); send("DOM.enable")
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/meeting-summary/"})
    assert _wait(send, "document.readyState === 'complete' && !!document.getElementById('msUp')", 30)
    time.sleep(0.5)
    _set_file(send, ".file-upload input[type=file]", path)
    assert _wait(send, "!document.getElementById('msParsed').hidden", 60), "解析區沒有出現"


_VISIBLE = ("(function(id){ var e = document.getElementById(id); if (!e || e.hidden) return false;"
            " var r = e.getBoundingClientRect(); return r.width > 20 && r.height > 5; })")


def test_the_replacement_goes_back_and_the_page_says_how_many_were_fixed(live):
    port, send, tx, _vtt, fake = live
    _upload(port, send, tx)
    # 從轉逐字稿來、那件作業還在保留時間內 → 那一塊出現、說明講得出能送到什麼時候
    assert _wait(send, _VISIBLE + "('msMrSend')", 20), "轉逐字稿來的逐字稿，「送回轉逐字稿」那一塊沒有出現"
    assert _wait(send, _VISIBLE + "('msMrSendGo')", 10), "能送的時候沒有畫出按鈕"
    note = _eval(send, "document.getElementById('msMrSendNote').textContent")
    assert "會議錄音轉逐字稿" in note and "可以送到" in note, note
    # **還沒加任何替換時不能按**（按了只會被退回）
    assert _eval(send, "document.getElementById('msMrSendGo').disabled") is True, (
        "一條替換都沒有，按鈕卻可以按")

    _eval(send, """(function(){
      document.getElementById('msMrFrom').value = 'proksmox';
      document.getElementById('msMrTo').value = 'Proxmox';
      document.getElementById('msMrAdd').click(); return 1; })()""")
    assert _wait(send, "document.querySelectorAll('#msMrList li').length === 1", 10)
    assert _wait(send, "document.getElementById('msMrSendGo').disabled === false", 5), (
        "加了替換之後按鈕還是不能按")
    # 取消勾選 → 又不能按（送的只有勾著的那幾條）
    _eval(send, "document.querySelector('#msMrList input').click(), 1")
    assert _eval(send, "document.getElementById('msMrSendGo').disabled") is True
    _eval(send, "document.querySelector('#msMrList input').click(), 1")
    assert _eval(send, "document.getElementById('msMrSendGo').disabled") is False

    _eval(send, "document.getElementById('msMrSendGo').click(), 1")
    assert _wait(send, _VISIBLE + "('msMrSendJob')", 10), "按下去之後沒有接上作業進度"
    assert _wait(send, "!document.getElementById('msMrSendLast').hidden", 90), (
        "重跑做完之後，頁面沒有講結果")
    last = _eval(send, "document.getElementById('msMrSendLast').textContent")
    assert "換了 5 處" in last, f"沒有講出換了幾處：{last!r}"

    # 送出去的是**整份**（原本的專有名詞還在）＋ 逐字稿裡實際的寫法（使用者打的是小寫）
    assert fake.retries and fake.retries[-1]["glossary"]["entries"] == [
        {"source": "嘉禾科技", "mode": "keep"},
        {"source": "Proxmox", "mode": "keep", "variants": ["Proksmox"]}], fake.retries
    # 會議摘要這一份不受影響
    assert _eval(send, "document.getElementById('msPrev').textContent").count("Proksmox") >= 1


def test_when_it_cannot_be_sent_there_is_no_button_only_the_reason(live):
    """轉逐字稿那邊按了「不用再改了」之後 —— 那一塊還在、講出為什麼，**但不畫按鈕**。"""
    port, send, tx, _vtt, fake = live
    _upload(port, send, tx)
    assert _wait(send, _VISIBLE + "('msMrSend')", 20)
    ok = _eval(send, f"""fetch('/tools/meeting-transcribe/done', {{method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{upload_id: '{_UID}'}})}}).then(r => r.ok)""")
    assert ok
    _upload(port, send, tx)
    assert _wait(send, _VISIBLE + "('msMrSend')", 20), "不能送的時候整塊不見了 —— 使用者看不出為什麼"
    assert _wait(send, "document.getElementById('msMrSendNote').textContent.indexOf('不能重跑校正') >= 0", 10), (
        _eval(send, "document.getElementById('msMrSendNote').textContent"))
    assert _eval(send, "document.getElementById('msMrSendActs').hidden") is True, (
        "不能送卻還畫著按鈕（按了只會失敗）")


def test_a_transcript_from_elsewhere_shows_nothing(live):
    port, send, _tx, vtt, _fake = live
    _upload(port, send, vtt)
    assert _wait(send, _VISIBLE + "('msManRep')", 10)
    time.sleep(1.0)
    assert _eval(send, "document.getElementById('msMrSend').hidden") is True, (
        "不是從轉逐字稿來的逐字稿，卻出現了「送回轉逐字稿」")
