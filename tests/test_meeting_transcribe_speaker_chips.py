"""轉逐字稿結果頁：上方的發言者標籤可以直接改名（v1.16.37）。

2026-10-02 使用者要求：「上面有找到八位，8 個顯示出來，也可以在上面這裡點 S1 S2 改人名」。
逐字稿上千段時，一段一段找名字來改不實際；標籤改的是**那一位的全部段落**。
同一次另外把「校正力道：punctuation_only」這種對方的代碼改成白話。

**真的在瀏覽器裡跑**：種一件已完成的作業與它的逐字稿，從「我的作業」那條路
（`?job=`）打開結果頁、點標籤、打名字、按 Enter —— 然後看三件事：
逐字稿裡那一位的每一段都換了名字、標籤上看得到原本的代號、**存檔真的寫進去了**
（只改畫面不存檔的話，重新整理就不見，而畫面上完全看不出來）。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(ROOT))
from tools.browser_probe import browser as _browser  # noqa: E402

pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")

_UID = "a" * 32
_JOB = "b" * 32
#: 第二件：要過校正、但已經請 JTLW 刪除（沒有排在延後 ACK 的清單裡）
_UID2 = "c" * 32
_JOB2 = "d" * 32
#: 第三件：一份**很大的 WAV**（逐字稿裡記的大小是 400 MB）—— 波形要由伺服器讀出來畫
#（2026-10-03 使用者回報「波形圖怎麼只剩一條線」：363 MB 的 WAV 超過瀏覽器解碼的 60 MB 門檻）
_UID3 = "e" * 32
_JOB3 = "f" * 32

_SEED = r"""
import json, sys, time
from app.config import settings
from app.core import jtlw_ack, jtlw_settings, job_store
from app.core.job_manager import Job
jtlw_settings.save({"enabled": True, "base_url": "http://127.0.0.1:1",
                    "api_key_enc": "seeded", "audio_base_url": "http://127.0.0.1:1",
                    "profile_id": "meeting.balanced"})
segs = []
for i in range(9):
    segs.append({"seq": i + 1, "speaker": ["S1", "S2", "S3"][i % 3],
                 "start_ms": i * 4000, "end_ms": i * 4000 + 3500,
                 "text": "第 %d 段" % (i + 1)})
# 第二件：S3 在會議裡報過名字（「可能是 Wendy」的提示），S2 那句「我是覺得」不是
segs2 = [dict(s) for s in segs]
segs2[2]["text"] = "大家好，我是 Wendy。"
segs2[4]["text"] = "我是覺得可以"
settings.temp_dir.mkdir(parents=True, exist_ok=True)
job_store.init()
for uid, jid, rid in ((sys.argv[1], sys.argv[2], "job_seed_1"),
                      (sys.argv[3], sys.argv[4], "job_seed_2")):
    (settings.temp_dir / ("mt_%s_transcript.json" % uid)).write_text(json.dumps({
        "segments": segs if uid == sys.argv[1] else segs2, "summary": {"correction_level": "punctuation_only",
                                      "correction": {"edited": 2, "unchanged": 7,
                                                     "variant_replacements": 4}},
        "speaker_names": {}, "speaker_overrides": {},
        "remote_job_id": rid, "tasks": ["transcribe", "diarize", "correct"],
        "terms": ["王小明", "Bianca", "Proxmox VE"],
        # 第一件：對方本機辨識（GPU 伺服器不能用時）只參考了前 2 個；
        # 第二件：平常的 GPU 伺服器那條路，辨識時不參考（v2.27 起照實回 0）
        "glossary": {"entries": 3, "keep_terms": 3,
                     "asr_bias_terms": 2 if uid == sys.argv[1] else 0},
        # 已知的錯寫法（v1.16.49）：第一件送出去了（對方 2.9），第二件對方版本較舊沒送
        "variants": {"Proxmox VE": ["Proksmox"]},
        "variants_sent": uid == sys.argv[1],
        "speaker_engine": "auto", "diarize_saturated": True,
        # 第一件寫了一句會議背景（不會送去辨識，轉送會議摘要時帶過去）；第二件沒寫
        "context": ("這是第四季規劃會議。\n王小明\nBianca\nProxmox VE"
                    if uid == sys.argv[1] else ""),
        "diarization": {"requested": "auto", "engine": "nemotron", "saturated": True}},
        ensure_ascii=False), encoding="utf-8")
    job_store.upsert(Job(id=jid, tool_id="meeting-transcribe", status="done",
                         progress=1.0, meta={"upload_id": uid, "filename": "x.m4a"},
                         created_at=time.time(), updated_at=time.time()))
