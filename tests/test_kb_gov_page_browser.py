"""「政府公開資料」管理頁在**真的瀏覽器**裡跑一次：卡片畫得出來、搜尋清單、勾選、
儲存選取、匯入，主控台不可以有任何錯誤（含 CSP 違規）。

這一頁的內容全是 JS 畫的 —— JS 停住的話畫面上只剩標題與說明，而「頁面回 200」
照樣成立（見 `test_pages_boot_in_a_browser.py` 的說明）。素材用上傳（不連外）放進去。
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


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _req(port: int, method: str, path: str, body=None, file=None):
    url = f"http://127.0.0.1:{port}{path}"
    headers, data = {}, None
    if file is not None:
        fname, content = file
        b = "govtestboundary"
        data = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{fname}\"\r\n"
                f"Content-Type: application/zip\r\n\r\n").encode() + content + f"\r\n--{b}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={b}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def _wait_idle(port: int, timeout: float = 60.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        st = _req(port, "GET", "/admin/knowledge/api/gov/status")
        if not st["running"]:
            return st
        time.sleep(0.2)
    raise AssertionError("背景作業沒有結束")


@pytest.fixture(scope="module")
def live():
    from tests.test_kb_gov_import import law_zip, order_zip
    data = tempfile.mkdtemp(prefix="kbgovpage-")
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
        _req(port, "POST", "/admin/knowledge/api/gov/packages/moj-law/upload", file=("law.zip", law_zip()))
        _wait_idle(port)
        _req(port, "POST", "/admin/knowledge/api/gov/packages/moj-order/upload", file=("order.zip", order_zip()))
        _wait_idle(port)
        # 行政院釋例：上傳一份不是 PDF 的檔 → 那張卡片「最近一次」是失敗（狀態標籤要是紅的）
        _req(port, "POST", "/admin/knowledge/api/gov/packages/ey-mailbox/upload", file=("x.pdf", b"not a pdf"))
        st = _wait_idle(port)
        by = {g["id"]: g for g in st["groups"]}
        assert by["moj"]["available"] == 4, by["moj"]
        assert by["ey"]["last"] and not by["ey"]["last"]["ok"], by["ey"]
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


class _Tab:
    """一個瀏覽器分頁：送 CDP 指令、跑 JS、收主控台錯誤（含 CSP 違規）。"""

    def __init__(self, cdp: int):
        import websockets.sync.client as wsc
        self.cdp = cdp
        req = urllib.request.Request(f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            self.tab = json.loads(r.read())
        self.ws = wsc.connect(self.tab["webSocketDebuggerUrl"], max_size=None, open_timeout=10)
        self.n = 0
        self.errs: list[str] = []
        self.send("Runtime.enable")
        self.send("Log.enable")
        self.send("Page.enable")

    def collect(self, m):
        if m.get("method") == "Runtime.exceptionThrown":
            d = m["params"]["exceptionDetails"]
            self.errs.append("例外：" + (d.get("exception", {}).get("description")
                                       or d.get("text", "?"))[:200])
        elif m.get("method") == "Log.entryAdded" and m["params"]["entry"].get("level") == "error":
            self.errs.append("主控台：" + m["params"]["entry"].get("text", "")[:200])

    def send(self, method, params=None):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        while True:
            m = json.loads(self.ws.recv(timeout=60))
            if m.get("id") == self.n:
                return m
            self.collect(m)

    def js(self, expr):
        r = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                           "awaitPromise": True})
        return r.get("result", {}).get("result", {}).get("value")

    def wait_for(self, expr, timeout=30.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.js(expr):
                return True
            time.sleep(0.2)
        return False

    def drain(self):
        time.sleep(0.5)
        try:
            while True:
                self.collect(json.loads(self.ws.recv(timeout=0.2)))
        except Exception:
            pass

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{self.cdp}/json/close/{self.tab['id']}", timeout=5).read()
        except Exception:
            pass


#: 每張卡片的狀態區讀出來：三格數字（清單 → 勾選 → 匯入）與「最近一次」
_STATUS_JS = """(() => {
  const out = {};
  document.querySelectorAll('section.gv-card').forEach((c) => {
    const tile = (k) => {
      const t = c.querySelector('.gv-status .gv-stat[data-stat="' + k + '"]');
      if (!t) return null;
      return {num: t.querySelector('.gv-stat-num').textContent.trim(),
              label: t.querySelector('.gv-stat-label').textContent.trim(),
              sub: t.querySelector('.gv-stat-sub').textContent.trim(),
              svg: !!t.querySelector('.gv-stat-top svg'), cls: t.className};
    };
    const last = c.querySelector('.gv-status .gv-last');
    const st = last && last.querySelector('.gv-st');
    out[c.dataset.gid] = {
      tiles: c.querySelectorAll('.gv-status .gv-stat').length,
      available: tile('available'), selected: tile('selected'), imported: tile('imported'),
      last: last ? {state: last.dataset.state, badge: st ? st.className : '',
                    badgeText: st ? st.textContent.trim() : '', badgeSvg: !!(st && st.querySelector('svg')),
                    act: (last.querySelector('.gv-last-act') || {}).textContent || '',
                    text: last.textContent,
                    chips: Object.fromEntries([...last.querySelectorAll('.gv-chip')].map(
                      (x) => [x.dataset.k, {n: x.querySelector('b').textContent.trim(), cls: x.className}]))} : null,
    };
  });
  return out;
})()"""


def test_gov_page_boots_searches_selects_and_imports(live):
    port, cdp = live
    t = _Tab(cdp)
    js, wait_for = t.js, t.wait_for

    def click_text(card_sel, text):
        return js("(() => { const b = [...document.querySelectorAll('" + card_sel + " button')]"
                  ".find((x) => x.textContent.trim() === " + json.dumps(text) + ");"
                  " if (!b) return false; b.click(); return true; })()")

    try:
        t.send("Page.navigate", {"url": f"http://127.0.0.1:{port}/admin/knowledge/gov"})
        moj = 'section[data-gid=\\"moj\\"]'
        assert wait_for("document.querySelectorAll('section.gv-card').length === 3"), \
            "三張來源卡片沒有畫出來（JS 沒有跑起來？）"
        assert "全國法規資料庫" in js("document.querySelector('section[data-gid=\"moj\"] h2').textContent")
        assert "文書處理手冊" in js("document.getElementById('govLinks').textContent")

        # 2026-10-08 使用者：「已下載 跟 已匯入 之類的顯示 再清楚排版一點 用圖 用表格」
        # —— 每張卡片三格數字（清單 → 勾選 → 匯入）＋「最近一次」的狀態標籤
        s = js(_STATUS_JS)
        for gid, c in s.items():
            assert c["tiles"] == 3, (gid, c)
            for k in ("available", "selected", "imported"):
                assert c[k] and c[k]["svg"] and c[k]["label"], (gid, k, c)
        assert s["moj"]["available"]["num"] == "4", s["moj"]           # 清單裡 4 項
        st0 = {g["id"]: g for g in _req(port, "GET", "/admin/knowledge/api/gov/status")["groups"]}
        sel0 = st0["moj"]["selected_count"]                              # 預設建議（照伺服器）
        assert s["moj"]["selected"]["num"] == f"{sel0:,}", (sel0, s["moj"])
        assert s["moj"]["imported"]["num"] == "0", s["moj"]
        assert "gv-stat-done" not in s["moj"]["imported"]["cls"], s["moj"]
        # 還沒下載的：數字是「—」、寫「還沒下載」
        assert s["ndc"]["available"]["num"] == "—" and "gv-stat-empty" in s["ndc"]["available"]["cls"], s["ndc"]
        assert "還沒下載" in s["ndc"]["available"]["sub"], s["ndc"]
        # 釋例是兩份 PDF：算「下載了幾份」
        assert s["ey"]["available"]["num"] == "0 / 2" and s["ey"]["available"]["label"] == "已下載的檔案", s["ey"]
        # 最近一次：成功綠、失敗紅；沒做過的不畫
        lm = s["moj"]["last"]
        assert lm and lm["state"] == "ok" and "gv-st-ok" in lm["badge"] and lm["badgeText"] == "成功", lm
        assert lm["act"] == "上傳並匯入" and lm["badgeSvg"], lm      # 上傳之後一樣接著匯入
        le = s["ey"]["last"]
        assert le and le["state"] == "err" and "gv-st-err" in le["badge"] and le["badgeText"] == "失敗", le
        assert "不是 PDF" in le["text"], le
        assert s["ndc"]["last"] is None, s["ndc"]

        # 2026-10-08 使用者：「做簡單一點」「按鈕要有 icon」「下載按鈕要醒目 用藍色」「卡片靠太近了」
        # 「請改為下載並匯入 不要分兩步」—— 每張卡片只有一顆藍色的「下載並匯入」
        lay = js("""(() => {
          const R = (e) => e.getBoundingClientRect();
          // 「全部清單 / 已勾選」與位階篩選是切換鈕（不是動作），不要求圖示
          const btns = [...document.querySelectorAll('section.gv-card button:not(.gv-tab):not(.gv-level), #govLinks .btn')];
          const cards = [...document.querySelectorAll('#govGroups > section, #govLinks > section')];
          const gaps = [];
          for (let i = 1; i < cards.length; i++) gaps.push(Math.round(R(cards[i]).top - R(cards[i - 1]).bottom));
          const dl = [...document.querySelectorAll('section.gv-card .gv-actions button')].filter((b) => b.textContent.trim() === '下載並匯入');
          const two = [...document.querySelectorAll('section.gv-card button')].filter((b) =>
            ['下載資料', '匯入選取的項目', '檢查更新'].includes(b.textContent.trim()));
          const st = %s;
          return {
            n: btns.length,
            noIcon: btns.filter((b) => !b.querySelector('svg')).map((b) => b.textContent.trim()),
            dl: dl.length, dlBlue: dl.every((b) => b.classList.contains('btn-primary')), two: two.length,
            // 「下載來源」預設收起來；只有最後一次下載失敗的那張自動打開
            src: st.groups.map((g) => {
              const d = document.querySelector('section[data-gid="' + g.id + '"] > details.gv-src');
              const failed = g.packages.some((p) => p.last && !p.last.ok);
              return !!d && d.open === failed; }),
            introGap: Math.round(R(cards[0]).top - R(document.querySelector('.gv-intro')).bottom),
            gaps: gaps,
            later: st.groups.filter((g) => !g.downloaded && g.kind !== 'pdfs').map((g) => {
              const c = document.querySelector('section[data-gid="' + g.id + '"]');
              return !!c.querySelector('.gv-pick-later') && c.querySelector('.gv-pick-body').hidden; }),
          };
        })()""" % json.dumps(_req(port, "GET", "/admin/knowledge/api/gov/status")))
        assert lay["n"] >= 10 and not lay["noIcon"], lay
        assert lay["dl"] == 3 and lay["dlBlue"] and lay["two"] == 0, lay
        assert lay["src"] and all(lay["src"]), lay
        assert lay["introGap"] >= 12 and min(lay["gaps"]) >= 12, lay    # 不可以貼在一起
        assert all(lay["later"]), lay                                    # 還沒下載的只講一句

        # 「這邊說169項，可是我看只有四項可以勾」：預設就列出整份清單（不是只有已勾選的）
        items = "document.querySelectorAll('section[data-gid=\"moj\"] .gv-items li')"
        assert wait_for(items + ".length === 4"), js(items + ".length")
        pg = "document.querySelector('section[data-gid=\"moj\"] .gv-pager-text').textContent"
        assert "共 4 項" in js(pg), js(pg)
        tabs = js("[...document.querySelectorAll('section[data-gid=\"moj\"] .gv-tab')].map((b) =>"
                  " [b.dataset.view, b.classList.contains('is-on'), b.querySelector('b').textContent])")
        assert tabs == [["all", True, "4"], ["sel", False, f"{sel0:,}"]], tabs
        # 法規位階篩選：法律 3、命令 1
        assert click_text(moj, "命令（1）")
        assert wait_for(items + ".length === 1"), js(items + ".length")
        assert click_text(moj, "全部類別")
        assert wait_for(items + ".length === 4")

        # 搜尋 → 勾選（「已勾選」那一格跟著畫面上的勾選走）
        js("document.querySelector('section[data-gid=\"moj\"] .gv-q').value = '範例公文'")
        assert click_text(moj, "搜尋")
        assert wait_for(items + ".length === 1"), "搜尋沒有結果"
        js("document.querySelector('section[data-gid=\"moj\"] .gv-items li input[type=checkbox]').click()")
        sel = js(_STATUS_JS)["moj"]["selected"]
        assert sel["num"] == f"{sel0 + 1:,}" and "還沒儲存" in sel["sub"], (sel0, sel)

        # 「要有個全選吧」：範圍是目前看到的（清掉搜尋＝整份清單，不只這一頁）；取消全選一樣
        # 已廢止的不跟著全選（匯入後也是停用、查不到）—— 而且要講出略過了幾項
        # （2026-10-09 使用者：「要全選只能 1000，可是一筆一筆選很痛苦」）
        assert click_text(moj, "清除搜尋")
        assert wait_for(items + ".length === 4")
        assert click_text(moj, "全選")
        assert wait_for("document.querySelector('section[data-gid=\"moj\"] .gv-stat[data-stat=\"selected\"] .gv-stat-num')"
                        f".textContent.trim() === '{sel0 + 3:,}'"), js(_STATUS_JS)["moj"]["selected"]
        ticks = js("[...document.querySelectorAll('section[data-gid=\"moj\"] .gv-items li')].map((li) =>"
                   " [li.textContent.includes('已廢止範例條例'), li.querySelector('input').checked])")
        assert sorted(ticks) == [[False, True]] * 3 + [[True, False]], ticks
        assert wait_for("[...document.querySelectorAll('#toast-host .toast')].some((t) =>"
                        " t.textContent.includes('1 項已廢止'))"), "略過已廢止的要講出來"
        assert click_text(moj, "取消全選")
        assert wait_for("document.querySelector('section[data-gid=\"moj\"] .gv-stat[data-stat=\"selected\"] .gv-stat-num')"
                        f".textContent.trim() === '{sel0:,}'")
        assert js("[...document.querySelectorAll('section[data-gid=\"moj\"] .gv-items li input')].every((i) => !i.checked)")
        # 「已勾選」那一頁：取消全選 → 一項都不剩；再回「全部清單」勾回那一部
        assert click_text(moj, "已勾選" + f"{sel0:,}")
        assert wait_for(items + f".length === {sel0}")
        assert js("document.querySelector('section[data-gid=\"moj\"] .gv-selall').hidden"), "已勾選那一頁不需要「全選」"
        assert click_text(moj, "全部清單4")
        assert wait_for(items + ".length === 4")
        js("[...document.querySelectorAll('section[data-gid=\"moj\"] .gv-items li')].find((li) =>"
           " li.textContent.includes('範例公文條例')).querySelector('input').click()")

        # 下載並匯入（一步）：這次下載失敗（網址指到不能連的位址，不連外）→ 照這台上次的清單匯入、講明
        # 備用網址也要換掉 —— 只換主要網址的話會改試備用網址，**真的連到外面去下載**（踩過）
        for pid in ("moj-law", "moj-order"):
            _req(port, "POST", f"/admin/knowledge/api/gov/packages/{pid}",
                 {"url": f"http://127.0.0.1:9/{pid}.zip", "alt_url": f"http://127.0.0.1:9/{pid}-alt.zip"})
        st_urls = {p_["id"]: p_ for g_ in _req(port, "GET", "/admin/knowledge/api/gov/status")["groups"]
                   for p_ in g_["packages"]}
        for pid in ("moj-law", "moj-order"):
            for k in ("url", "alt_url"):
                assert not st_urls[pid].get(k) or st_urls[pid][k].startswith("http://127.0.0.1:9/"), \
                    (pid, k, st_urls[pid].get(k))
        assert click_text(moj, "下載並匯入")
        assert wait_for("(() => { const c = document.querySelector('section[data-gid=\"moj\"]');"
                        " const n = c && c.querySelector('.gv-stat[data-stat=\"imported\"] .gv-stat-num');"
                        " const l = c && c.querySelector('.gv-status .gv-last');"
                        " return !!n && n.textContent.trim() === '1' && !!l && l.dataset.state === 'err'"
                        " && l.textContent.includes('下載並匯入') && l.textContent.includes('上次下載的清單'); })()",
                        timeout=60), js("document.querySelector('section[data-gid=\"moj\"] .gv-status').textContent")
        st = _req(port, "GET", "/admin/knowledge/api/gov/status")
        mm = {g["id"]: g for g in st["groups"]}["moj"]
        assert mm["imported_count"] == 1 and mm["last"]["summary"]["stale_list"]
        m = js(_STATUS_JS)["moj"]
        assert "gv-stat-done" in m["imported"]["cls"], m
        assert m["selected"]["num"] == f"{sel0 + 1:,}" and "還沒儲存" not in m["selected"]["sub"], m
        # 匯入的結果：四顆數字，新匯入 1 是綠的、失敗 0 不上色
        assert m["last"]["chips"]["new"]["n"] == "1" and "gv-chip-ok" in m["last"]["chips"]["new"]["cls"], m
        assert m["last"]["chips"]["failed"]["n"] == "0" and "gv-chip-err" not in m["last"]["chips"]["failed"]["cls"], m
        t.drain()
    finally:
        t.close()
    assert not t.errs, "政府公開資料管理頁的主控台有錯誤：\n  " + "\n  ".join(t.errs)


def test_offline_upload_uses_the_site_upload_component_and_really_uploads(live):
    """2026-10-08 使用者截圖：「不能連外？上傳檔案」是原生的「選擇檔案 未選擇任何檔案」＋一顆按鈕，
    沒套本站樣式 —— 改用全站共用的上傳元件（拖曳區）。這裡驗：每個檔案一個拖曳區、原生輸入框藏起來、
    label 對得到自己的輸入框，**而且真的上傳得了**（選了檔就送出、背景作業跑完、畫面更新）。"""
    import base64
    from tests.test_kb_gov_import import order_zip
    port, cdp = live
    t = _Tab(cdp)
    js, wait_for = t.js, t.wait_for
    try:
        t.send("Page.navigate", {"url": f"http://127.0.0.1:{port}/admin/knowledge/gov"})
        assert wait_for("document.querySelectorAll('section.gv-card').length === 3")
        js("document.querySelectorAll('details.gv-src, details.gv-upload').forEach((d) => { d.open = true; })")
        st = _req(port, "GET", "/admin/knowledge/api/gov/status")
        n_pkgs = sum(len(g["packages"]) for g in st["groups"])
        u = js("""(() => {
          const zones = [...document.querySelectorAll('section.gv-card .gv-pkg .file-upload')];
          const ids = zones.map((z) => z.querySelector('input[type=file]').id);
          return {
            n: zones.length,
            dropZones: zones.filter((z) => z.querySelector('label.drop-zone .drop-zone-text')).length,
            nativeVisible: zones.filter((z) => {
              const i = z.querySelector('input[type=file]'); return getComputedStyle(i).display !== 'none'; }).length,
            idsUnique: new Set(ids).size === ids.length && ids.every((x) => !!x),
            labelsMatch: zones.every((z) => z.querySelector('label.drop-zone').htmlFor === z.querySelector('input[type=file]').id),
            dashed: zones.every((z) => getComputedStyle(z.querySelector('.drop-zone')).borderTopStyle === 'dashed'),
            hint: (zones[0] && zones[0].querySelector('.drop-zone-hint').textContent) || '',
            pdfAccept: [...document.querySelectorAll('section[data-gid="ey"] .file-upload input[type=file]')]
                         .every((i) => (i.getAttribute('accept') || '').includes('pdf')),
            wsRow: document.querySelectorAll('section.gv-card .fu-ws-row').length,
            oldNative: document.querySelectorAll('section.gv-card .gv-upload-row').length,
          };
        })()""")
        assert u["n"] == n_pkgs and u["dropZones"] == n_pkgs, u
        assert u["nativeVisible"] == 0 and u["idsUnique"] and u["labelsMatch"] and u["dashed"], u
        assert "MB" in u["hint"] and u["pdfAccept"] and u["wsRow"] == 0 and u["oldNative"] == 0, u

        # 空檔案：在瀏覽器就擋下來、講出原因，不送出
        before = {g["id"]: g for g in st["groups"]}["ndc"]["last"]
        js("""(() => {
          const i = document.querySelector('#gvUp-ndc-rules-1 input[type=file]');
          const dt = new DataTransfer(); dt.items.add(new File([], 'empty.xml'));
          i.files = dt.files; i.dispatchEvent(new Event('change'));
        })()""")
        assert wait_for("(() => { const n = document.querySelector('#gvUp-ndc-rules-1').parentElement.querySelector('.gv-up-note');"
                        " return !!n && !n.hidden && n.textContent.includes('檔案是空的'); })()", timeout=10), \
            "空檔案沒有在畫面上講出原因"
        time.sleep(0.5)
        assert {g["id"]: g for g in _req(port, "GET", "/admin/knowledge/api/gov/status")["groups"]}["ndc"]["last"] == before

        # 真的上傳：法規・命令那一份換成同一個 zip
        t0 = time.time()
        b64 = base64.b64encode(order_zip()).decode()
        js("""(() => {
          const raw = atob('%s'); const a = new Uint8Array(raw.length);
          for (let k = 0; k < raw.length; k++) a[k] = raw.charCodeAt(k);
          const i = document.querySelector('#gvUp-moj-order input[type=file]');
          const dt = new DataTransfer(); dt.items.add(new File([a], 'ChOrder.zip', {type: 'application/zip'}));
          i.files = dt.files; i.dispatchEvent(new Event('change'));
        })()""" % b64)
        assert wait_for("(() => { const z = document.querySelector('#gvUp-moj-order');"
                        " return !!z && z.querySelector('.drop-zone-filename').textContent.includes('ChOrder.zip'); })()",
                        timeout=10), "選到的檔名與大小沒有顯示"
        end = time.time() + 60
        moj = None
        while time.time() < end:
            moj = {g["id"]: g for g in _req(port, "GET", "/admin/knowledge/api/gov/status")["groups"]}["moj"]
            if moj["last"] and moj["last"]["action"] == "install" and moj["last"]["at"] >= t0 - 1 and not moj["running"]:
                break
            time.sleep(0.3)
        else:
            raise AssertionError(f"拖曳區選了檔沒有真的上傳（或背景作業沒跑完）：{moj}")
        assert moj["last"]["ok"], moj
        pkg = {p["id"]: p for p in moj["packages"]}["moj-order"]
        assert pkg["origin"] == "upload" and pkg["last"]["ok"], pkg
        # 畫面跟著更新：moj 卡片「最近一次」變成上傳資料檔、成功
        assert wait_for("(() => { const l = document.querySelector('section[data-gid=\"moj\"] .gv-status .gv-last');"
                        " return !!l && l.dataset.state === 'ok' && l.textContent.includes('上傳並匯入'); })()",
                        timeout=30), "上傳完成後畫面沒有更新"
        t.drain()
    finally:
        t.close()
    assert not t.errs, "政府公開資料管理頁的主控台有錯誤：\n  " + "\n  ".join(t.errs)


_GEOM_JS = """(() => {
  const R = (e) => e.getBoundingClientRect();
  const bad = [];
  const doc = document.documentElement;
  if (doc.scrollWidth > window.innerWidth + 1) bad.push('整頁可以左右捲：' + doc.scrollWidth + ' > ' + window.innerWidth);
  document.querySelectorAll('section.gv-card').forEach((c) => {
    const cr = R(c), gid = c.dataset.gid;
    const tiles = [...c.querySelectorAll('.gv-status .gv-stat')];
    const tops = new Set(tiles.map((x) => Math.round(R(x).top)));
    if (tops.size !== 1) bad.push(gid + ' 三格沒有排成一列');
    tiles.forEach((x) => { const r = R(x);
      if (r.left < cr.left - 0.5 || r.right > cr.right + 0.5) bad.push(gid + ' 數字格超出卡片：' + x.dataset.stat);
      const num = x.querySelector('.gv-stat-num');
      if (num.scrollWidth > num.clientWidth + 1) bad.push(gid + ' 數字被切掉：' + x.dataset.stat); });
    const last = c.querySelector('.gv-status .gv-last');
    if (last && R(last).right > cr.right + 0.5) bad.push(gid + ' 最近一次超出卡片');
    c.querySelectorAll('.gv-pkg').forEach((p) => {
      const pr = R(p);
      if (pr.right > cr.right + 0.5) bad.push(gid + ' 檔案框超出卡片');
      const inps = [...p.querySelectorAll('input.gv-url, input.gv-alt')];
      inps.forEach((i) => { const r = R(i);
        if (r.width < 120) bad.push(gid + ' 網址框太窄：' + Math.round(r.width));
        if (r.right > pr.right - 4 || r.left < pr.left) bad.push(gid + ' 網址框超出：' + Math.round(r.right) + ' > ' + Math.round(pr.right)); });
      if (window.innerWidth > 700 && inps.length === 2) {
        const labs = [...p.querySelectorAll('.gv-url-label')];
        if (Math.abs(R(inps[0]).left - R(inps[1]).left) > 1) bad.push(gid + ' 兩格網址框沒有對齊');
        labs.forEach((l, k) => { const lr = R(l), ir = R(inps[k]);
          if (!(lr.right <= ir.left && Math.abs((lr.top + lr.bottom) / 2 - (ir.top + ir.bottom) / 2) < 6))
            bad.push(gid + ' 標籤沒有在輸入框左邊同一列'); });
      }
      const dz = p.querySelector('.drop-zone');
      if (dz && R(dz).right > pr.right + 0.5) bad.push(gid + ' 拖曳區超出');
    });
  });
  return {bad: bad, n: document.querySelectorAll('section.gv-card .gv-pkg input.gv-url').length};
})()"""


@pytest.mark.parametrize("width", [1440, 1000, 390])
def test_status_tiles_and_url_rows_fit_the_card(live, width):
    """數字三格排成一列、不超出卡片；網址兩格的標籤對齊、輸入框不超出卡片右緣（截圖裡被切掉過）；
    手機寬度整頁不可以左右捲。"""
    port, cdp = live
    t = _Tab(cdp)
    try:
        t.send("Emulation.setDeviceMetricsOverride",
               {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width < 500})
        t.send("Page.navigate", {"url": f"http://127.0.0.1:{port}/admin/knowledge/gov"})
        assert t.wait_for("document.querySelectorAll('section.gv-card .gv-status').length === 3")
        t.js("document.querySelectorAll('details.gv-src, details.gv-upload').forEach((d) => { d.open = true; })")
        time.sleep(0.3)
        g = t.js(_GEOM_JS)
        assert g["n"] >= 4, g                     # 真的量到網址框（不是空的清單）
        assert not g["bad"], f"寬 {width}：" + "；".join(g["bad"])
        t.drain()
    finally:
        t.close()
    assert not t.errs, "\n  ".join(t.errs)


def test_source_cards_collapse_on_title_click_and_remember_it(live):
    """來源卡片是**資料讀回來之後才由程式建的**：標題有收折箭頭，點了要真的收起來，重新整理也記得
    （2026-10-09 使用者：「點了卡片標題 怎麼不能收折」—— 原本收折只掛在頁面載入當下已經有的卡片上）。"""
    port, cdp = live
    t = _Tab(cdp)
    js, wait_for = t.js, t.wait_for
    card = "document.querySelector('section.gv-card[data-gid=\"moj\"]')"
    key = "'panel-collapsed:/admin/knowledge/gov:' + " + card + ".querySelector('h2').textContent.trim()"
    try:
        t.send("Page.navigate", {"url": f"http://127.0.0.1:{port}/admin/knowledge/gov"})
        assert wait_for("document.querySelectorAll('section.gv-card').length === 3")
        js(f"localStorage.removeItem({key}); true")
        body_shown = f"!!{card}.querySelector('.gv-status') && {card}.querySelector('.gv-status').getClientRects().length > 0"
        assert js(body_shown), "卡片內容一開始就看不到"
        js(f"{card}.querySelector('h2').click(); true")
        assert js(f"{card}.classList.contains('collapsed')"), "點了標題沒有收起來"
        assert not js(body_shown), "標了收折，內容卻還看得到"
        # 重新整理：卡片重新由程式建出來，記住的狀態要套上去
        t.send("Page.reload", {})
        assert wait_for(f"!!{card} && {card}.classList.contains('collapsed')"), "重新整理後沒有記住收折"
        js(f"{card}.querySelector('h2').click(); true")
        assert wait_for(body_shown), "再點一次沒有展開"
        t.drain()
        assert not t.errs, t.errs
    finally:
        try:
            js(f"localStorage.removeItem({key}); true")
        except Exception:
            pass
        t.close()


def test_saving_a_selection_says_it_still_needs_download_and_import(live):
    """2026-10-09 使用者：「勾選更多 按了儲存選取成功後 要提示 再按下載並匯入 才會有」。
    「儲存選取」只記下勾了哪些，不會匯入 —— 存好之後還有沒匯入的就問要不要現在開始；
    按「稍後再匯入」的話，按鈕旁一直留著「還有 N 項勾了但還沒匯入」；按「下載並匯入」就真的開始。
    （「下載並匯入」之前順手存選取的那一次不問 —— 主流程那條測試走的就是那條路。）"""
    port, cdp = live
    moj = 'section[data-gid=\\"moj\\"]'
    # 不連外：主要網址與備用網址都指到連不到的位址（只換主要的話會改試備用 —— 真的連到外面，踩過）
    for pid in ("moj-law", "moj-order"):
        _req(port, "POST", f"/admin/knowledge/api/gov/packages/{pid}",
             {"url": f"http://127.0.0.1:9/{pid}.zip", "alt_url": f"http://127.0.0.1:9/{pid}-alt.zip"})
    pk = {p["id"]: p for g_ in _req(port, "GET", "/admin/knowledge/api/gov/status")["groups"] for p in g_["packages"]}
    for pid in ("moj-law", "moj-order"):
        assert all(not pk[pid].get(k) or pk[pid][k].startswith("http://127.0.0.1:9/") for k in ("url", "alt_url"))
    _req(port, "POST", "/admin/knowledge/api/gov/moj/selection", {"keys": []})
    imported0 = {g_["id"]: g_ for g_ in _req(port, "GET", "/admin/knowledge/api/gov/status")["groups"]}["moj"]["imported_count"]

    t = _Tab(cdp)
    js, wait_for = t.js, t.wait_for

    def click_text(sel, text):
        return js("(() => { const b = [...document.querySelectorAll('" + sel + " button')]"
                  ".find((x) => x.textContent.trim() === " + json.dumps(text) + ");"
                  " if (!b) return false; b.click(); return true; })()")

    pending_js = ("(() => { const n = document.querySelector('" + moj + " .gv-actions .gv-pending');"
                  " return n ? {hidden: n.hidden, text: n.textContent, svg: !!n.querySelector('svg')} : null; })()")
    modal_js = ("(() => { const o = document.querySelector('.modal-overlay:not(.closing)'); if (!o) return null;"
                " return {title: o.querySelector('.modal-title-text').textContent, body: o.querySelector('.modal-body').textContent,"
                " ok: o.querySelector('.modal-ok').textContent.trim(),"
                " cancel: (o.querySelector('.modal-cancel') || {}).textContent}; })()")
    try:
        t.send("Page.navigate", {"url": f"http://127.0.0.1:{port}/admin/knowledge/gov"})
        assert wait_for("document.querySelectorAll('section.gv-card').length === 3")
        p0 = js(pending_js)
        assert p0 is not None and p0["hidden"], ("一項都沒勾時不可以出現提醒", p0)

        assert wait_for("document.querySelectorAll('" + moj + " .gv-items li').length === 4")
        assert click_text(moj, "全選")
        assert wait_for("[...document.querySelectorAll('" + moj + " .gv-items li input')].filter((i) => i.checked).length === 3")
        assert click_text(moj, "儲存選取")
        assert wait_for(modal_js + " !== null", timeout=15), "存好之後沒有提示要再按「下載並匯入」"
        m = _req(port, "GET", "/admin/knowledge/api/gov/status")
        mm = {g_["id"]: g_ for g_ in m["groups"]}["moj"]
        pending = mm["pending_count"]
        assert mm["selected_count"] == 3 and pending >= 1, mm      # 前提：真的有還沒匯入的
        d = js(modal_js)
        assert d["title"] == "已儲存選取的項目", d
        assert "下載並匯入" in d["body"] and f"還有 {pending} 項" in d["body"], d
        assert d["ok"] == "下載並匯入" and d["cancel"].strip() == "稍後再匯入", d
        assert not m["running"], "只是存選取，還不可以開始匯入"

        # 稍後再匯入：不開始，按鈕旁留著提醒（數字跟伺服器一樣）
        js("document.querySelector('.modal-overlay .modal-cancel').click()")
        assert wait_for("!document.querySelector('.modal-overlay')", timeout=5)
        assert wait_for("(() => { const p = " + pending_js + "; return !!p && !p.hidden; })()", timeout=10), js(pending_js)
        p1 = js(pending_js)
        assert f"還有 {pending} 項" in p1["text"] and "下載並匯入" in p1["text"] and p1["svg"], p1
        time.sleep(0.5)
        assert not _req(port, "GET", "/admin/knowledge/api/gov/status")["running"]
        assert {g_["id"]: g_ for g_ in _req(port, "GET", "/admin/knowledge/api/gov/status")["groups"]}["moj"]["imported_count"] == imported0

        # 再存一次、這次按「下載並匯入」：真的開始，匯入完提醒收起來
        assert click_text(moj, "儲存選取")
        assert wait_for(modal_js + " !== null", timeout=15)
        js("document.querySelector('.modal-overlay .modal-ok').click()")
        st = _wait_idle(port)
        mm = {g_["id"]: g_ for g_ in st["groups"]}["moj"]
        assert mm["imported_count"] == imported0 + pending and mm["pending_count"] == 0, mm
        assert wait_for("(() => { const p = " + pending_js + "; return !!p && p.hidden; })()", timeout=20), js(pending_js)
        t.drain()
    finally:
        t.close()
    assert not t.errs, "主控台有錯誤：\n  " + "\n  ".join(t.errs)
