"""騎縫章的預覽點下去要看到**真的比較大**的圖（使用者 2026-09-14 要求）。

第一版接上共用的放大檢視之後，實測開起來是 **300×424** —— 跟縮圖差不多，
等於沒放大。原因是逐頁預覽是 **78 dpi** 的縮圖，而放大檢視只是把同一張
原尺寸顯示（`max-width` 不會把小圖撐大）。

端點本來就有 `large=1`（150 dpi），但**不可以事先幫每一頁都算一份** ——
52 頁的文件配上前端同時發的請求，就會回到「每個預覽請求 90 秒」那個老問題
（v1.14.x 正式機實測）。所以是**點開才去要**。

判準是「放大檢視裡那張圖比縮圖大」，不是「有沒有開起來」。
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

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tools.browser_probe import (  # noqa: E402
    browser as _browser,
    uploadable_dir as _uploadable_dir,
)






def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


pytestmark = pytest.mark.skipif(
    _browser() is None
    or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")


@pytest.fixture(scope="module")
def opened():
    import fitz

    data = tempfile.mkdtemp(prefix="seamlb-")
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [_browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
         f"--remote-debugging-port={cdp}", "--remote-allow-origins=*", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    pdf = os.path.join(_uploadable_dir(), "seam_lb.pdf")
    doc = fitz.open()
    for i in range(2):
        pg = doc.new_page(width=595, height=842)
        pg.insert_text((72, 120), f"Seam test page {i + 1}", fontsize=20, fontname="helv")
    doc.save(pdf)
    doc.close()

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
        req = urllib.request.Request(
            f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
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

        def ev(expr):
            res = send("Runtime.evaluate",
                       {"expression": expr, "returnByValue": True})["result"]
            if "exceptionDetails" in res:
                exc = res["exceptionDetails"].get("exception", {}).get("description")
                raise AssertionError(f"頁面丟例外：{str(exc)[:300]}")
            return res.get("result", {}).get("value")

        send("Runtime.enable")
        send("Page.enable")
        send("DOM.enable")
        send("Page.navigate",
             {"url": f"http://127.0.0.1:{port}/tools/pdf-seam-stamp/"})
        time.sleep(4)
        d = send("DOM.getDocument", {"depth": -1})
        nid = send("DOM.querySelector",
                   {"nodeId": d["result"]["root"]["nodeId"],
                    "selector": "input[type=file]"})["result"].get("nodeId")
        if not nid:
            pytest.skip("找不到上傳欄位")
        send("DOM.setFileInputFiles", {"files": [pdf], "nodeId": nid})
        for _ in range(60):
            time.sleep(1)
            if ev("document.querySelectorAll('#smPagePv img[src]').length") >= 1:
                break
        else:
            pytest.skip("逐頁預覽沒出來（這台機器太慢？）")
        yield ev
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        br.terminate(); srv.terminate()
        for p in (br, srv):
            try:
                p.wait(timeout=6)
            except Exception:
                p.kill()
        shutil.rmtree(data, ignore_errors=True)


def test_clicking_a_preview_opens_a_genuinely_larger_image(opened):
    ev = opened
    thumb = ev("(()=>{const i=document.querySelector('#smPagePv img[src]');"
               "return i ? i.naturalWidth : 0})()")
    assert thumb > 0, "縮圖還沒載好"
    ev("document.querySelector('#smPagePv .jt-thumb').click()")
    big = 0
    for _ in range(40):
        time.sleep(0.5)
        big = ev("(()=>{const m=document.querySelector('.jt-lb-img');"
                 "return m ? m.naturalWidth : 0})()") or 0
        if big > thumb * 1.4:
            break
    assert big > thumb * 1.4, (
        f"放大檢視裡的圖是 {big}px、縮圖是 {thumb}px —— 等於沒放大。"
        " 預覽端點有 `large=1`（150 dpi），點開時要去要那一份。")


def test_the_lightbox_opens_and_closes(opened):
    ev = opened
    assert ev("(()=>{const b=document.querySelector('.jt-lightbox');"
              "return b && !b.hidden})()") is True, "放大檢視沒開起來"
    assert ev("(()=>{const c=document.querySelector('.jt-lb-cap');"
              "return c ? c.textContent : ''})()"), "圖說是空的（看不出這是第幾頁）"
    ev("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}))")
    assert ev("document.querySelector('.jt-lightbox').hidden") is True, "Esc 關不掉"
