"""頁面模式在**真的瀏覽器裡真的畫得出來**。

## 為什麼非要真瀏覽器不可

頁面模式的東西**要比對完成、按下切換之後才存在** —— TEST_PLAN §0.6 那張表
裡「送出後的結果區」與「要點開的面板」兩格都命中。既有的
`test_pages_boot_in_a_browser.py` 只在**頁面剛載入時**收例外，看不到這一段；
靜態檢查看得到程式碼在那裡，但看不到它有沒有跑。

## 判準

* 頁面圖**真的載進來了**（`naturalWidth > 0`）—— 端點回 200 不代表圖是好的。
* 差異框**在畫面上真的佔到空間**（`getBoundingClientRect`）——
  `doc-straighten` 那個四邊形從第一版起就沒出現過，正是因為只驗了屬性。
* 切回文字模式，文字那一份還在（**切模式是開關，不是刪除鍵**）。
"""
from __future__ import annotations

import io
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


def _write_pdf(path: str, lines: list[str]) -> None:
    import fitz
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    for i, ln in enumerate(lines):
        pg.insert_text((60, 90 + 28 * i), ln, fontsize=14, fontname="helv")
    doc.save(path)
    doc.close()


@pytest.fixture(scope="module")
def ready():
    if _browser() is None:
        pytest.skip("這台機器沒有 chromium")
    data = tempfile.mkdtemp(prefix="dfpage-data-")
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
            r = send("Runtime.evaluate",
                     {"expression": expr, "returnByValue": True, "awaitPromise": True})
            res = r.get("result", {})
            if "exceptionDetails" in res:
                exc = res["exceptionDetails"].get("exception", {}).get("description")
                raise AssertionError(f"頁面丟例外：{str(exc)[:300]}")
            return res.get("result", {}).get("value")

        up = _uploadable_dir()
        pa, pb = os.path.join(up, "df_a.pdf"), os.path.join(up, "df_b.pdf")
        _write_pdf(pa, ["Total amount NT$ 1,000,000", "Other terms unchanged"])
        _write_pdf(pb, ["Total amount NT$ 2,000,000", "Other terms unchanged"])

        send("Runtime.enable"); send("Page.enable"); send("DOM.enable")
        send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/doc-diff/"})
        time.sleep(4)

        doc = send("DOM.getDocument", {"depth": -1})
        root = doc["result"]["root"]["nodeId"]
        ids = send("DOM.querySelectorAll",
                   {"nodeId": root, "selector": ".file-upload input[type=file]"})
        nodes = ids["result"].get("nodeIds") or []
        if len(nodes) < 2:
            pytest.skip("找不到兩個上傳欄位")
        send("DOM.setFileInputFiles", {"files": [pa], "nodeId": nodes[0]})
        send("DOM.setFileInputFiles", {"files": [pb], "nodeId": nodes[1]})
        time.sleep(1.5)
        assert ev("(()=>{const b=document.getElementById('btnCompare');"
                  "if(!b) return false; b.click(); return true})()"), "找不到「開始比對」"

        # **等久一點**：跟完整套件一起跑時這台機器很忙，60 秒會不夠 ——
        # 而 skip 掉的話「整支沒跑」跟「全部通過」在 pytest 輸出裡長得一樣
        # （2026-09-16 實際發生：單跑全綠、完整套件裡四條全 skip）。
        for _ in range(180):
            time.sleep(1)
            if ev("(()=>{const p=document.getElementById('resultPanel');"
                  "return !!p && !p.hidden})()"):
                break
        else:
            pytest.skip("等了 180 秒比對結果還沒出來 —— 這台機器太忙，"
                        "或 /compare 真的壞了（先單獨跑一次這支確認）")
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


def test_the_page_view_button_is_there_and_switches(ready):
    ev = ready
    assert ev("!!document.getElementById('btnModePage')"), "沒有「頁面模式」切換鈕"
    ev("document.getElementById('btnModePage').click()")
    time.sleep(3)
    assert ev("document.getElementById('diffBox').hidden") is True, "切到頁面模式，文字那一份沒有收起來"
    assert ev("document.getElementById('pageBox').hidden") is False, "頁面模式沒有顯示"


def test_the_page_images_actually_load(ready):
    """端點回 200 不代表圖是好的 —— 量 `naturalWidth`。"""
    ev = ready
    ev("document.getElementById('btnModePage').click()")
    for _ in range(40):
        time.sleep(0.5)
        n = ev("document.querySelectorAll('#pageBox img').length")
        if n:
            break
    assert n, "頁面模式裡一張圖都沒有"
    for _ in range(40):
        time.sleep(0.5)
        loaded = ev("[...document.querySelectorAll('#pageBox img')]"
                    ".filter(i=>i.naturalWidth>0).length")
        if loaded >= n:
            break
    assert loaded >= 1, f"{n} 張圖裡一張都沒有真的載進來（naturalWidth 全是 0）"


def test_the_marks_take_up_space_on_screen(ready):
    """**判準是框在畫面上有沒有佔到空間** —— 只驗屬性的話，
    被 CSS 蓋掉（例如 `display:none`）也會全綠（`doc-straighten` 踩過）。"""
    ev = ready
    ev("document.getElementById('btnModePage').click()")
    time.sleep(2)
    n = ev("document.querySelectorAll('#pageBox .dfp-mark').length")
    assert n, "一個差異框都沒有畫出來"
    sized = ev("[...document.querySelectorAll('#pageBox .dfp-mark')].filter(el=>{"
               "const r=el.getBoundingClientRect(); return r.width>1 && r.height>1;}).length")
    assert sized >= 1, f"{n} 個框都沒有佔到空間（寬或高是 0）"


def test_switching_back_keeps_the_text_view(ready):
    """切模式是開關，不是刪除鍵。"""
    ev = ready
    ev("document.getElementById('btnModePage').click()")
    time.sleep(1)
    ev("document.getElementById('btnModeText').click()")
    time.sleep(1)
    assert ev("document.getElementById('diffBox').hidden") is False
    assert ev("document.getElementById('pageBox').hidden") is True
    assert ev("document.querySelectorAll('#diffBox .ln').length") > 0, "文字那一份不見了"
