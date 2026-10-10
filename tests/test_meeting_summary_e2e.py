"""會議摘要：**真的在瀏覽器裡跑一次**。

**為什麼一定要有這一條**：這支工具的結果頁幾乎全部是 JS 產生的（四張卡片、
章節、發言者佔比的 SVG、討論結構的樹、逐字稿、以及點引用跳回原文）。
本專案一整個家族的 bug 都是「元素都在、沒有 JS 例外、畫面看起來正常，
但那段程式根本沒跑」—— 只有真的跑一次 JS 才看得到。

實例裡的 LLM 指向一支**假的 OpenAI 相容伺服器**，所以：
* 驗得到我們自己的程式（解析、渲染、引用跳轉）
* **驗不到「模型會不會照做」** —— 那要拿真的模型測（本專案記過很多次）
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.core import meeting_insight as mi

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import browser_probe  # noqa: E402
from tools.browser_probe import profile_arg as _profile_arg  # noqa: E402

VTT = """WEBVTT

00:00:01.000 --> 00:00:06.000
<v 王小明>各位早，今天只談一件事：第四季的預算。

00:00:06.500 --> 00:00:12.000
<v 李美華>我看過草案了，行銷那一塊超出去兩百萬。

00:00:12.500 --> 00:00:20.000
<v 王小明>那就照原案走，行銷不加。李美華月底前把修訂版寄給法務。
"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _fake_llm(port: int) -> ThreadingHTTPServer:
    """最小的 OpenAI 相容端點 —— 依 prompt 裡的特徵回不同的固定答案。"""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 不要洗畫面
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            prompt = json.dumps(body, ensure_ascii=False)
            if "你是會議記錄整理員" in prompt:
                content = json.dumps({
                    "decisions": [{"text": "第四季行銷預算不加", "segment_ids": [3]}],
                    "actions": [{"text": "月底前把修訂版寄給法務", "owner": "李美華",
                                 "due_text": "月底", "segment_ids": [3]}],
                    "risks": [], "questions": []}, ensure_ascii=False)
            elif "切成" in prompt and "章節" in prompt:
                chapters = [{"title": "第四季預算", "start_seq": 1, "end_seq": 3}]
                # 兩個議題的那份素材（`VTT_TWO_TOPICS`）切成兩章 —— 只有一章的話
                # 「各議題佔比」那張圖根本不畫（一章就是 100%，畫了也沒有資訊），
                # 滑過長條的那條檢查就一路 skip（而 skip 跟通過在輸出裡長得一樣）。
                if "機房搬遷" in prompt:
                    chapters.append({"title": "機房搬遷", "start_seq": 4, "end_seq": 5})
                content = json.dumps({"chapters": chapters}, ensure_ascii=False)
            elif "三到五句" in prompt:
                content = json.dumps(
                    {"summary": "會議確認第四季行銷預算不加，修訂版月底前送法務。"},
                    ensure_ascii=False)
            else:
                content = json.dumps({"keep": [1, 2], "drop": [], "split": []},
                                     ensure_ascii=False)
            # **一定要用 SSE 回** —— `llm_client` 一律送 `stream: true`
            # （為了讓連線在長時間生成時保持活著），回一包普通 JSON 的話
            # 客戶端解不到任何 delta，看起來就像模型什麼都沒說。
            chunk = json.dumps({"choices": [{"delta": {"content": content}}]})
            body = (f"data: {chunk}\n\n" + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture(scope="module")
def live():
    import tempfile

    br_path = browser_probe.browser()
    if not br_path:
        pytest.skip("沒有 chromium")
    try:
        import websockets.sync.client  # noqa: F401
    except ImportError:
        pytest.skip("沒有 websockets")

    data = tempfile.mkdtemp(prefix="mse2e-")
    port, cdp, llm_port = _free_port(), _free_port(), _free_port()
    llm = _fake_llm(llm_port)

    # **要明寫「認證關閉」** —— 資料庫裡一有使用者，產品的 fail-secure
    # 就會自動改用本機認證，整站變成登入頁（v1.15.49 記過）。
    Path(data).mkdir(parents=True, exist_ok=True)
    (Path(data) / "auth_settings.json").write_text(
        json.dumps({"backend": "off"}), encoding="utf-8")
    (Path(data) / "llm_settings.json").write_text(json.dumps({
        "enabled": True,
        "base_url": f"http://127.0.0.1:{llm_port}/v1",
        "api_key": "x", "model": "fake", "timeout": 60,
    }), encoding="utf-8")

    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [br_path, "--headless=new", "--no-sandbox", "--disable-gpu",
         _profile_arg(), f"--remote-debugging-port={cdp}", "--remote-allow-origins=*", "about:blank"],
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

        # **素材要放在瀏覽器讀得到的地方** —— snap 版看到的 /tmp 是它自己的
        vtt = Path(browser_probe.uploadable_dir()) / "mse2e.vtt"
        vtt.write_text(VTT, encoding="utf-8")
        yield port, send, str(vtt)
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


def _wait(send, expr, secs=120):
    end = time.time() + secs
    while time.time() < end:
        if _eval(send, expr):
            return True
        time.sleep(0.4)
    return False



def _set_file(send, selector, path):
    """把檔案塞進 `<input type=file>`。

    **不要用 `DOM.getDocument` ＋ `DOM.querySelector` 拿 nodeId** ——
    導覽之後那份節點表會失效，症狀是
    `Could not find node with given id`，而且時好時壞（看導覽完成的時機）。
    改成先用 `Runtime.evaluate` 取到物件，再用 `DOM.requestNode` 換成 nodeId。
    """
    # **整棵樹都要抓（`depth: -1`）** —— 只抓 root 的話那個 input 不在
    # DOM agent 的節點表裡，`requestNode` 會回 `nodeId: 0`。
    # 而且要在**確認頁面已經就緒之後**才抓：導覽前拿的那一份會失效，
    # 症狀是 `Could not find node with given id`，時好時壞（看導覽完成的時機）。
    # **要重試** —— `DOM.documentUpdated` 事件會把節點表整份作廢，
    # 而它可能剛好落在 `getDocument` 與 `querySelector` 之間，
    # 症狀是 `Could not find node with given id`，**時好時壞**。
    # 看到這種「單跑會過、合跑會壞」的指紋就先懷疑共用狀態（本專案第 N 次）。
    #
    # **頁面要整頁載完才塞檔案**（2026-10-10 查到的偶發）：`msUp` 一出現就塞的話，
    # 頁尾的腳本可能還沒跑 —— 上傳元件還沒接上 `change`，或那個 input 之後被換掉，
    # 結果 input 裡 0 個檔案、伺服器一筆上傳都沒收到，測試等 40 秒判「解析結果沒出現」。
    # 所以先等 `readyState` 是 complete，塞完再確認 input 真的拿到檔案，沒有就重塞。
    _wait(send, "document.readyState === 'complete'", 30)
    # 上傳元件拿到檔案會把檔名寫進 `.drop-zone-filename` —— 兩個都看，元件清空 input 也不會被當成沒塞進去而重送
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
            if _wait(send, has, 3):
                return
            last = "塞了檔案但 input 裡沒有"
            continue
        last = node
        time.sleep(0.5)
    raise AssertionError(f"找不到 {selector}：{last}")

def test_the_whole_flow_works_in_a_real_browser(live):
    port, send, vtt = live
    send("Page.enable")
    send("Runtime.enable")
    send("DOM.enable")
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/meeting-summary/"})
    assert _wait(send, "!!document.getElementById('msUp')", 30), "頁面沒開起來"

    # ---- 上傳 ----
    _set_file(send, ".file-upload input[type=file]", vtt)

    assert _wait(send, "!document.getElementById('msParsed').hidden", 40), \
        "解析結果沒出現 —— 上傳那條路沒接上"

    # **發言者要認得出來**，不然發言者佔比整個沒有意義
    assert _eval(send, "document.getElementById('msNSpk').textContent") == "2"
    assert int(_eval(send, "document.getElementById('msNSeg').textContent")) >= 3
    assert _eval(send,
                 "document.getElementById('msPrev').querySelectorAll('.ms-prev-row').length") >= 3
    # 有時間 → 不可以顯示「沒有時間」那段警告
    assert _eval(send, "document.getElementById('msNoTimes').hidden") is True

    # ---- 分析 ----
    _eval(send, "document.getElementById('msStart').click(), 1")
    assert _wait(send, "!document.getElementById('msResult').hidden", 180), \
        "結果區沒出現"

    # 摘要真的畫出來了（不是空字串）
    summary = _eval(send, "document.getElementById('msSummary').textContent")
    assert summary and "行銷" in summary, f"摘要沒渲染：{summary!r}"

    # 四張卡片都在，而且決議那張有內容
    assert _eval(send, "document.querySelectorAll('#msCards .ms-card').length") \
        == len(mi.KINDS), "卡片張數要跟 `meeting_insight.KINDS` 一致"
    dec = _eval(send, "document.querySelector('#msCards .k-decision').textContent")
    assert "第四季行銷預算不加" in dec

    # **引用按鈕要真的存在**，那是這支工具的賣點
    cites = _eval(send, "document.querySelectorAll('#msCards .ms-cite').length")
    assert cites >= 1, "一個引用按鈕都沒有"

    # 逐字稿渲染出來了
    assert _eval(send, "document.querySelectorAll('#msScript .ms-line').length") >= 3


def test_clicking_a_citation_highlights_the_line_it_points_at(live):
    """**判準落在畫面上** —— 只驗「按鈕有 data-seq」的話，跳轉壞掉也會全綠。"""
    port, send, vtt = live
    assert _wait(send, "!document.getElementById('msResult').hidden", 10), \
        "要接在上一條之後跑"
    seq = _eval(send, "document.querySelector('#msCards .ms-cite').dataset.seq")
    assert seq
    _eval(send, "document.querySelector('#msCards .ms-cite').click(), 1")
    assert _wait(send, f"!!document.querySelector('#ms-seg-{seq}.hit')", 10), \
        "點了引用，對應的那一段沒有被標起來"


def test_the_mindmap_nodes_jump_to_the_transcript(live):
    """心智圖的節點要點得下去 —— **這是把圖搬到前端的唯一理由**。

    使用者 2026-09-19：「這圖改用前端產圖後 要可以點選 連去逐字稿」。
    原本是伺服器畫好的 `<img>`，裡面的東西完全點不動，而這支工具的賣點
    正是「每一格都指得回原文」。
    """
    port, send, vtt = live
    assert _wait(send, "!document.getElementById('msResult').hidden", 10), \
        "要接在第一條之後跑"
    if _eval(send, "document.getElementById('msMapWrap').hidden"):
        pytest.skip("這份素材畫不出心智圖（節點太少）")

    assert _wait(send,
                 "(function(){var s=document.querySelector('#msMap svg');"
                 "if(!s)return false;var r=s.getBoundingClientRect();"
                 "return r.width>50 && r.height>20;})()", 20), \
        "心智圖沒有畫出來，或是沒有佔到空間"

    seq = _eval(send, "(function(){var g=document.querySelector('#msMap [data-seq]');"
                      "return g ? g.getAttribute('data-seq') : null;})()")
    assert seq, "心智圖上沒有任何可以點的節點"
    _eval(send, "document.querySelector('#msMap [data-seq]')"
                ".dispatchEvent(new MouseEvent('click',{bubbles:true})), 1")
    assert _wait(send, f"!!document.querySelector('#ms-seg-{seq}.hit')", 10), \
        "點了心智圖上的節點，逐字稿沒有跳過去"


def test_the_transcript_is_not_trapped_in_its_own_scrollbar(live):
    """逐字稿要整份攤開（使用者 2026-09-19：「不要區塊有捲軸」）。

    區塊內捲軸有兩個問題：點引用跳過去變成在一個小視窗裡捲動，
    而且用頁面捲軸永遠看不到後面的內容。
    """
    port, send, vtt = live
    assert _wait(send, "!document.getElementById('msResult').hidden", 10)
    over = _eval(send, "getComputedStyle(document.getElementById('msScript')).overflowY")
    assert over in ("visible", "auto"), f"overflow-y 是 {over!r}"
    trapped = _eval(send, "(function(){var s=document.getElementById('msScript');"
                          "return s.scrollHeight > s.clientHeight + 4;})()")
    assert trapped is False, "逐字稿被關在自己的捲軸裡 —— 那一區不可以設 max-height"


def test_the_charts_are_not_scaled_by_css(live):
    """圖畫多寬就顯示多寬 —— **不可以被 CSS 縮放**。

    使用者同一天回報了兩個方向：「圖二字又過大」（容器隱藏時量到 0，
    退到預設寬度再被 `width:100%` 撐大 1.7 倍）與「寬度不夠時 字又過小」
    （畫得比容器寬，被 `max-width:100%` 壓下去）。**兩個的症狀都是字級不對**，
    而根因都是「畫的寬度」跟「顯示的寬度」對不起來。

    判準就落在那兩個數字上：`viewBox` 的寬度要等於實際畫面寬度
    （容許 3% —— 捲軸出現 / 次像素）。
    """
    port, send, vtt = live
    assert _wait(send, "!document.getElementById('msResult').hidden", 10)
    for box in ("msSpkChart", "msTimeline", "msMap"):
        got = _eval(send, "(function(){var s=document.querySelector('#" + box + " svg');"
                          "if(!s)return null;var vb=(s.getAttribute('viewBox')||'').split(' ');"
                          "return {vb:parseFloat(vb[2]),"
                          "real:s.getBoundingClientRect().width};})()")
        if not got or not got.get("vb"):
            continue
        vb, real = got["vb"], got["real"]
        assert abs(real - vb) <= vb * 0.03, (
            f"#{box}：viewBox 寬 {vb}，畫面上是 {real} —— "
            "被 CSS 縮放了，字級會跟著跑掉")


#: 兩個議題的會議 —— 假模型看到「機房搬遷」會切成兩章（見 `_fake_llm`）。
#: **前三段跟 `VTT` 一字不差**：之後的測試撈「最新一件已完成的分析」來驗，
#: 撈到這一件也要對得起來（決議引用第 3 段、發言者同樣兩位）。
VTT_TWO_TOPICS = VTT + """
00:00:21.000 --> 00:00:30.000
<v 李美華>另外機房搬遷排在下個月，機櫃要先清點。

00:00:31.000 --> 00:00:40.000
<v 王小明>機房搬遷那天停機四小時，公告下週發。
"""


def test_hovering_a_bar_lights_up_its_legend_row(live):
    """滑過長條，對應的圖例亮著、其餘變淡（2026-09-19 使用者要求）。

    量的是**章節佔比**那張圖 —— 發言者那張已經併進表格了。

    **這條以前一直是 skip**（「章節圖上只有 0 個可對應的列」）：假模型只切得出一章，
    而一章的會議那張圖本來就不畫（`meeting_charts.js` 的 `timeline` 要兩章以上）——
    不是圖壞了，是素材不夠。現在用兩個議題的素材自己跑一件，**一定要真的量到**：
    量不到就紅，不 skip。

    **滑鼠是真的移過去**（CDP `Input.dispatchMouseEvent`），判準落在畫面上：
    其餘的列真的變淡（算出來的不透明度），不是只看 class —— 樣式那一條被拿掉的話，
    class 照樣切換、畫面卻一點變化都沒有。
    """
    port, send, _vtt = live
    path = Path(browser_probe.uploadable_dir()) / "mse2e-two-topics.vtt"
    path.write_text(VTT_TWO_TOPICS, encoding="utf-8")
    _fresh_upload(port, send, str(path))
    _eval(send, "document.getElementById('msStart').click(), 1")
    assert _wait(send, "!document.getElementById('msResult').hidden && "
                       "!document.getElementById('msChapWrap').hidden && "
                       "document.querySelectorAll('#msTimeline .mc-row').length >= 4", 180), (
        "兩個議題的會議，「各議題佔比」那張圖沒有畫出來（應該有兩條長條＋兩列圖例）")
    n = _eval(send, "document.querySelectorAll('#msTimeline .mc-row').length")
    assert n == 4, f"兩章應該是兩條長條＋兩列圖例，實際 {n} 個"

    # 圖畫完會「照鏡子」：量到的寬度跟畫的差太多就重畫一次（整張 SVG 換掉）—— 滑鼠移上去之後
    # 才換掉的話，亮著的狀態跟著舊的那張一起不見。先等那張圖穩定下來（同一個節點維持 1 秒）。
    _eval(send, "window.__tlSvg = document.querySelector('#msTimeline svg'), 1")
    stable_since = time.time()
    end = time.time() + 15
    while time.time() < end and time.time() - stable_since < 1.0:
        time.sleep(0.2)
        if not _eval(send, "document.querySelector('#msTimeline svg') === window.__tlSvg"):
            _eval(send, "window.__tlSvg = document.querySelector('#msTimeline svg'), 1")
            stable_since = time.time()

    state = """(function(){
      var rows = document.querySelectorAll('#msTimeline .mc-row');
      var lit = 0, faded = 0, fadedOpacity = [];
      for (var i=0;i<rows.length;i++){
        if (rows[i].classList.contains('mc-lit')) lit++;
        if (rows[i].classList.contains('mc-faded')) {
          faded++;
          fadedOpacity.push(parseFloat(getComputedStyle(rows[i]).opacity));
        }
      }
      return {lit: lit, faded: faded, total: rows.length, fadedOpacity: fadedOpacity};
    })()"""
    # 把第一條長條捲進畫面，量它在視窗裡的位置，**真的把滑鼠移過去**（先移到別處，
    # 確定這一次是「移進來」—— 滑鼠本來就停在那個位置的話，瀏覽器不會再發一次 mouseover）
    out = None
    for _attempt in range(3):
        pos = _eval(send, """(function(){
          var bar = document.querySelector('#msTimeline .mc-row rect');
          bar.scrollIntoView({block: 'center'});
          var r = bar.getBoundingClientRect();
          return {x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width};
        })()""")
        assert pos and pos["w"] > 0, f"長條沒有佔到空間：{pos}"
        send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 2, "y": 2})
        send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": pos["x"], "y": pos["y"]})
        # 不透明度有 0.12 秒的轉場 —— 等它落定
        if _wait(send, "(function(){ var f = document.querySelector('#msTimeline .mc-row.mc-faded');"
                       " return !!f && parseFloat(getComputedStyle(f).opacity) < 0.5; })()", 3):
            break
    out = _eval(send, state)
    assert out["lit"] == 2, f"滑過去之後，那一個議題的長條與圖例應該一起亮著：{out}"
    assert out["faded"] == 2, f"其餘的列沒有變淡 —— 那就看不出在對應哪一個：{out}"
    assert out["lit"] + out["faded"] == out["total"], f"有列兩種狀態都沒有：{out}"
    assert all(o < 0.5 for o in out["fadedOpacity"]), (
        f"class 換了，畫面上卻沒有變淡（樣式沒有套上）：{out['fadedOpacity']}")

    # 滑鼠移開 → 全部恢復
    send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 2, "y": 2})
    assert _wait(send, "document.querySelectorAll('#msTimeline .mc-row.mc-faded, "
                       "#msTimeline .mc-row.mc-lit').length === 0", 5), "滑鼠移開之後沒有恢復"


