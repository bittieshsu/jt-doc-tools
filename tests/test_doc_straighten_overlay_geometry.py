"""掃描修正：拖曳四個角的座標對映（要真的瀏覽器才量得到）。

使用者 2026-09-13 回報「我拉的預覽點十字準星位置跟實際落點位置不一樣」。
根因是**疊圖層用「圖框」當座標系，而圖是 `object-fit: contain` 畫在框中間**：
框 541×972、圖只畫了 541×766，上下各留 103px。手柄擺在框的 40% 高度，
換算出去卻是圖的 37.3% —— **垂直差了約 22 個畫素**，而且越靠邊差越多。

不是只有放大鏡不準：**送去伺服器的四個角座標也是錯的**，所以裁出來的紙緣
會整片偏掉。

既有的檢查一條都抓不到：元素都在、沒有 JS 例外、沒有殘留中文、截圖也看不出
（手柄看起來就在紙角上）。**只有把數字量出來才看得到。**

這裡量兩件事，判準是**畫素級吻合**：
  1. 手柄放在 (0.30, 0.40) 時，它的中心要落在疊圖層的 (30%, 40%)。
  2. 放大鏡十字準星底下的那個**原圖畫素**，要等於 (0.30, 0.40) 對應的畫素。
"""
from __future__ import annotations

import importlib.util
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.browser_probe import browser as _browser  # noqa: E402



def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