# 第一件還在延後 ACK 的保留期內（可以補專有名詞重跑）；第二件已經請 JTLW 刪除
jtlw_ack.defer("job_seed_1", sys.argv[1])

# 第三件：真的 WAV（兩秒，前一秒大聲、後一秒小聲），逐字稿記的大小是 400 MB
import math, struct, wave
uid3 = sys.argv[5]
adir = settings.data_dir / "speech_audio"
adir.mkdir(parents=True, exist_ok=True)
with wave.open(str(adir / ("%s.bin" % uid3)), "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
    w.writeframes(b"".join(struct.pack("<h", int(32767 * (0.9 if i < 8000 else 0.05)
                                                   * math.sin(2 * math.pi * 300 * i / 8000)))
                           for i in range(16000)))
(settings.temp_dir / ("mt_%s_meta.json" % uid3)).write_text(json.dumps(
    {"filename": "big.wav", "content_type": "audio/wav"}), encoding="utf-8")
(settings.temp_dir / ("mt_%s_transcript.json" % uid3)).write_text(json.dumps({
    "segments": segs, "speaker_names": {}, "speaker_overrides": {},
    "remote_job_id": "job_seed_3", "tasks": ["transcribe", "diarize"],
    "source": {"size_bytes": 400_000_000}}, ensure_ascii=False), encoding="utf-8")
job_store.upsert(Job(id=sys.argv[6], tool_id="meeting-transcribe", status="done",
                     progress=1.0, meta={"upload_id": uid3, "filename": "big.wav"},
                     created_at=time.time(), updated_at=time.time()))
"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def live():
    data = tempfile.mkdtemp(prefix="mtchips-")
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    subprocess.run([sys.executable, "-c", _SEED, _UID, _JOB, _UID2, _JOB2, _UID3, _JOB3],
                   cwd=ROOT, env=env,
                   check=True, capture_output=True, timeout=120)
    port, cdp = _free_port(), _free_port()
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [_browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
         f"--remote-debugging-port={cdp}", "--remote-allow-origins=*",
         "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.skip("實例或瀏覽器起不來")
        yield port, cdp, pathlib.Path(data)
    finally:
        br.terminate(); srv.terminate()
        try:
            br.wait(timeout=5); srv.wait(timeout=5)
        except Exception:
            br.kill(); srv.kill()
        shutil.rmtree(data, ignore_errors=True)


def _page(live, job: str = _JOB):
    import websockets.sync.client as wsc
    port, cdp, _ = live
    req = urllib.request.Request(f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        tab = json.loads(r.read())
    ws = wsc.connect(tab["webSocketDebuggerUrl"], max_size=None, open_timeout=10)
    n = [0]

    def ev(expr):
        n[0] += 1
        ws.send(json.dumps({"id": n[0], "method": "Runtime.evaluate",
                            "params": {"expression": expr, "returnByValue": True,
                                       "awaitPromise": True}}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == n[0]:
                res = m.get("result", {})
                assert "exceptionDetails" not in res, res.get("exceptionDetails")
                return res.get("result", {}).get("value")

    n[0] += 1
    ws.send(json.dumps({"id": n[0], "method": "Page.navigate", "params": {
        "url": f"http://127.0.0.1:{port}/tools/meeting-transcribe/?job={job}"}}))
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if ev("!!document.getElementById('mtSpkChips') && "
                  "!document.getElementById('mtSpkChips').hidden"):
                break
        except Exception:
            pass
        time.sleep(0.3)
    return ws, ev


def test_speakers_are_listed_and_renaming_a_chip_renames_every_segment(live):
    ws, ev = _page(live)
    try:
        chips = ev("[...document.querySelectorAll('#mtSpkChips .mt-chip')].map(c => c.textContent)")
        assert chips and len(chips) == 3, f"三位發言者應該有三個標籤：{chips}"
        assert "S1" in chips[0] and "3 段" in chips[0], chips

        ev("document.querySelector('#mtSpkChips .mt-chip .mt-chip-name').click()")
        ev("""(() => { const i = document.querySelector('#mtSpkChips input');
                 i.value = 'Bianca';
                 i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); })()""")
        time.sleep(1.0)
        names = ev("[...document.querySelectorAll('#mtSegs .mt-row')].map(r => r.children[1].textContent)")
        assert names[0::3] == ["Bianca"] * 3, f"S1 的每一段都要換成新名字：{names}"
        assert names[1] == "S2" and names[2] == "S3", f"別人的名字不可以被動到：{names}"
        first = ev("document.querySelector('#mtSpkChips .mt-chip').textContent")
        assert "Bianca" in first and "S1" in first, f"標籤上要看得到原本的代號：{first}"

        saved = json.loads((live[2] / "temp" / f"mt_{_UID}_transcript.json")
                           .read_text(encoding="utf-8"))
        assert saved.get("speaker_names") == {"S1": "Bianca"}, (
            f"只改了畫面沒有存檔：{saved.get('speaker_names')}")
    finally:
        ws.close()


def test_the_segment_count_on_a_chip_jumps_to_that_speakers_first_line(live):
    """點標籤上的「N 段」→ 跳到那一位第一次說話的那一段（2026-10-03 使用者要求：
    「點人名或 S1 S2 可編輯，點 XX 段可快進到該人的第一筆說話」）。

    判準：逐字稿真的**捲到那一行**、那一行標出來，而且**不會打開改名**（兩顆分開）。
    先把逐字稿框壓矮，不然九段全都看得到，「有沒有捲過去」驗不出來。"""
    ws, ev = _page(live)
    try:
        ev("""(() => { const b = document.getElementById('mtSegs');
                 b.style.maxHeight = '40px'; b.scrollTop = 0; return 1; })()""")
        before = ev("document.getElementById('mtSegs').scrollTop")
        # S3 第一次說話是第 3 段
        ev("document.querySelector('#mtSpkChips .mt-chip[data-speaker=\"S3\"] .mt-chip-jump').click()")
        time.sleep(0.4)
        got = ev("""(() => { const b = document.getElementById('mtSegs');
                 const r = b.querySelector('.mt-row[data-seq="3"]').getBoundingClientRect();
                 const v = b.getBoundingClientRect();
                 return {scroll: b.scrollTop, visible: r.top >= v.top - 1 && r.top < v.bottom,
                         lit: [...b.querySelectorAll('.mt-row.mt-jump')].map(x => x.dataset.seq),
                         editing: !!document.querySelector('#mtSpkChips input')}; })()""")
        assert got["scroll"] > before, f"逐字稿沒有捲動：{got}"
        assert got["visible"], f"捲過去了但第 3 段不在框裡看得到的地方：{got}"
        assert got["lit"] == ["3"], f"要標出的是那一位第一次說話的那一段：{got['lit']}"
        assert not got["editing"], "點「N 段」打開了改名 —— 兩顆按鈕要各做各的事"
    finally:
        ws.close()


def test_after_renaming_the_transcript_can_be_saved_to_the_workspace_again(live):
    """存至工作區之後改了名字，要能再存一次，而且存出去的是**改過的名字**
    （2026-10-03 使用者回報：存完才發現人名錯，改完按鈕停在「已存至工作區」按不下去）。

    判準落在**工作區裡真的多出來的那份檔案的內容**：只把按鈕解開、存出去的仍是 S2 的話，
    畫面看起來一模一樣（這一次就順手抓到純文字原本寫的是代號）。"""
    ws, ev = _page(live)
    try:
        if not ev("!!document.getElementById('mtSaveWs') && !document.getElementById('mtSaveWs').hidden"):
            pytest.skip("這個實例的工作區被停用")
        count = "fetch('/workspace/api/list').then(r => r.json()).then(d => (d.files || []).length)"
        n0 = ev(count)
        ev("document.getElementById('mtSaveWs').click(), 1")
        deadline = time.time() + 15
        while time.time() < deadline and ev(count) <= n0:
            time.sleep(0.3)
        assert ev(count) == n0 + 1, "第一次存至工作區沒有存進去"
        time.sleep(0.3)
        assert ev("document.getElementById('mtSaveWs').disabled") is True, (
            "存完按鈕應該先顯示已存（這條是前提：不成立的話下面驗不到「改名後解開」）")

        ev("document.querySelector('#mtSpkChips .mt-chip[data-speaker=\"S2\"] .mt-chip-name').click()")
        ev("""(() => { const i = document.querySelector('#mtSpkChips input');
                 i.value = '王小明';
                 i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); })()""")
        time.sleep(0.8)
        assert ev("document.getElementById('mtSaveWs').disabled") is False, (
            "改了名字之後「存至工作區」還是按不下去")
        ev("document.getElementById('mtSaveWs').click(), 1")
        deadline = time.time() + 15
        while time.time() < deadline and ev(count) <= n0 + 1:
            time.sleep(0.3)
        assert ev(count) == n0 + 2, "改名後再存一次，工作區沒有多出檔案"
        text = ev("""fetch('/workspace/api/list').then(r => r.json())
                 .then(d => d.files.sort((a, b) => b.saved_at - a.saved_at)[0].file_id)
                 .then(id => fetch('/workspace/file/' + id)).then(r => r.text())""")
        assert "王小明：第 2 段" in text, f"存出去的不是改過的名字：{text[:200]}"
        assert "S2：" not in text, f"存出去的還有代號：{text[:200]}"
    finally:
        ws.close()


def test_the_correction_level_is_shown_in_plain_words(live):
    ws, ev = _page(live)
    try:
        txt = ev("document.getElementById('mtCorrection').textContent")
        assert "punctuation_only" not in (txt or ""), f"直接印出了對方的代碼：{txt}"
        assert "只修標點" in (txt or ""), txt
    finally:
        ws.close()


def test_terms_used_only_in_correction_say_so_not_first_zero(live):
    """平常走 GPU 伺服器時對方照實回 `asr_bias_terms: 0`（v2.27）—— 畫面要寫「只用在校正」，
    **不可以寫成「辨識時參考前 0 個」**（照實回報之後，舊的寫法正式機上就是那一句）。"""
    ws, ev = _page(live, _JOB2)
    try:
        txt = ev("document.getElementById('mtCorrection').textContent") or ""
        assert "專有名詞 3 個" in txt, txt
        assert "只用在校正" in txt, f"沒有講專有名詞只用在校正：{txt}"
        assert "前 0 個" not in txt, f"寫出了「前 0 個」：{txt}"
    finally:
        ws.close()


def test_the_variant_replacements_are_reported_or_not_sent_is_said(live):
    """已知的錯寫法（v1.16.49）：送出去的寫出換了幾處；對方版本較舊沒送的要講出來 ——
    不然使用者寫了替換、逐字稿卻沒變，分不出是沒送還是沒找到。"""
    for job, want, never in ((_JOB, "照聽錯的寫法換了 4 處", "沒有送出"),
                             (_JOB2, "聽錯的寫法沒有送出", "換了 4 處")):
        ws, ev = _page(live, job)
        try:
            txt = ev("document.getElementById('mtCorrection').textContent") or ""
            assert want in txt, f"{job[:4]}：{txt}"
            assert never not in txt, f"{job[:4]}：{txt}"
        finally:
            ws.close()


def test_the_terms_and_the_full_slots_are_spelled_out(live):
    """送了幾個專有名詞、辨識時只參考了前幾個；新方法 8 個位置都用滿時提醒填人數重送
    （v1.16.39，JTLW glossary ＋ v2.26 `saturated`）。判準是畫面上真的看得到那幾句。"""
    ws, ev = _page(live)
    try:
        txt = ev("document.getElementById('mtCorrection').textContent") or ""
        assert "專有名詞 3 個" in txt, txt
        assert "辨識時參考前 2 個" in txt, f"只參考了前 2 個卻沒講：{txt}"
        note = ev("(() => { const n = document.getElementById('mtDiarNote');"
                  " return n.hidden ? null : n.textContent; })()")
        assert note and "8 個位置都用滿" in note, f"沒有提醒 8 個位置用滿：{note!r}"
    finally:
        ws.close()


def test_the_retry_box_offers_the_terms_and_says_until_when(live):
    """還在保留期內：「補專有名詞，重跑校正」那一塊看得到（收折著）、寫出可以補到什麼時候、
    輸入框預先帶好原本送出的專有名詞（使用者要給的是**整份**）。"""
    ws, ev = _page(live)
    try:
        st = ev("""(() => { const b = document.getElementById('mtRetry');
                   return {hidden: b.hidden, open: b.open,
                           shown: b.getBoundingClientRect().height > 0,
                           sum: b.querySelector('summary').textContent,
                           terms: document.getElementById('mtRetryTerms').value,
                           go: document.getElementById('mtRetryGo').disabled,
                           done: document.getElementById('mtRetryDone').disabled,
                           gone: document.getElementById('mtRetryGone').hidden}; })()""")
        assert st["hidden"] is False and st["shown"], f"重跑那一塊沒有出現：{st}"
        assert st["open"] is False, "預設要收折 —— 多數人不需要，攤開會把逐字稿往下推"
        assert "可以補到" in st["sum"], f"沒有寫出可以補到什麼時候：{st['sum']!r}"
        # 錯寫法要寫回箭頭行 —— 重跑是取代整份清單，沒帶回來的話這一次寫的錯寫法就不見了（v1.16.49）
        assert st["terms"] == "王小明\nBianca\nProksmox → Proxmox VE", \
            f"沒有帶入原本的專有名詞與錯寫法：{st['terms']!r}"
        assert st["go"] is False and st["done"] is False, st
        assert st["gone"] is True, st
    finally:
        ws.close()


def test_a_speaker_who_said_their_name_gets_a_one_click_suggestion(live):
    """發言者在會議裡報過名字（「大家好，我是 Wendy」）→ 標籤上「可能是 Wendy」，按一下就換進去
    （2026-10-03 使用者要求）。

    判準：只有**報過名字的那一位**有提示（說「我是覺得可以」的那位沒有）、滑過看得到是哪一段說的、
    按下去**每一段都換了名字而且存檔了**、換好之後提示消失（已經有名字就不再建議）。"""
    ws, ev = _page(live, _JOB2)
    try:
        hints = ev("""[...document.querySelectorAll('#mtSpkChips .mt-chip')].map(c =>
                 [c.dataset.speaker, (c.querySelector('.mt-chip-hint') || {}).textContent || null])""")
        assert dict(hints) == {"S1": None, "S2": None, "S3": "可能是 Wendy"}, hints
        title = ev("document.querySelector('.mt-chip[data-speaker=\"S3\"] .mt-chip-hint').title")
        assert "第 3 段" in title and "我是 Wendy" in title, title

        ev("document.querySelector('.mt-chip[data-speaker=\"S3\"] .mt-chip-hint').click(), 1")
        time.sleep(1.0)
        names = ev("[...document.querySelectorAll('#mtSegs .mt-row')].map(r => r.children[1].textContent)")
        assert names[2::3] == ["Wendy"] * 3, f"S3 的每一段都要換成那個名字：{names}"
        assert names[0] == "S1" and names[1] == "S2", f"別人的名字不可以被動到：{names}"
        assert not ev("!!document.querySelector('#mtSpkChips .mt-chip-hint')"), (
            "已經改好名字了，提示還在")
        assert not ev("!!document.querySelector('#mtSpkChips input')"), "按提示不該打開改名框"
        saved = json.loads((live[2] / "temp" / f"mt_{_UID2}_transcript.json")
                           .read_text(encoding="utf-8"))
        assert saved.get("speaker_names") == {"S3": "Wendy"}, (
            f"只改了畫面沒有存檔：{saved.get('speaker_names')}")
        assert "name_hints" not in saved, "建議被寫進逐字稿了"
    finally:
        ws.close()


def test_a_deleted_copy_says_to_resubmit(live):
    """已經請 JTLW 刪除的：不出現重跑那一塊，改講「要改專有名詞請重新送件」。"""
    ws, ev = _page(live, _JOB2)
    try:
        st = ev("""(() => ({box: document.getElementById('mtRetry').hidden,
                            gone: document.getElementById('mtRetryGone').hidden,
                            text: document.getElementById('mtRetryGone').textContent}))()""")
        assert st["box"] is True, f"已經刪除了還讓人重跑：{st}"
        assert st["gone"] is False and "重新送件" in st["text"], st
    finally:
        ws.close()


def test_no_more_changes_asks_first_and_then_closes_the_window(live):
    """「不用再改了」要先問（刪了就不能再重跑）；確定之後那一塊收起來、講出下一步。

    這台假實例連不到 JTLW，所以 ACK 送不出去 —— 仍然要從這一刻起不能再重跑，
    並排在下一輪巡檢再送（清單上的到期時間改成現在）。**放在最後**：會改到第一件的狀態。"""
    ws, ev = _page(live)
    try:
        ev("document.getElementById('mtRetry').open = true")
        ev("document.getElementById('mtRetryDone').click()")
        for _ in range(30):
            if ev("!!document.querySelector('.modal-ok')"):
                break
            time.sleep(0.2)
        assert ev("!!document.querySelector('.modal-ok')"), "沒有先問就直接刪了"
        ev("document.querySelector('.modal-ok').click()")
        st = None
        for _ in range(60):
            st = ev("""(() => ({box: document.getElementById('mtRetry').hidden,
                                gone: document.getElementById('mtRetryGone').hidden,
                                text: document.getElementById('mtRetryGone').textContent}))()""")
            if st["box"]:
                break
            time.sleep(0.3)
        assert st and st["box"] is True, f"按了「不用再改了」之後還讓人重跑：{st}"
        assert st["gone"] is False and "重新送件" in st["text"], st
        pend = json.loads((live[2] / "jtlw_pending_ack.json").read_text(encoding="utf-8"))
        assert pend["job_seed_1"]["due_at"] <= time.time() + 1, \
            f"送不出去的沒有排到下一輪就送：{pend}"
    finally:
        ws.close()


def test_a_big_wav_still_gets_a_waveform(live):
    """**很大的 WAV 要有波形**（2026-10-03 使用者回報「波形圖怎麼只剩一條線」）。

    瀏覽器只解 60 MB 以下的檔案；WAV 改由伺服器讀。判準是**畫布上真的有波形**：
    只有時間軸的話，畫面只有中間那 4 px 的一條，上下四分之一是空的。
    前一秒大聲、後一秒小聲 —— 左半邊的波形要比右半邊高。"""
    ws, ev = _page(live, _JOB3)
    try:
        deadline = time.time() + 20
        got = None
        while time.time() < deadline:
            got = ev("""(() => {
              const c = document.getElementById('mtWave');
              if (!c || !c.width) return null;
              const g = c.getContext('2d'), w = c.width, h = c.height;
              const d = g.getImageData(0, 0, w, h).data;
              const colTop = x => { for (let y = 0; y < h; y++)
                                      if (d[(y * w + x) * 4 + 3] > 0) return y; return h; };
              const left = colTop(Math.floor(w * 0.25)), right = colTop(Math.floor(w * 0.75));
              const note = document.getElementById('mtWaveNote');
              return {left, right, h, noteHidden: note.hidden};
            })()""")
            if got and got["left"] < got["h"] / 4:
                break
            time.sleep(0.3)
        assert got and got["left"] < got["h"] / 4, f"大 WAV 只畫了一條時間軸：{got}"
        assert got["left"] < got["right"], f"大聲那一段的波形沒有比小聲那一段高：{got}"
        assert got["noteHidden"] is True, "有波形卻還掛著「沒有畫波形」的說明"
    finally:
        ws.close()


def test_background_sentences_are_said_to_go_to_the_summary(live):
    """「專有名詞或會議背景」寫了句子 → 結果頁講出「會議背景轉送會議摘要時會帶過去」；
    只寫了詞的那一件不講（反向對照）。"""
    ws, ev = _page(live)
    try:
        txt = ev("document.getElementById('mtCorrection').textContent") or ""
        assert "會議背景轉送會議摘要時會帶過去" in txt, txt
    finally:
        ws.close()
    ws, ev = _page(live, _JOB2)
    try:
        txt = ev("document.getElementById('mtCorrection').textContent") or ""
        assert "會議背景轉送會議摘要時會帶過去" not in txt, txt
    finally:
        ws.close()
