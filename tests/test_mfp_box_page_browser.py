"""機內信箱取件 —— 在真的瀏覽器裡跑一次（對本機一台假的 FUJIFILM 信箱）。

使用者 2026-10-14：「取件功能請儘可能在使用流程給 user 越簡單越易懂越好」。驗的是那個流程：

* 清單上 FUJIFILM 的事務機有「信箱取件」；面板一開始只有信箱編號、信箱密碼、「列出文件」（帳號欄位藏著）。
* 只用 HTTP 的事務機：第一次問一次同意（按「繼續」自動再試），之後這台瀏覽器不再問。
* 信箱有密碼沒填 → 說要密碼；填了 → 列出文件。
* 勾文件 → 按鈕變「取件（1 份）」→ 完成：存進工作區（預設 PDF）、看得到協定 WebDAV、每份文件的結果。
* 收合面板時清掉密碼欄。整個過程沒有 JavaScript 錯誤。

假設備在 127.0.0.1 —— 實例用這支測試裡的啟動程式碼放行 127.0.0.1 與那個埠。未啟用登入、從 localhost 連。
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

from tests.test_mfp_box import FakeBox, tiff_bytes
from tests.test_mfp_fetch_page_browser import _Tab, _browser, _free_port, _open, _profile_arg  # noqa: F401

pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")

_LAUNCH = """
import sys
sys.path.insert(0, {root!r})
import app.core.mfp_client as m
_real = m.check_address
m.check_address = lambda ip: None if ip == "127.0.0.1" else _real(ip)
m.ALLOWED_PORTS = tuple(m.ALLOWED_PORTS) + ({fake}, {no_dav})
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port={port}, log_level="warning")
"""

HOME = ("<html><head><title>Apeos C5571 - FUJIFILM Business Innovation</title></head>"
        "<body>Internet Services</body></html>").encode()


@pytest.fixture(scope="module")
def live():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    fake = FakeBox()
    fake.home = HOME
    srv_fake = fake.serve()
    fport = srv_fake.server_address[1]
    fake.port = fport
    # 第二組連線方式：網頁伺服器在、但不接 WebDAV（PROPFIND 一律 406）—— 客戶實機 Apeos C5571 的 HTTPS 443 就是這樣
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class NoDav(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_PROPFIND(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            self.send_response(406)
            self.send_header("Content-Length", "0")
            self.end_headers()
    no_dav = ThreadingHTTPServer(("127.0.0.1", 0), NoDav)
    threading.Thread(target=no_dav.serve_forever, daemon=True).start()
    fake.no_dav_port = no_dav.server_address[1]
    data = tempfile.mkdtemp(prefix="mfpbox-")
    fake.data_dir = data
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen([sys.executable, "-c", _LAUNCH.format(root=str(root), fake=fport, no_dav=fake.no_dav_port, port=port)],
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
        no_dav.shutdown()
        try:
            br.wait(timeout=5)
            srv.wait(timeout=5)
        except Exception:
            br.kill()
            srv.kill()
        shutil.rmtree(data, ignore_errors=True)


def test_mailbox_retrieval_flow(live):
    port, cdp, fake = live
    fake.realm = "box-1"
    fake.add_whole(5, data=tiff_bytes(3), name="報價單", Pages="3")
    base = f"http://localhost:{port}"
    with _open(cdp) as ws:
        t = _Tab(ws)
        t.send("Runtime.enable")
        t.send("Log.enable")
        t.send("Page.enable")
        t.send("Network.enable")
        t.send("Network.setCookie", {"name": "jtdt_locale", "value": "zh-Hant", "url": base})
        t.send("Page.navigate", {"url": base + "/tools/mfp-fetch/"})
        assert t.wait_for("document.querySelector('#mfListWrap .mf-empty, #mfListWrap table') "
                          "&& !document.querySelector('#mfListWrap').textContent.includes('載入中')")
        t.js(f"""(() => {{ const s = document.getElementById('mfTarget');
                 const o = document.createElement('option'); o.value = 'http:{fake.port}'; o.textContent = 'x';
                 s.append(o); s.value = 'http:{fake.port}';
                 document.getElementById('mfHost').value = '127.0.0.1';
                 document.getElementById('mfTestBtn').click(); return true; }})()""")
        assert t.wait_for("!document.getElementById('mfResultPanel').hidden")
        t.js("document.getElementById('mfSaveBtn').click()")
        assert t.wait_for("!!document.querySelector('[data-mf-box]')"), "FUJIFILM 的事務機要有「信箱取件」"
        t.js("document.querySelector('[data-mf-box]').click()")
        assert t.wait_for("!document.getElementById('mfBoxPanel').hidden")
        assert t.js("document.getElementById('mfBoxLogin').hidden"), "一開始不問事務機帳號"
        # 只用 HTTP：問一次同意
        t.js("document.getElementById('mfBoxNo').value = '1'; document.getElementById('mfBoxListBtn').click()")
        assert t.wait_for("!document.getElementById('mfBoxConfirm').hidden"), "HTTP 要先問同意"
        assert "HTTP" in t.js("document.getElementById('mfBoxConfirm').innerText")
        t.js("document.querySelector('#mfBoxConfirm .btn-primary').click()")
        # 同意後自動再試 → 這次說要信箱密碼
        assert t.wait_for("!document.getElementById('mfBoxError').hidden "
                          "&& document.getElementById('mfBoxError').innerText.includes('信箱密碼')"), \
            t.js("document.getElementById('mfBoxError').innerText")
        assert not fake_auth(fake), "沒填密碼就不可以送帳密"
        t.js("document.getElementById('mfBoxPw').value = '1234'; document.getElementById('mfBoxListBtn').click()")
        assert t.wait_for("!document.getElementById('mfBoxDocs').hidden"), t.js("document.getElementById('mfBoxError').innerText")
        assert "報價單" in t.js("document.getElementById('mfBoxTable').innerText")
        assert t.js("document.getElementById('mfBoxFetchBtn').disabled"), "沒勾文件不能按取件"
        t.js("document.querySelector('#mfBoxTable input[data-doc]').click()")
        assert "1" in t.js("document.getElementById('mfBoxFetchLabel').innerText")
        assert t.js("!!document.querySelector('#mfBoxPanel .jt-select-trigger')"), "存成要是本站樣式的下拉"
        t.shot("mfp_box_list_1280", 1280)
        t.js("document.getElementById('mfBoxFetchBtn').click()")
        assert t.wait_for("document.getElementById('mfBoxState').classList.contains('done')", 60), \
            t.js("document.getElementById('mfBoxState').innerText")
        state = t.js("document.getElementById('mfBoxState').innerText")
        assert "WebDAV" in state and "3" in state and "報價單" in state, state
        assert t.js("!!document.querySelector('#mfBoxState a[href^=\"/workspace/file/\"]')"), "完成後要能打開工作區的檔案"
        t.shot("mfp_box_done_1280", 1280)
        t.shot("mfp_box_done_390", 390)
        # 同一台再開一次：不再問 HTTP 同意、信箱編號記得
        t.js("document.getElementById('mfBoxClose').click()")
        assert t.js("document.getElementById('mfBoxPw').value") == "", "收合要清掉密碼"
        t.js("document.querySelector('[data-mf-box]').click()")
        assert t.js("document.getElementById('mfBoxNo').value") == "1"
        assert t.js("document.getElementById('mfBoxTargetWrap').hidden"), "只有一組連線方式時不出現選單"

        # ---- 連線方式（v1.16.98，客戶實機：HTTPS 443 對 WebDAV 回 406，原廠文件寫 WebDAV 預設在 80）
        import json as _json
        import sqlite3
        db = sqlite3.connect(os.path.join(fake.data_dir, "mfp.sqlite"))
        rid, rep_s = db.execute("select id, last_report from mfp_connections").fetchone()
        rep = _json.loads(rep_s)
        rep["summary"]["reachable_on"] = [f"http:{fake.no_dav_port}", f"http:{fake.port}"]
        db.execute("update mfp_connections set last_report=? where id=?", (_json.dumps(rep), rid))
        db.commit()
        db.close()
        t.send("Page.navigate", {"url": base + "/tools/mfp-fetch/"})
        assert t.wait_for("!!document.querySelector('[data-mf-box]')"), "重新載入清單"
        t.js("document.querySelector('[data-mf-box]').click()")
        assert t.wait_for("!document.getElementById('mfBoxPanel').hidden")
        assert not t.js("document.getElementById('mfBoxTargetWrap').hidden"), "兩組連線方式要給選"
        opts = t.js("[...document.getElementById('mfBoxTarget').options].map(o => o.textContent).join('|')")
        assert opts == f"HTTP {fake.no_dav_port}|HTTP {fake.port}", opts
        assert t.js("document.getElementById('mfBoxTarget').value") == f"http:{fake.port}", "預設沿用上次成功的那組"
        t.js(f"""(() => {{ const s = document.getElementById('mfBoxTarget'); s.value = 'http:{fake.no_dav_port}';
                 s.dispatchEvent(new Event('change'));
                 document.getElementById('mfBoxPw').value = '1234';
                 document.getElementById('mfBoxListBtn').click(); return true; }})()""")
        assert t.wait_for("!document.getElementById('mfBoxError').hidden"), "不接 WebDAV 的那組要看到錯誤"
        err = t.js("document.getElementById('mfBoxError').innerText")
        assert "WebDAV" in err and f"HTTP {fake.port}" in err, err
        t.js(f"""(() => {{ const s = document.getElementById('mfBoxTarget'); s.value = 'http:{fake.port}';
                 s.dispatchEvent(new Event('change')); document.getElementById('mfBoxListBtn').click(); return true; }})()""")
        assert t.wait_for("!document.getElementById('mfBoxDocs').hidden"), t.js("document.getElementById('mfBoxError').innerText")
        t.shot("mfp_box_target_1280", 1280)
        # 設備端的錯誤（要同意 HTTP、要信箱密碼）是 422 —— 瀏覽器主控台一定會記一行「載入失敗」，那是預期的
        assert not [e for e in t.errs if "422" not in e], t.errs


def fake_auth(fake):
    return [r for r in fake.requests if r["headers"].get("authorization")]