pytestmark = pytest.mark.skipif(
    _browser() is None or importlib.util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")


def _sample_photo(tmpdir: Path) -> Path:
    """一張**長寬比跟圖框不一樣**的合成照片。

    比例一樣的話 contain 不會留白，這條測試就驗不到任何東西
    （會變成「掃 0 個檔也全綠」那種假通過）。圖框是 46vh × 100%，
    所以用一張明顯偏方的圖，保證上下或左右一定有留白。
    """
    import cv2
    import numpy as np

    img = np.full((600, 800, 3), (150, 105, 60), np.uint8)
    quad = np.int32([[120, 80], [700, 110], [660, 520], [90, 480]])
    cv2.fillPoly(img, [quad], (245, 245, 242))
    for y in range(160, 470, 60):
        cv2.line(img, (170, y), (600, y), (40, 40, 40), 5)
    out = tmpdir / "photo.png"
    cv2.imwrite(str(out), img)
    return out


@pytest.fixture(scope="module")
def live():
    data = tempfile.mkdtemp(prefix="dsgeom-")
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # **瀏覽器讀得到的位置**：snap 版的 chromium 看不到 /opt，連 /tmp 都是
    # 它自己的那一個 —— 素材放在別處時症狀是 `net::ERR_FILE_NOT_FOUND`，
    # 看起來完全像我們的上傳程式壞掉（CLAUDE.md ⑲）。
    home = Path(os.path.expanduser("~")) / "snap" / "chromium" / "common"
    shot = home if home.is_dir() else Path(tempfile.mkdtemp(prefix="dsgeom-img-"))
    shot.mkdir(parents=True, exist_ok=True)
    photo = _sample_photo(shot)
    br = subprocess.Popen(
        [_browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
         "--window-size=1500,2200", f"--remote-debugging-port={cdp}",
         "--remote-allow-origins=*", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        yield port, cdp, photo
    finally:
        # **自己起的自己收**（使用者 2026-09-13 交代）：不可以用名字樣式批次殺，
        # 這台機器上還有別的專案的 chromium。
        br.terminate()
        srv.terminate()
        for p in (br, srv):
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        shutil.rmtree(data, ignore_errors=True)
        photo.unlink(missing_ok=True)


def _measure(port: int, cdp: int, photo: Path) -> dict:
    import websockets.sync.client as wsc

    req = urllib.request.Request(
        f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
    tab = json.loads(urllib.request.urlopen(req, timeout=10).read())
    with wsc.connect(tab["webSocketDebuggerUrl"], max_size=None,
                     open_timeout=10) as ws:
        n = [0]

        def send(method, params=None):
            n[0] += 1
            ws.send(json.dumps({"id": n[0], "method": method,
                                "params": params or {}}))
            while True:
                m = json.loads(ws.recv())
                if m.get("id") == n[0]:
                    return m

        def ev(expr):
            r = send("Runtime.evaluate",
                     {"expression": expr, "returnByValue": True})
            return r["result"]["result"].get("value")

        send("Page.enable")
        send("DOM.enable")
        send("Runtime.enable")
        send("Page.navigate",
             {"url": f"http://127.0.0.1:{port}/tools/doc-straighten/"})
        time.sleep(3)
        doc = send("DOM.getDocument", {"depth": -1})
        nd = send("DOM.querySelector", {"nodeId": doc["result"]["root"]["nodeId"],
                                        "selector": "input[type=file]"})
        send("DOM.setFileInputFiles",
             {"files": [str(photo)], "nodeId": nd["result"]["nodeId"]})
        for _ in range(60):
            if ev("(function(){var e=document.getElementById('dsPvPanel');"
                  "return !!(e && !e.hidden)})()"):
                break
            time.sleep(1)
        else:
            pytest.fail("預覽面板沒有出現 —— 上傳或預覽壞了")
        ev("document.querySelector('input[name=\"dsQuadMode\"]"
           "[value=\"manual\"]').click()")
        time.sleep(1.5)
        tx, ty = 0.30, 0.40
        ev("""(function(){
          var art=document.getElementById('dsArt').getBoundingClientRect();
          var h=document.querySelectorAll('#dsHandles .ds-h')[0];
          function pe(t,x,y){return new PointerEvent(t,{clientX:x,clientY:y,
            bubbles:true,cancelable:true,pointerId:1});}
          h.dispatchEvent(pe('pointerdown',art.left+art.width*0.5,
                             art.top+art.height*0.5));
          document.dispatchEvent(pe('pointermove',art.left+art.width*%f,
                                    art.top+art.height*%f));
        })()""" % (tx, ty))
        time.sleep(0.4)
        raw = ev("""(function(){
          var img=document.getElementById('dsPvBefore');
          var art=document.getElementById('dsArt').getBoundingClientRect();
          var lo=document.querySelector('.ds-loupe');
          if(!lo) return JSON.stringify({error:'放大鏡沒有出現'});
          var lr=lo.getBoundingClientRect(), cs=getComputedStyle(lo);
          var bs=cs.backgroundSize.split(' ').map(parseFloat);
          var bp=cs.backgroundPosition.split(' ').map(parseFloat);
          var bw=parseFloat(cs.borderTopWidth);
          var crossX=(lr.width-2*bw)/2, crossY=(lr.height-2*bw)/2;
          var srcX=(crossX-bp[0])/bs[0]*img.naturalWidth;
          var srcY=(crossY-bp[1])/bs[1]*img.naturalHeight;
          var h0=document.querySelectorAll('#dsHandles .ds-h')[0]
                   .getBoundingClientRect();
          return JSON.stringify({
            natural:[img.naturalWidth,img.naturalHeight],
            box:[document.getElementById('dsStage').clientWidth,
                 document.getElementById('dsStage').clientHeight],
            art:[art.width,art.height],
            underCross:[srcX,srcY],
            expect:[%f*img.naturalWidth,%f*img.naturalHeight],
            handle:[h0.left+h0.width/2-art.left,h0.top+h0.height/2-art.top],
            handleExpect:[%f*art.width,%f*art.height]});
        })()""" % (tx, ty, tx, ty))
        return json.loads(raw)


def test_the_overlay_matches_the_picture_not_the_box(live):
    port, cdp, photo = live
    m = _measure(port, cdp, photo)
    assert "error" not in m, m.get("error")
    nw, nh = m["natural"]
    bw, bh = m["box"]
    aw, ah = m["art"]
    # 前提：這張圖在框裡**真的有留白** —— 沒有留白就驗不到任何東西
    assert abs(aw / ah - nw / nh) < 0.01, "疊圖層的比例要跟原圖一樣"
    assert abs(ah - bh) > 8 or abs(aw - bw) > 8, (
        "這張素材在圖框裡沒有留白，這條測試等於沒驗到東西 —— 換一張比例不同的")
    # ① 手柄要落在疊圖層的 30% / 40%
    hx, hy = m["handle"]
    ex, ey = m["handleExpect"]
    assert abs(hx - ex) <= 1.5 and abs(hy - ey) <= 1.5, (
        f"手柄位置偏了：{m['handle']} vs 期望 {m['handleExpect']}")
    # ② 放大鏡十字準星底下的原圖畫素要等於那一點
    ux, uy = m["underCross"]
    px, py = m["expect"]
    assert abs(ux - px) <= 2 and abs(uy - py) <= 2, (
        f"放大鏡取樣偏了：準星底下是原圖 ({ux:.0f},{uy:.0f})，"
        f"應該是 ({px:.0f},{py:.0f}) —— 差 "
        f"({abs(ux-px):.0f},{abs(uy-py):.0f}) 個畫素")