def test_the_shared_download_button_is_not_duplicated(live):
    """結果區自己有一整排下載按鈕，共用的那顆不可以再冒出來。

    使用者 2026-09-19：「為何這裡會有 下載 .json 檔案? 是弄錯嗎
    下面不就有一排按鈕了」。
    """
    port, send, vtt = live
    assert _wait(send, "!document.getElementById('msResult').hidden", 10)
    shown = _eval(send, "(function(){var b=document.querySelector('#msJob .job-download');"
                        "return b ? !b.hidden : false;})()")
    assert shown is False, "共用進度列的下載鈕還看得到 —— 跟結果區那排重複了"


def test_the_chapter_timeline_spine_has_no_gaps(live):
    """時間軸那條線要連得起來（2026-09-19 使用者回報「都有打空白」）。

    線是每一列各畫一段（`.ms-chap-dot::before`），而 `align-self:stretch`
    只撐到**內容框** —— 列有上下內距，所以每兩列之間會空一段，
    軸線就變成一截一截的。

    **判準量的是「線有沒有蓋過那段內距」**，不是「有沒有畫線」——
    只驗有沒有線的話，斷成一截一截照樣全綠。

    **不靠模型產出的章節**：假模型只切得出一章，這條就會一路 skip，
    而「跳過」跟「通過」在 pytest 輸出裡長得一模一樣（本專案記過很多次）。
    改成自己塞三列進去量中間那一列 —— 要驗的本來就是樣式表的幾何關係，
    跟章節內容無關。量完還原，不要留給後面的測試。
    """
    port, send, vtt = live
    assert _wait(send, "!document.getElementById('msResult').hidden", 10)
    got = _eval(send, """(function(){
      var box = document.getElementById('msChap');
      var keep = box.innerHTML, wrap = document.getElementById('msChapWrap');
      var wasHidden = wrap.hidden;
      wrap.hidden = false;
      var one = '<button type="button" class="ms-chap-row">'
              + '<span class="ms-chap-t">0:00</span>'
              + '<span class="ms-chap-dot"><i></i></span>'
              + '<span class="ms-chap-body"><span class="ms-chap-name">x</span>'
              + '<span class="ms-chap-bar"><span></span><em>1%</em></span></span>'
              + '</button>';
      box.innerHTML = one + one + one;
      var r = box.querySelectorAll('.ms-chap-row')[1];
      var dot = r.querySelector('.ms-chap-dot');
      var cs = getComputedStyle(r), ls = getComputedStyle(dot, '::before');
      var out = {padTop: parseFloat(cs.paddingTop),
                 padBottom: parseFloat(cs.paddingBottom),
                 lineTop: parseFloat(ls.top), lineBottom: parseFloat(ls.bottom),
                 width: parseFloat(ls.width)};
      box.innerHTML = keep; wrap.hidden = wasHidden;
      return out;
    })()""")
    assert got, "量不到那三列"
    assert got["width"] > 0, "根本沒有畫出軸線"
    assert got["padTop"] > 0, "列沒有上下內距的話，這條檢查就沒有意義了"
    assert got["lineTop"] <= -got["padTop"] + 0.5, (
        f"軸線上方少蓋了 {got['padTop'] + got['lineTop']:.1f}px 的內距 —— 線會斷開")
    assert got["lineBottom"] <= -got["padBottom"] + 0.5, (
        f"軸線下方少蓋了 {got['padBottom'] + got['lineBottom']:.1f}px 的內距")


