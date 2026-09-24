"""側欄的捲軸要**按得住、拖得動**（v1.16.18，使用者回報）。

側欄把原生捲軸藏起來（`scrollbar-width: none`），自己畫一條浮在右緣的捲軸 ——
但那條是 `pointer-events: none` 的**純裝飾**：看起來是捲軸，按下去卻穿過它，
變成從那一點開始選取底下的文字，一拖就把整片側欄反白。

判準用**真的滑鼠事件**（CDP `Input.dispatchMouseEvent`）拖一次：
側欄有沒有跟著捲、捲的量對不對、有沒有選到文字。只驗 CSS 的 `pointer-events`
的話，換個寫法把事件擋掉照樣會過。
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
sys.path.insert(0, str(ROOT))
from tools.browser_probe import browser as _browser  # noqa: E402

pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")

#: 視窗矮一點，側欄一定要捲
_W, _H = 1280, 560
#: 往下拖多少 px
_DRAG = 120


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def live():
    data = tempfile.mkdtemp(prefix="sbdrag-")
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
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
        yield port, cdp
    finally:
        br.terminate(); srv.terminate()
        try:
            br.wait(timeout=5); srv.wait(timeout=5)
        except Exception:
            br.kill(); srv.kill()
        shutil.rmtree(data, ignore_errors=True)


def _drag_session(live):
    """開首頁、拿到捲軸的中心點、用滑鼠往下拖 `_DRAG` px，回傳量到的結果。"""
    import websockets.sync.client as wsc

    port, cdp = live
    req = urllib.request.Request(
        f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        tab = json.loads(r.read())
    try:
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

            def ev(js):
                r = send("Runtime.evaluate", {"expression": js, "returnByValue": True})
                res = r.get("result", {})
                assert "exceptionDetails" not in res, res.get("exceptionDetails")
                return res.get("result", {}).get("value")

            def mouse(kind, x, y, **kw):
                send("Input.dispatchMouseEvent",
                     {"type": kind, "x": x, "y": y, "button": "left", **kw})

            send("Emulation.setDeviceMetricsOverride",
                 {"width": _W, "height": _H, "deviceScaleFactor": 1, "mobile": False})
            send("Page.enable")
            send("Page.navigate", {"url": f"http://127.0.0.1:{port}/"})
            deadline = time.time() + 20
            while time.time() < deadline and ev("document.readyState") != "complete":
                time.sleep(0.2)
            time.sleep(0.5)
            before = ev("""(() => {
              const sc = document.querySelector('#sidebar .sidebar-scroll');
              const th = document.getElementById('sidebarScrollbar');
              sc.scrollTop = 0; sc.dispatchEvent(new Event('scroll'));
              const r = th.getBoundingClientRect();
              return {x: r.left + r.width / 2, y: r.top + r.height / 2,
                      h: r.height, shown: th.style.display !== 'none',
                      room: sc.clientHeight - th.offsetHeight,
                      max: sc.scrollHeight - sc.clientHeight};
            })()""")
            x, y = before["x"], before["y"]
            mouse("mouseMoved", x, y, buttons=0)
            mouse("mousePressed", x, y, buttons=1, clickCount=1)
            for i in range(1, 7):
                mouse("mouseMoved", x, y + _DRAG * i / 6, buttons=1)
            mouse("mouseReleased", x, y + _DRAG, buttons=0, clickCount=1)
            time.sleep(0.2)
            after = ev("""(() => ({
              top: document.querySelector('#sidebar .sidebar-scroll').scrollTop,
              selected: String(window.getSelection() || '').trim().length,
              dragging: document.getElementById('sidebar').classList.contains('sb-dragging'),
            }))()""")
            return before, after
    finally:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{cdp}/json/close/{tab['id']}", timeout=5).read()
        except Exception:
            pass


def test_the_sidebar_scrollbar_can_be_dragged(live):
    before, after = _drag_session(live)
    assert before["shown"] and before["max"] > 50, (
        f"前提：側欄要捲得動（{before}）—— 不然這條什麼都沒驗到")
    expected = min(before["max"], _DRAG * before["max"] / max(1, before["room"]))
    assert after["top"] > 0, "按住捲軸往下拖，側欄一動也沒動"
    assert abs(after["top"] - expected) <= max(12, expected * 0.1), (
        f"捲的量不對：拖 {_DRAG}px 應捲到約 {expected:.0f}，實際 {after['top']}")
    assert after["selected"] == 0, (
        f"拖捲軸時選到了 {after['selected']} 個字 —— 事件穿過捲軸落到底下的文字")
    assert not after["dragging"], "放開滑鼠之後還停在拖動狀態"
