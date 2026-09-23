"""疑難排解頁的搜尋要真的會過濾（要真的瀏覽器才驗得到）。

使用者 2026-09-14：「q&a 要有搜尋」。

**靜態頁面的 JS 沒有任何既有檢查看得到** —— `node --check` 只驗語法，
而「搜尋框在、但過濾沒接上」的語法完全合法。這條在無頭瀏覽器裡實際打字，
數還剩幾則。

> 我第一版的臨時驗證腳本回報「完全沒過濾」，實際上是**腳本自己寫錯**
> （f-string 把 IIFE 的大括號吃掉了）。診斷順序：先確認**程式有沒有跑**
> （`dataset.text` 有沒有被填）、`hidden` 會不會真的隱藏、有沒有例外 ——
> 三項都正常才回頭懷疑測試本身。
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.repo_paths import public_root  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.browser_probe import browser as _browser  # noqa: E402

DOCS = public_root(ROOT) / "docs"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")


@pytest.fixture(scope="module")
def served():
    """把 docs/ 用本機 HTTP 伺服器端出來（`file://` 的同源規則會擋住一些東西）。"""
    port = _free_port()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(DOCS))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cdp = _free_port()
    br = subprocess.Popen(
        [_browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
         f"--remote-debugging-port={cdp}", "--remote-allow-origins=*", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.skip("瀏覽器起不來")
        yield port, cdp
    finally:
        # 自己起的自己收（這台機器上還有別的專案的瀏覽器，不可以按名字批次殺）
        br.terminate()
        try:
            br.wait(timeout=5)
        except Exception:
            br.kill()
        srv.shutdown()


def _probe(port: int, cdp: int, page: str) -> dict:
    import websockets.sync.client as wsc

    req = urllib.request.Request(f"http://127.0.0.1:{cdp}/json/new?about:blank",
                                 method="PUT")
    tab = json.loads(urllib.request.urlopen(req, timeout=10).read())
    with wsc.connect(tab["webSocketDebuggerUrl"], max_size=None, open_timeout=10) as ws:
        n = [0]

        def send(method, params=None):
            n[0] += 1
            ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
            while True:
                m = json.loads(ws.recv())
                if m.get("id") == n[0]:
                    return m

        def ev(expr):
            r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            return r["result"]["result"].get("value")

        send("Page.enable")
        send("Runtime.enable")
        send("Page.navigate", {"url": f"http://127.0.0.1:{port}/{page}"})
        time.sleep(2.5)

        def type_and_count(text: str) -> int:
            ev("var q=document.getElementById('tsQ');"
               f"q.value={json.dumps(text)};"
               "q.dispatchEvent(new Event('input'));")
            time.sleep(0.3)
            return ev("[].slice.call(document.querySelectorAll('.ts-item'))"
                      ".filter(function(e){return !e.hidden}).length")

        total = ev("document.querySelectorAll('.ts-item').length")
        miss = type_and_count("zzzznotarealthing")
        empty_shown = ev("!document.getElementById('tsEmpty').hidden")
        hit = type_and_count("fetch failed")
        chips = ev("[].slice.call(document.querySelectorAll('.ts-toc a'))"
                   ".filter(function(a){return !a.hidden}).length")
        back = type_and_count("")
        return {"total": total, "miss": miss, "empty": empty_shown,
                "hit": hit, "chips": chips, "back": back}


@pytest.mark.parametrize("page", ["troubleshooting.html", "troubleshooting-en.html"])
def test_the_search_filters_the_entries(served, page):
    port, cdp = served
    m = _probe(port, cdp, page)
    assert m["total"] >= 8, f"{page} 的項目太少（{m['total']}）—— 這條會驗不到東西"
    assert m["miss"] == 0, f"{page}：搜一個不存在的字還剩 {m['miss']} 則"
    assert m["empty"] is True, f"{page}：沒有結果時要顯示提示"
    assert 1 <= m["hit"] < m["total"], (
        f"{page}：搜 'fetch failed' 應該只剩少數幾則，實際 {m['hit']}/{m['total']}")
    # **目錄也要跟著過濾** —— 不然點下去會跳到被隱藏的段落
    assert m["chips"] <= m["hit"], f"{page}：目錄膠囊沒有跟著過濾（{m['chips']}）"
    assert m["back"] == m["total"], f"{page}：清空之後沒有全部回來"