def test_the_speaking_distribution_lives_in_the_table(live):
    """發言分布與統計**合併成同一張表**（使用者 2026-09-19 指示）。

    原本是左圖右表並排，要逐列對齊 —— 順序、列高、起點都得一致，
    而列高受字型與瀏覽器影響，量出來還是會差幾個像素
    （實測對完中線仍差 8~11px，而且每一列的誤差還不一樣）。
    合併之後**由結構保證對齊**，一列就是一列，不需要量任何東西。

    判準三條，缺一不可：
    * 分布那一欄真的在表格裡（不是另一張並排的圖）
    * 色塊用**百分比**定位 —— 換欄寬不必重畫，也不會再有對齊問題
    * 整列點得下去，跳到那個人第一次發言的地方
    """
    port, send, vtt = live
    assert _wait(send, "!document.getElementById('msResult').hidden", 10)
    if _eval(send, "document.getElementById('msSpkWrap').hidden"):
        pytest.skip("這份素材沒有發言者區")

    assert _eval(send, "!!document.querySelector('#msSpkTable .ms-trk')"), \
        "「發言分布」沒有在表格裡 —— 合併沒有做到"
    assert _eval(send, "!document.getElementById('msSpkChart')"), \
        "還留著並排的那張圖，會跟表格重複"

    got = _eval(send, """(function(){
      var m = document.querySelector('#msSpkTable .ms-trk-bg > i');
      if (!m) return null;
      return {left: m.style.left, width: m.style.width,
              marks: document.querySelectorAll('#msSpkTable .ms-trk-bg > i').length,
              rows: document.querySelectorAll('#msSpkTable tr[data-seq]').length};
    })()""")
    assert got, "分布那一欄裡一個色塊都沒有"
    assert got["left"].endswith("%") and got["width"].endswith("%"), (
        f"色塊不是用百分比定位（left={got['left']!r} width={got['width']!r}）"
        " —— 換欄寬就會跑掉")
    assert got["rows"] >= 2, "表格沒有可以點的列"

    seq = _eval(send, "document.querySelector('#msSpkTable tr[data-seq]').dataset.seq")
    assert seq, "列上沒有段號，點了跳不到逐字稿"
    _eval(send, "document.querySelector('#msSpkTable tr[data-seq]')"
                ".dispatchEvent(new MouseEvent('click',{bubbles:true})), 1")
    assert _wait(send, f"!!document.querySelector('#ms-seg-{seq}.hit')", 10), \
        "點了發言者那一列，逐字稿沒有跳過去"


def test_opening_from_my_jobs_shows_the_result(live):
    """從「我的作業」按「開啟」要看得到結果（2026-09-19 回報「下面沒東西」）。

    網址帶 `?job=…` 回來時，作業早就跑完了 —— 輪詢第一次就讀到 `done`，
    接著去讀結果，但**那一步需要 `upload_id`，而它是上傳當下才有的**，
    重新開一頁是空的，於是安靜地什麼都不做：進度列寫著「完成」，下面一片空白。

    判準是**真的重新載入一次**那個網址，然後看結果區有沒有東西 ——
    只驗「有沒有讀 `?job=`」的話，讀了卻沒接上照樣全綠。
    """
    port, send, vtt = live
    # 作業編號從「我的作業」那支清單 API 撈 —— 跟畫面上那顆「開啟」同一個來源。
    jid = _eval(send, """fetch('/api/jobs', {headers:{Accept:'application/json'}})
      .then(function(r){ return r.json(); })
      .then(function(d){
        var list = Array.isArray(d) ? d : (d.jobs || d.items || []);
        for (var i=0;i<list.length;i++){
          var j = list[i];
          if ((j.tool === 'meeting-summary' || j.tool_id === 'meeting-summary')
              && j.status === 'done') return j.id || j.job_id;
        }
        return null;
      })""")
    assert jid, ("撈不到已完成的會議摘要作業 —— 這條測試存在的理由就是那條路，"
                 "撈不到就等於沒驗（「跳過」跟「通過」在輸出裡長得一樣）")

    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/meeting-summary/?job={jid}"})
    assert _wait(send, "!!document.getElementById('msResult')", 30), "頁面沒開起來"
    assert _wait(send, "!document.getElementById('msResult').hidden", 30), \
        "從「我的作業」開啟之後結果區沒有出現"
    # **張數從 `KINDS` 算** —— 寫死的話加第六類時會紅得莫名其妙，
    # 而且紅的是這支 e2e（跑一次要好幾分鐘），不是那個真正該提醒的地方。
    assert _wait(send, "document.querySelectorAll('#msCards .ms-card').length === "
                 f"{len(mi.KINDS)}", 15), \
        "結果區是空的 —— `?job=` 接回來之後沒有讀到結果"
    assert _eval(send, "document.querySelectorAll('#msScript .ms-line').length") >= 3, \
        "逐字稿沒有接回來"


def test_the_topic_bar_sits_close_to_its_title(live):
    """長度條要**貼著**它的標題（2026-09-19 使用者要求）。

    標題與它底下的長度條是同一件事，離太遠就看起來像兩列。
    間距來自兩處：列的行高（一般內文約 1.5，標題底下會多出三四個像素）
    與長度條自己的 `margin-top` —— **只調其中一個不夠**。

    **判準量的是畫面上的距離**，不是 CSS 的數值：改行高、改 margin、
    改字級都會動到它，量座標才涵蓋得了全部。
    """
    port, send, vtt = live
    assert _wait(send, "!document.getElementById('msResult').hidden", 10)
    gap = _eval(send, """(function(){
      var box = document.getElementById('msChap');
      var keep = box.innerHTML, wrap = document.getElementById('msChapWrap');
      var wasHidden = wrap.hidden; wrap.hidden = false;
      box.innerHTML = '<button type="button" class="ms-chap-row">'
        + '<span class="ms-chap-t">0:00</span>'
        + '<span class="ms-chap-dot"><i></i></span>'
        + '<span class="ms-chap-body">'
        + '<span class="ms-chap-name">議題標題</span>'
        + '<span class="ms-chap-bar"><span></span><em>9%</em></span>'
        + '</span></button>';
      var nameEl = box.querySelector('.ms-chap-name');
      var n = nameEl.getBoundingClientRect();
      var b = box.querySelector('.ms-chap-bar').getBoundingClientRect();
      var cs = getComputedStyle(nameEl);
      var out = {gap: b.top - n.bottom,
                 lh: parseFloat(cs.lineHeight) / parseFloat(cs.fontSize)};
      box.innerHTML = keep; wrap.hidden = wasHidden;
      return out;
    })()""")
    assert gap is not None, "量不到那一列"
    # **門檻是量出來的**：收緊前 4.00px、收緊後 1.00px，取中間的 2.5。
    # 寫 4.0 的話舊值剛好過關（實測踩過）——
    # 「看起來合理的整數」很容易變成永遠成立的門檻。
    assert gap["gap"] <= 2.5, (
        f"色條離標題 {gap['gap']:.1f}px —— 太遠，看起來像兩列"
        "（收緊前是 4.0、收緊後是 1.0）")
    # `getBoundingClientRect` 量不到**行距**（行高收緊時方塊跟著縮，
    # 上面那個間距不會變），所以行高要另外驗 —— 只驗間距的話，
    # 有人把行高調回 1.5，標題底下多出三四個像素照樣全綠。
    assert gap["lh"] <= 1.35, (
        f"標題的行高是字級的 {gap['lh']:.2f} 倍 —— 底下會多出空白，"
        "色條看起來仍然離得遠")


def test_the_longest_topic_label_stays_on_one_line(live):
    """最長的那一列，百分比與時間不可以被擠到折行（2026-09-19 回報）。

    長條原本直接跟文字搶寬度 —— 最長的那一列長條佔滿，文字就折成兩行。
    改成長條畫在一條**固定寬度的軌道**裡，文字永遠有位置。

    **判準是那段文字的高度是不是只有一行**，不是「CSS 有沒有 nowrap」——
    換個寫法擠壓到它一樣會折行。
    """
    port, send, vtt = live
    assert _wait(send, "!document.getElementById('msResult').hidden", 10)
    got = _eval(send, """(function(){
      var box = document.getElementById('msChap');
      var keep = box.innerHTML, wrap = document.getElementById('msChapWrap');
      var wasHidden = wrap.hidden; wrap.hidden = false;
      // 最長的那一列（長條 100%）＋ 一個長的標題，最容易擠到文字
      box.innerHTML = '<button type="button" class="ms-chap-row">'
        + '<span class="ms-chap-t">29:17</span>'
        + '<span class="ms-chap-dot"><i></i></span>'
        + '<span class="ms-chap-body">'
        + '<span class="ms-chap-name">大促變更凍結與壓力測試規劃</span>'
        + '<span class="ms-chap-bar"><span class="ms-chap-track">'
        + '<i style-placeholder></i></span><em>15.2%　6:30</em></span>'
        + '</span></button>';
      var fill = box.querySelector('.ms-chap-track > i');
      if (fill) fill.style.width = '100%';
      var em = box.querySelector('.ms-chap-bar > em');
      var r = em.getBoundingClientRect();
      var lh = parseFloat(getComputedStyle(em).fontSize) * 2;
      var out = {h: r.height, twoLines: lh};
      box.innerHTML = keep; wrap.hidden = wasHidden;
      return out;
    })()""")
    assert got, "量不到那一列"
    assert got["h"] < got["twoLines"], (
        f"百分比與時間佔了 {got['h']:.0f}px —— 折行了"
        f"（兩行大約 {got['twoLines']:.0f}px）")


def test_clicking_a_block_jumps_to_that_moment_not_the_first_one(live):
    """點發言分布上的色塊，要跳到**那個時間**講的那一段。

    使用者 2026-09-19：「這裡應是對到對應時間位置的色塊 就要跳去時間對應的行
    不是只跳相關的第一行」。原本整列共用一個段號（那個人第一次發言），
    所以不管點哪一格都跳到同一個地方 —— 而使用者眼前看到的是某個時間點。

    **判準是點第二格跳到的段號跟第一格不同**，不是「有沒有 data-seq」——
    整列共用同一個段號時，每一格也都「有」那個屬性。
    """
    port, send, vtt = live
    assert _wait(send, "!document.getElementById('msResult').hidden", 10)
    if _eval(send, "document.getElementById('msSpkWrap').hidden"):
        pytest.skip("這份素材沒有發言者區")
    # **先找「色塊」，不要先要求它有段號** —— 用 `i[data-seq]` 當選擇器的話，
    # 沒有段號時找不到東西 → skip，而「跳過」跟「通過」在輸出裡長得一樣
    # （實測：拿掉每格的段號，這條只是 skip 不是紅）。
    got = _eval(send, """(function(){
      var rows = document.querySelectorAll('#msSpkTable tr[data-seq]');
      for (var i=0;i<rows.length;i++){
        var m = rows[i].querySelectorAll('.ms-trk-bg > i');
        if (m.length >= 2) return {row: rows[i].dataset.seq,
                                   a: m[0].getAttribute('data-seq'),
                                   b: m[1].getAttribute('data-seq'),
                                   n: m.length};
      }
      return null;
    })()""")
    assert got, "沒有一列有兩格以上的色塊 —— 這條檢查等於沒有執行"
    assert got["a"] and got["b"], (
        "色塊沒有自己的段號 —— 點下去只會跳到這個人第一次發言的地方")
    assert got["a"] != got["b"], (
        f"兩格色塊指向同一段（{got['a']}）—— 還是整列共用一個段號")

    _eval(send, """(function(){
      var rows = document.querySelectorAll('#msSpkTable tr[data-seq]');
      for (var i=0;i<rows.length;i++){
        var m = rows[i].querySelectorAll('.ms-trk-bg > i[data-seq]');
        if (m.length >= 2) { m[1].dispatchEvent(new MouseEvent('click',{bubbles:true})); return 1; }
      }
    })()""")
    assert _wait(send, f"!!document.querySelector('#ms-seg-{got['b']}.hit')", 10), (
        f"點了第二格，逐字稿沒有跳到第 {got['b']} 段")


