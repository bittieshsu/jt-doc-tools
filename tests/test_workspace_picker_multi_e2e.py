"""「從工作區載入」一次選多個檔 —— 在真的瀏覽器裡勾、真的載入。

2026-10-28 客戶建議：掃描拼合一次要正反面兩個檔，一個一個點太慢。
收多個檔的上傳框（`<input multiple>`）現在可以在挑選視窗裡勾好幾個，按「載入選取的檔案」
一次載入；**勾的順序就是載入的順序**（合併時順序有意義）。單檔的上傳框維持點一下就載入。

三種接法各驗一次：
* 掃描拼合：工具自己的上傳框（`attachWorkspaceLoadButton`）
* 檔案合併：共用上傳元件（`FileUpload`），收多個檔
* PDF 壓縮：共用上傳元件，單檔 —— 不可以出現多選
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
sys.path.insert(0, ROOT)
from tools.browser_probe import browser as _browser  # noqa: E402
from tools.browser_probe import profile_arg as _profile_arg  # noqa: E402

pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")

NAMES = ["證件正面.pdf", "證件反面.pdf"]

_SEED = r"""
import fitz
from app.core import workspace as ws
ws.save_settings({"enabled": True, "per_user_quota_mb": 500, "max_file_mb": 50,
                  "max_audio_mb": 500, "retention_hours": -1})
for i, name in enumerate(%r):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 80 + i * 400
    page.draw_rect(fitz.Rect(60, y, 320, y + 160), color=None, fill=(0.2, 0.4, 0.8))
    data = doc.tobytes(); doc.close()
    ws.save_bytes_for_key(ws.key_for_user_id(None), data, name, "手動上傳")
    import time; time.sleep(0.05)
""" % (NAMES,)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def page():
    data = tempfile.mkdtemp(prefix="wsmulti-")
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    subprocess.run([sys.executable, "-c", _SEED], cwd=ROOT, env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [_browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
         _profile_arg(), f"--remote-debugging-port={cdp}", "--remote-allow-origins=*",
         "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws = None
    try:
        for _ in range(160):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.skip("實例或瀏覽器起不來")

        import websockets.sync.client as wsc
        req = urllib.request.Request(
            f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            tab = json.loads(r.read())
        ws = wsc.connect(tab["webSocketDebuggerUrl"], max_size=None, open_timeout=10)
        n = [0]

        def send(method, params=None):
            n[0] += 1
            i = n[0]
            ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
            while True:
                m = json.loads(ws.recv(timeout=180))
                if m.get("id") == i:
                    return m

        def ev(expr):
            r = send("Runtime.evaluate",
                     {"expression": expr, "returnByValue": True, "awaitPromise": True})
            res = r.get("result", {})
            if "exceptionDetails" in res:
                exc = res["exceptionDetails"].get("exception", {}).get("description")
                raise AssertionError(f"頁面丟例外：{str(exc)[:300]}")
            return res.get("result", {}).get("value")

        def wait(expr, secs=30):
            for _ in range(secs * 4):
                v = ev(expr)
                if v:
                    return v
                time.sleep(0.25)
            return ev(expr)

        def goto(path):
            send("Page.navigate", {"url": f"http://127.0.0.1:{port}{path}"})
            time.sleep(1)
            assert wait("document.readyState === 'complete'"), f"{path} 沒載完"
            time.sleep(0.5)

        send("Runtime.enable")
        send("Page.enable")
        yield goto, ev, wait
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        br.terminate(); srv.terminate()
        for p in (br, srv):
            try:
                p.wait(timeout=6)
            except Exception:
                p.kill()
        shutil.rmtree(data, ignore_errors=True)


_OPEN_AND_PICK_BOTH = """
(async () => {
  const cards = () => Array.from(document.querySelectorAll('#ws-picker-modal .ws-pick-card'));
  const byName = (n) => cards().find(c => c.querySelector('.ws-pick-name').textContent === n);
  const foot = document.querySelector('#ws-picker-modal .ws-picker-foot');
  const ok = document.querySelector('#ws-picker-modal .ws-picker-ok');
  const before = {footShown: !!foot && !foot.hidden && foot.offsetHeight > 0, okDisabled: ok.disabled};
  // 先勾反面、再勾正面 —— 載入順序要照勾的順序
  byName(%r).click(); byName(%r).click();
  const label = ok.textContent;
  const checked = cards().filter(c => c.getAttribute('aria-checked') === 'true').length;
  ok.click();
  return {...before, label, checked, closed: document.getElementById('ws-picker-modal').hidden};
})()
""" % (NAMES[1], NAMES[0])

_PICKER_READY = ("document.querySelectorAll('#ws-picker-modal .ws-pick-card').length >= 2"
                 " && !document.getElementById('ws-picker-modal').hidden")


def test_scan_merge_loads_two_workspace_files_at_once(page):
    goto, ev, wait = page
    goto("/tools/scan-merge/")
    ev("document.getElementById('wsLoadBtn').click()")
    assert wait(_PICKER_READY), "挑選視窗沒有列出工作區的檔案"
    r = ev(_OPEN_AND_PICK_BOTH)
    assert r["footShown"], "收多個檔的上傳框沒有出現多選的按鈕列"
    assert r["okDisabled"], "還沒勾任何檔案時「載入」不可以按"
    assert r["checked"] == 2 and "2" in r["label"], r
    assert r["closed"]
    got = wait("crops.length === 2 && crops.map(c => c.filename)", 90)
    assert got == [NAMES[1], NAMES[0]], f"兩份都要載入、順序照勾的順序：{got}"


def test_pdf_merge_loads_two_workspace_files_at_once(page):
    goto, ev, wait = page
    goto("/tools/pdf-merge/")
    ev("document.querySelector('.ws-load-btn').click()")
    assert wait(_PICKER_READY), "挑選視窗沒有列出工作區的檔案"
    r = ev(_OPEN_AND_PICK_BOTH)
    assert r["footShown"] and r["checked"] == 2, r
    got = wait("files.length === 2 && files.map(f => f.name)", 30)
    assert got == [NAMES[1], NAMES[0]], got


def test_single_file_upload_still_loads_on_one_click(page):
    goto, ev, wait = page
    goto("/tools/pdf-compress/")
    ev("document.querySelector('.ws-load-btn').click()")
    assert wait(_PICKER_READY), "挑選視窗沒有列出工作區的檔案"
    r = ev("""(() => {
      const foot = document.querySelector('#ws-picker-modal .ws-picker-foot');
      const card = Array.from(document.querySelectorAll('#ws-picker-modal .ws-pick-card'))
        .find(c => c.querySelector('.ws-pick-name').textContent === %r);
      const hasCheck = !!card.querySelector('.ws-pick-check');
      card.click();
      return {footHidden: foot.hidden, hasCheck,
              closed: document.getElementById('ws-picker-modal').hidden};
    })()""" % NAMES[0])
    assert r["footHidden"] and not r["hasCheck"], f"單檔的上傳框不可以出現多選：{r}"
    assert r["closed"], "單檔的上傳框點一下就要載入"
    got = wait("(document.querySelector('.drop-zone-filename')||{}).textContent", 20)
    assert got == NAMES[0], got
