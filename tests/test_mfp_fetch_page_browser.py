"""事務機取件頁：在真的瀏覽器裡跑一次（連線測試 → 儲存 → 詳細 → 被擋的位址）。

畫面幾乎全是 JS 畫的，只測端點看不到「按了有沒有反應、結果畫不畫得出來、會不會把設備的
HTML 當成網頁執行」。所以起一個拋棄式實例＋無頭瀏覽器，對**本機一台假的事務機網頁**實際操作。

假事務機在 127.0.0.1 —— 正式規則會擋回送位址，所以實例用**這支測試裡的啟動程式碼**放行
127.0.0.1 與那個埠（正式程式碼沒有任何放行開關）。未啟用登入、從 localhost 連 —— 這正是
「只准本機」那條會放行的情況。

截圖存到 `JTDT_SHOT_DIR`（有設才存），給發版前逐張目視用。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.browser_probe import browser as _browser  # noqa: E402
from tools.browser_probe import profile_arg as _profile_arg  # noqa: E402

pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


_LAUNCH = """
import sys
sys.path.insert(0, {root!r})
import app.core.mfp_client as m
_real = m.check_address
m.check_address = lambda ip: None if ip == "127.0.0.1" else _real(ip)
m.ALLOWED_PORTS = tuple(m.ALLOWED_PORTS) + ({fake},)
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port={port}, log_level="warning")
"""


@pytest.fixture(scope="module")
def live():
    from tests.test_mfp_client import COOKIE_VALUE, _Handler
    fake_srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=fake_srv.serve_forever, daemon=True).start()
    fake = fake_srv.server_address[1]
    data = tempfile.mkdtemp(prefix="mfppage-")
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen([sys.executable, "-c", _LAUNCH.format(root=str(ROOT), fake=fake, port=port)],
                           cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [_browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
         _profile_arg(), f"--remote-debugging-port={cdp}", "--remote-allow-origins=*", "about:blank"],
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
        yield port, cdp, fake, COOKIE_VALUE
    finally:
        br.terminate()
        srv.terminate()
        fake_srv.shutdown()
        try:
            br.wait(timeout=5)
            srv.wait(timeout=5)
        except Exception:
            br.kill()
            srv.kill()
        shutil.rmtree(data, ignore_errors=True)


class _Tab:
    def __init__(self, ws):
        self.ws, self.n, self.errs = ws, 0, []

    def send(self, method, params=None):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        while True:
            m = json.loads(self.ws.recv(timeout=60))
            if m.get("id") == self.n:
                return m
            if m.get("method") == "Runtime.exceptionThrown":
                d = m["params"]["exceptionDetails"]
                self.errs.append("例外：" + (d.get("exception", {}).get("description") or d.get("text", "?"))[:200])
            elif m.get("method") == "Log.entryAdded" and m["params"]["entry"].get("level") == "error":
                t = m["params"]["entry"].get("text", "")
                if "status of 400" not in t:      # 被擋的位址本來就回 400
                    self.errs.append("主控台：" + t[:200])

    def js(self, expr):
        r = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        return r.get("result", {}).get("result", {}).get("value")

    def wait_for(self, expr, timeout=40.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.js(expr):
                return True
            time.sleep(0.2)
        return False

    def shot(self, name, width):
        d = os.environ.get("JTDT_SHOT_DIR")
        if not d:
            return
        self.send("Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width < 600})
        time.sleep(0.4)
        h = self.js("Math.ceil(document.documentElement.scrollHeight)") or 900
        self.send("Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": min(int(h), 6000), "deviceScaleFactor": 1, "mobile": width < 600})
        time.sleep(0.4)
        r = self.send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
        Path(d).mkdir(parents=True, exist_ok=True)
        Path(d, f"{name}.png").write_bytes(base64.b64decode(r["result"]["data"]))


def _open(cdp):
    import websockets.sync.client as wsc
    req = urllib.request.Request(f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        tab = json.loads(r.read())
    return wsc.connect(tab["webSocketDebuggerUrl"], max_size=None, open_timeout=10)


@pytest.mark.parametrize("locale", ["zh-Hant", "en", "ja"])
def test_connection_test_save_detail_and_refusal(live, locale):
    port, cdp, fake, cookie_value = live
    base = f"http://localhost:{port}"
    with _open(cdp) as ws:
        t = _Tab(ws)
        t.send("Runtime.enable")
        t.send("Log.enable")
        t.send("Page.enable")
        t.send("Network.enable")
        t.send("Network.setCookie", {"name": "jtdt_locale", "value": locale, "url": base})
        t.send("Page.navigate", {"url": base + "/tools/mfp-fetch/"})
        assert t.wait_for("document.querySelector('#mfListWrap .mf-empty, #mfListWrap table') "
                          "&& !document.querySelector('#mfListWrap').textContent.includes('載入中')"), \
            "「我的事務機」沒有載入（JS 沒跑起來？）"
        assert t.js("!!document.querySelector('h1 .tool-beta')"), "標題旁要有 Beta"
        # 連線方式：本站的下拉，不是瀏覽器原生的（使用者 2026-10-14 指出）；跟旁邊的輸入框一樣高
        assert t.js("!!document.querySelector('#mfTarget.jt-select-native') "
                    "&& !!document.querySelector('.mf-add .jt-select-trigger')"), "連線方式還是瀏覽器原生的下拉"
        # 「連線測試」按鈕也一樣高（使用者 2026-10-14：右邊按鈕高度沒有跟左邊一樣高）
        hs = t.js("[document.getElementById('mfHost'), document.querySelector('.mf-add .jt-select-trigger'),"
                  " document.getElementById('mfTestBtn')].map(e => Math.round(e.getBoundingClientRect().height))")
        assert max(hs) - min(hs) <= 1, f"輸入框、下拉、連線測試按鈕高度不同：{hs}"
        t.js("document.querySelector('.mf-add .jt-select-trigger').click()")
        assert t.wait_for("!!document.querySelector('.jt-select-trigger.open')"), "下拉打不開"
        t.shot(f"mfp_{locale}_target_open_1280", 1280)
        t.js("document.querySelector('.mf-add .jt-select-trigger').click()")
        t.shot(f"mfp_{locale}_empty_1280", 1280)

        # ---- 連線測試（假事務機在 127.0.0.1:fake，用 HTTP）
        t.js(f"""(() => {{ const s = document.getElementById('mfTarget');
                 const o = document.createElement('option'); o.value = 'http:{fake}'; o.textContent = 'HTTP {fake}';
                 s.append(o); s.value = 'http:{fake}';
                 document.getElementById('mfHost').value = '127.0.0.1';
                 document.getElementById('mfTestBtn').click(); return true; }})()""")
        assert t.wait_for("!document.getElementById('mfResultPanel').hidden"), "結果面板沒有出現"
        body = t.js("document.getElementById('mfResult').innerText")
        assert "Apeos C5571" in body and "72.18.5" in body, body[:500]
        assert cookie_value not in t.js("document.body.innerText"), "Cookie 值出現在畫面上"
        if locale != "zh-Hant":
            assert "已遮蔽" not in body, "遮蔽標記沒有照介面語言顯示"
            assert "masked" in body.lower() or "マスク" in body
        if locale == "en":
            summary = t.js("document.querySelector('#mfResult .mf-summary').innerText + "
                           "document.querySelector('#mfResult .mf-evidence').innerText")
            assert not any(ch in summary for ch in "：（）、"), f"英文介面出現全形標點：{summary}"
        assert not t.js("!!document.querySelector('#mfResult frame, #mfResult form, #mfResult script')"), \
            "設備回的 HTML 被當成網頁元素放進畫面"
        assert t.js("document.querySelectorAll('#mfResult .mf-step').length") >= 4
        t.js("document.querySelectorAll('#mfResult details').forEach(d => d.open = true)")
        t.shot(f"mfp_{locale}_result_1280", 1280)
        t.shot(f"mfp_{locale}_result_390", 390)

        # ---- 儲存 → 出現在清單 → 詳細
        t.js("document.getElementById('mfSaveName').value = '三樓'; document.getElementById('mfSaveBtn').click()")
        assert t.wait_for("document.querySelectorAll('#mfListWrap tbody tr').length === 1"), "儲存後清單沒有出現"
        # 清單的按鈕每一顆都有圖示；只留圖示的有可讀名稱（使用者 2026-10-14：右邊按鈕沒 icon）
        btns = t.js("[...document.querySelectorAll('#mfListWrap tbody button')].map(b => "
                    "[!!b.querySelector('svg'), (b.innerText.trim() || b.getAttribute('aria-label') || '')])")
        assert btns and all(b[0] for b in btns), f"有按鈕沒有圖示：{btns}"
        assert all(b[1] for b in btns), f"只留圖示的按鈕沒有可讀名稱：{btns}"
        # 相容性：膠囊只放短狀態、一行（使用者 2026-10-14：相容性的標籤不好看）；清單裡說明放在滑鼠提示，
        # 免得整列被撐高（同日：「排列 下一版要改善」）—— 測試結果的摘要照舊另起一行
        chip = t.js("(() => { const c = document.querySelector('#mfListWrap .mf-status');"
                    " const lh = parseFloat(getComputedStyle(c).lineHeight) || 18;"
                    " return [c.getBoundingClientRect().height <= lh + 8, !!c.title]; })()")
        assert chip == [True, True], f"相容性標籤折行或沒有說明：{chip}"
        t.js("[...document.querySelectorAll('#mfListWrap tbody button')][1].click()")
        assert t.wait_for("!document.getElementById('mfDetailPanel').hidden "
                          "&& document.getElementById('mfDetail').innerText.includes('C5571')")
        assert t.js("document.querySelector('[data-mf-detail]').getAttribute('aria-expanded')") == "true"
        t.shot(f"mfp_{locale}_saved_1280", 1280)
        # 詳細要能收回來（使用者 2026-10-14）：同一顆按鈕再按一次收合；詳細區的「收合」也可以
        t.js("document.querySelector('[data-mf-detail]').click()")
        assert t.wait_for("document.getElementById('mfDetailPanel').hidden"), "再按一次「詳細」沒有收合"
        assert t.js("document.querySelector('[data-mf-detail]').getAttribute('aria-expanded')") == "false"
        t.js("document.querySelector('[data-mf-detail]').click()")
        assert t.wait_for("!document.getElementById('mfDetailPanel').hidden")
        t.js("document.getElementById('mfDetailClose').click()")
        assert t.wait_for("document.getElementById('mfDetailPanel').hidden"), "詳細區的「收合」沒有作用"
        t.js("document.querySelector('[data-mf-detail]').click()")
        assert t.wait_for("!document.getElementById('mfDetailPanel').hidden")

        # ---- 改名（站內對話框，非同步）→ 清單上的名字跟著變
        t.js("[...document.querySelectorAll('#mfListWrap tbody button')][3].click()")
        assert t.wait_for("!!document.querySelector('.modal-input')"), "改名對話框沒有出現"
        t.js("const i = document.querySelector('.modal-input'); i.value = '五樓'; "
             "document.querySelector('.modal-ok').click()")
        assert t.wait_for("document.querySelector('#mfListWrap tbody td').textContent === '五樓'"), "改名沒有生效"

        # ---- 被擋的位址：顯示原因，不是空白
        t.js("document.getElementById('mfHost').value = '169.254.169.254';"
             "document.getElementById('mfTarget').value = 'auto';"
             "document.getElementById('mfTestBtn').click()")
        assert t.wait_for("!document.getElementById('mfAddError').hidden")
        err = t.js("document.getElementById('mfAddError').innerText")
        assert err and "{" not in err, f"被擋的原因直接倒出原始 JSON：{err}"
        assert "169.254.169.254" in err
        if locale != "zh-Hant":
            # 日文也用漢字 —— 判準是「不是中文原文」，不是「沒有漢字」
            from app.core import mfp_client
            assert mfp_client.MESSAGES["ADDR_LINK_LOCAL"] not in err, f"錯誤訊息沒有翻譯：{err}"
        t.shot(f"mfp_{locale}_refused_1280", 1280)

        # ---- 移除（站內確認對話框）→ 清單變空，下一個語言從空清單開始
        t.js("[...document.querySelectorAll('#mfListWrap tbody button')][4].click()")
        assert t.wait_for("!!document.querySelector('.modal-ok')"), "確認對話框沒有出現"
        t.js("document.querySelector('.modal-ok').click()")
        assert t.wait_for("!!document.querySelector('#mfListWrap .mf-empty')"), "移除後清單沒有變空"
        assert not t.errs, t.errs