def _scroll_state(send):
    return _eval(send, """(() => {
      const b = document.getElementById('msStart').getBoundingClientRect();
      return {y: window.scrollY, top: b.top, bottom: b.bottom, h: window.innerHeight};
    })()""")


def test_arriving_from_the_transcribe_tool_scrolls_to_the_start_button(live):
    """從「會議錄音轉逐字稿」按「轉送會議摘要」過來：解析完直接捲到「開始分析」
    （使用者 2026-09-23 要求 —— 上面的上傳區與背景欄位都不用再看）。

    工作區在認證關閉的測試實例裡用不了，所以**只把取檔那一步換掉**
    （頁面載入前包一層 `fetch`，`/workspace/file/…` 直接回逐字稿），
    網址參數、上傳元件接手、解析、畫面都是真的。
    """
    port, send, vtt = live
    send("Page.enable"); send("Runtime.enable")
    body = json.dumps(Path(vtt).read_text(encoding="utf-8"))
    added = send("Page.addScriptToEvaluateOnNewDocument", {"source": """
      (() => {
        const real = window.fetch.bind(window);
        window.fetch = (u, o) => String(u).startsWith('/workspace/file/')
          ? Promise.resolve(new Response(%s, {status: 200,
              headers: {'Content-Type': 'text/vtt'}}))
          : real(u, o);
      })();""" % body})
    try:
        send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/meeting-summary/"
                                       f"?from_ws={'a' * 32}&from_name=mse2e.vtt"})
        assert _wait(send, "!!document.getElementById('msParsed') && "
                           "!document.getElementById('msParsed').hidden", 60), "轉送來的逐字稿沒有解析出來"
        time.sleep(1.5)                       # 平滑捲動要一點時間
        st = _scroll_state(send)
        assert st["y"] > 0, f"畫面沒有往下捲：{st}"
        assert 0 <= st["top"] and st["bottom"] <= st["h"], f"「開始分析」不在畫面裡：{st}"
    finally:
        send("Page.removeScriptToEvaluateOnNewDocument",
             {"identifier": added["result"]["identifier"]})


def test_uploading_by_hand_does_not_jump_the_page(live):
    """反向對照：自己拖檔案進來時**不可以**自己捲走 —— 只有轉送過來才捲。
    只驗上一條的話，改成「每次解析完都捲」也會過。"""
    port, send, vtt = live
    send("Page.enable"); send("Runtime.enable"); send("DOM.enable")
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/meeting-summary/"})
    assert _wait(send, "document.readyState === 'complete'", 30)
    time.sleep(0.5)
    _set_file(send, ".file-upload input[type=file]", vtt)
    assert _wait(send, "!!document.getElementById('msParsed') && "
                       "!document.getElementById('msParsed').hidden", 60)
    time.sleep(1.5)
    st = _scroll_state(send)
    assert st["y"] == 0, f"自己上傳時畫面被捲走了：{st}"


def test_renaming_in_the_transcript_updates_the_cards_and_the_table(live):
    """**逐字稿改名，其他區塊要跟著換**（v1.16.10，使用者截圖回報：
    逐字稿改成新名字之後，待辦卡片還寫「負責：舊名字」、「發言統計」也還是舊的）。

    判準落在**畫面上**：伺服器改對了但前端沒重畫的話，使用者看到的一樣是舊名字。
    """
    port, send, vtt = live
    send("Page.enable"); send("Runtime.enable"); send("DOM.enable")
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/meeting-summary/"})
    assert _wait(send, "document.readyState === 'complete' && "
                       "!!document.getElementById('msUp')", 30)
    time.sleep(0.5)
    _set_file(send, ".file-upload input[type=file]", vtt)
    assert _wait(send, "!document.getElementById('msParsed').hidden", 60)
    _eval(send, "document.getElementById('msStart').click(), 1")
    assert _wait(send, "!document.getElementById('msResult').hidden", 180)
    assert "李美華" in _eval(send, "document.querySelector('#msCards .k-action').textContent")

    # 在逐字稿點「李美華」→ 改成「李經理」→ Enter（範圍預設是「全部」）
    assert _eval(send, """(function(){
      var sp = Array.prototype.find.call(
        document.querySelectorAll('#msScript .ms-spk'),
        function (e) { return e.textContent === '李美華'; });
      if (!sp) return false;
      sp.click();
      var inp = sp.querySelector('input.ms-name-edit');
      if (!inp) return false;
      inp.value = '李經理';
      inp.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
      return true;
    })()"""), "逐字稿裡點不到「李美華」或改名框沒出現"

    assert _wait(send, "document.querySelector('#msCards .k-action').textContent"
                       ".indexOf('李經理') >= 0", 20), (
        "待辦卡片還是舊名字 —— 改名只改到逐字稿")
    assert "李美華" not in _eval(send,
                                "document.querySelector('#msCards .k-action').textContent")
    if not _eval(send, "document.getElementById('msSpkWrap').hidden"):
        table = _eval(send, "document.getElementById('msSpkTable').textContent")
        assert "李經理" in table and "李美華" not in table, (
            f"「發言統計」沒有跟著換：{table[:120]!r}")


# ---------------------------------------------------------------------------
# v1.16.37：版面主題預覽、HTML 匯出、存至工作區（2026-10-02 使用者要求）
#
# 這三樣都是**畫面上的接線**：伺服器端各自單獨測得到（預覽端點、下載端點），
# 測不到的是「按鈕有沒有接上、預覽框裡有沒有東西、匯出的檔案是不是那一頁」——
# 本專案這一類的 bug 全部長成「元素都在、沒有例外、按了沒反應」。
# ---------------------------------------------------------------------------

_FIND_DONE = """fetch('/api/jobs', {headers:{Accept:'application/json'}})
      .then(function(r){ return r.json(); })
      .then(function(d){
        var list = Array.isArray(d) ? d : (d.jobs || d.items || []);
        for (var i=0;i<list.length;i++){
          var j = list[i];
          if ((j.tool === 'meeting-summary' || j.tool_id === 'meeting-summary')
              && j.status === 'done') return j.id || j.job_id;
        }
        return null;
      })"""


def _open_done_result(send, port, vtt):
    """打開一件已完成的會議摘要。**沒有就自己跑一件** —— 不依賴前面那幾條先跑過
    （用 `-k` 單跑時前面的測試不會執行）。"""
    send("Page.enable"); send("Runtime.enable"); send("DOM.enable")
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/meeting-summary/"})
    assert _wait(send, "document.readyState === 'complete' && "
                       "!!document.getElementById('msUp')", 30)
    jid = _eval(send, _FIND_DONE)
    if not jid:
        time.sleep(0.5)
        _set_file(send, ".file-upload input[type=file]", vtt)
        assert _wait(send, "!document.getElementById('msParsed').hidden", 60)
        _eval(send, "document.getElementById('msStart').click(), 1")
        assert _wait(send, "!document.getElementById('msResult').hidden", 180)
        jid = _eval(send, _FIND_DONE)
    assert jid, "撈不到已完成的會議摘要作業 —— 沒有東西可驗（跳過不等於通過）"
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/meeting-summary/?job={jid}"})
    assert _wait(send, "!!document.getElementById('msResult') && "
                       "!document.getElementById('msResult').hidden", 30)
    assert _wait(send, "document.querySelectorAll('#msCards .ms-card').length > 0", 15)


_PREVIEW_TH = """(function(){
  var f = document.getElementById('msThemePreview');
  try {
    var th = f && f.contentDocument && f.contentDocument.querySelector('table th');
    return th ? getComputedStyle(th).backgroundColor : null;
  } catch (e) { return null; }
})()"""


def test_the_theme_preview_shows_the_chosen_theme_and_is_remembered(live):
    """選主題之前要看得出會長怎樣，而且**換了主題預覽要跟著換**。

    判準是預覽框**裡面**的表頭底色 —— 只驗 iframe 的 `src` 有沒有變的話，
    預覽端點壞掉（或被 CSP 擋掉）照樣全綠。
    """
    port, send, vtt = live
    _open_done_result(send, port, vtt)
    _eval(send, "document.getElementById('msThemePreview').scrollIntoView(), 1")
    assert _wait(send, _PREVIEW_TH, 20), "預覽框裡沒有表格 —— 預覽沒有畫出來"
    assert _eval(send, "document.getElementById('msTheme').value") == "classic", (
        "預設主題應該是清爽（畫面上標「預設」的那一個）")
    from app.tools.markdown_to_doc import themes as _th
    # 下拉是本專案的 `jt-select`，不是瀏覽器原生的（使用者 2026-10-03）：
    # 原生的那個藏起來、旁邊有自己畫的按鈕，每個主題前面一排三個色票、下面一句說明
    shown = json.loads(_eval(send, """JSON.stringify((function(){
      var s = document.getElementById('msTheme'), w = s.closest('.jt-select-wrap');
      if (!w) return null;
      var dot = function (d) { return getComputedStyle(d).backgroundColor; };
      return {native_hidden: s.classList.contains('jt-select-native'),
              label: w.querySelector('.jt-select-trigger .jt-select-label').textContent,
              trig_dots: Array.from(w.querySelectorAll('.jt-select-trigger .jt-select-swatch')).map(dot),
              opts: Array.from(w.querySelectorAll('.jt-select-option')).map(function (o) {
                return {v: o.dataset.value,
                        dots: Array.from(o.querySelectorAll('.jt-select-swatch')).map(dot),
                        sub: (o.querySelector('.jt-select-option-sub') || {}).textContent || ''};
              })};
    })())"""))
    assert shown, "版面主題還是瀏覽器原生的下拉 —— 沒有套上本專案的樣式"
    assert shown["native_hidden"], shown
    assert shown["label"] == _th.THEMES["classic"]["name"], shown["label"]
    assert len(shown["trig_dots"]) == 3, f"下拉按鈕上沒有色票：{shown['trig_dots']}"
    assert [o["v"] for o in shown["opts"]] == list(_th.THEMES), "下拉裡的主題跟主題清單對不上"
    for o in shown["opts"]:
        assert len(o["dots"]) == 3 and all(d not in ("rgba(0, 0, 0, 0)", "transparent")
                                           for d in o["dots"]), (
            f"{o['v']} 的色票沒有顏色（CSP 擋掉了？）：{o['dots']}")
        assert o["sub"], f"{o['v']} 沒有說明"
    before = _eval(send, _PREVIEW_TH)
    try:
        # 照使用者的操作：點開下拉、點「商務報告」
        _eval(send, "(function(){ var w = document.getElementById('msTheme')"
                    ".closest('.jt-select-wrap'); w.querySelector('.jt-select-trigger').click();"
                    " w.querySelector('.jt-select-option[data-value=\"report\"]').click();"
                    " return 1; })()")
        assert _eval(send, "document.getElementById('msTheme').value") == "report", (
            "點了下拉裡的選項，原本的 select 沒有跟著變")
        assert _wait(send, f"(function(){{ var c = {_PREVIEW_TH}; "
                           f"return c && c !== {json.dumps(before)}; }})()", 20), (
            "換成商務報告之後預覽的表頭顏色沒變 —— 預覽沒有跟著主題走")
        # 記住選擇：重新開一次頁面，下拉要停在剛剛選的那一個 ——
        # **顯示的那一份也要**（只改了隱藏的 select 的話，畫面寫「清爽」、下載的是商務報告）
        _open_done_result(send, port, vtt)
        assert _eval(send, "document.getElementById('msTheme').value") == "report", (
            "重新開頁之後主題又回到預設 —— 沒有記住使用者的選擇")
        assert _wait(send, "(document.getElementById('msTheme').closest('.jt-select-wrap')"
                           ".querySelector('.jt-select-label') || {}).textContent === "
                           + json.dumps(_th.THEMES["report"]["name"]), 5), (
            "記住的主題沒有顯示在下拉上 —— 畫面跟實際下載的主題不一樣")
        href = _eval(send, "document.getElementById('msDlPdf').getAttribute('href') || ''")
        assert "theme=report" in href, f"下載網址沒有帶記住的主題：{href}"
    finally:
        _eval(send, "(function(){ try { localStorage.removeItem('jtdt.meetingSummary.theme'); }"
                    " catch (e) {} return 1; })()")


