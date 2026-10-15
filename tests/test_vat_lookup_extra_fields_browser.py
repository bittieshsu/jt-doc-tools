"""統編查詢頁：稅籍登記的其他欄位在真的瀏覽器裡畫得出來（v1.16.92）。

卡片與 CSV 都是 JS 組的 —— 只測 API 看不到「欄位名稱打錯，畫面那一格是空的」。
起一個拋棄式實例（資料目錄是暫存的，先匯入杜撰資料），用無頭瀏覽器查一筆分公司、一筆總公司、一筆舊資料。
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

from tests.test_mfp_fetch_page_browser import ROOT, _Tab, _browser, _free_port, _open, _profile_arg

pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")

_LAUNCH = """
import sys
sys.path.insert(0, {root!r})
from tests.test_vat_db_extra_fields import _csv
from app.core import vat_db
vat_db.ingest_csv(_csv(), source="BGMOPEN1")
vat_db.append_csv("統一編號,單位名稱,機關所在縣市\\n56789012,嘉禾市立範例國小,嘉禾市\\n".encode(), source="學校", category="學校")
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port={port}, log_level="warning")
"""


@pytest.fixture(scope="module")
def live():
    data = tempfile.mkdtemp(prefix="vatpage-")
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen([sys.executable, "-c", _LAUNCH.format(root=str(ROOT), port=port)],
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


def _query(t, q):
    t.js(f"document.getElementById('qInput').value = '{q}'; document.getElementById('btnSearch').click(); true")
    # 等「這一次」的結果（上一筆的卡片還在畫面上）；回傳布林，不回 DOM 元素（傳回 Python 會變成空物件）
    done = f"[...document.querySelectorAll('#results .vl-card')].some(c => c.innerText.includes('{q}'))"
    assert t.wait_for(done), (f"查詢 {q} 沒有結果卡片", t.js("document.getElementById('results').innerText"), t.errs)
    return t.js("document.querySelector('#results .vl-card').innerText")


@pytest.mark.parametrize("locale", ["zh-Hant", "en"])
def test_cards_show_the_tax_registration_fields(live, locale):
    port, cdp = live
    base = f"http://localhost:{port}"
    with _open(cdp) as ws:
        t = _Tab(ws)
        for m in ("Runtime.enable", "Log.enable", "Page.enable", "Network.enable"):
            t.send(m)
        t.send("Network.setCookie", {"name": "jtdt_locale", "value": locale, "url": base})
        t.send("Page.navigate", {"url": base + "/tools/vat-lookup/"})
        assert t.wait_for("document.readyState === 'complete' && !!document.getElementById('qInput')")
        time.sleep(0.5)
        branch = _query(t, "23456781")
        hq = _query(t, "12345675")
        school = _query(t, "56789012")
        if locale == "zh-Hant":
            assert "總機構統編" in branch and "12345675" in branch, branch
            assert "0 元" in branch and "2010-12-31" in branch and "否" in branch, branch
            assert "50,000,000 元" in hq and "2015-04-13" in hq and "是" in hq, hq
            assert "總機構統編" not in hq, "總公司自己不顯示總機構統編"
            assert "資本額" not in school and "設立日期" not in school, f"學校沒有這幾項就不顯示：{school}"
        else:
            assert "Head office company ID" in branch and "NT$0" in branch, branch
            assert "NT$50,000,000" in hq and "Established" in hq and "Yes" in hq, hq
        # CSV 匯出多四欄：攔下載，只看產生的內容
        t.js("""window.__csv = null; (() => { const mk = URL.createObjectURL.bind(URL);
                URL.createObjectURL = (b) => { b.text().then(x => { window.__csv = x; }); return mk(b); }; })(); true""")
        t.js("document.getElementById('qInput').value = '範例'; document.getElementById('btnSearch').click(); true")
        assert t.wait_for("!!document.querySelector('[data-act=\"export-csv\"]')"), "搜尋結果沒有匯出按鈕"
        t.js("document.querySelector('[data-act=\"export-csv\"]').click()")
        assert t.wait_for("window.__csv !== null"), "匯出沒有產生內容"
        csv = t.js("window.__csv")
        head = csv.lstrip("﻿").splitlines()[0]
        want = ["資本額", "設立日期", "使用統一發票", "總機構統編"] if locale == "zh-Hant" else \
            ["Capital", "Established", "Issues uniform invoices", "Head office company ID"]
        assert all(w in head for w in want), head
        assert "50000000,2015-04-13,Y," in csv, csv[:400]
        assert not t.errs, t.errs
