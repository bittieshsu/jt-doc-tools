"""掃描修正的四邊形疊圖：**在真的瀏覽器裡真的畫得出來**。

## 為什麼要一支端到端

使用者連著回報兩次（2026-09-13「十字準星跟實際落點不一樣」、
2026-09-14「四個角拉起來還是沒有連線」）。第二次的根因是這樣的：

* `SVGElement` **沒有 `hidden` 這個 IDL 屬性** —— `svg.hidden = false` 只是在
  物件上掛一個沒人看的 expando，`hidden` 那個標記一個字都沒動。
* `platform.css` 的 `[hidden] { display: none !important; }` 是**作者樣式**、
  沒有命名空間限定（瀏覽器內建的 `html.css` 有 `@namespace`，只管 HTML
  元素；我們這條沒有）→ **SVG 照樣被蓋掉**。

所以那個四邊形從第一版起就永遠 `display:none`，而且：

* **沒有任何 JS 例外** → `test_pages_boot_in_a_browser` 是綠的
* 旁邊四個手柄是 `<div>`，`.hidden` 正常 → 畫面上「點在、線不在」，
  看起來像「還沒做」而不是「壞了」
* `points` 屬性其實一直都算對 → 只驗屬性的測試也是綠的

**只驗「屬性有設」擋不住這一類。** 判準要是「那條線在畫面上真的佔了空間」
（`getBoundingClientRect`），那是唯一同時涵蓋 display、尺寸與座標的訊號。

`tests/test_no_svg_dot_hidden.py` 是同一件事的靜態檢查（便宜、每次都跑）；
這一支是行為檢查，兩支都要 —— 靜態那支擋不住「換一種方式把它藏起來」。
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
    or __import__("importlib").util.find_spec("websockets") is None
    or __import__("importlib").util.find_spec("cv2") is None,
    reason="沒有 chromium / websockets / OpenCV —— 這條要真的瀏覽器才驗得到")


def _make_photo(path: str) -> None:
    """合成一張「深色桌面上的斜紙」。

    **不用真實樣本**：`temp_pdfs/` 含客戶資料、不上 git，公開樹跑不到。
    """
    import cv2
    import numpy as np

    h, w = 900, 1200
    img = np.full((h, w, 3), (40, 60, 110), np.uint8)
    sheet = np.full((640, 480, 3), 245, np.uint8)
    for i, line in enumerate(["Purchase Agreement", "Party A / Party B",
                              "Date: 2026-01-05", "Amount: 120,000"]):
        cv2.putText(sheet, line, (40, 120 + i * 90), cv2.FONT_HERSHEY_SIMPLEX,
                    1.0, (30, 30, 30), 2, cv2.LINE_AA)
    m = cv2.getRotationMatrix2D((240, 320), 11, 1.0)
    m[0, 2] += (w - 480) / 2
    m[1, 2] += (h - 640) / 2
    img = cv2.warpAffine(sheet, m, (w, h), dst=img,
                         borderMode=cv2.BORDER_TRANSPARENT)
    cv2.imwrite(path, img)


@pytest.fixture(scope="module")
def page():
    """拋棄式實例 ＋ 無頭瀏覽器，回傳一個可以送 CDP 指令的函式。"""
    data = tempfile.mkdtemp(prefix="dsquad-")
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [_browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
         f"--remote-debugging-port={cdp}", "--remote-allow-origins=*",
         "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
                m = json.loads(ws.recv())
                if m.get("id") == i:
                    return m

        yield port, send
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


@pytest.fixture(scope="module")
def loaded(page):
    """上傳一張合成照片、切到「自己拉四個角」，回傳 evaluate 函式。"""
    port, send = page
    photo = os.path.join(_uploadable_dir(), "ds_quad_sheet.png")
    _make_photo(photo)

    send("Runtime.enable")
    send("Page.enable")
    send("DOM.enable")
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/doc-straighten/"})
    time.sleep(4)

    def ev(expr):
        r = send("Runtime.evaluate",
                 {"expression": expr, "returnByValue": True, "awaitPromise": True})
        res = r.get("result", {})
        if "exceptionDetails" in res:
            exc = res["exceptionDetails"].get("exception", {}).get("description")
            raise AssertionError(f"頁面丟例外：{str(exc)[:300]}")
        return res.get("result", {}).get("value")

    doc = send("DOM.getDocument", {"depth": -1})
    node = send("DOM.querySelector", {"nodeId": doc["result"]["root"]["nodeId"],
                                      "selector": "input[type=file]"})
    nid = node["result"].get("nodeId")
    if not nid:
        pytest.skip("找不到上傳欄位")
    send("DOM.setFileInputFiles", {"files": [photo], "nodeId": nid})

    for _ in range(60):
        time.sleep(1)
        shown = ev("(()=>{const p=document.getElementById('dsPvPanel');"
                   "return !!p && !p.hidden})()")
        if shown:
            break
    else:
        pytest.skip("預覽沒出來（可能是這台機器太慢）")

    assert ev("(()=>{const r=document.getElementById('dsQuadEdit');"
              "if(!r) return false; r.click(); return true})()"), "找不到「自己拉四個角」"
    time.sleep(2.5)
    return ev


def test_the_quad_outline_is_actually_painted(loaded):
    """**這條是重點**：線要在畫面上真的佔到空間。

    只驗 `points` 有值的話，`display:none` 的那個 bug 照樣全綠。
    """
    ev = loaded
    box = ev("(()=>{const r=document.getElementById('dsQuadLine')"
             ".getBoundingClientRect();return [r.width, r.height]})()")
    assert box and box[0] > 20 and box[1] > 20, (
        f"四個角的連線在畫面上量不到（{box}）——"
        " 可能又被 [hidden] 蓋掉了，或 points 沒設。")


def test_the_svg_is_not_left_display_none(loaded):
    """把根因直接釘住：`<svg>` 不可以還留著 `hidden` 標記。"""
    ev = loaded
    assert ev("getComputedStyle(document.getElementById('dsQuadSvg')).display") != "none"
    assert ev("document.getElementById('dsQuadSvg').hasAttribute('hidden')") is False, (
        "`hidden` 標記還在 —— SVGElement 沒有 `hidden` 這個屬性，"
        "要用 toggleAttribute 才拿得掉")


def test_four_handles_and_a_closed_polygon(loaded):
    """四個手柄、四個頂點 —— 少一個就不是四邊形。"""
    ev = loaded
    assert ev("document.querySelectorAll('#dsHandles .ds-h').length") == 4
    pts = ev("document.getElementById('dsQuadLine').getAttribute('points')") or ""
    assert len(pts.split()) == 4, f"頂點數不是 4：{pts!r}"


def test_the_outline_follows_the_handles(loaded):
    """線要跟手柄在同一個座標系上 —— 疊圖層算錯的話兩者會分家。

    （2026-09-13 那次「十字準星跟實際落點不一樣」就是疊圖層的座標系錯了。）
    """
    ev = loaded
    same = ev("""(()=>{
      const art = document.getElementById('dsArt').getBoundingClientRect();
      const hs = [...document.querySelectorAll('#dsHandles .ds-h')].map(h => {
        const r = h.getBoundingClientRect();
        return [(r.left + r.width/2 - art.left)/art.width,
                (r.top + r.height/2 - art.top)/art.height];
      });
      const pts = document.getElementById('dsQuadLine').getAttribute('points')
        .split(' ').map(p => p.split(',').map(Number).map(v => v/100));
      return hs.every((h,i) => Math.abs(h[0]-pts[i][0]) < 0.02
                            && Math.abs(h[1]-pts[i][1]) < 0.02);
    })()""")
    assert same, "手柄與連線對不起來 —— 疊圖層的座標系又錯了"
