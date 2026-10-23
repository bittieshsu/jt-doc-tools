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
        assert not t.js("!!document.querySelector('h1 .tool-beta')"), "v1.17.2 起標題旁不標 Beta"
        # ---- eSCL 旁的「?」（使用者 2026-10-15）：按下才出現本站樣式的說明，不是瀏覽器原生提示
        help_ = ("(() => { const n = document.querySelector('.mf-beta-note [data-mf-help]');"
                 " if (!n) return null; const b = n.querySelector('button'), p = n.querySelector('.mf-help-pop');"
                 " const r = p.getBoundingClientRect();"
                 " return {open: !p.hidden && r.width > 0, exp: b.getAttribute('aria-expanded'), title: b.title,"
                 " before: n.previousSibling ? n.previousSibling.textContent.slice(-12) : '',"
                 " left: r.left, right: r.right, vw: window.innerWidth, text: p.innerText,"
                 " bg: getComputedStyle(p).backgroundColor}; })()")
        h = t.js(help_)
        assert h, "Beta 說明裡 eSCL 旁邊沒有「?」"
        assert "eSCL" in h["before"], f"「?」沒有接在 eSCL 後面：{h['before']!r}"
        assert not h["open"] and h["exp"] == "false" and not h["title"], f"一開始就打開或用了原生 title：{h}"
        note = t.js("document.querySelector('.mf-beta-note').innerText")
        assert note.count("eSCL") >= 1 and ("實機" in note or "real device" in note or "実機" in note), note
        for width in (1280, 390):
            t.send("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1,
                                                          "mobile": width < 600})
            # 等版面真的變成這個寬度再按（整批測試時機器忙，0.3 秒不夠，量到舊版面就超出畫面；2026-10-23 紅過一次）
            assert t.wait_for(f"document.documentElement.clientWidth <= {width}"), "版面寬度沒有換過來"
            time.sleep(0.3)
            t.js("document.querySelector('.mf-beta-note .mf-help-btn').click()")
            assert t.wait_for("!document.getElementById('mfEsclHelp').hidden"), "按「?」沒有出現說明"
            time.sleep(0.2)     # 下一個畫面還會再定位一次（requestAnimationFrame）
            h = t.js(help_)
            assert h["open"] and h["exp"] == "true" and "AirPrint" in h["text"], h
            assert h["bg"] == "rgb(17, 24, 39)", f"說明框不是本站樣式：{h['bg']}"
            # 跟指定的寬度比，不跟 innerWidth 比（手機模擬下內容溢出時 innerWidth 會跟著撐大，永遠過）
            assert h["left"] >= 0 and h["right"] <= width, f"{width} 寬：說明框超出畫面 {h}"
            t.shot(f"mfp_{locale}_escl_help_{width}", width)
            # 點說明框裡面不收；點外面收起
            t.js("document.getElementById('mfEsclHelp').click()")
            assert t.js("!document.getElementById('mfEsclHelp').hidden"), "點說明框本身不該收起"
            t.js("document.querySelector('h1').click()")
            assert t.wait_for("document.getElementById('mfEsclHelp').hidden"), "點外面沒有收起"
        # 再按一次「?」收起；Esc 也收起
        t.js("document.querySelector('.mf-beta-note .mf-help-btn').click()")
        assert t.wait_for("!document.getElementById('mfEsclHelp').hidden")
        t.js("document.querySelector('.mf-beta-note .mf-help-btn').click()")
        assert t.wait_for("document.getElementById('mfEsclHelp').hidden"), "再按一次「?」沒有收起"
        t.js("document.querySelector('.mf-beta-note .mf-help-btn').click()")
        t.js("document.querySelector('.mf-beta-note .mf-help-btn').dispatchEvent("
             "new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
        assert t.wait_for("document.getElementById('mfEsclHelp').hidden"), "按 Esc 沒有收起"
        t.send("Emulation.clearDeviceMetricsOverride", {})
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

        # ---- 下載診斷報告：按下展開格式選單（使用者 2026-10-15：txt md json html pdf），由伺服器產生檔案
        # 攔下載：記檔名與大小，不真的存檔（不可以 fetch blob: 網址 —— 站台的 CSP 不准，跟正式環境一樣）
        t.js("""window.__dl = []; (() => { const orig = HTMLAnchorElement.prototype.click;
                const mk = URL.createObjectURL.bind(URL);
                URL.createObjectURL = (b) => { window.__blob = b; return mk(b); };
                HTMLAnchorElement.prototype.click = function () {
                  if (this.download) { const b = window.__blob;
                    window.__dl.push([this.download, b ? b.size : -1, b ? b.type : '']); return; }
                  return orig.call(this); }; })(); true""")
        t.js("document.getElementById('mfDownloadBtn').click()")
        assert t.wait_for("!document.querySelector('#mfSaveBar [data-mf-report-menu]').hidden"), "格式選單沒有打開"
        fmts = t.js("[...document.querySelectorAll('#mfSaveBar [data-mf-report-menu] [data-fmt]')].map(b => b.dataset.fmt)")
        assert fmts == ["txt", "md", "json", "html", "pdf"], fmts
        t.shot(f"mfp_{locale}_report_menu_1280", 1280)
        t.js("document.querySelector('#mfSaveBar [data-fmt=\"pdf\"]').click()")
        assert t.wait_for("window.__dl.length === 1"), \
            ("選了 PDF 沒有下載", t.js("document.getElementById('mfSaveMsg').innerText"), t.errs)
        name, size, _typ = t.js("window.__dl[0]")
        assert name.startswith("mfp-diagnostic-127.0.0.1-") and name.endswith(".pdf") and size > 1000, (name, size)
        assert t.js("document.querySelector('#mfSaveBar [data-mf-report-menu]').hidden"), "選完選單沒有收起來"
        assert t.js("document.getElementById('mfDownloadBtn').getAttribute('aria-expanded')") == "false"

        # ---- 儲存 → 出現在清單 → 詳細
        t.js("document.getElementById('mfSaveName').value = '三樓'; document.getElementById('mfSaveBtn').click()")
        assert t.wait_for("document.querySelectorAll('#mfListWrap tbody tr').length === 1"), "儲存後清單沒有出現"
        # v1.17.3：每一台標出來源（自己新增 / 管理員共用），不是只在名稱後面放小字
        src = t.js("[[...document.querySelectorAll('#mfListWrap thead th')].map(th => th.innerText.trim()).join('|'),"
                   " (document.querySelector('#mfListWrap tbody .mf-src') || {}).className || '']")
        assert "來源" in src[0] or "Source" in src[0] or "提供元" in src[0], src
        assert src[1] == "mf-src own", src
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
        # 下載診斷報告不在清單那一列，在「詳細」裡（使用者 2026-10-15）
        lab = t.js("document.querySelector('#mfDetailReportBtn span').textContent")
        assert lab and not any(lab == x[1] for x in btns), f"清單那一列還有下載診斷報告：{btns}"
        t.js("document.querySelector('[data-mf-detail]').click()")
        assert t.wait_for("!document.getElementById('mfDetailPanel').hidden "
                          "&& document.getElementById('mfDetail').innerText.includes('C5571')")
        assert t.js("document.querySelector('[data-mf-detail]').getAttribute('aria-expanded')") == "true"
        assert t.js("!document.querySelector('#mfDetailPanel [data-mf-report]').hidden "
                    "&& document.getElementById('mfDetailReportBtn').getBoundingClientRect().width > 0"), \
            "「詳細」裡沒有下載診斷報告"
        t.shot(f"mfp_{locale}_saved_1280", 1280)
        t.js("document.getElementById('mfDetailReportBtn').click()")
        assert t.wait_for("!document.querySelector('#mfDetailPanel [data-mf-report-menu]').hidden")
        t.shot(f"mfp_{locale}_detail_report_menu_1280", 1280)
        t.shot(f"mfp_{locale}_detail_report_menu_390", 390)
        for i, fmt in enumerate(["txt", "md", "json", "html"], start=2):
            if fmt != "txt":
                t.js("document.getElementById('mfDetailReportBtn').click()")
            t.js(f"document.querySelector('#mfDetailPanel [data-fmt=\"{fmt}\"]').click()")
            assert t.wait_for(f"window.__dl.length === {i}"), f"詳細裡選 {fmt} 沒有下載"
            name, size, _typ = t.js(f"window.__dl[{i - 1}]")
            assert name.endswith("." + fmt) and size > 200, (fmt, name, size)
        # 點選單外面要收起來
        t.js("document.getElementById('mfDetailReportBtn').click()")
        assert t.wait_for("!document.querySelector('#mfDetailPanel [data-mf-report-menu]').hidden")
        t.js("document.getElementById('mfDetailTitle').click()")
        assert t.wait_for("document.querySelector('#mfDetailPanel [data-mf-report-menu]').hidden"), "點外面沒有收起來"
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
        t.js("document.querySelector('[data-mf-rename]').click()")    # 不用位置找：FUJIFILM 的那一列前面多了「信箱取件」
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
        t.js("document.querySelector('#mfListWrap .mf-del').click()")
        assert t.wait_for("!!document.querySelector('.modal-ok')"), "確認對話框沒有出現"
        t.js("document.querySelector('.modal-ok').click()")
        assert t.wait_for("!!document.querySelector('#mfListWrap .mf-empty')"), "移除後清單沒有變空"
        assert not t.errs, t.errs
