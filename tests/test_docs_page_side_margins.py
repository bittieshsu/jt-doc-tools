"""介紹站的說明頁在手機上，內文左右至少留 16px（2026-10-09 做合規支援頁時拍手機截圖才看到）。

`.ts-main` 原本寫 `padding: 32px 0 56px`，把 `.container` 左右 24px 的留白蓋成 0 ——
疑難排解頁從上線起在手機上字就貼著螢幕邊。量的是**畫面上的位置**（瀏覽器量），不是 CSS 字面。
"""
from __future__ import annotations

import json
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.browser_probe import browser as _browser  # noqa: E402
from tools.repo_paths import public_root  # noqa: E402

pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才量得到")

PAGES = ["troubleshooting.html", "compliance.html", "compliance-en.html", "compliance-ja.html"]

MEASURE = """(() => {
  const m = document.querySelector('main');
  const vw = document.documentElement.clientWidth;
  let left = vw, right = 0;
  m.querySelectorAll('h1, h2, h3, p, li, table, .cp-table-wrap, figure, .ts-toc').forEach((e) => {
    const r = e.getBoundingClientRect();
    if (!r.width) return;
    left = Math.min(left, r.left); right = Math.max(right, r.right);
  });
  return JSON.stringify({vw, left, right,
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth});
})()"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def measure():
    docs = public_root(ROOT) / "docs"
    if not (docs / "compliance.html").is_file():
        pytest.skip("找不到介紹站的 docs/")
    port, cdp = _free_port(), _free_port()
    srv = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                           cwd=str(docs), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen([_browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
                           f"--remote-debugging-port={cdp}", "--remote-allow-origins=*", "about:blank"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws = None
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.skip("靜態伺服器或瀏覽器起不來")
        import websockets.sync.client as wsc
        req = urllib.request.Request(f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            tab = json.loads(r.read())
        ws = wsc.connect(tab["webSocketDebuggerUrl"], max_size=None, open_timeout=10)
        n = [0]

        def send(method, params=None):
            n[0] += 1
            ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
            while True:
                m = json.loads(ws.recv(timeout=120))
                if m.get("id") == n[0]:
                    return m

        send("Page.enable")
        send("Emulation.setDeviceMetricsOverride",
             {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})

        def one(page: str) -> dict:
            send("Page.navigate", {"url": f"http://127.0.0.1:{port}/{page}"})
            time.sleep(1.2)
            r = send("Runtime.evaluate", {"expression": MEASURE, "returnByValue": True})
            return json.loads(r["result"]["result"]["value"])

        yield one
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        for p in (br, srv):
            p.terminate()
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()


@pytest.mark.parametrize("page", PAGES)
def test_text_keeps_a_side_gutter_on_a_phone(measure, page):
    m = measure(page)
    assert m["overflow"] <= 0, f"{page} 在手機上出現橫向捲軸：{m}"
    assert m["left"] >= 16 and m["vw"] - m["right"] >= 16, f"{page} 內文貼著螢幕邊：{m}"
