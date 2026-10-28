"""兩張卡片中間夾了看不見的元素時，間距照樣要在。

`platform.css` 的 `.panel + .panel { margin-top: 16px }` 只在兩張卡片緊鄰時才生效。系統狀態頁的
「使用者檔案用量」與「資料庫健康狀態」中間夾了一個放翻譯字串的 `<template>`，兩張卡片就黏在一起
（使用者 2026-10-28 截圖）。掃過全站還有 6 處夾了 `<template>` 或隱藏的 `<input>`。

判準落在畫面：用真的瀏覽器載入真的 `platform.css`，量第二張卡片的 `margin-top`。
反向對照：中間是看得見的區塊時不可以被這條規則加上間距。
"""
from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import time
import urllib.request

import pytest

from tests.test_official_doc_tool import ROOT, _free_port

CSS = ROOT / "static" / "css" / "platform.css"

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/platform.css"></head><body>
<div class="panel">A</div><div class="panel" id="adjacent">x</div>
<div class="panel">B</div><template><span>t</span></template><div class="panel" id="after_template">x</div>
<div class="panel">C</div><input type="hidden" value="1"><div class="panel" id="after_hidden_input">x</div>
<div class="panel">D</div><template></template><script>1</script><div class="panel" id="after_two_hidden">x</div>
<div class="panel">E</div><div class="other">visible</div><div class="panel" id="after_visible_block">x</div>
</body></html>"""


def test_panels_keep_their_gap_across_invisible_siblings():
    sys.path.insert(0, str(ROOT))
    from tools import browser_probe

    br_path = browser_probe.browser()
    if not br_path:
        pytest.skip("沒有 chromium")
    try:
        import websockets.sync.client as wsc
    except ImportError:
        pytest.skip("沒有 websockets")

    # snap 版 chromium 讀不到 /tmp 底下的檔案（沙箱），所以用 HTTP 提供測試頁與真正的 platform.css
    css = CSS.read_bytes()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body, ctype = (css, "text/css") if self.path == "/platform.css" else (PAGE.encode(), "text/html; charset=utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/gap.html"
    cdp = _free_port()
    br = subprocess.Popen(
        [br_path, "--headless=new", "--no-sandbox", "--disable-gpu", browser_probe.profile_arg(), f"--remote-debugging-port={cdp}", "--remote-allow-origins=*",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws = None
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.skip("瀏覽器起不來")
        req = urllib.request.Request(f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            tab = json.loads(r.read())
        ws = wsc.connect(tab["webSocketDebuggerUrl"], max_size=None, open_timeout=10)
        n = [0]

        def send(method, params=None):
            n[0] += 1
            i = n[0]
            ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
            while True:
                m = json.loads(ws.recv(timeout=60))
                if m.get("id") == i:
                    return m

        def js(expr):
            return send("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                             "awaitPromise": True})["result"]["result"].get("value")

        send("Page.enable")
        send("Page.navigate", {"url": url})
        for _ in range(40):
            if js("document.readyState === 'complete' && document.styleSheets.length > 0"):
                break
            time.sleep(0.25)
        gap = {k: js(f"getComputedStyle(document.getElementById('{k}')).marginTop")
               for k in ("adjacent", "after_template", "after_hidden_input", "after_two_hidden",
                         "after_visible_block")}
    finally:
        if ws is not None:
            ws.close()
        br.terminate()
        br.wait(timeout=10)
        srv.shutdown()
    assert gap["adjacent"] == "16px", gap
    assert gap["after_template"] == "16px", gap
    assert gap["after_hidden_input"] == "16px", gap
    assert gap["after_two_hidden"] == "16px", gap
    assert gap["after_visible_block"] == "0px", "中間是看得見的區塊時不應該被加間距"
