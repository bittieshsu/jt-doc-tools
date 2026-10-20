"""KYOCERA / RICOH 直接取件 —— 在真的瀏覽器裡跑一次（對本機的假 KYOCERA、假 RICOH，v1.16.95）。

* KYOCERA：連線方式有 HTTPS 9091 / HTTP 9090 兩種；面板多一欄「文件匣密碼」、說明換成「依原廠工具的分析寫成」。
  列出文件匣（標號＋名稱、共享的標出來）→ 選有密碼的文件匣先看到錯誤、填密碼再列 → 取件完成、顯示 SOAP。
* RICOH：位置顯示「文件伺服器」、有密碼的文件標「有密碼」、欄位叫「文件密碼」；文件名稱顯示設備上的名稱。
* 整個過程沒有 JavaScript 錯誤（設備端的 422 是預期的）。
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
from tests.test_mfp_soap import FakeKyocera, FakeRicoh

pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")

_LAUNCH = """
import sys
sys.path.insert(0, {root!r})
import app.core.mfp_client as m
_real = m.check_address
m.check_address = lambda ip: None if ip in ("127.0.0.1", "127.0.0.2") else _real(ip)
m.ALLOWED_PORTS = tuple(m.ALLOWED_PORTS) + ({kyo}, {ricoh})
import app.core.mfp_soap as s
s.KYO_PORTS = {{"https": {kyo}, "http": {kyo}}}
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port={port}, log_level="warning")
"""


def _with_home(cls, html: bytes):
    class WithHome(cls):
        def handle_get(self, path, headers):
            if path in ("/", "/index.html"):
                return 200, html, "text/html; charset=utf-8"
            return super().handle_get(path, headers)
    return WithHome


@pytest.fixture(scope="module")
def live():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    kyo = _with_home(FakeKyocera, b"<html><head><title>Command Center RX</title></head>"
                                  b"<body>KYOCERA TASKalfa 3554ci</body></html>")().start()
    # 第二台要另一個位址（同一個位址第二次會被當成重複）。不用 localhost：有的機器上它也解析到 ::1，
    # 連線測試會把每一個解析出來的位址都檢查一遍（GitHub CI 上這支因此紅過）
    ricoh = _with_home(FakeRicoh, b"<html><head><title>Web Image Monitor</title></head>"
                                  b"<body>RICOH IM C3010</body></html>")()
    ricoh.auth_url_host = ricoh.url_host_override = "127.0.0.2"
    ricoh.start("127.0.0.2")
    data = tempfile.mkdtemp(prefix="mfpsoap-")
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen([sys.executable, "-c", _LAUNCH.format(root=str(root), kyo=kyo.port, ricoh=ricoh.port,
                                                                 port=port)],
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
        yield port, cdp, kyo, ricoh
    finally:
        br.terminate()
        srv.terminate()
        kyo.stop()
        ricoh.stop()
        try:
            br.wait(timeout=5)
            srv.wait(timeout=5)
        except Exception:
            br.kill()
            srv.kill()
        shutil.rmtree(data, ignore_errors=True)


def _add_device(t, fport, host="127.0.0.1"):
    """連線測試 → 儲存 → 開這台的「取件」面板（清單上最新那一台在最前面或最後面都找得到）。"""
    before = t.js("document.querySelectorAll('[data-mf-share]').length")
    t.js(f"""(() => {{ const s = document.getElementById('mfTarget');
             const o = document.createElement('option'); o.value = 'http:{fport}'; o.textContent = 'x';
             s.append(o); s.value = 'http:{fport}';
             document.getElementById('mfHost').value = '{host}';
             document.getElementById('mfTestBtn').click(); return true; }})()""")
    # 結果面板在第二台時本來就開著 —— 要等「儲存」那一列（這次的結果）出來
    assert t.wait_for("!document.getElementById('mfResultPanel').hidden && !document.getElementById('mfSaveBar').hidden"), \
        ("連線測試沒有可儲存的結果", t.js("document.getElementById('mfResultPanel').innerText.slice(0, 800)"))
    t.js("document.getElementById('mfSaveBtn').click()")
    assert t.wait_for(f"document.querySelectorAll('[data-mf-share]').length > {before}"), \
        ("要有「取件」", t.js("document.getElementById('mfSaveMsg').innerText"))
    t.js("""(() => { const ids = [...document.querySelectorAll('[data-mf-share]')].map(b => +b.dataset.mfShare);
                     const max = Math.max(...ids);
                     document.querySelector('[data-mf-share=\"' + max + '\"]').click(); return true; })()""")
    assert t.wait_for("!document.getElementById('mfSharePanel').hidden"), "取件面板沒有打開"


def _list_with_consent(t):
    t.js("document.getElementById('mfShareListBtn').click()")
    assert t.wait_for("!document.getElementById('mfShareConfirm').hidden "
                      "|| !document.getElementById('mfShareFiles').hidden"), "HTTP 要先問同意"
    if not t.js("document.getElementById('mfShareConfirm').hidden"):
        t.js("document.querySelector('#mfShareConfirm .btn-primary').click()")
    assert t.wait_for("!document.getElementById('mfShareFiles').hidden"), \
        t.js("document.getElementById('mfShareError').innerText")


def test_kyocera_and_ricoh_retrieval_flow(live):
    port, cdp, kyo, ricoh = live
    base = f"http://localhost:{port}"
    with _open(cdp) as ws:
        t = _Tab(ws)
        for d in ("Runtime", "Log", "Page", "Network"):
            t.send(f"{d}.enable")
        t.send("Network.setCookie", {"name": "jtdt_locale", "value": "zh-Hant", "url": base})
        t.send("Page.navigate", {"url": base + "/tools/mfp-fetch/"})
        assert t.wait_for("document.querySelector('#mfListWrap .mf-empty, #mfListWrap table') "
                          "&& !document.querySelector('#mfListWrap').textContent.includes('載入中')"), "清單沒有載入"

        # ---- KYOCERA
        _add_device(t, kyo.port)
        assert t.js("[...document.getElementById('mfShareRoute').options].map(o => o.value).join(',')") \
            == "kyocera_soap,kyocera_soap_http"
        assert not t.js("document.getElementById('mfShareRouteWrap').hidden"), "兩種連線方式要給選"
        assert not t.js("document.getElementById('mfShareExtraWrap').hidden"), "KYOCERA 要有文件匣密碼欄"
        assert "文件匣密碼" in t.js("document.getElementById('mfShareExtraLabel').innerText")
        assert t.js("document.getElementById('mfShareNoteManual').hidden") \
            and not t.js("document.getElementById('mfShareNoteDirect').hidden"), "說明要換成「依原廠工具的分析」"
        t.js("""(() => { const s = document.getElementById('mfShareRoute'); s.value = 'kyocera_soap_http';
                         s.dispatchEvent(new Event('change')); return true; })()""")
        t.js("document.getElementById('mfShareUser').value = 'clerk'; document.getElementById('mfSharePw').value = 'pw-1'")
        _list_with_consent(t)
        roots = t.js("[...document.getElementById('mfShareRoot').options].map(o => o.textContent).join('|')")
        assert "1 會計" in roots and "共享" in roots.split("|")[0] and "2 個人" in roots, roots
        table = t.js("document.getElementById('mfShareTable').innerText")
        assert "合約" in table and "報價 / 2026" in table and "~" not in table, "顯示設備上的名稱，不是編碼過的識別碼"
        assert "1 會計" in t.js("document.getElementById('mfShareCrumbs').innerText")
        # 有密碼的文件匣：先看到錯誤，填了文件匣密碼再列
        t.js("""(() => { const s = document.getElementById('mfShareRoot'); s.value = '2';
                         s.dispatchEvent(new Event('change')); return true; })()""")
        assert t.wait_for("!document.getElementById('mfShareError').hidden"), "有密碼的文件匣應該先看到錯誤"
        assert "文件匣密碼" in t.js("document.getElementById('mfShareError').innerText")
        t.js("document.getElementById('mfShareExtra').value = '1234'")
        t.js("""(() => { const s = document.getElementById('mfShareRoot'); s.value = '2';
                         s.dispatchEvent(new Event('change')); return true; })()""")
        assert t.wait_for("document.getElementById('mfShareTable').innerText.includes('私人')"), \
            t.js("document.getElementById('mfShareError').innerText")
        t.shot("mfp_soap_kyocera_1280", 1280)
        t.js("document.querySelector('#mfShareTable input[data-file]').click()")
        t.js("document.getElementById('mfShareFetchBtn').click()")
        assert t.wait_for("document.getElementById('mfShareState').classList.contains('done')", 60), \
            t.js("document.getElementById('mfShareState').innerText")
        state = t.js("document.getElementById('mfShareState').innerText")
        assert "SOAP" in state and "私人" in state, state
        t.js("document.getElementById('mfShareClose').click()")
        assert t.js("document.getElementById('mfShareExtra').value") == "", "收合時清掉密碼"

        # ---- RICOH
        _add_device(t, ricoh.port, host="127.0.0.2")
        assert t.js("document.getElementById('mfShareRouteWrap').hidden"), "只有一種連線方式不出現選單"
        assert "文件密碼" in t.js("document.getElementById('mfShareExtraLabel').innerText")
        t.js("document.getElementById('mfShareUser').value = 'clerk'; document.getElementById('mfSharePw').value = 'pw-2'")
        _list_with_consent(t)
        assert t.js("document.getElementById('mfShareRootWrap').hidden"), "只有一個位置不出現選單"
        assert "文件伺服器" in t.js("document.getElementById('mfShareCrumbs').innerText")
        table = t.js("document.getElementById('mfShareTable').innerText")
        assert "會議紀錄" in table and "有密碼" in table and "doc-1" not in table, table
        t.shot("mfp_soap_ricoh_1280", 1280)
        t.shot("mfp_soap_ricoh_390", 390)
        assert not [e for e in t.errs if "422" not in e], t.errs


def test_ricoh_sign_in_method_choice(live):
    """v1.17.0：客戶 RICOH 只有非票證登入。v1.17.3（客戶：「自動判斷不夠自動」）：自動判斷、帳號空白時直接用
    「不需登入」列出（不送帳密）並照實註明；選「不需登入」後帳號、密碼收起來，列得出文件伺服器「共用資料夾」裡的文件；
    User Code 有自己的欄位。"""
    from tests.test_mfp_soap import _CUSTOMER_CLASSES
    port, cdp, _kyo, ricoh = live
    ricoh.classes, ricoh.docs_in_folder = _CUSTOMER_CLASSES, True
    ricoh.calls.clear()
    base = f"http://localhost:{port}"
    with _open(cdp) as ws:
        t = _Tab(ws)
        for d in ("Runtime", "Log", "Page", "Network"):
            t.send(f"{d}.enable")
        t.send("Network.setCookie", {"name": "jtdt_locale", "value": "zh-Hant", "url": base})
        t.send("Page.navigate", {"url": base + "/tools/mfp-fetch/"})
        assert t.wait_for("document.querySelector('#mfListWrap .mf-empty, #mfListWrap table') "
                          "&& !document.querySelector('#mfListWrap').textContent.includes('載入中')"), "清單沒有載入"
        if t.js("document.querySelectorAll('[data-mf-share]').length") < 2:     # 單獨跑這一支：自己加設備
            if not t.js("document.querySelectorAll('[data-mf-share]').length"):
                _add_device(t, _kyo.port)
            _add_device(t, ricoh.port, host="127.0.0.2")
        else:
            t.js("""(() => { const ids = [...document.querySelectorAll('[data-mf-share]')].map(b => +b.dataset.mfShare);
                             document.querySelector('[data-mf-share=\"' + Math.max(...ids) + '\"]').click(); return true; })()""")
        assert t.wait_for("!document.getElementById('mfSharePanel').hidden")
        assert not t.js("document.getElementById('mfShareModeWrap').hidden"), "RICOH 要有「登入方式」"
        assert not t.js("document.getElementById('mfShareModeHint').hidden")
        t.js("""(() => { const s = document.getElementById('mfShareMode'); s.value = 'auto';
                         s.dispatchEvent(new Event('change'));
                         document.getElementById('mfShareUser').value = ''; return true; })()""")
        _list_with_consent(t)
        assert t.wait_for("document.getElementById('mfShareTable').innerText.includes('合約')"), \
            t.js("document.getElementById('mfShareError').innerText")
        assert "不需登入" in t.js("document.getElementById('mfShareAutoNote').innerText"), "要照實說自動用了不需登入"
        auto_starts = [c for c in ricoh.calls if c["method"] == "startSession"]
        assert len(auto_starts) == 1 and auto_starts[0]["params"]["stringIn"] == "", "不送任何帳密、只送一次"
        ricoh.calls.clear()
        t.js("""(() => { const s = document.getElementById('mfShareMode'); s.value = 'none';
                         s.dispatchEvent(new Event('change')); return true; })()""")
        assert t.js("document.getElementById('mfShareUserWrap').hidden") \
            and t.js("document.getElementById('mfSharePwWrap').hidden") \
            and t.js("document.getElementById('mfShareCodeWrap').hidden"), "不需登入：帳號、密碼、User Code 都收起來"
        _list_with_consent(t)
        assert t.wait_for("document.getElementById('mfShareTable').innerText.includes('合約')"), \
            t.js("document.getElementById('mfShareError').innerText")
        starts = [c for c in ricoh.calls if c["method"] == "startSession"]
        assert len(starts) == 1 and starts[0]["params"]["stringIn"] == ""
        t.shot("mfp_soap_ricoh_none_1280", 1280)
        t.shot("mfp_soap_ricoh_none_390", 390)
        t.js("""(() => { const s = document.getElementById('mfShareMode'); s.value = 'user_code';
                         s.dispatchEvent(new Event('change')); return true; })()""")
        assert not t.js("document.getElementById('mfShareCodeWrap').hidden") \
            and t.js("document.getElementById('mfShareUserWrap').hidden"), "User Code 有自己的欄位"
        assert t.js("document.documentElement.scrollWidth <= 1280")
        assert not [e for e in t.errs if "422" not in e], t.errs
