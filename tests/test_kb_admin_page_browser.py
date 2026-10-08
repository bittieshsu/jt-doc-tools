"""知識庫管理頁在**真的瀏覽器**裡開一次：主控台不可以有錯誤，而且按鈕真的接上了
（列出資料集、打開文件清單、檢索測試查得到結果）。

只看「頁面回 200」不夠 —— 這個專案踩過很多次「畫面看起來正常、JS 一行都沒跑」
（見 `test_pages_boot_in_a_browser.py` 的說明）。這一頁的內容幾乎全是 JS 畫的，
JS 停住的話畫面上只剩標題。
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

pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")

HANDBOOK = ("壹、總述\n一、本手冊所稱文書，指處理公務之一切資料。\n貳、公文製作\n"
            "十八、公文用語：下級對上級稱「鈞」；上級對下級稱「貴」。\n")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _req(port: int, method: str, path: str, body=None, files=None):
    url = f"http://127.0.0.1:{port}{path}"
    headers = {}
    data = None
    if files is not None:
        boundary = "kbtestboundary"
        parts = []
        for name, (fname, content, ctype) in files:
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                         f"filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n".encode() + content + b"\r\n")
        data = b"".join(parts) + f"--{boundary}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


@pytest.fixture(scope="module")
def live():
    data = tempfile.mkdtemp(prefix="kbpage-")
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [_browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
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
        # 種一個資料集與一份啟用中的文件（認證關閉的拋棄式實例）
        ds = _req(port, "POST", "/admin/knowledge/api/datasets",
                  {"name": "瀏覽器測試手冊", "category": "writing_rules"})
        _req(port, "POST", f"/admin/knowledge/api/datasets/{ds['id']}/upload",
             files=[("files", ("h.txt", HANDBOOK.encode("utf-8"), "text/plain"))])
        for _ in range(100):
            vs = _req(port, "GET", f"/admin/knowledge/api/datasets/{ds['id']}/versions")["versions"]
            if vs and vs[0]["status"] == "ready":
                break
            time.sleep(0.2)
        _req(port, "POST", f"/admin/knowledge/api/versions/{vs[0]['id']}/activate", {})
        yield port, cdp
    finally:
        br.terminate()
        srv.terminate()
        try:
            br.wait(timeout=5)
            srv.wait(timeout=5)
        except Exception:
            br.kill()
            srv.kill()
        shutil.rmtree(data, ignore_errors=True)


def test_knowledge_page_boots_and_its_buttons_work(live):
    import websockets.sync.client as wsc
    port, cdp = live
    req = urllib.request.Request(f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        tab = json.loads(r.read())
    errs: list[str] = []
    try:
        with wsc.connect(tab["webSocketDebuggerUrl"], max_size=None, open_timeout=10) as ws:
            n = [0]

            def collect(m):
                if m.get("method") == "Runtime.exceptionThrown":
                    d = m["params"]["exceptionDetails"]
                    errs.append("例外：" + (d.get("exception", {}).get("description")
                                          or d.get("text", "?"))[:200])
                elif m.get("method") == "Log.entryAdded" and m["params"]["entry"].get("level") == "error":
                    errs.append("主控台：" + m["params"]["entry"].get("text", "")[:200])

            def send(method, params=None):
                n[0] += 1
                ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    m = json.loads(ws.recv(timeout=60))
                    if m.get("id") == n[0]:
                        return m
                    collect(m)

            def js(expr):
                r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                              "awaitPromise": True})
                return r.get("result", {}).get("result", {}).get("value")

            def wait_for(expr, timeout=20.0):
                end = time.time() + timeout
                while time.time() < end:
                    if js(expr):
                        return True
                    time.sleep(0.2)
                return False

            send("Runtime.enable")
            send("Log.enable")
            send("Page.enable")
            send("Page.navigate", {"url": f"http://127.0.0.1:{port}/admin/knowledge"})
            assert wait_for("document.querySelectorAll('#kbDsRows tr').length > 0"), \
                "資料集表格沒有畫出來（JS 沒有跑起來？）"
            assert "瀏覽器測試手冊" in js("document.getElementById('kbDsRows').textContent")
            assert "只有關鍵字" in js("document.getElementById('kbStatus').textContent")
            # 打開文件清單
            js("document.querySelector('#kbDsRows .btn-primary').click()")
            assert wait_for("!document.getElementById('kbDocPanel').hidden && "
                            "document.querySelectorAll('#kbVerRows tr').length > 0"), "文件清單沒有出現"
            assert "啟用中" in js("document.getElementById('kbVerRows').textContent")
            # 檢索測試
            js("document.getElementById('kbQ').value = '下級機關對上級機關的稱謂'; "
               "document.getElementById('kbSearch').click()")
            assert wait_for("document.querySelectorAll('#kbResults .kb-chunk').length > 0"), \
                "檢索測試沒有結果"
            assert "鈞" in js("document.getElementById('kbResults').textContent")
            # 新增資料集的表單打得開
            js("document.getElementById('kbNewDs').click()")
            assert js("!document.getElementById('kbDsForm').hidden")
            time.sleep(0.5)
            try:
                while True:
                    collect(json.loads(ws.recv(timeout=0.2)))
            except Exception:
                pass
    finally:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/close/{tab['id']}", timeout=5).read()
        except Exception:
            pass
    assert not errs, "知識庫管理頁的主控台有錯誤：\n  " + "\n  ".join(errs)