def test_every_card_heading_shows_its_icon_on_the_page_and_in_the_export(live):
    """五張卡片的標題圖示要真的畫出線條 —— 查不到圖示時畫的是**空的 `<svg>`**，
    沒有錯誤、版面也沒跑掉，只是少了圖示（「事件與影響」那張就是這樣一直沒有）。
    匯出的 HTML 是整頁複製，所以兩邊一起驗。"""
    port, send, vtt = live
    _open_done_result(send, port, vtt)
    heads = _eval(send, """JSON.stringify(Array.from(
        document.querySelectorAll('#msCards .ms-card > h3')).map(function (h) {
          var svg = h.querySelector('svg');
          return {k: h.parentNode.className, n: svg ? svg.children.length : -1};
        }))""")
    import json as _json
    heads = _json.loads(heads)
    assert len(heads) >= 5, heads
    empty = [h["k"] for h in heads if h["n"] < 1]
    assert not empty, f"這幾張卡片的標題圖示是空的：{empty}"
    _eval(send, """(function(){ window.__blobs = [];
      var o = URL.createObjectURL.bind(URL);
      URL.createObjectURL = function (b) { window.__blobs.push(b); return o(b); };
      document.getElementById('msDlHtml').click(); return 1; })()""")
    assert _wait(send, "window.__blobs && window.__blobs.length > 0", 30), "按了 HTML 沒有產生檔案"
    html = _eval(send, "window.__blobs[window.__blobs.length - 1].text()")
    import re as _re
    titles = _re.findall(r'<section class="ms-card[^"]*"><h3>(<svg.*?</svg>)', html, _re.S)
    assert len(titles) == len(heads), (len(titles), len(heads))
    assert all("<path" in t for t in titles), "匯出的 HTML 裡有卡片標題的圖示是空的"


def test_the_html_export_is_the_page_itself_and_opens_offline(live):
    """「開起來就跟在 jtdt 上面看得一模一樣」（2026-10-02 使用者要求）。

    三件事：①內容是畫面上那一份（卡片、逐字稿都在）②**不依賴伺服器**
    （沒有外部樣式表或腳本 —— 寄出去的檔案，收件的人連不到我們）
    ③真的打開來，樣式有套上、點引用照樣跳到逐字稿。
    """
    port, send, vtt = live
    _open_done_result(send, port, vtt)
    cards = _eval(send, "document.querySelectorAll('#msCards .ms-card').length")
    _eval(send, """(function(){ window.__blobs = [];
      var o = URL.createObjectURL.bind(URL);
      URL.createObjectURL = function (b) { window.__blobs.push(b); return o(b); };
      document.getElementById('msDlHtml').click(); return 1; })()""")
    assert _wait(send, "window.__blobs && window.__blobs.length > 0", 30), (
        "按了 HTML 沒有產生檔案")
    html = _eval(send, "window.__blobs[0].text()")
    import re as _re
    # 容器是 `ms-cards`，只數 class 裡**剛好**有 `ms-card` 這個字的
    got = len(_re.findall(r'class="(?:[^"]*\s)?ms-card(?:\s[^"]*)?"', html))
    assert got == cards, f"匯出的卡片數 {got} 跟畫面上的 {cards} 不一樣"
    assert 'id="msScript"' in html and "ms-line" in html, "匯出的檔案裡沒有逐字稿"
    assert "<link" not in html and "<script src" not in html, (
        "匯出的檔案還連到伺服器上的樣式或腳本 —— 寄出去就沒有樣式了")
    # 看的是**元素**，不是字串：頁面的樣式表整份帶過去，裡面本來就會提到這些名字
    #（`.ms-ex-main:not(:has(.ms-ex-ws…))` 那條）—— 那不是畫面上的操作
    for gone in (r'id="msDlPdf"', r'id="msThemePreview"', r'class="[^"]*\bms-ex-ws\b'):
        assert not _re.search(gone, html), f"畫面上的操作 {gone} 跟著被匯出了（在檔案裡按了沒作用）"

    # 真的打開它：換成那份 HTML，看樣式與點引用
    frame = send("Page.getFrameTree")["result"]["frameTree"]["frame"]["id"]
    send("Page.navigate", {"url": "about:blank"})
    time.sleep(0.5)
    send("Page.setDocumentContent", {"frameId": frame, "html": html})
    assert _wait(send, "document.querySelectorAll('.ms-card').length > 0", 10)
    radius = _eval(send, "getComputedStyle(document.querySelector('.ms-card')).borderTopLeftRadius")
    assert radius and radius != "0px", "打開之後卡片沒有樣式 —— 頁面自己的樣式沒有帶進去"
    # 全站樣式表（變數、字級、`.muted`…）**也要一起帶進去** —— 只驗卡片的話，那一條
    # 是頁面自己的 `<style>`，全站樣式表漏掉照樣過（變異驗證抓到的）
    bg = _eval(send, "getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()")
    assert bg, "打開之後沒有全站樣式表的變數 —— platform.css 沒有一起帶進去"
    seq = _eval(send, "(function(){ var b = document.querySelector('#msCards [data-seq]');"
                      " if (!b) return null; b.click(); return b.dataset.seq; })()")
    assert seq, "匯出的卡片上沒有引用可以點"
    assert _wait(send, f"!!document.querySelector('#ms-seg-{seq}.hit')", 5), (
        "在匯出的檔案裡點引用沒有跳到逐字稿那一段")


def test_the_report_can_be_saved_to_the_workspace(live):
    """「要有存至工作區」（2026-10-02 使用者要求）。

    判準是**工作區裡真的多了那個檔案**，不是「按鈕變成已存」——
    只改按鈕文字、實際沒存進去的話，畫面看起來一模一樣。
    """
    port, send, vtt = live
    _open_done_result(send, port, vtt)
    if not _eval(send, "!!document.querySelector('.ms-ex-ws')"):
        pytest.skip("這個實例的工作區被停用")
    before = _eval(send, "fetch('/workspace/api/list').then(r => r.json())"
                         ".then(d => (d.files || d.items || []).length)")
    _eval(send, "document.querySelector('.ms-ex-ws [data-ws-fmt=\"md\"]').click(), 1")
    assert _wait(send, f"fetch('/workspace/api/list').then(r => r.json())"
                       f".then(d => (d.files || d.items || []).length > {before or 0})", 30), (
        "按了存至工作區，工作區裡沒有多出檔案")
    names = _eval(send, "fetch('/workspace/api/list').then(r => r.json())"
                        ".then(d => (d.files || d.items || []).map(f => f.name || f.filename))")
    assert any(str(n).endswith(".md") for n in names), f"存進去的不是 Markdown：{names}"
    # 存完按鈕要維持原樣 —— 共用元件會把整顆換成「已存至工作區」，四顆就分不出哪顆是哪個格式
    label = _eval(send, "document.querySelector('.ms-ex-ws [data-ws-fmt=\"md\"]').textContent.trim()")
    assert label == "Markdown", f"存完之後按鈕變成「{label}」"
    assert _eval(send, "!!document.querySelector('.ms-ex-ws [data-ws-fmt=\"md\"] svg')"), (
        "存完之後按鈕的圖示不見了")


def test_the_workspace_picker_shows_when_each_file_was_saved(live):
    """「從工作區載入」的挑選視窗要寫出每個檔案的存入時間（2026-10-03 使用者回報：
    工作區裡有兩個同名的逐字稿，挑選視窗只列檔名與大小，不知道要選哪一個）。

    判準是**畫面上那張卡片寫的時間跟工作區記的存入時間一樣**（照瀏覽器的時區、
    格式跟「我的工作區」那一頁相同），而且是從真的「從工作區載入」按鈕打開的。"""
    import time as _t
    port, send, _ = live
    send("Page.enable"); send("Runtime.enable")
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/meeting-summary/"})
    assert _wait(send, "document.readyState === 'complete' && !!window.openWorkspacePicker", 30)
    if not _eval(send, "!!document.querySelector('.ws-load-btn')"):
        pytest.skip("這個實例的工作區被停用")
    name = "同名逐字稿-picker.txt"
    ids = []
    for body in ("王小明：第一份。", "王小明：第二份。"):
        fid = _eval(send, f"""(function(){{
          var fd = new FormData();
          fd.append('name', {json.dumps(name)}); fd.append('source_tool', 'meeting-transcribe');
          fd.append('file', new Blob([{json.dumps(body)}], {{type: 'text/plain'}}), {json.dumps(name)});
          return fetch('/workspace/save', {{method: 'POST', body: fd}})
            .then(r => r.json()).then(d => (d.file || {{}}).file_id || null); }})()""")
        assert fid, "存進工作區失敗"
        ids.append(fid)
    try:
        listed = _eval(send, "fetch('/workspace/api/list').then(r => r.json()).then(d => d.files)")
        saved = {f["file_id"]: f["saved_at"] for f in listed if f["file_id"] in ids}
        assert len(saved) == 2
        _eval(send, "document.querySelector('.ws-load-btn').click(), 1")
        assert _wait(send, "document.querySelectorAll('#ws-picker-modal .ws-pick-card').length >= 2", 15), (
            "按「從工作區載入」沒有打開挑選視窗")
        shown = _eval(send, """[...document.querySelectorAll('#ws-picker-modal .ws-pick-card')]
          .map(c => [c.dataset.id, (c.querySelector('.ws-pick-time') || {}).textContent || ''])""")
        shown = dict(shown)
        for fid in ids:
            want = _t.strftime("%Y-%m-%d %H:%M", _t.localtime(saved[fid]))
            assert shown.get(fid) == want, (
                f"挑選視窗上沒有寫存入時間（同名檔案分不出來）：畫面「{shown.get(fid)}」、應為「{want}」")
    finally:
        for fid in ids:
            _eval(send, f"""(function(){{ var fd = new FormData(); fd.append('file_id', {json.dumps(fid)});
              return fetch('/workspace/delete', {{method: 'POST', body: fd}}).then(() => 1); }})()""")


