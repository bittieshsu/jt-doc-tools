"""從事務機掃描、共用事務機的管理頁 —— 在真的瀏覽器裡跑一次（對本機一台假的 eSCL 設備）。

* 事務機取件：連線測試讀到 eSCL → 儲存 → 清單出現「掃描」→ 選項照能力表 → 開始掃描 →
  完成、存進工作區（有「開啟」）→「最近的掃描」列出來。
* 管理頁（設定 → 共用事務機）：同一份畫面、標題與儲存鈕換成共用事務機 → 建立 → 清單有「成員」→
  成員面板打得開、存得了。

假設備在 127.0.0.1 —— 實例用這支測試裡的啟動程式碼放行 127.0.0.1 與那個埠（正式程式碼沒有放行開關）。
未啟用登入、從 localhost 連（「只准本機」那條會放行；管理頁在未啟用登入時也開放）。
截圖存到 `JTDT_SHOT_DIR`（有設才存）。
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
from tests.test_mfp_escl import Fake, pdf_bytes

pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")

_LAUNCH = """
import sys
sys.path.insert(0, {root!r})
import app.core.mfp_client as m
_real = m.check_address
m.check_address = lambda ip: None if ip == "127.0.0.1" else _real(ip)
m.ALLOWED_PORTS = tuple(m.ALLOWED_PORTS) + ({fake},)
import app.core.mfp_escl as e
e.STATUS_POLL = 0.1
e.PAGE_POLL = 0.1
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port={port}, log_level="warning")
"""


@pytest.fixture(scope="module")
def live():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    fake = Fake()
    srv_fake = fake.serve()
    fport = srv_fake.server_address[1]
    fake.port = fport
    data = tempfile.mkdtemp(prefix="mfpscan-")
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen([sys.executable, "-c", _LAUNCH.format(root=str(root), fake=fport, port=port)],
                           cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        yield port, cdp, fake
    finally:
        br.terminate()
        srv.terminate()
        srv_fake.shutdown()
        try:
            br.wait(timeout=5)
            srv.wait(timeout=5)
        except Exception:
            br.kill()
            srv.kill()
        shutil.rmtree(data, ignore_errors=True)


def _start(t, base, path, locale="zh-Hant"):
    t.send("Runtime.enable")
    t.send("Log.enable")
    t.send("Page.enable")
    t.send("Network.enable")
    t.send("Network.setCookie", {"name": "jtdt_locale", "value": locale, "url": base})
    t.send("Page.navigate", {"url": base + path})
    assert t.wait_for("document.querySelector('#mfListWrap .mf-empty, #mfListWrap table') "
                      "&& !document.querySelector('#mfListWrap').textContent.includes('載入中')"), \
        "清單沒有載入（JS 沒跑起來？）"


def _test_and_save(t, fport, name):
    t.js(f"""(() => {{ const s = document.getElementById('mfTarget');
             const o = document.createElement('option'); o.value = 'http:{fport}'; o.textContent = 'HTTP {fport}';
             s.append(o); s.value = 'http:{fport}';
             document.getElementById('mfHost').value = '127.0.0.1';
             document.getElementById('mfTestBtn').click(); return true; }})()""")
    assert t.wait_for("!document.getElementById('mfResultPanel').hidden"), \
        "測試結果沒有出現：" + str(t.js("document.getElementById('mfAddError').innerText"))
    body = t.js("document.getElementById('mfResult').innerText")
    assert "Fake LaserJet MFP M1" in body, "連線測試要讀到 eSCL 能力表"
    t.js(f"document.getElementById('mfSaveName').value = {name!r}; document.getElementById('mfSaveBtn').click()")
    assert t.wait_for("document.querySelectorAll('#mfListWrap tbody tr').length >= 1"), "儲存後清單沒有出現"


@pytest.mark.parametrize("locale", ["zh-Hant", "en"])
def test_scan_from_the_tool_page(live, locale):
    port, cdp, fake = live
    base = f"http://localhost:{port}"
    with _open(cdp) as ws:
        t = _Tab(ws)
        _start(t, base, "/tools/mfp-fetch/", locale)
        _test_and_save(t, fake.port, "三樓" if locale == "zh-Hant" else "3F")
        assert t.wait_for("!!document.querySelector('[data-mf-scan]')"), "支援 eSCL 的事務機要有「掃描」"
        # 清單每一列的按鈕排成一排（使用者 2026-10-14：「排列 下一版要改善」）—— 1280 寬
        t.send("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 900, "deviceScaleFactor": 1,
                                                      "mobile": False})
        time.sleep(0.3)
        assert t.js("[...document.querySelectorAll('#mfListWrap .mf-row-actions')].every(box => "
                    "new Set([...box.children].map(b => Math.round(b.getBoundingClientRect().top))).size === 1)"), \
            "按鈕折成兩排"
        t.js("document.querySelector('[data-mf-scan]').click()")
        assert t.wait_for("!document.getElementById('mfScanPanel').hidden")
        opts = t.js("['mfScanSource','mfScanColor','mfScanRes','mfScanPaper'].map(id => "
                    "[...document.getElementById(id).options].map(o => o.value))")
        assert opts[0] == ["platen"] and opts[1] == ["color", "gray"] and opts[2] == ["200", "300", "600"], opts
        assert t.js("document.getElementById('mfScanRes').value") == "300"
        assert t.js("!!document.querySelector('#mfScanPanel .jt-select-trigger')"), "掃描選項要是本站樣式的下拉"
        t.shot(f"mfp_scan_{locale}_panel_1280", 1280)
        fake.docs = [pdf_bytes(2)]
        t.js("document.getElementById('mfScanName').value = '合約'; document.getElementById('mfScanStart').click()")
        assert t.wait_for("document.getElementById('mfScanState').classList.contains('done')", 60), \
            t.js("document.getElementById('mfScanState').innerText")
        state = t.js("document.getElementById('mfScanState').innerText")
        assert "2" in state, state
        assert t.js("!!document.querySelector('#mfScanState a[href^=\"/workspace/file/\"]')"), "完成後要能打開工作區的檔案"
        # 也要能直接下載（使用者 2026-10-14：「掃描完成 應該還要有下載按鈕」）
        assert t.js("!!document.querySelector('#mfScanState a[href$=\"?dl=1\"]')"), "完成後要有下載"
        # 走什麼協定要顯示（使用者 2026-10-14）
        assert "eSCL" in state and ("HTTP " + str(fake.port)) in state, state
        # 沒填檔名：用瀏覽器這邊的時間（伺服器可能是 UTC）—— 這次有填「合約」，檔名就是它
        assert "合約.pdf" in t.js("document.getElementById('mfScansWrap').innerText")
        assert t.wait_for("document.querySelectorAll('#mfScansWrap tbody tr').length >= 1"), "最近的掃描沒有列出來"
        # 「最近的掃描與取件」的按鈕排成一排、不超出外框（使用者 2026-10-15：欄位還有很大空位，按鈕卻被擠成三行）
        for width in (1280, 1180, 900):
            t.send("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1,
                                                          "mobile": False})
            time.sleep(0.3)
            rows = t.js("[...document.querySelectorAll('#mfScansWrap tbody tr')].map(tr => { const bs = "
                        "[...tr.querySelectorAll('td:last-child .btn')]; return [bs.length, "
                        "new Set(bs.map(b => Math.round(b.getBoundingClientRect().top))).size]; })")
            assert rows and all(n >= 2 and (lines == 1 or width < 1100) for n, lines in rows), \
                f"{width} 寬：按鈕折行了 {rows}"
            assert t.js("(() => { const w = document.getElementById('mfScansWrap');"
                        " return w.querySelector('table').getBoundingClientRect().right"
                        " <= w.getBoundingClientRect().right + 1; })()"), f"{width} 寬：表格超出外框"
            # 空位不可以堆在時間那一欄：時間格比裡面的字寬不到 40 px（多的寬度給狀態）
            gap = t.js("(() => { const td = document.querySelector('#mfScansWrap tbody td');"
                       " const r = document.createRange(); r.selectNodeContents(td);"
                       " return Math.round(td.getBoundingClientRect().width - r.getBoundingClientRect().width); })()")
            assert gap < 40, f"{width} 寬：時間那一欄空了 {gap} px"
        t.shot(f"mfp_scan_{locale}_done_1280", 1280)
        t.shot(f"mfp_scan_{locale}_done_390", 390)
        # 手機寬度：事務機名稱不可以一個字一行（格子變區塊後還留著 1% 寬）
        t.send("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 900, "deviceScaleFactor": 1, "mobile": True})
        time.sleep(0.3)
        assert t.js("(() => { const c = document.querySelector('#mfScansWrap tbody td:nth-child(2) .mf-cap');"
                    " return c.getBoundingClientRect().height < parseFloat(getComputedStyle(c).fontSize) * 2; })()"), \
            "390 寬：事務機名稱折成一字一行"
        t.send("Emulation.clearDeviceMetricsOverride", {})
        # 收拾：移除這台，下一個語言從空清單開始
        t.js("[...document.querySelectorAll('#mfListWrap tbody button')].find(b => b.classList.contains('mf-del')).click()")
        assert t.wait_for("!!document.querySelector('.modal-ok')")
        t.js("document.querySelector('.modal-ok').click()")
        assert t.wait_for("!!document.querySelector('#mfListWrap .mf-empty')")
        assert not t.errs, t.errs


def test_shared_admin_page(live):
    port, cdp, fake = live
    base = f"http://localhost:{port}"
    with _open(cdp) as ws:
        t = _Tab(ws)
        _start(t, base, "/admin/mfp")
        assert "共用事務機" in t.js("document.querySelector('h1').innerText")
        assert t.js("document.querySelector('#mfSaveBtn').innerText").strip() == "儲存為共用事務機"
        _test_and_save(t, fake.port, "總公司")
        assert t.wait_for("[...document.querySelectorAll('#mfListWrap tbody button')].some(b => b.innerText.includes('成員'))")
        assert not t.js("!!document.querySelector('[data-mf-scan]')"), "管理頁不掃描"
        assert t.js("!!document.querySelector('#mfListWrap .mf-del')"), "管理頁要能移除共用事務機"
        fits = ("(() => { const w = document.getElementById('mfListWrap'); const tb = w.querySelector('table');"
                " return tb.getBoundingClientRect().right <= w.getBoundingClientRect().right + 1; })()")
        one_row = ("[...document.querySelectorAll('#mfListWrap .mf-row-actions')].every(box => "
                   "new Set([...box.children].map(b => Math.round(b.getBoundingClientRect().top))).size === 1)")
        for width in (1280, 900):
            t.send("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1,
                                                          "mobile": False})
            time.sleep(0.3)
            assert t.js(fits), f"{width} 寬：表格超出外框（最右邊的按鈕被擠出去）"
            if width >= 1280:
                assert t.js(one_row), "1280 寬：按鈕要排成一排"
        t.send("Emulation.clearDeviceMetricsOverride", {})
        assert t.js("[...document.querySelectorAll('#mfListWrap th')].every(th => th.getBoundingClientRect().height < 44)"), \
            "表頭不可以被擠成直排"
        t.js("[...document.querySelectorAll('#mfListWrap tbody button')].find(b => b.innerText.includes('成員')).click()")
        assert t.wait_for("!document.getElementById('mfMembersPanel').hidden"), "成員面板沒有打開"
        t.js("document.getElementById('mfMembersSave').click()")
        assert t.wait_for("document.getElementById('mfMembersMsg').innerText.includes('0')"), "成員存不了"
        t.shot("mfp_shared_admin_1280", 1280)
        # 共用的這台出現在工具頁，標「共用」，沒有改名與移除
        t.send("Page.navigate", {"url": base + "/tools/mfp-fetch/"})
        assert t.wait_for("!!document.querySelector('#mfListWrap .mf-tag.shared')"), "工具頁要列出共用事務機"
        assert not t.js("!!document.querySelector('#mfListWrap .mf-del')"), "共用事務機在工具頁不能移除"
        assert not t.errs, t.errs
