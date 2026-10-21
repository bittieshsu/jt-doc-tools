"""設定 → MCP 的「連線設定」排版 —— 在真的瀏覽器裡量（v1.17.6）。

使用者 2026-10-21 截圖：「啟用 MCP 區塊太空了」—— 兩欄排版裡它跟內容很多的「站台網址」同一列，
被撐成跟那張一樣高，裡面只有一行字。改成：短的開關、數字兩兩一列，站台網址（含檔案大小）自己一整列。
這裡量：①短卡片不被撐高（同一列兩張高度相近、都不超過一個上限）②站台網址那張佔滿兩欄
③窄螢幕單欄、不橫向捲動 ④沒填網址時有「示意」說明，填了就收起來。
"""
from __future__ import annotations

import os
import shutil
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
    data = tempfile.mkdtemp(prefix="jtdt-mcpadm-")
    port, cdp = _free_port(), _free_port()
    env = dict(os.environ, JTDT_DATA_DIR=data, JTDT_CSRF_DISABLE="1")
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                            "--port", str(port), "--log-level", "warning"], cwd=root, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen([_browser(), "--headless=new", "--no-sandbox", "--disable-gpu", _profile_arg(),
                           f"--remote-debugging-port={cdp}", "--remote-allow-origins=*", "about:blank"],
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
        yield port, cdp
    finally:
        br.terminate()
        srv.terminate()
        for p in (br, srv):
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        shutil.rmtree(data, ignore_errors=True)


_TILES = """[...document.querySelectorAll('.mcp-set > .mcp-tile')].map(t => { const r = t.getBoundingClientRect();
    return {id: (t.querySelector('input') || {}).id, top: Math.round(r.top), h: Math.round(r.height), w: Math.round(r.width),
            wide: t.classList.contains('mcp-tile-wide')}; })"""


@pytest.mark.parametrize("locale", ["zh-Hant", "en", "ja"])
def test_connection_settings_layout(live, locale):
    port, cdp = live
    base = f"http://127.0.0.1:{port}"
    with _open(cdp) as ws:
        t = _Tab(ws)
        for d in ("Runtime", "Log", "Page", "Network"):
            t.send(f"{d}.enable")
        t.send("Network.setCookie", {"name": "jtdt_locale", "value": locale, "url": base})
        t.send("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
        t.send("Page.navigate", {"url": base + "/admin/mcp"})
        assert t.wait_for("document.readyState === 'complete' && !!document.getElementById('mcp-limits')")
        tiles = t.js(_TILES)
        set_w = t.js("Math.round(document.querySelector('.mcp-set').getBoundingClientRect().width)")
        rows = {}
        for x in tiles:
            rows.setdefault(x["top"], []).append(x)
        for top, row in rows.items():
            if len(row) == 2:
                hs = [x["h"] for x in row]
                assert max(hs) < 140, f"{locale}：同一列的短卡片被撐高 {row}"
        site = next(x for x in tiles if x["id"] == "mcp-site")
        assert site["wide"] and site["w"] >= set_w - 2, f"站台網址要佔滿整列 {site} / {set_w}"
        # 檔案大小在站台網址的右邊（寬螢幕），不是排在下面拉長整張卡
        side = t.js("(() => { const a = document.querySelector('.mcp-site-main').getBoundingClientRect(),"
                    " b = document.getElementById('mcp-limits').getBoundingClientRect(); return [b.left >= a.right - 1, Math.round(b.top - a.top)]; })()")
        assert side[0] and abs(side[1]) < 4, f"檔案大小要在右邊：{side}"
        enabled = next(x for x in tiles if x["id"] == "mcp-enabled")
        assert enabled["h"] < 120, f"「啟用 MCP」被撐高：{enabled}"
        # 沒填網址：有「示意」說明；填了就收起來
        assert t.js("!document.getElementById('mcp-prev-note').hidden")
        t.js("const s = document.getElementById('mcp-site'); s.value = 'https://doc.example.test'; s.dispatchEvent(new Event('input'))")
        assert t.js("document.getElementById('mcp-prev-note').hidden")
        assert t.js("document.getElementById('mcp-prev-up').textContent") == "https://doc.example.test/mcp/files/upload"
        t.shot(f"mcp_admin_settings_{locale}_1280", 1280)
        # 手機寬度：單欄、不可以橫向捲動
        t.send("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 900, "deviceScaleFactor": 1, "mobile": True})
        time.sleep(0.4)
        assert t.js("document.documentElement.scrollWidth") <= 390 + 1, "390 寬出現橫向捲動"
        widths = {x["w"] for x in t.js(_TILES)}
        assert len(widths) == 1, f"390 寬要單欄：{widths}"
        t.shot(f"mcp_admin_settings_{locale}_390", 390)
        t.send("Emulation.clearDeviceMetricsOverride", {})
        assert not t.errs, t.errs
