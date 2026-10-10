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
from tools.browser_probe import profile_arg as _profile_arg  # noqa: E402

pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")

# 「𠀋」是擴充 B 區的字（UTF-16 佔兩格）：放在符合處前面，標記位置換算錯一格就看得出來
HANDBOOK = ("壹、總述\n一、本手冊所稱文書，指處理公務之一切資料。\n貳、公文製作\n"
            "十八、公文用語：𠀋下級對上級稱「鈞」；上級對下級稱「貴」。\n")


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


def test_search_scope_is_spelled_out_and_matches_are_marked(live):
    """「都不勾＝全部資料集」原本是一行灰字，使用者說會被忽略（2026-10-09）：改成一列醒目的
    「查詢範圍：全部 N 個資料集」，勾了就變成「只查勾選的 N 個」並出現「改回全部」。
    結果裡問題的詞要用黃底標出來，標記的字一定要是原文裡那幾個字（位置不可以錯開）。"""
    import websockets.sync.client as wsc
    port, cdp = live
    req = urllib.request.Request(f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        tab = json.loads(r.read())
    try:
        with wsc.connect(tab["webSocketDebuggerUrl"], max_size=None, open_timeout=10) as ws:
            n = [0]

            def send(method, params=None):
                n[0] += 1
                ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    m = json.loads(ws.recv(timeout=60))
                    if m.get("id") == n[0]:
                        return m

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

            send("Page.enable")
            send("Page.navigate", {"url": f"http://127.0.0.1:{port}/admin/knowledge"})
            assert wait_for("document.querySelectorAll('#kbSearchDs input').length > 0"), \
                "檢索測試的資料集勾選框沒有畫出來"
            scope = """(function () { var b = document.getElementById('kbScope');
                var cs = getComputedStyle(b);
                return {hidden: b.hidden, text: document.getElementById('kbScopeText').textContent,
                        limited: b.classList.contains('is-limited'),
                        tip: !document.getElementById('kbScopeTip').hidden,
                        reset: !document.getElementById('kbScopeAll').hidden,
                        border: cs.borderLeftWidth, h: b.getBoundingClientRect().height}; })()"""
            s0 = js(scope)
            assert not s0["hidden"] and s0["h"] > 0, s0
            assert "全部 1 個資料集" in s0["text"] and not s0["limited"] and s0["tip"] and not s0["reset"], s0
            assert s0["border"] == "4px", f"查詢範圍要是醒目的一列（左邊色線），不是一行灰字：{s0}"
            js("var c = document.querySelector('#kbSearchDs input'); c.checked = true; "
               "c.dispatchEvent(new Event('change', {bubbles: true}))")
            s1 = js(scope)
            assert "只查勾選的 1 個" in s1["text"] and s1["limited"] and s1["reset"] and not s1["tip"], s1
            js("document.getElementById('kbScopeAll').click()")
            s2 = js(scope)
            assert "全部 1 個" in s2["text"] and not s2["limited"], s2
            assert js("document.querySelectorAll('#kbSearchDs input:checked').length") == 0

            q = "下級機關對上級機關的稱謂"
            js(f"document.getElementById('kbQ').value = {json.dumps(q)}; "
               "document.getElementById('kbSearch').click()")
            assert wait_for("document.querySelectorAll('#kbResults .kb-chunk').length > 0"), "沒有結果"
            marks = js("Array.from(document.querySelectorAll('#kbResults mark.kb-hl'))"
                       ".map(function (m) { return m.textContent; })")
            assert marks, "結果裡沒有任何黃底標記"
            assert any("上級" in m or "下級" in m for m in marks), marks
            # 位置錯開的話會標到「𠀋」的半個字或錯的字
            assert all(set(m.replace("\n", "")) <= set(q) for m in marks), marks
            assert js("!document.getElementById('kbHlNote').hidden"), "有標記時要說明黃底是什麼"
            bg = js("getComputedStyle(document.querySelector('#kbResults mark.kb-hl')).backgroundColor")
            assert bg == "rgb(253, 230, 138)", bg
    finally:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/close/{tab['id']}", timeout=5).read()
        except Exception:
            pass


# ---------------------------------------------------------------- 資料集很多時的挑選清單

# 名稱刻意有長有短（政府公開資料的法規名稱可以很長），還有一個停用的
_MANY = ([("文書處理手冊", "writing_rules"), ("機關檔案管理作業手冊", "writing_rules"),
          ("本機關公文分層負責明細表", "agency_rules")]
         + [(f"範例法規第{i:03d}號組織條例", "business_law") for i in range(1, 61)]
         + [("中央政府興建臺灣區高速公路第一期工程建設公債發行條例施行細則（舊）", "business_law"),
            ("臺北市公文範例", "examples")])


@pytest.fixture(scope="module")
def many():
    data = tempfile.mkdtemp(prefix="kbpick-")
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
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
        ids = {}
        for name, cat in _MANY:
            ids[name] = _req(port, "POST", "/admin/knowledge/api/datasets",
                             {"name": name, "category": cat})["id"]
        off = ids["中央政府興建臺灣區高速公路第一期工程建設公債發行條例施行細則（舊）"]
        _req(port, "POST", f"/admin/knowledge/api/datasets/{off}",
             {"name": "中央政府興建臺灣區高速公路第一期工程建設公債發行條例施行細則（舊）",
              "category": "business_law", "enabled": False})
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


def _open_tab(cdp, width, height):
    req = urllib.request.Request(f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


PICK_JS = """(function () {
  var box = document.getElementById('kbSearchDs');
  var labs = Array.from(box.querySelectorAll('.kb-ds-pick')).filter(function (l) {
    return !l.hidden && !l.closest('[hidden]'); });
  var rects = labs.map(function (l) { var b = l.getBoundingClientRect();
    var n = l.querySelector('.kb-ds-pick-name'); var nb = n.getBoundingClientRect();
    return {left: b.left, right: b.right, top: b.top, bottom: b.bottom, width: b.width,
            nameH: nb.height, lineH: parseFloat(getComputedStyle(n).lineHeight),
            ellipsis: n.scrollWidth > n.clientWidth + 1, title: l.title, text: n.textContent}; });
  var lb = box.getBoundingClientRect();
  return {open: !document.getElementById('kbPick').hidden, shown: rects.length, rects: rects,
          listH: lb.height, scrollH: box.scrollHeight, count: document.getElementById('kbPickCount').textContent,
          groups: Array.from(box.querySelectorAll('.kb-pick-group')).filter(function (g) {
            return !g.hidden; }).map(function (g) {
            return g.querySelector('.kb-pick-gh').textContent; }),
          cats: Array.from(document.querySelectorAll('#kbPickCats .kb-pick-cat')).map(function (b) {
            return [b.textContent, b.getAttribute('aria-pressed')]; }),
          pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth};
})()"""


def test_many_datasets_sit_in_a_tidy_filterable_picker(many):
    """資料集上千個時（政府公開資料匯入的法規）勾選框整片攤開很亂（2026-10-09 使用者：「排列一下 太亂了」）：
    平常收起來；展開後依類別分組、排成同寬的格子、名稱太長就省略（滑鼠移上去看全名）、清單本身有高度上限，
    可以打字篩選、按類別篩、只看已勾選、勾選目前列出的。"""
    import websockets.sync.client as wsc
    port, cdp = many
    tab = _open_tab(cdp, 1280, 900)
    try:
        with wsc.connect(tab["webSocketDebuggerUrl"], max_size=None, open_timeout=10) as ws:
            n = [0]

            def send(method, params=None):
                n[0] += 1
                ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    m = json.loads(ws.recv(timeout=60))
                    if m.get("id") == n[0]:
                        return m

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

            send("Emulation.setDeviceMetricsOverride",
                 {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False})
            send("Page.enable")
            send("Page.navigate", {"url": f"http://127.0.0.1:{port}/admin/knowledge"})
            assert wait_for("document.querySelectorAll('#kbSearchDs input').length === %d" % len(_MANY))
            p0 = js(PICK_JS)
            assert not p0["open"] and p0["shown"] == 0, "平常要收起來，不可以整片攤開"
            assert "全部 %d 個資料集" % len(_MANY) in js("document.getElementById('kbScopeText').textContent")
            js("document.getElementById('kbScopePick').click(); true")
            p = js(PICK_JS)
            assert p["open"] and p["shown"] == len(_MANY), p["shown"]
            assert js("document.getElementById('kbScopePick').getAttribute('aria-expanded')") == "true"
            # 同寬的格子、每一格只有一行
            widths = {round(r["width"]) for r in p["rects"]}
            assert len(widths) == 1, f"每一格要一樣寬：{sorted(widths)}"
            cols = len({round(r["left"]) for r in p["rects"]})
            assert cols >= 3, f"1280 寬至少要排三欄：{cols}"
            assert all(r["nameH"] <= r["lineH"] * 1.5 for r in p["rects"]), "名稱折行了"
            long = [r for r in p["rects"] if r["text"].startswith("中央政府興建")][0]
            assert long["ellipsis"] and long["title"] == long["text"], "太長的名稱要省略、滑鼠移上去看得到全名"
            # 清單有高度上限、捲得動；頁面沒有橫向捲軸
            assert p["listH"] <= 380 and p["scrollH"] > p["listH"] + 50, (p["listH"], p["scrollH"])
            assert p["pageOverflow"] <= 0
            # 依類別分組，組名帶數量；類別按鈕帶數量
            assert [g.rstrip("0123456789") for g in p["groups"]] == ["文書規範", "機關規定", "業務法規", "公文範例"], p["groups"]
            assert p["groups"][2].endswith("61"), p["groups"]
            assert ["全部%d" % len(_MANY), "true"] in p["cats"], p["cats"]
            # 打字篩選（台／臺通用）
            js("var q = document.getElementById('kbPickQ'); q.value = '台北市'; "
               "q.dispatchEvent(new Event('input')); true")
            f = js(PICK_JS)
            assert [r["text"] for r in f["rects"]] == ["臺北市公文範例"], f["rects"]
            assert "1" in f["count"]
            js("var q = document.getElementById('kbPickQ'); q.value = '找不到的名字'; "
               "q.dispatchEvent(new Event('input')); true")
            assert js(PICK_JS)["shown"] == 0
            assert js("!document.getElementById('kbPickNone').hidden")
            js("var q = document.getElementById('kbPickQ'); q.value = ''; "
               "q.dispatchEvent(new Event('input')); true")
            # 按類別篩，再「勾選目前列出的」→ 只勾到那一類
            js("document.querySelector('#kbPickCats .kb-pick-cat[data-cat=\"writing_rules\"]').click(); true")
            c = js(PICK_JS)
            assert c["shown"] == 2 and c["groups"][0].startswith("文書規範"), c["groups"]
            js("document.getElementById('kbPickShown').click(); true")
            assert js("document.querySelectorAll('#kbSearchDs input:checked').length") == 2
            assert "只查勾選的 2 個" in js("document.getElementById('kbScopeText').textContent")
            # 只看已勾選（換回全部類別也只剩那兩個）
            js("document.querySelector('#kbPickCats .kb-pick-cat[data-cat=\"\"]').click(); true")
            js("var o = document.getElementById('kbPickOnly'); o.click(); true")
            assert js(PICK_JS)["shown"] == 2
            js("document.getElementById('kbScopeAll').click(); true")
            assert js(PICK_JS)["shown"] == 0 and js("!document.getElementById('kbPickNone').hidden")
            js("var o = document.getElementById('kbPickOnly'); o.click(); true")
            assert js(PICK_JS)["shown"] == len(_MANY)
            # 停用的資料集看得出來
            off = js("Array.from(document.querySelectorAll('#kbSearchDs .kb-ds-pick.is-off'))"
                     ".map(function (l) { return l.textContent; })")
            assert len(off) == 1 and off[0].endswith("停用"), off
            shot = os.environ.get("JTDT_KB_PICK_SHOT")
            if shot:
                import base64
                js("window.scrollTo(0, document.getElementById('kbScope').getBoundingClientRect().top"
                   " + window.scrollY - 80); true")
                time.sleep(0.3)
                r = send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
                open(shot, "wb").write(base64.b64decode(r["result"]["data"]))
    finally:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/close/{tab['id']}", timeout=5).read()
        except Exception:
            pass
