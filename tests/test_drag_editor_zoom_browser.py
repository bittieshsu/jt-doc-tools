"""拖曳定位編輯器（用印、浮水印、資產編輯共用）放大後頁面比例不可以跑掉（v1.17.4，使用者截圖）。

症狀：用印預覽 100% 正常；切到 150% 之後，視窗不夠寬時頁面框被壓窄、高度照放大的值 →
PDF 底圖在框裡等比縮小、上方空一大塊白，看起來「範圍放大、內容沒變」；把視窗拉寬才變大。
原因：外框是 flex，頁面框是 flex 子元素，預設會縮（只縮寬、不縮高）。

這裡在真的瀏覽器裡、窄視窗下把編輯器放大，量頁面框的寬高比要等於紙張比例，寬度也要真的變大。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

from tests.test_mfp_fetch_page_browser import _Tab, _browser, _free_port, _open, _profile_arg  # noqa: F401

pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets")


@pytest.fixture(scope="module")
def live():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    data = tempfile.mkdtemp(prefix="jtdt-dpe-")
    port, cdp = _free_port(), _free_port()
    env = dict(os.environ, JTDT_DATA_DIR=data, JTDT_CSRF_DISABLE="1")
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                            "--port", str(port), "--log-level", "warning"], cwd=root, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen([_browser(), "--headless=new", "--no-sandbox", "--disable-gpu", _profile_arg(),
                           f"--remote-debugging-port={cdp}", "--remote-allow-origins=*", "about:blank"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2)
            break
        except Exception:
            time.sleep(0.5)
    yield port, cdp
    br.terminate()
    srv.terminate()
    for p in (br, srv):
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()


@pytest.mark.parametrize("zoom", [1.5, 2, 3])
def test_zoom_keeps_the_paper_aspect_in_a_narrow_window(live, zoom):
    port, cdp = live
    with _open(cdp) as ws:
        t = _Tab(ws)
        t.send("Page.enable")
        t.send("Runtime.enable")
        t.send("Emulation.setDeviceMetricsOverride", {"width": 1100, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        t.send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/pdf-stamp/"})
        assert t.wait_for("!!window.DragPositionEditor && !!document.querySelector('.dpe-wrapper')")
        res = t.js(f"""(() => {{
            const root = document.querySelector('.dpe-wrapper');
            let el = root; while (el) {{ if (el.hidden) el.hidden = false; if (el.style) el.style.display = ''; el = el.parentElement; }}
            const ed = new DragPositionEditor({{root, asset_url: '', paper_w_mm: 210, paper_h_mm: 297,
                                                value: {{x_mm: 10, y_mm: 10, width_mm: 40, height_mm: 30}}}});
            ed.setZoom(1);
            const r1 = ed.getPaperEl().getBoundingClientRect();
            ed.setZoom({zoom});
            const r2 = ed.getPaperEl().getBoundingClientRect();
            return [r1.width, r1.height, r2.width, r2.height];
        }})()""")
        w1, h1, w2, h2 = res
        assert w1 > 50 and h1 > 50, res
        assert abs((w2 / h2) - (210 / 297)) < 0.01, f"放大 {zoom} 倍後頁面框比例跑掉：{res}"
        assert w2 > w1 * (zoom - 0.05), f"寬度沒有跟著放大（被外框壓回去）：{res}"
