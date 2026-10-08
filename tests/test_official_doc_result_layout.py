"""公文撰擬：草稿下面那幾節（檢查結果、版本、匯出）的版面 —— 用瀏覽器量。

2026-10-08 使用者截圖：「檢查結果」「版本」「匯出」只是一串標題、間距不一；版本清單一整列寬、
「看這一版」孤零零地在最右邊；匯出那一區左邊的標題（版面 ／ 版面加註 ／ 下載 ／ 存至工作區）
跟控制項對不齊，八顆下載鈕排成一長條分不出種類。之後又要「版面加註」改成跟「辦公文件轉圖片」
一樣、有圖示的選項卡片（`.option-card`，這裡是複選）。

**判準一律是瀏覽器量出來的位置**，不是 CSS 寫了什麼：

* 寬的時候匯出是一張表：每一列的標題在同一條左緣、控制項在同一條左緣；
  下載與存至工作區的按鈕排進同一組欄位（同一欄左緣相同、寬度相同）；標題不壓到控制項。
* 版本清單只跟內容一樣寬：每一列的動作在同一條左緣，緊接在時間後面，不在整列最右邊。
* 窄（手機）的時候：標題在上、控制項在下；沒有橫向捲軸；卡片、按鈕都不超出所在的卡片；
  選項卡片裡的字沒有被切掉。
* 三種介面語言都量（英 / 日的字比中文長）。
* 函那四項的卡片：勾了才出現那一格；取消勾選＝值清空（送出去的就是「不印」）。

資料在另起的一支服務裡（自己的資料目錄、種一份機關範本，「版面」那一列才會出現），
LLM 指向假的 OpenAI 相容伺服器（`test_official_doc_tool._fake_llm_server`）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests.test_official_doc_tool import LETTER_INPUT, NARRATIVE, ROOT, _fake_llm_server, _free_port

SEED = r'''
import io, sys, zipfile
sys.path.insert(0, ".")
from app.core import official_doc_sources as ods
from tests.test_official_doc_template import SIGN_TPL, LETTER_TPL
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("一般公文表單/簽.odt", SIGN_TPL)
    z.writestr("一般公文表單/函.odt", LETTER_TPL)
ods.install_upload("archives-templates", buf.getvalue(), "templates.zip")
print("seeded")
'''

SIGN_INPUT = {"mode": "sign", "narrative": NARRATIVE, "unit": "資訊室", "addressee": "主任秘書\n局長",
              "closing": "核示", "length": "normal"}


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

    data = tempfile.mkdtemp(prefix="odlayout-e2e-")
    port, cdp, llm_port = _free_port(), _free_port(), _free_port()
    llm = _fake_llm_server(llm_port)
    # **要明寫「認證關閉」** —— 資料庫裡一有使用者，fail-secure 會自動改用本機認證
    (Path(data) / "auth_settings.json").write_text(json.dumps({"backend": "off"}), encoding="utf-8")
    (Path(data) / "llm_settings.json").write_text(json.dumps({
        "enabled": True, "base_url": f"http://127.0.0.1:{llm_port}/v1",
        "model": "fake", "timeout_seconds": 60}), encoding="utf-8")
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    seed = subprocess.run([sys.executable, "-c", SEED], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    assert "seeded" in seed.stdout, seed.stderr[-2000:]
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [br_path, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
         f"--remote-debugging-port={cdp}", "--remote-allow-origins=*", "about:blank"],
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
        req = urllib.request.Request(f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
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

        send("Runtime.enable")
        send("Network.enable")
        base = f"http://127.0.0.1:{port}"
        send("Page.navigate", {"url": f"{base}/tools/official-doc/"})
        assert _until(send, "document.readyState === 'complete' && !!document.getElementById('odGo')", 30)
        jobs = {"sign": _make_case(send, SIGN_INPUT), "letter": _make_case(send, LETTER_INPUT)}
        assert all(len(j or "") == 32 for j in jobs.values()), jobs
        yield base, send, errs, jobs
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        br.terminate()
        srv.terminate()
        llm.shutdown()


def _eval(send, expr):
    r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    return (r.get("result", {}).get("result", {}) or {}).get("value")


def _until(send, expr, secs=60):
    end = time.time() + secs
    while time.time() < end:
        if _eval(send, expr):
            return True
        time.sleep(0.3)
    return False


def _make_case(send, body) -> str | None:
    """在頁面裡送一件（跟畫面同一個端點），等它跑完，再存一個第 2 版（版本清單才有兩列）。"""
    return _eval(send, """(async function(){
      var r = await fetch('/tools/official-doc/start', {method:'POST',
        headers:{'Content-Type':'application/json'}, body: %s});
      var d = await r.json();
      for (var i = 0; i < 300; i++) {
        var j = await (await fetch('/api/jobs/' + d.job_id)).json();
        if (j.status === 'done') break;
        if (j.status === 'error') return null;
        await new Promise(function(r){ setTimeout(r, 300); });
      }
      var res = await (await fetch('/tools/official-doc/result/' + d.case_id)).json();
      await fetch('/tools/official-doc/revisions', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({case_id: d.case_id, text: res.draft.text + '\\n（第二版）', base_rev: 1,
                              source: 'edit'})});
      return d.job_id; })()""" % json.dumps(json.dumps(body, ensure_ascii=False)))


def _open(send, base, jid, locale, width):
    send("Network.setCookie", {"name": "jtdt_locale", "value": locale, "url": base})
    send("Emulation.setDeviceMetricsOverride",
         {"width": width, "height": 1000, "deviceScaleFactor": 1, "mobile": width < 600})
    send("Page.navigate", {"url": f"{base}/tools/official-doc/?job={jid}"})
    assert _until(send, "!!document.getElementById('odResult') && !document.getElementById('odResult').hidden && "
                        "document.querySelectorAll('#odRevs li').length >= 2 && "
                        "!document.getElementById('odTplRow').hidden", 30), (locale, width, "結果沒有出來")
    time.sleep(0.4)


#: 量版面：回傳 Python 這邊要判斷的幾何
_MEASURE = r"""(function(){
  function R(e){ var r = e.getBoundingClientRect(); return {l:r.left, r:r.right, t:r.top, b:r.bottom, w:r.width, h:r.height}; }
  function vis(e){ return !!e && e.getClientRects().length > 0; }
  var out = {sw: document.documentElement.scrollWidth, iw: innerWidth, boxes: [], over: [], clipped: []};
  // 每一張卡片（檢查結果、版本、匯出）：裡面看得到的東西都不可以超出卡片
  document.querySelectorAll('#odResultBody > .od-box').forEach(function(box){
    if (!vis(box)) return;
    var b = R(box);
    out.boxes.push(b);
    box.querySelectorAll('*').forEach(function(e){
      // 本站下拉把原生的 <select> 藏在畫面外（-9999px），那個不算
      if (!vis(e) || e.closest('.jt-select-panel') || e.classList.contains('jt-select-native')) return;
      var r = R(e);
      if (r.w === 0 || r.h === 0) return;
      if (r.r > b.r + 0.5 || r.l < b.l - 0.5)
        out.over.push([e.tagName, e.id, String(e.className).slice(0, 40), Math.round(r.l), Math.round(r.r), Math.round(b.r)]);
    });
  });
  // 選項卡片：字沒有被切掉、卡片沒有超出那一格格子
  var grid = document.querySelector('.od-extra-cards');
  out.cards = Array.from(document.querySelectorAll('.od-extra-cards .option-card')).filter(vis).map(function(c){
    if (c.scrollWidth > c.clientWidth + 1) out.clipped.push(c.textContent.trim().slice(0, 20));
    return R(c);
  });
  out.cardsGrid = R(grid);
  // 匯出那張表
  out.labs = Array.from(document.querySelectorAll('#odExportSec .od-xp-lab')).filter(vis).map(function(e){
    return Object.assign(R(e), {txt: e.textContent.trim()}); });
  out.ctls = Array.from(document.querySelectorAll('#odExportSec .od-xp-row > .od-xp-ctl, #odExportSec .od-xp > .od-xp-ctl'))
    .filter(vis).map(R);
  out.knames = Array.from(document.querySelectorAll('#odExportSec .od-xp-kname')).filter(vis).map(R);
  // 格式那一行在寬的時候是 `display:contents`（沒有自己的方框）—— 用「不在藏起來的那一列裡」判斷
  out.kinds = Array.from(document.querySelectorAll('#odExportSec .od-xp-kind')).filter(function(k){
    return !k.closest('[hidden]'); }).map(function(k){
    return Array.from(k.querySelectorAll('.btn')).map(R); });
  // 每一列的標題與它右邊的第一個東西（控制項或格式名稱）
  out.pairs = Array.from(document.querySelectorAll('#odExportSec .od-xp-row')).filter(function(r){
    return !r.hidden; }).map(function(row){
      var lab = row.querySelector('.od-xp-lab');
      var nxt = row.querySelector('.od-xp-ctl, .od-xp-kname');
      return {lab: R(lab), next: R(nxt)}; });
  // 「下載」與「存至工作區」之間的分隔線：在兩列中間，而且看得到
  var ws = document.getElementById('odWsRow'), sep = document.getElementById('odWsSep');
  if (ws && !ws.hidden) {
    out.wsSep = {vis: vis(sep), box: sep ? R(sep) : null,
      dlBottom: Math.max.apply(null, Array.from(document.querySelectorAll('#odDlRow .btn')).filter(vis).map(function(e){ return R(e).b; })),
      wsTop: R(ws.querySelector('.od-xp-lab')).t,
      border: sep ? getComputedStyle(sep).borderTopWidth : ''};
  }
  // 版本清單
  var revs = document.getElementById('odRevs');
  out.revs = R(revs);
  out.revRows = Array.from(revs.querySelectorAll('.od-rev')).map(function(li){
    return {sub: R(li.querySelector('.od-rev-sub')), act: R(li.querySelector('.od-rev-act')),
            btn: R(li.querySelector('.od-rev-act .btn'))}; });
  out.revBody = R(revs.parentElement);
  return out;
})()"""


def _check_common(g, where):
    assert g["sw"] <= g["iw"] + 1, (where, "橫向捲軸", g["sw"], g["iw"])
    assert len(g["boxes"]) >= 3, (where, "檢查結果 / 版本 / 匯出不是三張卡片", g["boxes"])
    assert not g["over"], (where, "有東西超出所在的卡片", g["over"][:8])
    assert not g["clipped"], (where, "選項卡片裡的字被切掉", g["clipped"])
    grid = g["cardsGrid"]
    assert len(g["cards"]) >= 3, (where, "版面加註沒有畫成選項卡片", g["cards"])
    for c in g["cards"]:
        assert c["r"] <= grid["r"] + 0.5 and c["l"] >= grid["l"] - 0.5, (where, "選項卡片超出格子", c, grid)
    w = g.get("wsSep")
    assert w, (where, "存至工作區那一列沒有出來，量不到分隔線")
    assert w["vis"] and w["border"] not in ("", "0px"), (where, "下載與存至工作區之間沒有分隔線", w)
    assert w["dlBottom"] <= w["box"]["t"] + 0.5 and w["box"]["b"] <= w["wsTop"] + 0.5, \
        (where, "分隔線不在下載與存至工作區中間", w)
    # 版本清單不超出卡片的內容區
    assert g["revs"]["r"] <= g["revBody"]["r"] + 0.5, (where, g["revs"], g["revBody"])


def _check_wide(g, where):
    # 每一列的標題同一條左緣、標題不壓到右邊的東西
    labs = g["labs"]
    assert len(labs) >= 4, (where, "標題少了（版面 / 版面加註 / 下載 / 存至工作區）", [x["txt"] for x in labs])
    assert max(x["l"] for x in labs) - min(x["l"] for x in labs) <= 1, (where, "標題左緣沒對齊", labs)
    for p in g["pairs"]:
        assert p["lab"]["r"] <= p["next"]["l"] + 0.5, (where, "標題壓到控制項", p)
        assert abs(p["lab"]["t"] - p["next"]["t"]) <= 12, (where, "標題跟控制項不在同一列", p)
    # 控制項（版面、版面加註）與格式名稱（文件 / 圖片 / 文字）在同一條左緣
    lefts = [c["l"] for c in g["ctls"]] + [k["l"] for k in g["knames"]]
    assert max(lefts) - min(lefts) <= 1, (where, "控制項沒有對齊同一條線", lefts)
    # 按鈕排成欄：同一欄左緣、寬度相同；同一個格式的按鈕在同一列
    kinds = g["kinds"]
    assert len(kinds) >= 4 and [len(k) for k in kinds[:3]] == [3, 2, 3], (where, kinds)
    for i in range(3):
        col = [k[i] for k in kinds if len(k) > i]
        assert max(c["l"] for c in col) - min(c["l"] for c in col) <= 1, (where, f"第 {i + 1} 欄按鈕沒對齊", col)
        assert max(c["w"] for c in col) - min(c["w"] for c in col) <= 1, (where, f"第 {i + 1} 欄按鈕寬度不同", col)
    for k in kinds:
        assert max(b["t"] for b in k) - min(b["t"] for b in k) <= 1, (where, "同一個格式的按鈕折行了", k)
    # 版本清單：動作那一欄對齊，而且緊接在時間後面（不是整列最右邊）
    rows = g["revRows"]
    assert len(rows) >= 2
    assert max(r["act"]["l"] for r in rows) - min(r["act"]["l"] for r in rows) <= 1, (where, "動作沒對齊", rows)
    for r in rows:
        assert abs(r["sub"]["t"] - r["btn"]["t"]) <= 14, (where, "時間與動作不在同一行", r)
        assert r["act"]["l"] - r["sub"]["r"] <= 48, (where, "「看這一版」離時間太遠", r)
    assert g["revs"]["r"] - max(r["act"]["r"] for r in rows) <= 24, (where, "清單比內容寬一大截", g["revs"], rows)


def _check_narrow(g, where):
    # 標題在上、控制項在下
    for p in g["pairs"]:
        assert p["lab"]["b"] <= p["next"]["t"] + 0.5, (where, "手機上標題沒有放在控制項上面", p)
    # 一列至少兩張選項卡片（一張一列要捲很久）
    tops = sorted({round(c["t"]) for c in g["cards"]})
    assert len(tops) < len(g["cards"]), (where, "手機上選項卡片一張一列", g["cards"])


@pytest.mark.parametrize("locale", ["zh-Hant", "en", "ja"])
def test_result_cards_and_export_table_line_up(live, locale):
    base, send, errs, jobs = live
    for mode in ("sign", "letter"):
        for width in (1920, 1280, 390):
            _open(send, base, jobs[mode], locale, width)
            if mode == "letter":
                # 函：四張加註卡片都勾起來（下面的那幾格都出現）再量
                _eval(send, "['odCopyMarkOn','odSendMethodOn','odDelegateOn','odRecvAddrOn'].forEach(function(id){"
                            "var c=document.getElementById(id); if (!c.checked) c.click(); }), 1")
                time.sleep(0.3)
            g = _eval(send, _MEASURE)
            where = (locale, mode, width)
            _check_common(g, where)
            if width >= 1280:
                _check_wide(g, where)
            else:
                _check_narrow(g, where)
    send("Emulation.clearDeviceMetricsOverride")
    assert not errs, errs


def test_letter_extra_cards_show_their_field_and_clear_it(live):
    """函那四項是卡片：勾了才出現那一格（下拉帶入第一個選項）；取消勾選＝值清空、那一格收起來，
    記住的選擇也跟著改 —— 送給伺服器的就是原本那幾個欄位，空值＝不印。"""
    base, send, errs, jobs = live
    _open(send, base, jobs["letter"], "zh-Hant", 1280)
    _eval(send, "localStorage.removeItem('jtdt.officialDoc.extras'), 1")
    _open(send, base, jobs["letter"], "zh-Hant", 1280)
    assert _eval(send, "!document.getElementById('odExtrasLetter').hidden"), "函的加註卡片沒有出現"
    for cb, field, box in (("odCopyMarkOn", "odCopyMark", "odCopyMarkF"),
                           ("odSendMethodOn", "odSendMethod", "odSendMethodF")):
        assert _eval(send, f"!document.getElementById('{cb}').checked && document.getElementById('{box}').hidden")
        _eval(send, f"document.getElementById('{cb}').click(), 1")
        assert _eval(send, f"!document.getElementById('{box}').hidden"), f"勾了 {cb}，那一格沒有出現"
        v = _eval(send, f"document.getElementById('{field}').value")
        assert v, f"勾了 {cb}，下拉還是「不標示」"
        _eval(send, f"document.getElementById('{cb}').click(), 1")
        assert _eval(send, f"document.getElementById('{box}').hidden && document.getElementById('{field}').value === ''"), \
            f"取消勾選 {cb}，值沒有清空（匯出照樣會印）"
    # 文字欄位：勾了出現、打字；取消勾選清空，記住的選擇也清掉
    _eval(send, "document.getElementById('odDelegateOn').click(), 1")
    assert _eval(send, "!document.getElementById('odDelegateF').hidden && "
                       "document.activeElement === document.getElementById('odDelegate')"), "勾了分層負責，沒有跳到那一格"
    _eval(send, "(function(){var f=document.getElementById('odDelegate'); f.value='業務主管';"
                "f.dispatchEvent(new Event('input')); return 1;})()")
    assert json.loads(_eval(send, "localStorage.getItem('jtdt.officialDoc.extras')"))["delegate"] == "業務主管"
    _eval(send, "document.getElementById('odDelegateOn').click(), 1")
    assert _eval(send, "document.getElementById('odDelegateF').hidden && document.getElementById('odDelegate').value === ''")
    assert json.loads(_eval(send, "localStorage.getItem('jtdt.officialDoc.extras')"))["delegate"] == ""
    # 下拉選回「不標示」：卡片跟著取消勾選
    _eval(send, "document.getElementById('odCopyMarkOn').click(), 1")
    _eval(send, "(function(){var s=document.getElementById('odCopyMark'); s.value='';"
                "s.dispatchEvent(new Event('change')); return 1;})()")
    assert _eval(send, "!document.getElementById('odCopyMarkOn').checked && document.getElementById('odCopyMarkF').hidden")
    # 記住的值放回來時，有值的那一項卡片是勾著的
    _eval(send, "localStorage.setItem('jtdt.officialDoc.extras', JSON.stringify({page_numbers:true, binding_line:false,"
                "copy_mark:'副本', send_method:'', delegate:''})), 1")
    _open(send, base, jobs["letter"], "zh-Hant", 1280)
    assert _eval(send, "document.getElementById('odCopyMarkOn').checked && !document.getElementById('odCopyMarkF').hidden "
                       "&& document.getElementById('odCopyMark').value === '副本' && "
                       "!document.getElementById('odSendMethodOn').checked")
    # 共用的卡片樣式（platform.css）對勾選框也生效：框本身藏起來、勾著的那張看得出來（邊框顏色不同）
    st = _eval(send, """(function(){
      var on = document.getElementById('odPageNo'), off = document.getElementById('odBinding');
      on.checked = true; off.checked = false;
      var a = getComputedStyle(on.closest('.option-card')).borderTopColor,
          b = getComputedStyle(off.closest('.option-card')).borderTopColor;
      return {hidden: getComputedStyle(off).opacity === '0' && getComputedStyle(off).position === 'absolute',
              differs: a !== b};})()""")
    assert st == {"hidden": True, "differs": True}, st
    # 卡片是真的勾選框：Tab 走得到（焦點在勾選框上）
    assert _eval(send, "(function(){var c=document.getElementById('odBinding'); c.focus();"
                       "return document.activeElement === c;})()")
    _eval(send, "localStorage.removeItem('jtdt.officialDoc.extras'), 1")
    send("Emulation.clearDeviceMetricsOverride")
    assert not errs, errs


def test_the_live_preview_tip_takes_you_to_the_preview(live):
    """版面加註下面要講清楚「按了會即時預覽、預覽在上面」（2026-10-08 使用者：「明顯一點」）：
    提示框看得到，按「往上看預覽」之後預覽圖真的在畫面裡、而且閃一下框線。"""
    base, send, errs, jobs = live
    _open(send, base, jobs["letter"], "zh-Hant", 1280)
    tip = _eval(send, """(function(){var t=document.getElementById('odLiveTip');
      var r=t.getBoundingClientRect();
      return {vis: r.width > 0 && r.height > 0, txt: t.textContent,
              after: !!(document.getElementById('odExtras').compareDocumentPosition(t) & 4) ||
                     document.getElementById('odExtras').contains(t)};})()""")
    assert tip["vis"] and "即時預覽" in tip["txt"] and tip["after"], tip
    # 先捲到最下面，預覽圖不在畫面裡
    _eval(send, "window.scrollTo(0, document.body.scrollHeight), 1")
    time.sleep(0.3)
    assert not _eval(send, "(function(){var r=document.getElementById('odPvBox').getBoundingClientRect();"
                           "return r.bottom > 0 && r.top < innerHeight;})()"), "前提不成立：預覽圖本來就在畫面裡"
    _eval(send, "document.getElementById('odLiveGo').click(), 1")
    assert _eval(send, "document.getElementById('odPvBox').classList.contains('od-pv-flash')"), "沒有閃框線"
    assert _until(send, "(function(){var r=document.getElementById('odPvBox').getBoundingClientRect();"
                        "return r.top >= 0 && r.top < innerHeight;})()", 5), "按了之後預覽圖沒有捲進畫面"
    send("Emulation.clearDeviceMetricsOverride")
    assert not errs, errs
