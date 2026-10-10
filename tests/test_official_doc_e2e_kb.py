"""公文撰擬：知識庫、機關範本、機關名稱建議 —— 真的在瀏覽器裡跑一次。

單元與端點測試驗得到伺服器那一側；這支驗的是**畫面的接線**：勾選框真的送出 `use_kb`、
參考資料那一節真的畫出來、範本下拉只列這個文別能套的、受文者打字真的出現建議、
選了範本匯出時伺服器真的套上了。這一類 bug 在本專案一律長成「元素都在、沒有例外、
按了沒反應」，只有真的跑一次 JS 才看得到。

資料是在**另一個資料目錄**裡種的（另起一支服務），不碰開發機與測試共用的資料庫。
LLM 指向一支假的 OpenAI 相容伺服器（`test_official_doc_tool._fake_llm_server`）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests.test_official_doc_tool import NARRATIVE, ROOT, _fake_llm_server, _free_port

KB_TEXT = ("資訊設備汰換作業要點\n"
           "一、資訊室印表機等資訊設備使用年限屆滿、經常故障或零件停產者，得辦理汰換。\n"
           "二、汰換採購金額未達公告金額者，依政府採購法第22條第1項第7款規定辦理。\n")
BOOK = [{"orgId": "Q1000000", "orgName": "嘉禾市政府", "statusCode": "T"},
        {"orgId": "Q20000000B", "orgName": "嘉禾市東湖區公所", "statusCode": "T"}]

SEED = r'''
import hashlib, io, json, sys, zipfile
sys.path.insert(0, ".")
from app.core.kb import indexer, store
from app.core import official_doc_sources as ods
from tests.test_official_doc_template import SIGN_TPL, LETTER_TPL

ds = store.create_dataset({"name": "資訊設備採購規定", "category": "business_law"})
data = sys.argv[1].encode("utf-8")
v = store.create_version(ds["id"], title="資訊設備汰換作業要點", filename="x.txt", ext=".txt",
                         data=data, sha256=hashlib.sha256(data).hexdigest(), meta={})
assert indexer.process_version(v["id"]).get("ok")
store.transition(v["id"], allowed_from=("ready",), to="active", activated_by="seed")

# 一部法規（政府公開資料匯入的格式：一條一段、帶條號與章節）。名稱是編的
from app.core.kb import law_text
law = ("# 範例設備管理法\n- 法規位階：法律\n---\n## 第 四 章 財物管理\n### 第 59 條\n"
       "資訊室印表機等資訊設備使用年限屆滿、零件停產者，得以設備費汰換。\n")
ds2 = store.create_dataset({"name": "範例設備管理法", "category": "business_law"})
d2 = law.encode("utf-8")
v2 = store.create_version(ds2["id"], title="範例設備管理法", filename="law.md", ext=".md",
                          data=d2, sha256=hashlib.sha256(d2).hexdigest(),
                          meta={"version_label": "民國114年1月1日修正"})
store.put_gov_version(v2["id"], group_id="moj-law", item_key="X0000001", fmt=law_text.FORMAT_LAW,
                      modified_on="20250101", license="政府資料開放授權條款第1版",
                      attribution="資料來源：範例法規資料庫（示範出處）。")
assert indexer.process_version(v2["id"]).get("ok")
store.transition(v2["id"], allowed_from=("ready",), to="active", activated_by="seed")

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("一般公文表單/簽.odt", SIGN_TPL)
    z.writestr("一般公文表單/函.odt", LETTER_TPL)
    z.writestr("一般公文表單/書函.odt", LETTER_TPL)
ods.install_upload("archives-templates", buf.getvalue(), "templates.zip")
ods.install_upload("archives-address-book", sys.argv[2].encode("utf-8"), "book.json")
print("seeded")
'''


@pytest.fixture(scope="module")
def live():
    import os
    import subprocess
    import sys
    import tempfile
    import urllib.request

    sys.path.insert(0, str(ROOT))
    from tools import browser_probe

    br_path = browser_probe.browser()
    if not br_path:
        pytest.skip("沒有 chromium")
    try:
        import websockets.sync.client as wsc
    except ImportError:
        pytest.skip("沒有 websockets")

    data = tempfile.mkdtemp(prefix="odkb-e2e-")
    port, cdp, llm_port = _free_port(), _free_port(), _free_port()
    llm = _fake_llm_server(llm_port)
    (Path(data) / "auth_settings.json").write_text(json.dumps({"backend": "off"}),
                                                   encoding="utf-8")
    (Path(data) / "llm_settings.json").write_text(json.dumps({
        "enabled": True, "base_url": f"http://127.0.0.1:{llm_port}/v1",
        "model": "fake", "timeout_seconds": 60}), encoding="utf-8")
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    seed = subprocess.run([sys.executable, "-c", SEED, KB_TEXT,
                           json.dumps(BOOK, ensure_ascii=False)],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert "seeded" in seed.stdout, seed.stderr[-2000:]

    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [br_path, "--headless=new", "--no-sandbox", "--disable-gpu",
         browser_probe.profile_arg(), f"--remote-debugging-port={cdp}", "--remote-allow-origins=*", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        req = urllib.request.Request(f"http://127.0.0.1:{cdp}/json/new?about:blank",
                                     method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            tab = json.loads(r.read())
        ws = wsc.connect(tab["webSocketDebuggerUrl"], max_size=None, open_timeout=10)
        n = [0]
        errs: list[str] = []

        def send(method, params=None):
            n[0] += 1
            i = n[0]
            ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
            while True:
                m = json.loads(ws.recv(timeout=180))
                if m.get("method") == "Runtime.exceptionThrown":
                    d = m["params"]["exceptionDetails"]
                    errs.append((d.get("exception", {}).get("description")
                                 or d.get("text", "?"))[:300])
                if m.get("id") == i:
                    return m

        yield port, send, errs
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        for p in (br, srv):            # 只收自己起的那兩支（不用名字批次殺）
            p.terminate()
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()
        llm.shutdown()


def _js(send, expr: str):
    r = send("Runtime.evaluate", {"expression": expr, "awaitPromise": True,
                                  "returnByValue": True})
    res = r.get("result", {})
    if "exceptionDetails" in res:
        raise AssertionError(res["exceptionDetails"])
    return res.get("result", {}).get("value")


def _wait(send, expr: str, timeout: float = 60.0):
    end = time.time() + timeout
    while time.time() < end:
        v = _js(send, expr)
        if v:
            return v
        time.sleep(0.3)
    return None


def _open(send, port: int) -> None:
    send("Runtime.enable")
    send("Page.enable")
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/official-doc/"})
    # 要等整頁載完（腳本都跑過）才點 —— 只等元素出現的話，頁面還在載入就點下去（v1.16.63 多了三支腳本，空檔變長）
    assert _wait(send, "document.readyState === 'complete' && !!document.getElementById('odGo')"), "頁面沒有載入"


def test_kb_checkbox_sends_use_kb_and_the_references_show_up(live):
    port, send, errs = live
    _open(send, port)
    assert _js(send, "!!document.getElementById('odUseKb')"), "看得到資料集時勾選框要出現"
    _js(send, f"""(function () {{
        var ta = document.getElementById('odNarrative');
        ta.value = {json.dumps(NARRATIVE)};
        ta.dispatchEvent(new Event('input'));
        document.getElementById('odUseKb').checked = true;
        document.getElementById('odGo').click();
        return true; }})()""")
    shown = _wait(send, "(function(){var s=document.getElementById('odRefSec');"
                        "return s && !s.hidden && document.querySelectorAll('#odRefs li').length;})()",
                  timeout=90)
    assert shown, "勾了參考知識庫，結果要有「參考資料」那一節"
    text = _js(send, "document.getElementById('odRefs').textContent")
    assert "資訊設備汰換作業要點" in text and "業務依據" in text, text
    # 用途標籤跟知識庫管理頁同一份（不是另寫一份）
    from app.core.kb import store
    assert store.PURPOSES["substantive_basis"] in text
    _check_law_card(send)
    assert not errs, errs


def _check_law_card(send):
    """參考資料卡片（2026-10-09 使用者：「裡面文字很多」「他有找到法條 那可以幹嘛? 我這樣看不出來」）：
    法規名稱旁邊標條號、章節只寫一次（不帶法規名稱）、條文只露開頭、出處在最下面寫一次；
    下面有可以做的事：草稿寫到這部法規時講出第幾行並可以標出來、複製「名稱＋條號」、看全文。"""
    card = _js(send, """(function () {
        var li = Array.from(document.querySelectorAll('#odRefs .od-ref')).find(function (x) {
            return x.querySelector('.od-ref-title').textContent === '範例設備管理法'; });
        if (!li) return null;
        var ex = li.querySelector('.od-ref-excerpt');
        return {art: (li.querySelector('.od-ref-art') || {}).textContent || '',
                where: (li.querySelector('.od-ref-where') || {}).textContent || '',
                exLines: Math.round(ex.getBoundingClientRect().height / parseFloat(getComputedStyle(ex).lineHeight)),
                full: li.querySelector('.od-ref-text').hidden,
                acts: Array.from(li.querySelectorAll('.od-ref-acts button')).filter(function (b) {
                    return !b.hidden; }).map(function (b) { return b.dataset.act; }),
                attrInCard: li.textContent.indexOf('示範出處') >= 0,
                attrs: document.getElementById('odRefAttrs').hidden ? '' : document.getElementById('odRefAttrs').textContent,
                legend: document.getElementById('odRefLegend').textContent}; })()""")
    assert card, "沒有那一部法規的卡片（檢索沒查到？）"
    assert card["art"] == "第59條", card
    assert card["where"] == "第四章 財物管理・民國114年1月1日修正", card
    assert card["exLines"] <= 2 and card["full"], card
    assert not card["attrInCard"] and card["attrs"].count("示範出處") == 1, card
    assert "可以當草稿的依據" in card["legend"], card["legend"]
    assert "copy" in card["acts"] and "full" in card["acts"] and "show" not in card["acts"], card
    import os
    if os.environ.get("JTDT_REF_SHOT"):
        import base64
        send("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 900, "deviceScaleFactor": 1,
                                                    "mobile": False})
        _js(send, "document.getElementById('odDraft').value += '\\n依範例設備管理法第59條規定辦理。';"
                  "document.getElementById('odDraft').dispatchEvent(new Event('input'));"
                  "document.getElementById('odRefSec').scrollIntoView(); true")
        time.sleep(0.8)
        r = send("Page.captureScreenshot", {"format": "png"})
        open(os.environ["JTDT_REF_SHOT"], "wb").write(base64.b64decode(r["result"]["data"]))
        _js(send, "var ta=document.getElementById('odDraft'); ta.value = ta.value.replace("
                  "'\\n依範例設備管理法第59條規定辦理。', ''); ta.dispatchEvent(new Event('input')); true")
        time.sleep(0.5)
    # 草稿寫到這部法規 → 講出第幾行、可以在草稿中標出來
    _js(send, """(function () { var ta = document.getElementById('odDraft');
        ta.value = ta.value + '\\n依範例設備管理法第59條規定辦理。';
        ta.dispatchEvent(new Event('input')); return true; })()""")
    found = _wait(send, """(function () {
        var li = Array.from(document.querySelectorAll('#odRefs .od-ref')).find(function (x) {
            return x.querySelector('.od-ref-title').textContent === '範例設備管理法'; });
        var f = li.querySelector('.od-ref-found');
        return !f.hidden && f.textContent; })()""", timeout=10)
    lines = _js(send, "document.getElementById('odDraft').value.split('\\n').length")
    assert found == f"草稿第 {lines} 行寫到這部法規", (found, lines)
    _js(send, """(function () {
        var li = Array.from(document.querySelectorAll('#odRefs .od-ref')).find(function (x) {
            return x.querySelector('.od-ref-title').textContent === '範例設備管理法'; });
        li.querySelector('button[data-act="show"]').click(); return true; })()""")
    sel = _js(send, """(function () { var ta = document.getElementById('odDraft');
        return ta.value.slice(ta.selectionStart, ta.selectionEnd); })()""")
    assert sel == "範例設備管理法第59條", sel
    # 看全文 → 攤開、開頭那兩行收起來
    _js(send, """(function () {
        var li = Array.from(document.querySelectorAll('#odRefs .od-ref')).find(function (x) {
            return x.querySelector('.od-ref-title').textContent === '範例設備管理法'; });
        li.querySelector('button[data-act="full"]').click(); return true; })()""")
    assert _js(send, """(function () {
        var li = Array.from(document.querySelectorAll('#odRefs .od-ref')).find(function (x) {
            return x.querySelector('.od-ref-title').textContent === '範例設備管理法'; });
        return !li.querySelector('.od-ref-text').hidden &&
               getComputedStyle(li.querySelector('.od-ref-excerpt')).display === 'none'; })()""")
    # 改回原本的草稿，不影響後面的測試
    _js(send, """(function () { var ta = document.getElementById('odDraft');
        ta.value = ta.value.replace('\\n依範例設備管理法第59條規定辦理。', '');
        ta.dispatchEvent(new Event('input')); return true; })()""")


def test_template_select_lists_only_what_fits_and_export_applies_it(live):
    port, send, errs = live
    # 上一條產生的那份是「簽」：下拉只列「簽」那一份（書函、函不列）
    assert _wait(send, "!document.getElementById('odTplRow').hidden"), "簽要有可以套的範本"
    labels = _js(send, "Array.from(document.querySelectorAll('#odTpl option'))"
                       ".map(function(o){return o.textContent})")
    assert len(labels) == 2 and labels[1].startswith("簽"), labels
    assert not any("函" in x for x in labels), labels
    key = _js(send, "document.querySelectorAll('#odTpl option')[1].value")
    # 走畫面上的下載鈕那條路（案件編號在頁面的閉包裡）：攔下那一次匯出請求的回應標頭
    header = _js(send, f"""(async function () {{
        document.getElementById('odTpl').value = {json.dumps(key)};
        var orig = window.fetch, seen = '';
        window.fetch = async function (u, o) {{
            var r = await orig(u, o);
            if (String(u).indexOf('/export') >= 0) {{
                seen = r.status + ' ' + (r.headers.get('X-Jtdt-Template') || '');
            }}
            return r; }};
        document.querySelector('[data-od-dl="odt"]').click();
        for (var i = 0; i < 150 && !seen; i++) await new Promise(function (r) {{ setTimeout(r, 100); }});
        window.fetch = orig;
        return seen; }})()""")
    assert header == "200 applied", header
    assert not errs, errs


def test_receiver_suggestions_come_from_the_address_book(live):
    port, send, errs = live
    _open(send, port)
    _js(send, """(function () {
        document.querySelector('input[name="odMode"][value="letter"]').click();
        var r = document.getElementById('odReceiver');
        r.value = '嘉禾';
        r.dispatchEvent(new Event('input'));
        return true; })()""")
    # 本站樣式的清單（v1.16.68，`org_picker.js`）：每列名稱＋機關代碼；原生 datalist 已經拿掉
    names = _wait(send, "Array.from(document.querySelectorAll('#odReceiverOrgList .op-name'))"
                        ".map(function(o){return o.textContent}).join('|')", timeout=10)
    assert names and "嘉禾市東湖區公所" in names, names
    assert not _js(send, "document.getElementById('odReceiver').getAttribute('list')")
    assert "政府資料開放授權條款" in _js(send, "document.getElementById('odLetter').textContent")
    assert not errs, errs
