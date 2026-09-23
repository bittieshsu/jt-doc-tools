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
                content = json.dumps({"chapters": [
                    {"title": "第四季預算", "start_seq": 1, "end_seq": 3}]},
                    ensure_ascii=False)
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
                m = json.loads(ws.recv())
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
            return
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


def test_hovering_a_bar_lights_up_its_legend_row(live):
    """滑過長條，對應的圖例亮著、其餘變淡（2026-09-19 使用者要求）。

    量的是**章節佔比**那張圖 —— 發言者那張已經併進表格了。
    """
    port, send, vtt = live
    assert _wait(send, "!document.getElementById('msResult').hidden", 10)
    if _eval(send, "document.getElementById('msChapWrap').hidden"):
        pytest.skip("這份素材沒有章節")
    n = _eval(send, "document.querySelectorAll('#msTimeline .mc-row').length")
    if not n or n < 2:
        pytest.skip(f"章節圖上只有 {n} 個可對應的列")
    out = _eval(send, """(function(){
      var rows = document.querySelectorAll('#msTimeline .mc-row');
      rows[0].dispatchEvent(new MouseEvent('mouseover', {bubbles:true}));
      var lit = 0, faded = 0;
      for (var i=0;i<rows.length;i++){
        if (rows[i].classList.contains('mc-lit')) lit++;
        if (rows[i].classList.contains('mc-faded')) faded++;
      }
      return {lit: lit, faded: faded, total: rows.length};
    })()""")
    assert out["lit"] >= 1, "滑過去之後沒有任何一列亮起來"
    assert out["faded"] >= 1, "其餘的列沒有變淡 —— 那就看不出在對應哪一個"
    assert out["lit"] + out["faded"] == out["total"], "有列兩種狀態都沒有"


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
    assert got["padTop"] > 0, "列沒有上下內距的話，這條守門就沒有意義了"
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
    assert got, "沒有一列有兩格以上的色塊 —— 這條守門等於沒有執行"
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
    逐字稿改成新名字之後，待辦卡片還寫「負責：舊名字」、「誰講了多少」也還是舊的）。

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
            f"「誰講了多少」沒有跟著換：{table[:120]!r}")