def test_the_share_chart_on_the_page_is_sorted_longest_first(live):
    """畫面上的「各議題時間佔比」：清單由上到下、長條由左到右都是由高到低
    （2026-10-02 使用者要求）。伺服器那一份另有測試；**這一份是瀏覽器畫的**，
    兩邊各自寫了一次排序，只測一邊的話另一邊漂掉沒人發現。

    顏色要跟著議題走（同一個議題在議題時間軸上是同一個顏色），不是跟著名次。
    """
    port, send, _ = live
    send("Page.enable"); send("Runtime.enable")
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/meeting-summary/"})
    assert _wait(send, "!!window.MeetingCharts", 30)
    got = _eval(send, """(function(){
      var box = document.createElement('div');
      box.style.width = '700px'; document.body.appendChild(box);
      var pal = ['#111111', '#222222', '#333333'];
      MeetingCharts.render({palette: pal, kinds: {}, into: {timeline: box},
        data: {chapters: [
          {title: '短', duration_ms: 60000, start_ms: 0, segment_ids: [1]},
          {title: '最長', duration_ms: 300000, start_ms: 60000, segment_ids: [2]},
          {title: '中', duration_ms: 120000, start_ms: 360000, segment_ids: [3]}]}});
      var svg = box.querySelector('svg');
      var titles = [...svg.querySelectorAll('text')].map(t => [t.getAttribute('y'), t.textContent])
        .filter(p => ['短', '最長', '中'].includes(p[1]))
        .sort((a, b) => +a[0] - +b[0]).map(p => p[1]);
      var bars = [...svg.querySelectorAll('rect')].filter(r => +r.getAttribute('height') > 20)
        .sort((a, b) => +a.getAttribute('x') - +b.getAttribute('x'))
        .map(r => r.getAttribute('fill'));
      box.remove();
      return {titles: titles, bars: bars};
    })()""")
    assert got["titles"] == ["最長", "中", "短"], f"清單沒有由高到低：{got['titles']}"
    assert got["bars"] == ["#222222", "#333333", "#111111"], (
        f"長條沒有由左到右由高到低、或顏色沒有跟著議題走：{got['bars']}")


def test_opening_from_my_jobs_brings_back_the_start_button_and_the_background(live):
    """從「我的作業」按「開啟」回來，**「開始分析」要在**，會議背景也要帶回來
    （2026-10-02 使用者回報「分析那個按鈕怎麼不見了」）。

    原本那條路只讀結果，解析區（連同開始分析）一直藏著 —— 想改一下背景再跑一次，
    只能把逐字稿重新上傳。判準是**按鈕真的看得到**、背景真的回到框裡。"""
    port, send, vtt = live
    ctx = "這是第四季預算會議，與會者：王小明（財務長）、李美華（行銷經理）。"
    send("Page.enable"); send("Runtime.enable"); send("DOM.enable")
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/meeting-summary/"})
    assert _wait(send, "document.readyState === 'complete' && "
                       "!!document.getElementById('msUp')", 30)
    time.sleep(0.5)
    _set_file(send, ".file-upload input[type=file]", vtt)
    assert _wait(send, "!document.getElementById('msParsed').hidden", 60)
    _eval(send, f"document.getElementById('msCtxBox').value = {json.dumps(ctx)}, 1")
    _eval(send, "document.getElementById('msStart').click(), 1")
    assert _wait(send, "!document.getElementById('msResult').hidden", 180)
    jid = _eval(send, """fetch('/api/jobs', {headers:{Accept:'application/json'}})
      .then(r => r.json()).then(d => {
        const l = (Array.isArray(d) ? d : (d.jobs || d.items || []))
          .filter(j => (j.tool === 'meeting-summary' || j.tool_id === 'meeting-summary')
                       && j.status === 'done')
          .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
        return l.length ? (l[0].id || l[0].job_id) : null; })""")
    assert jid, "撈不到剛剛那件作業"

    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/meeting-summary/?job={jid}"})
    assert _wait(send, "!!document.getElementById('msResult') && "
                       "!document.getElementById('msResult').hidden", 30)
    assert _wait(send, "!document.getElementById('msParsed').hidden && "
                       "document.getElementById('msStart').offsetParent !== null", 15), (
        "從「我的作業」打開之後看不到「開始分析」")
    assert _eval(send, "document.getElementById('msNSeg').textContent") == "3", (
        "解析區的段落數不對 —— 解析摘要沒有接回來")
    assert _eval(send, "document.getElementById('msCtxBox').value") == ctx, (
        "會議背景沒有帶回來 —— 重跑一次等於把背景丟掉")


def test_the_background_is_on_the_result_page_and_in_the_html_export(live):
    """會議背景要跟著結果走（2026-10-02 使用者要求「存 json、存至工作區時也要把會議背景存進去」）。

    HTML 匯出是**整頁複製**，所以背景要先出現在結果頁上：標題之後、摘要之前，原文照錄
    （分行留著、`<b>` 不可以被當成標記）；沒填就整節不出現。"""
    port, send, vtt = live
    ctx = "會議主題：第四季預算\n與會者：王小明（財務長）\n<b>不是粗體</b>"
    send("Page.enable"); send("Runtime.enable"); send("DOM.enable")
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/meeting-summary/"})
    assert _wait(send, "document.readyState === 'complete' && "
                       "!!document.getElementById('msUp')", 30)
    time.sleep(0.5)
    _set_file(send, ".file-upload input[type=file]", vtt)
    assert _wait(send, "!document.getElementById('msParsed').hidden", 60)

    # 沒填背景：那一節不出現
    _eval(send, "document.getElementById('msCtxBox').value = '', 1")
    _eval(send, "document.getElementById('msStart').click(), 1")
    assert _wait(send, "!document.getElementById('msResult').hidden && "
                       "document.querySelectorAll('#msCards .ms-card').length > 0", 180)
    assert _eval(send, "document.getElementById('msCtxSec').hidden") is True, (
        "沒填會議背景，結果頁卻出現空的「會議背景」")

    # 填了再跑一次：出現在摘要前面、原文照錄
    _eval(send, f"document.getElementById('msCtxBox').value = {json.dumps(ctx)}, 1")
    _eval(send, "document.getElementById('msStart').click(), 1")
    assert _wait(send, "!document.getElementById('msResult').hidden && "
                       "!document.getElementById('msCtxSec').hidden", 180), (
        "填了會議背景，結果頁上沒有那一節")
    assert _eval(send, "document.getElementById('msCtxView').textContent") == ctx
    assert _eval(send, "document.querySelectorAll('#msCtxView b').length") == 0, (
        "背景裡的 <b> 被當成標記了 —— 使用者寫的字要原樣顯示")
    assert _eval(send, "getComputedStyle(document.getElementById('msCtxView')).whiteSpace") == "pre-wrap", (
        "背景的分行被併成一行")
    before = _eval(send, "!!(document.getElementById('msCtxSec').compareDocumentPosition("
                         "document.getElementById('msSummary')) & Node.DOCUMENT_POSITION_FOLLOWING)")
    assert before, "會議背景要在摘要前面"

    _eval(send, """(function(){ window.__blobs = [];
      var o = URL.createObjectURL.bind(URL);
      URL.createObjectURL = function (b) { window.__blobs.push(b); return o(b); };
      document.getElementById('msDlHtml').click(); return 1; })()""")
    assert _wait(send, "window.__blobs && window.__blobs.length > 0", 30)
    html = _eval(send, "window.__blobs[0].text()")
    import re as _re
    m = _re.search(r'<section[^>]*id="msCtxSec"[^>]*>(.*?)</section>', html, _re.S)
    assert m and " hidden" not in m.group(0).split(">", 1)[0], "HTML 匯出裡沒有會議背景"
    assert "第四季預算" in m.group(1) and "&lt;b&gt;不是粗體&lt;/b&gt;" in m.group(1), m.group(1)


