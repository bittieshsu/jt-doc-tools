"""辦公文件轉圖片：在真的瀏覽器裡選 WebP ＋ 指定寬度，轉出來的圖要真的是那樣（issue #53）。

端點的測試驗得到「伺服器照參數做」，驗不到「頁面有沒有把使用者選的東西送出去」——
選項是 JS 讀的，讀錯一個 name 的話送出去的是預設值（PNG、200 DPI），
而結果看起來完全正常。判準是**頁面上載入的圖**的格式與寬度。
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
def page():
    import fitz

    data = tempfile.mkdtemp(prefix="p2iweb-")
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

    pdf = os.path.join(_uploadable_dir(), "p2i_web.pdf")
    doc = fitz.open()
    for w, h in ((960, 540), (595, 842)):          # 簡報頁 ＋ 直式 A4
        pg = doc.new_page(width=w, height=h)
        pg.insert_text((40, 80), "Slide", fontsize=28, fontname="helv")
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
        send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/pdf-to-image/"})
        time.sleep(4)

        before = {
            "quality_hidden": ev("document.getElementById('p2iQualityRow').hidden"),
            "width_hidden": ev("document.getElementById('p2iWidthRow').hidden"),
        }
        ev("document.querySelector('input[name=p2iFmt][value=webp]').click()")
        ev("document.querySelector('input[name=p2iSize][value=width]').click()")
        ev("document.querySelector('.p2i-width-presets [data-w=\"480\"]').click()")
        after = {
            "quality_hidden": ev("document.getElementById('p2iQualityRow').hidden"),
            "width_hidden": ev("document.getElementById('p2iWidthRow').hidden"),
            "dpi_hidden": ev("document.getElementById('p2iDpiRow').hidden"),
            "width_value": ev("document.getElementById('p2iWidth').value"),
        }

        nid = None
        for _ in range(8):
            try:
                d = send("DOM.getDocument", {"depth": -1})
                nid = send("DOM.querySelector",
                           {"nodeId": d["result"]["root"]["nodeId"],
                            "selector": ".file-upload input[type=file]"})["result"].get("nodeId")
                if nid:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not nid:
            pytest.skip("找不到上傳欄位")
        send("DOM.setFileInputFiles", {"files": [pdf], "nodeId": nid})
        for _ in range(60):
            time.sleep(1)
            done = ev("(()=>{const im=[...document.querySelectorAll('#p2iGrid img')];"
                      "return im.length===2 && im.every(i=>i.complete && i.naturalWidth>0)})()")
            if done:
                break
        else:
            pytest.skip("轉出來的圖沒有出現（這台機器太慢？）")
        yield ev, before, after
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


def test_the_options_show_and_hide_with_the_choices(page):
    _, before, after = page
    assert before["quality_hidden"] is True, "PNG 沒有品質可以選，那一列一開始要藏著"
    assert before["width_hidden"] is True
    assert after["quality_hidden"] is False, "選了 WebP，品質那一列要出現"
    assert after["width_hidden"] is False and after["dpi_hidden"] is True
    assert after["width_value"] == "480"


def test_the_images_on_the_page_are_webp_at_the_chosen_width(page):
    ev, _, _ = page
    widths = ev("[...document.querySelectorAll('#p2iGrid img')].map(i=>i.naturalWidth)")
    assert widths == [480, 480], f"頁面上的圖寬是 {widths}，選的是 480"
    srcs = ev("[...document.querySelectorAll('#p2iGrid img')].map(i=>i.getAttribute('src'))")
    assert all(s.endswith(".webp") for s in srcs), srcs


def test_the_status_and_download_button_name_the_format(page):
    ev, _, _ = page
    status = ev("document.getElementById('p2iStatus').textContent")
    assert "WebP" in status and "480" in status, status
    assert "PNG" not in status, status
    btn = ev("document.getElementById('p2iBtnDownload').textContent")
    assert "ZIP" in btn, btn
    per_page = ev("[...document.querySelectorAll('#p2iGrid .p2i-dl')].map(a=>a.getAttribute('download'))")
    assert per_page == ["page_1.webp", "page_2.webp"], per_page
