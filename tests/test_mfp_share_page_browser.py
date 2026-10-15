"""檔案分享取件 —— 在真的瀏覽器裡跑一次（對本機一台假的 Canon Advanced Box，WebDAV）。

* Canon 的事務機清單上有「取件」；面板一開始只有帳號、密碼、「列出檔案」（只有一種連線方式時不出現選單）。
* 只用 HTTP：問一次同意，按「繼續」自動再試 → 列出共享空間，標「共享」。
* 點資料夾進去、麵包屑回上一層；勾檔案 →「取件（1 份）」→ 完成（TIFF 轉成 PDF）、顯示 WebDAV。
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
from tests.test_mfp_share import FakeCanon

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
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port={port}, log_level="warning")
"""

HOME = b"<html><head><title>Canon iR-ADV DX C5840 / Remote UI</title></head><body>Remote UI</body></html>"


@pytest.fixture(scope="module")
def live():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    fake = FakeCanon()
    fake.home = HOME
    srv_fake = fake.serve()
    fport = srv_fake.server_address[1]
    fake.port = fport
    data = tempfile.mkdtemp(prefix="mfpshare-")
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


def test_share_retrieval_flow(live):
    port, cdp, fake = live
    base = f"http://localhost:{port}"
    with _open(cdp) as ws:
        t = _Tab(ws)
        for d in ("Runtime", "Log", "Page", "Network"):
            t.send(f"{d}.enable")
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
        assert t.wait_for("!!document.querySelector('[data-mf-share]')"), "Canon 的事務機要有「取件」"
        t.js("document.querySelector('[data-mf-share]').click()")
        assert t.wait_for("!document.getElementById('mfSharePanel').hidden")
        # Canon 有 WebDAV 與 SMB 兩種 —— 預設 WebDAV
        assert t.js("document.getElementById('mfShareRoute').value") == "canon_webdav"
        t.js("document.getElementById('mfShareListBtn').click()")
        assert t.wait_for("!document.getElementById('mfShareConfirm').hidden"), "HTTP 要先問同意"
        t.js("document.querySelector('#mfShareConfirm .btn-primary').click()")
        assert t.wait_for("!document.getElementById('mfShareFiles').hidden"), \
            t.js("document.getElementById('mfShareError').innerText")
        assert not t.js("document.getElementById('mfShareSharedNote').hidden"), "共享空間要標出來"
        assert "報價.pdf" in t.js("document.getElementById('mfShareTable').innerText")
        t.js("document.querySelector('#mfShareTable tr[data-dir]').click()")
        assert t.wait_for("document.getElementById('mfShareTable').innerText.includes('合約.tif')")
        assert "掃描" in t.js("document.getElementById('mfShareCrumbs').innerText")
        t.js("document.querySelector('#mfShareTable input[data-file]').click()")
        assert "1" in t.js("document.getElementById('mfShareFetchLabel').innerText")
        t.shot("mfp_share_list_1280", 1280)
        t.js("document.getElementById('mfShareFetchBtn').click()")
        assert t.wait_for("document.getElementById('mfShareState').classList.contains('done')", 60), \
            t.js("document.getElementById('mfShareState').innerText")
        state = t.js("document.getElementById('mfShareState').innerText")
        assert "WebDAV" in state and "合約" in state, state
        assert t.js("!!document.querySelector('#mfShareState a[href^=\"/workspace/file/\"]')")
        t.shot("mfp_share_done_1280", 1280)
        t.shot("mfp_share_done_390", 390)
        assert not [e for e in t.errs if "422" not in e], t.errs