def test_the_export_is_grouped_by_download_and_workspace(live):
    """匯出區分兩張卡片：「下載」與「存至工作區」，卡片裡依格式分行（2026-10-03 使用者要求；
    v1.16.40 是依格式分三張、每張各寫一次下載 / 存至工作區，同一個動作散在三張卡片裡）。

    判準量在畫面上，三種寬度各量一次（1280 筆電、1600、1920 大螢幕；版面依右邊的寬度換）：
    * 卡片標題依序是「下載」「存至工作區」；卡片裡每一顆按鈕的圖示都跟**卡片標題的圖示**相同，
      兩張卡片的圖示不同 —— 按鈕本身就講得出動作
    * 每一行的名稱是格式，不是動作（動作只寫在卡片標題）
    * 上下疊的版面：卡片拉滿右邊、兩張卡片的按鈕起點對齊；**任何版面**每一行按鈕都排得進一行
    * 沒有任何寬度會讓匯出區橫向捲動
    """
    port, send, vtt = live
    _open_done_result(send, port, vtt)
    try:
        for width in (1280, 1600, 1920):
            send("Emulation.setDeviceMetricsOverride",
                 {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": False})
            time.sleep(0.6)
            g = _eval(send, """(function(){
              const main = document.querySelector('.ms-ex-main');
              const box = document.querySelector('.ms-export-box');
              const svg = el => ((el && el.querySelector('svg')) || {}).innerHTML || '';
              const cardEls = [...document.querySelectorAll('.ms-ex-group:not(.ms-ex-setting)')]
                .filter(c => !c.hidden);
              const cards = cardEls.map(c => ({
                title: c.querySelector('.ms-ex-title b').textContent.trim(),
                icon: svg(c.querySelector('.ms-ex-title')),
                width: Math.round(c.getBoundingClientRect().width),
                top: Math.round(c.getBoundingClientRect().top),
                rows: [...c.querySelectorAll('.ms-ex-act')].map(a => {
                  const btns = [...a.querySelectorAll('.btn')].filter(b => !b.hidden);
                  return {label: a.querySelector('.ms-ex-label').textContent.trim(),
                          icons: btns.map(svg),
                          tops: btns.map(b => Math.round(b.getBoundingClientRect().top)),
                          lefts: btns.map(b => Math.round(b.getBoundingClientRect().left)),
                          widths: btns.map(b => Math.round(b.getBoundingClientRect().width)),
                          left: btns.length ? Math.round(btns[0].getBoundingClientRect().left) : null};
                })}));
              const f = document.getElementById('msThemePreview').getBoundingClientRect();
              return {cards, main: Math.round(main.getBoundingClientRect().width),
                      overflow: box.scrollWidth - box.clientWidth,
                      paper: [Math.round(f.width), Math.round(f.height)]};
            })()""")
            titles = [c["title"] for c in g["cards"]]
            assert titles in (["下載", "存至工作區"], ["下載"]), f"{width}: 卡片不是依動作分的：{titles}"
            for c in g["cards"]:
                assert c["rows"], f"{width}: 「{c['title']}」那張卡片是空的"
                for ln in c["rows"]:
                    assert ln["label"] not in ("下載", "存至工作區"), (
                        f"{width}: 動作又寫在每一行上了（「{ln['label']}」）—— 動作只寫在卡片標題")
                    assert set(ln["icons"]) == {c["icon"]}, (
                        f"{width}: 「{c['title']} / {ln['label']}」的按鈕圖示跟卡片標題的不一樣")
            if len(g["cards"]) == 2:
                assert g["cards"][0]["icon"] != g["cards"][1]["icon"], (
                    f"{width}: 下載與存至工作區用同一個圖示，看不出差別")
            assert g["overflow"] <= 1, f"{width}: 匯出區橫向捲動了 {g['overflow']}px"
            w, h = g["paper"]
            assert w > 100 and 1.3 < h / w < 1.5, f"{width}: 預覽不是一頁紙的比例：{w}×{h}"
            for c in g["cards"]:
                for ln in c["rows"]:
                    assert len(set(ln["tops"])) == 1, (
                        f"{width}: 「{c['title']} / {ln['label']}」那一行按鈕被擠成兩行：{ln['tops']}")
            # 上下疊（筆電與一般寬度）＝卡片的 top 都不同；大螢幕兩張並排時同一個高度起頭。
            # **不可以用格線欄數判斷**：「名稱在左」那種版面也是三欄，v1.16.40 第一版就是這樣
            # 把該驗的寬度整個跳過（變異驗證抓到）。
            tops = [c["top"] for c in g["cards"]]
            if len(set(tops)) == len(tops):
                assert min(c["width"] for c in g["cards"]) >= g["main"] - 2, (
                    f"{width}: 卡片沒有拉滿右邊（{[c['width'] for c in g['cards']]} / {g['main']}）")
                lefts = {ln["left"] for c in g["cards"] for ln in c["rows"]}
                assert max(lefts) - min(lefts) <= 1, f"{width}: 兩張卡片的按鈕起點沒有對齊：{lefts}"
            # **按鈕排成一張表**（2026-10-03 使用者：「分類對了，但看起來還是有點亂」）：
            # 同一欄（每一行的第 N 顆）一樣寬、左緣對齊 —— 上下疊時兩張卡片一起比，
            # 並排時各自比。只驗起點的話，每顆寬度不同、第二顆以後照樣歪。
            groups = [g["cards"]] if len(set(tops)) == len(tops) else [[c] for c in g["cards"]]
            for cards in groups:
                for i in range(3):
                    col = [(ln["lefts"][i], ln["widths"][i]) for c in cards for ln in c["rows"]
                           if len(ln["lefts"]) > i]
                    if len(col) < 2:
                        continue
                    ls, ws = {x for x, _ in col}, {w for _, w in col}
                    assert max(ls) - min(ls) <= 1 and max(ws) - min(ws) <= 1, (
                        f"{width}: 第 {i + 1} 欄的按鈕沒有上下對齊（左緣 {sorted(ls)}、寬 {sorted(ws)}）")
    finally:
        send("Emulation.clearDeviceMetricsOverride")


def test_an_exported_json_dropped_back_in_shows_the_result(live):
    """匯出的 .json 傳回來，結果直接出現、不再送模型；有附逐字稿所以「開始分析」也在
    （2026-10-02 使用者問「匯出過的 .json 可以再傳回來在這工具裡呈現嗎」）。"""
    port, send, vtt = live
    _open_done_result(send, port, vtt)
    uid = _eval(send, """fetch('/api/jobs', {headers:{Accept:'application/json'}})
      .then(r => r.json()).then(d => {
        const l = (Array.isArray(d) ? d : (d.jobs || d.items || []))
          .filter(j => (j.tool === 'meeting-summary' || j.tool_id === 'meeting-summary')
                       && j.status === 'done');
        return l.length ? (l[0].id || l[0].job_id) : null; })
      .then(id => id ? fetch('/api/jobs/' + encodeURIComponent(id)).then(r => r.json()) : null)
      .then(j => j ? (j.meta || {}).upload_id : null)""")
    assert uid, "撈不到已完成的分析"
    with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/tools/meeting-summary/download/{uid}?fmt=json",
            timeout=30) as r:
        data = r.read()
    path = Path(browser_probe.uploadable_dir()) / "mse2e-export.json"
    path.write_bytes(data)
    cards = _eval(send, "document.querySelectorAll('#msCards .ms-card').length")

    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/meeting-summary/"})
    assert _wait(send, "document.readyState === 'complete' && "
                       "!!document.getElementById('msUp')", 30)
    time.sleep(0.5)
    _set_file(send, ".file-upload input[type=file]", str(path))
    assert _wait(send, "!document.getElementById('msResult').hidden && "
                       "document.querySelectorAll('#msCards .ms-card').length > 0", 30), (
        "傳回匯出的 JSON 之後結果沒有出現")
    assert _eval(send, "document.querySelectorAll('#msCards .ms-card').length") == cards
    assert _eval(send, "document.querySelectorAll('#msScript .ms-line').length") >= 3, (
        "逐字稿沒有跟著回來 —— 引用點不到原文")
    assert _eval(send, "!document.getElementById('msParsed').hidden && "
                       "document.getElementById('msStart').offsetParent !== null"), (
        "有附逐字稿的匯出檔傳回來，應該可以再按「開始分析」")
    assert _eval(send, "document.getElementById('msShapeRow').hidden"), (
        "換排法對匯入的結果沒有意義，那一列應該收起來")


VTT_TYPO = """WEBVTT

00:00:01.000 --> 00:00:06.000
<v 王小明>各位早，今天只談一件事：第四季的預算。

00:00:06.500 --> 00:00:12.000
<v 李美華>報價我已經寄給 Bianka 了，Bianka 會再確認。

00:00:12.500 --> 00:00:20.000
<v 王小明>那就照原案走，行銷不加。李美華月底前把修訂版寄給法務。
"""


def test_the_background_suggests_spelling_fixes_and_keeps_the_original(live):
    """會議背景寫了 `Bianca`、逐字稿寫成 `Bianka` → 解析區列出建議（不先勾）；
    勾了再分析，逐字稿換掉、滑過看得到原文，結果頁講出換過什麼
    （2026-10-02 使用者決定：會議背景的專有名詞要拿來修逐字稿的錯字）。

    判準量在畫面上：清單真的出現、勾選真的送出去、換過的那一行真的帶著原文 ——
    只驗端點的話，前端沒把勾選送給 `/start` 也照樣全綠。"""
    port, send, _vtt = live
    path = Path(browser_probe.uploadable_dir()) / "mse2e-typo.vtt"
    path.write_text(VTT_TYPO, encoding="utf-8")
    send("Page.enable"); send("Runtime.enable"); send("DOM.enable")
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/meeting-summary/"})
    assert _wait(send, "document.readyState === 'complete' && "
                       "!!document.getElementById('msUp')", 30)
    time.sleep(0.5)
    _set_file(send, ".file-upload input[type=file]", str(path))
    assert _wait(send, "!document.getElementById('msParsed').hidden", 60)

    # 沒填背景：沒有建議
    time.sleep(1.2)
    assert _eval(send, "document.getElementById('msTermFix').hidden") is True

    _eval(send, """(function(){ const b = document.getElementById('msCtxBox');
      b.closest('details') && (b.closest('details').open = true);
      b.value = '與會者：王小明（財務長）、Bianca（PM）';
      b.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()""")
    assert _wait(send, "!document.getElementById('msTermFix').hidden", 20), (
        "填了會議背景，建議清單沒有出現")
    rows = _eval(send, """[...document.querySelectorAll('#msTermFixList li')].map(li => {
      const cb = li.querySelector('input[type=checkbox]');
      return {from: cb.dataset.from, to: cb.dataset.to, checked: cb.checked,
              text: li.textContent}; })""")
    assert [(r["from"], r["to"]) for r in rows] == [("Bianka", "Bianca")], rows
    assert rows[0]["checked"] is False, "建議要由使用者勾，不可以預先勾好"
    assert "2" in rows[0]["text"], f"處數沒有顯示：{rows[0]['text']!r}"
    vis = _eval(send, "(function(){ const r = document.getElementById('msTermFixList')"
                      ".getBoundingClientRect(); return r.width > 50 && r.height > 10; })()")
    assert vis, "清單在畫面上沒有佔到空間"

    # 背景填好之後換一份逐字稿（這份沒有寫錯）→ 清單要跟著重比，不可以留著上一份的建議
    _set_file(send, ".file-upload input[type=file]", _vtt)
    assert _wait(send, "document.getElementById('msTermFix').hidden && "
                       "!document.getElementById('msTermFixNone').hidden", 30), (
        "換了一份逐字稿，建議清單還停在上一份")
    _set_file(send, ".file-upload input[type=file]", str(path))
    assert _wait(send, "!document.getElementById('msTermFix').hidden", 30)

    # 打錯的背景（晚到的舊回應不可以蓋掉新的）：改成跟逐字稿一樣的寫法 → 清單消失
    _eval(send, """(function(){ const b = document.getElementById('msCtxBox');
      b.value = '與會者：Bianka（PM）';
      b.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()""")
    assert _wait(send, "document.getElementById('msTermFix').hidden && "
                       "!document.getElementById('msTermFixNone').hidden", 20), (
        "背景改成跟逐字稿一樣的寫法之後，清單應該換成「都對得上」")

    _eval(send, """(function(){ const b = document.getElementById('msCtxBox');
      b.value = '與會者：王小明（財務長）、Bianca（PM）';
      b.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()""")
    assert _wait(send, "!document.getElementById('msTermFix').hidden", 20)
    _eval(send, "document.getElementById('msTermFixAll').click(), 1")
    assert _eval(send, "document.querySelector('#msTermFixList input').checked") is True

    _eval(send, "document.getElementById('msStart').click(), 1")
    assert _wait(send, "!document.getElementById('msResult').hidden && "
                       "document.querySelectorAll('#msScript .ms-line').length >= 3", 180)
    fixed = _eval(send, """[...document.querySelectorAll('#msScript .ms-fixed')].map(e =>
      ({text: e.textContent, title: e.title}))""")
    assert len(fixed) == 1, f"換過的段落應該剛好一段：{fixed}"
    assert "Bianca" in fixed[0]["text"] and "Bianka" not in fixed[0]["text"], fixed
    assert "Bianka" in fixed[0]["title"], f"滑過看不到原文：{fixed[0]['title']!r}"
    note = _eval(send, "(function(){ const n = document.getElementById('msReplNote');"
                       " return n.hidden ? null : n.textContent; })()")
    assert note and "Bianka" in note and "Bianca" in note, f"結果頁沒講換過什麼：{note!r}"

    # ---- 從「我的作業」打開：建議清單回來，上一次勾過的預先勾回去 ----
    # 背景框是解析區裡的東西，這條路只有 `showParsed` 會去問建議；
    # 漏掉的話使用者看不到上次換過什麼，再按一次分析就安靜地把替換丟掉。
    jid = _eval(send, """fetch('/api/jobs', {headers:{Accept:'application/json'}})
      .then(r => r.json()).then(d => {
        const l = (Array.isArray(d) ? d : (d.jobs || d.items || []))
          .filter(j => (j.tool === 'meeting-summary' || j.tool_id === 'meeting-summary')
                       && j.status === 'done')
          .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
        return l.length ? (l[0].id || l[0].job_id) : null; })""")
    assert jid
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/meeting-summary/?job={jid}"})
    assert _wait(send, "!!document.getElementById('msResult') && "
                       "!document.getElementById('msResult').hidden", 30)
    assert _wait(send, "!!document.getElementById('msTermFix') && "
                       "!document.getElementById('msTermFix').hidden", 20), (
        "從「我的作業」打開之後，建議清單沒有回來")
    assert _eval(send, "document.querySelector('#msTermFixList input').checked") is True, (
        "上一次勾過的沒有預先勾回去 —— 再分析一次就會把替換丟掉")
    assert _eval(send, "document.querySelectorAll('#msScript .ms-fixed').length") == 1


def _fresh_upload(port, send, vtt, typed: str = ""):
    """開一個新的頁面、（選擇性）先在背景框打字，再上傳逐字稿，等解析區出現。"""
    send("Page.enable"); send("Runtime.enable"); send("DOM.enable")
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/meeting-summary/"})
    assert _wait(send, "document.readyState === 'complete' && "
                       "!!document.getElementById('msUp')", 30)
    time.sleep(0.5)
    if typed:
        _eval(send, f"document.getElementById('msCtxBox').value = {json.dumps(typed)}, 1")
    _set_file(send, ".file-upload input[type=file]", vtt)
    assert _wait(send, "!document.getElementById('msParsed').hidden", 60)


def test_the_same_transcript_brings_back_its_background_and_it_can_be_cleared(live):
    """同一份逐字稿再分析時，帶入上一次填的會議背景（2026-10-03 使用者要求：
    「要，但使用者可以刪除或修改」）。

    判準：①框是空的 → 帶回來、提示講出來、框可以直接改 ②框裡已經有別的字 → **不覆蓋**，
    改給「改用上一次的」③按「清除」→ 框清空，**下一次不再帶入**（伺服器那一份真的刪了）。"""
    port, send, vtt = live
    ctx = "這次是第四季預算會議（帶入測試）\n與會者：王小明（財務長）"

    _fresh_upload(port, send, vtt)
    _eval(send, f"document.getElementById('msCtxBox').value = {json.dumps(ctx)}, 1")
    _eval(send, "document.getElementById('msStart').click(), 1")
    assert _wait(send, "!document.getElementById('msResult').hidden && "
                       "document.querySelectorAll('#msCards .ms-card').length > 0", 180)

    # ① 空的框 → 帶回來
    _fresh_upload(port, send, vtt)
    assert _wait(send, "!document.getElementById('msCtxMem').hidden", 10), (
        "同一份逐字稿上傳之後，沒有出現「已帶入上一次的背景」")
    assert _eval(send, "document.getElementById('msCtxBox').value") == ctx, (
        "框裡不是上一次填的背景")
    assert _eval(send, "document.getElementById('msCtxMemUse').hidden") is True
    assert _eval(send, "document.getElementById('msCtxBox').disabled") is False, "框不可以鎖住"

    # ② 框裡已經有別的字 → 不覆蓋，按「改用上一次的」才換
    typed = "我自己先打的背景"
    _fresh_upload(port, send, vtt, typed)
    assert _wait(send, "!document.getElementById('msCtxMem').hidden", 10)
    assert _eval(send, "document.getElementById('msCtxBox').value") == typed, (
        "使用者自己打的字被蓋掉了")
    assert _eval(send, "document.getElementById('msCtxMemUse').hidden") is False
    _eval(send, "document.getElementById('msCtxMemUse').click(), 1")
    assert _eval(send, "document.getElementById('msCtxBox').value") == ctx

    # ③ 清除 → 框清空、提示收起、下一次不再帶入
    _eval(send, "document.getElementById('msCtxMemClear').click(), 1")
    assert _wait(send, "document.getElementById('msCtxMem').hidden && "
                       "document.getElementById('msCtxBox').value === ''", 10), (
        "按了「清除」框裡還是那一段")
    _fresh_upload(port, send, vtt)
    time.sleep(0.5)
    assert _eval(send, "document.getElementById('msCtxMem').hidden") is True, (
        "清除之後再上傳，還是帶入了 —— 伺服器那一份沒有刪")
    assert _eval(send, "document.getElementById('msCtxBox').value") == ""


def test_the_background_entered_when_transcribing_is_filled_in(live):
    """轉逐字稿時在「專有名詞或會議背景」寫的，轉送過來要帶進會議背景（2026-10-03 使用者要求）。

    判準：①框裡是那一段、提示講的是「轉逐字稿時填的」②按「清除」框清空 ——
    這一份沒有「記住」可言，不必（也不可以因為問伺服器失敗就）留著。"""
    port, send, vtt = live
    ctx = "這是一場備份規劃會議（轉逐字稿帶入測試）。\n宏範 Carol\n範例 Dora"
    segs = [{"seq": i + 1, "speaker": ["S1", "S2"][i % 2], "start_ms": i * 4000,
             "end_ms": i * 4000 + 3500,
             "text": f"轉逐字稿帶背景的測試第 {i + 1} 段，內容要夠長才算得出指紋。"}
            for i in range(6)]
    src = Path(vtt).with_name("mse2e-transcribed.json")
    src.write_text(json.dumps({"segments": segs, "context": ctx}, ensure_ascii=False),
                   encoding="utf-8")
    _fresh_upload(port, send, str(src))
    assert _wait(send, "!document.getElementById('msCtxMem').hidden", 10), (
        "轉逐字稿送來的背景沒有帶進來")
    assert _eval(send, "document.getElementById('msCtxBox').value") == ctx
    note = _eval(send, "document.getElementById('msCtxMemText').textContent") or ""
    assert "轉逐字稿" in note, f"提示沒講是轉逐字稿時填的：{note!r}"
    _eval(send, "document.getElementById('msCtxMemClear').click(), 1")
    assert _wait(send, "document.getElementById('msCtxMem').hidden && "
                       "document.getElementById('msCtxBox').value === ''", 10), "按了「清除」框裡還是那一段"


VTT_MANUAL = """WEBVTT

00:00:01.000 --> 00:00:06.000
<v 王小明>這台 Groxmoxity 叢集要升級，簡報用 POWPOYNT 做。

00:00:06.500 --> 00:00:12.000
<v 李美華>groxmoxity 的備份也一起做，月底前寄給法務。

00:00:12.500 --> 00:00:20.000
<v 王小明>那就照原案走，行銷不加。李美華月底前把修訂版寄給法務。
"""


def test_adding_my_own_replacement_changes_the_whole_transcript(live):
    """「自己加替換」（2026-10-03 使用者：「POWPOYNT 應該是 PowerPoint」「Proximity 應該是 Proxmox」）。

    判準量在畫面上：①打小寫也找得到大寫、處數是整份逐字稿的 ②找不到的講出來、不加進清單
    ③分析後那幾段真的換掉、滑過看得到原文、結果頁講出換過什麼
    ④從「我的作業」打開，自己加的那幾列畫回來而且是勾著的 —— 不然再分析一次就換回原文。"""
    port, send, _vtt = live
    path = Path(browser_probe.uploadable_dir()) / "mse2e-manual.vtt"
    path.write_text(VTT_MANUAL, encoding="utf-8")
    _fresh_upload(port, send, str(path))
    assert _wait(send, "!document.getElementById('msManRep').hidden", 10), (
        "上傳之後看不到「自己加替換」")

    def add(src, dst, enter=False):
        _eval(send, f"""(function(){{
          document.getElementById('msMrFrom').value = {json.dumps(src)};
          document.getElementById('msMrTo').value = {json.dumps(dst)};
          {"document.getElementById('msMrTo').dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));"
           if enter else "document.getElementById('msMrAdd').click();"}
          return 1; }})()""")

    add("groxmoxity", "Proxmox")
    assert _wait(send, "document.querySelectorAll('#msMrList li').length === 1", 10), (
        "按了「加入」清單沒有出現")
    row = _eval(send, "document.querySelector('#msMrList li').textContent")
    assert "Groxmoxity" in row and "groxmoxity" in row and "Proxmox" in row, row
    assert "2" in row, f"處數不對（整份逐字稿有兩處）：{row!r}"
    vis = _eval(send, "(function(){ const r = document.getElementById('msMrList')"
                      ".getBoundingClientRect(); return r.width > 50 && r.height > 10; })()")
    assert vis, "清單在畫面上沒有佔到空間"
    assert _eval(send, "document.getElementById('msMrFrom').value") == "", "加入之後輸入框沒清空"

    add("Proxmox", "X")
    assert _wait(send, "!document.getElementById('msMrMsg').hidden", 10)
    assert "Proxmox" in _eval(send, "document.getElementById('msMrMsg').textContent")
    assert _eval(send, "document.querySelectorAll('#msMrList li').length") == 1, (
        "找不到的寫法不可以加進清單")

    add("POWPOYNT", "PowerPoint", enter=True)
    assert _wait(send, "document.querySelectorAll('#msMrList li').length === 2", 10), (
        "在輸入框按 Enter 沒有加入")

    _eval(send, "document.getElementById('msStart').click(), 1")
    assert _wait(send, "!document.getElementById('msResult').hidden && "
                       "document.querySelectorAll('#msScript .ms-line').length >= 3", 180)
    fixed = _eval(send, """[...document.querySelectorAll('#msScript .ms-fixed')].map(e =>
      ({text: e.textContent, title: e.title}))""")
    assert len(fixed) == 2, f"換過的段落應該是兩段：{fixed}"
    assert "Proxmox" in fixed[0]["text"] and "PowerPoint" in fixed[0]["text"], fixed
    assert "Groxmoxity" not in fixed[0]["text"] and "Groxmoxity" in fixed[0]["title"], fixed
    assert fixed[1]["text"].startswith("Proxmox") or "Proxmox 的備份" in fixed[1]["text"], fixed
    note = _eval(send, "(function(){ const n = document.getElementById('msReplNote');"
                       " return n.hidden ? null : n.textContent; })()")
    assert note and "POWPOYNT" in note and "PowerPoint" in note, f"結果頁沒講換過什麼：{note!r}"

    jid = _eval(send, """fetch('/api/jobs', {headers:{Accept:'application/json'}})
      .then(r => r.json()).then(d => {
        const l = (Array.isArray(d) ? d : (d.jobs || d.items || []))
          .filter(j => (j.tool === 'meeting-summary' || j.tool_id === 'meeting-summary')
                       && j.status === 'done')
          .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
        return l.length ? (l[0].id || l[0].job_id) : null; })""")
    assert jid
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/meeting-summary/?job={jid}"})
    assert _wait(send, "!!document.getElementById('msResult') && "
                       "!document.getElementById('msResult').hidden", 30)
    assert _wait(send, "document.querySelectorAll('#msMrList li').length === 2", 20), (
        "從「我的作業」打開之後，自己加的替換沒有畫回來")
    assert _eval(send, "[...document.querySelectorAll('#msMrList input')].every(c => c.checked)"), (
        "畫回來的替換沒有勾著 —— 再分析一次就會換回原文")
    rows = _eval(send, "[...document.querySelectorAll('#msMrList li')].map(li => li.textContent)")
    assert "groxmoxity" in rows[0] and "POWPOYNT" in rows[1], (
        f"重新打開時順序跟加入時不同：{rows}")
    assert "2" in rows[0], f"重新打開時處數要照原文算（換過的段落 text 已經是 Proxmox）：{rows}"
