"""PDF 編輯器：**斷線 / 誤關分頁之後編輯內容要救得回來**。

現況（v1.15.48 之前）是救不回來：編輯狀態只活在那個分頁裡的 Fabric 物件上，
伺服器那份 `pe_<id>_out.pdf` 是**已經燒進去的 PDF**（不是編輯狀態），
而且 2 小時後被暫存清理掃掉 —— 拿回來也不能再拖動文字框。

這一層只做瀏覽器端（localStorage）：實作小，斷線 / 當掉 / 誤關分頁都救得回來。
**換電腦就沒有** —— 畫面上有講，這裡釘住那句話還在。

用真的瀏覽器跑：這是「接線 / 時序」的行為，假測試頁驗不到
（`feedback_cdp_test_template_wiring`）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, ROOT)
from tools.browser_probe import (  # noqa: E402
    browser as _browser,
    uploadable_dir as _uploadable_dir,
)

sys.path.insert(0, str(ROOT))

CHROME = "/usr/bin/chromium-browser"
pytestmark = pytest.mark.skipif(
    not Path(CHROME).exists(), reason="沒有 chromium，跳過真瀏覽器測試")


def test_the_draft_notice_says_it_is_browser_only():
    """**「只存在這台電腦」這句話要留著。**

    使用者會以為草稿在伺服器上（其他工具的產出都在伺服器）——
    換一台電腦打開卻什麼都沒有，那比沒有草稿更糟。
    """
    html = (ROOT / "app/tools/pdf_editor/templates/pdf_editor.html").read_text(
        encoding="utf-8")
    assert "peDraftBar" in html, "缺少草稿提示列"
    assert "只存在這台電腦的瀏覽器裡" in html, "少了「草稿只在這台電腦」那句話"


def test_the_draft_is_cleared_after_a_manual_save():
    """**手動存檔之後要清掉草稿** —— 不然下次開檔會看到一條「上次沒編輯完」，
    而其實那一份已經存好了。自動預覽不算（它每動一下就跑一次）。
    """
    html = (ROOT / "app/tools/pdf_editor/templates/pdf_editor.html").read_text(
        encoding="utf-8")
    assert re.search(r"if \(!isAuto\) peDraftClear\(\);", html), \
        "手動存檔成功之後沒有清掉草稿"


def test_a_draft_from_another_file_is_not_offered():
    """**同一份檔案才提議接續**。

    把 A 檔的編輯套到 B 檔上，版面看起來「好像可以」但位置全錯 ——
    而且使用者不會發現（同本專案「值悄悄填進別的欄位」那一條）。
    """
    html = (ROOT / "app/tools/pdf_editor/templates/pdf_editor.html").read_text(
        encoding="utf-8")
    m = re.search(r"function peDraftOffer\(\)\s*\{(.*?)\n\}", html, re.S)
    assert m, "找不到 peDraftOffer"
    body = m.group(1)
    assert "fileName" in body and "pageCount" in body, \
        "提議接續之前沒有比對檔名與頁數"


def test_an_empty_state_never_overwrites_a_real_draft():
    """**重新開檔時的那張空快照不可以蓋掉草稿。**

    `snapshotHistory()` 在載入完成時就會對「還沒編輯的空畫布」拍一張 ——
    那一張在 1.5 秒後把上一次的草稿洗掉，**使用者根本來不及按「接續」**。
    實測就是這樣：存的時候有 1 個物件，重整後讀回來是 0 個。
    """
    html = (ROOT / "app/tools/pdf_editor/templates/pdf_editor.html").read_text(
        encoding="utf-8")
    m = re.search(r"function peDraftSave\(\)\s*\{(.*?)\n\}", html, re.S)
    assert m, "找不到 peDraftSave"
    body = m.group(1)
    assert "empty" in body and "return" in body, \
        "peDraftSave 沒有擋掉「空的編輯狀態」"


# ---------------------------------------------------------------------------
# 真的在瀏覽器裡跑一次：編輯 → 重整 → 接續 → 物件回來了
#
# **這一類只有真瀏覽器驗得到**：靜態檢查看得到程式碼在那裡，看不到
# 「重新開檔時的空快照會不會先把草稿洗掉」—— 而實際踩到的正是那個
#（存的時候有 1 個物件，重整後讀回來是 0 個）。
# ---------------------------------------------------------------------------

from tests.test_doc_straighten_quad_overlay_e2e import (   # noqa: E402
    _browser, _free_port, _uploadable_dir,
)


def _sample_pdf(path: str) -> str:
    import fitz

    d = fitz.open()
    pg = d.new_page(width=595, height=842)
    pg.insert_text((72, 120), "Draft recovery test", fontsize=18)
    d.save(path)
    d.close()
    return path


@pytest.fixture(scope="module")
def editor():
    """拋棄式實例 ＋ 無頭瀏覽器。"""
    import json as _json
    import os as _os
    import shutil as _shutil
    import subprocess as _sp
    import sys as _sys
    import tempfile as _tf
    import time as _time
    import urllib.request as _ur

    br_path = _browser()
    if not br_path:
        pytest.skip("沒有 chromium")
    data = _tf.mkdtemp(prefix="pedraft-")
    port, cdp = _free_port(), _free_port()
    env = {**_os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = _sp.Popen([_sys.executable, "-m", "uvicorn", "app.main:app",
                     "--host", "127.0.0.1", "--port", str(port),
                     "--log-level", "warning"],
                    cwd=ROOT, env=env, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    br = _sp.Popen([br_path, "--headless=new", "--no-sandbox", "--disable-gpu",
                    f"--remote-debugging-port={cdp}", "--remote-allow-origins=*",
                    "about:blank"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    ws = None
    try:
        for _ in range(160):
            try:
                _ur.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                _ur.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=1)
                break
            except Exception:
                _time.sleep(0.5)
        else:
            pytest.skip("實例或瀏覽器起不來")
        import websockets.sync.client as wsc
        req = _ur.Request(f"http://127.0.0.1:{cdp}/json/new?about:blank",
                          method="PUT")
        with _ur.urlopen(req, timeout=10) as r:
            tab = _json.loads(r.read())
        ws = wsc.connect(tab["webSocketDebuggerUrl"], max_size=None,
                         open_timeout=10)
        n = [0]

        def send(method, params=None):
            n[0] += 1
            i = n[0]
            ws.send(_json.dumps({"id": i, "method": method,
                                 "params": params or {}}))
            while True:
                m = _json.loads(ws.recv(timeout=180))
                if m.get("id") == i:
                    return m

        sample = _sample_pdf(str(Path(_uploadable_dir()) / "pedraft.pdf"))
        yield port, send, sample
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        br.terminate(); srv.terminate()
        for pr in (br, srv):
            try:
                pr.wait(timeout=6)
            except Exception:
                pr.kill()
        _shutil.rmtree(data, ignore_errors=True)


def test_a_draft_really_comes_back_after_a_reload(editor):
    import time as _time

    port, send, sample = editor

    def ev(expr):
        r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return ((r.get("result") or {}).get("result") or {}).get("value")

    def open_and_upload():
        send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/pdf-editor/"})
        _time.sleep(3.0)
        doc = send("DOM.getDocument", {"depth": -1})["result"]
        node = send("DOM.querySelector",
                    {"nodeId": doc["root"]["nodeId"],
                     "selector": ".file-upload input[type=file]"})["result"]
        assert node.get("nodeId"), "找不到主要的上傳框"
        send("DOM.setFileInputFiles", {"files": [sample],
                                       "nodeId": node["nodeId"]})
        ev("document.querySelector('.file-upload input[type=file]')"
           ".dispatchEvent(new Event('change',{bubbles:true}))")
        for _ in range(40):
            _time.sleep(1)
            if ev("!document.getElementById('editor-panel').hidden"):
                return True
        return False

    send("Page.enable"); send("Runtime.enable"); send("DOM.enable")
    assert open_and_upload(), "編輯器沒有開起來"

    # 放一個文字框，等草稿寫進 localStorage
    assert ev("""(() => {
        const c = pageCanvases[0];
        const t = new fabric.Textbox('草稿測試', {left: 80, top: 80, fontSize: 24});
        t._peType = 'text'; c.add(t); c.requestRenderAll(); snapshotHistory();
        return c.getObjects().length;
      })()""") == 1
    for _ in range(12):
        _time.sleep(1)
        if ev("!!localStorage.getItem('jtdt-pe-draft-v1')"):
            break
    assert ev("JSON.parse(localStorage.getItem('jtdt-pe-draft-v1'))"
              ".snap.pages[0].objects.length") == 1, "草稿沒有存到那個物件"

    # 重整（＝誤關分頁 / 斷線之後回來）再開同一份檔案
    assert open_and_upload(), "重整後編輯器沒有開起來"
    _time.sleep(2.0)
    assert ev("!document.getElementById('peDraftBar').hidden"), \
        "重整後沒有出現「上次有一份沒編輯完的草稿」"
    # **這一步是真正的判準**：空快照若把草稿洗掉，這裡就只會回 0
    assert ev("JSON.parse(localStorage.getItem('jtdt-pe-draft-v1'))"
              ".snap.pages[0].objects.length") == 1, \
        "重新開檔時的空快照把草稿洗掉了"

    ev("document.getElementById('peDraftResume').click()")
    for _ in range(12):
        _time.sleep(1)
        if ev("pageCanvases[0].getObjects().length") >= 1:
            break
    assert ev("""(() => pageCanvases[0].getObjects()
        .filter(o => (o.text || '').includes('草稿測試')).length)()""") == 1, \
        "按了「接續上次的編輯」，但物件沒有回來"
