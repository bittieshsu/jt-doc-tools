"""左側選單配色與內建 Logo —— 真的在瀏覽器裡量（2026-10-11 加入）。

判準：

* 12 種配色逐一套上，**實量**側欄裡每一種文字的顏色與它底下的背景（側欄的頂端顏色疊上該元件自己的半透明底），
  算 WCAG 對比：
  - 預設以外的 11 種：站台名稱、分類列、工具名稱、工具說明、版本、通知列與帳號列（含 `@ldap` 那段登入來源）、
    分類旁的數字、滑過的工具、目前所在的那一格都要 4.5:1 以上，搜尋框的提示字 3:1 以上；
  - 浮動捲軸（非文字）：滑過 / 拖曳時 3:1 以上，而且要比平常**更**明顯（淺色配色原本是反過來變淡）；
  - 預設（靛紫）**跟改版前一模一樣**（背景、文字顏色逐一比對原本寫死的值）。
* 設定頁點一張配色卡：不重新整理，側欄當場換色、`<html data-sb-theme>` 跟著換；重新整理之後照舊。
  淺色背景時 Jason Tools Logo 換成白底用的那一版。
* 每張配色卡都有預覽（迷你側欄的背景就是那一種配色）、名稱、一句說明。
  漸層、純色各一排 6 張（桌面寬度）；手機寬度兩欄，每一排都排滿。
* 內建 Logo 選「文件圖示」：側欄、頁籤圖示都換成 `logo-doc.png`，而且那張圖真的載得到。
* 手機寬度：設定頁沒有橫向捲動。
* 套用失敗（伺服器回錯誤、連不上）：畫面維持原本的選擇、顯示錯誤，存下來的設定也沒變。
* 快速連點兩張：最後停在最後點的那一張，畫面跟存下來的一致。
* 英文、日文介面：卡片一樣每組排滿、名稱與說明不會被切掉或撐出卡片。
"""
from __future__ import annotations

import json
import re
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def live():
    """全新的實例（認證關閉）＋ 開著遠端除錯的瀏覽器。回 (網址開頭, send, 例外清單)。"""
    sys.path.insert(0, str(ROOT))
    from tools import browser_probe
    br_path = browser_probe.browser()
    if not br_path or not browser_probe.browser_runs(br_path):
        pytest.skip("沒有可用的瀏覽器")
    try:
        import websockets.sync.client as wsc
    except ImportError:
        pytest.skip("沒有 websockets")
    data = tempfile.mkdtemp(prefix="sbtheme-")
    Path(data, "auth_settings.json").write_text(json.dumps({"backend": "off"}), encoding="utf-8")
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data}
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                            "--port", str(port), "--log-level", "warning"],
                           cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [br_path, "--headless=new", "--no-sandbox", "--disable-gpu", browser_probe.profile_arg(),
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
                m = json.loads(ws.recv(timeout=120))
                if m.get("method") == "Runtime.exceptionThrown":
                    d = m["params"]["exceptionDetails"]
                    errs.append((d.get("exception", {}).get("description") or d.get("text", "?"))[:300])
                if m.get("id") == i:
                    return m

        send("Runtime.enable")
        send("Page.enable")
        send("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 1000, "deviceScaleFactor": 1,
                                                    "mobile": False})
        yield f"http://127.0.0.1:{port}", send, errs
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        for p in (br, srv):            # 只收自己起的那兩支
            p.terminate()
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()
        shutil.rmtree(data, ignore_errors=True)


def js(send, expr: str):
    r = send("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True})
    res = r.get("result", {})
    if "exceptionDetails" in res:
        raise AssertionError(res["exceptionDetails"])
    return res.get("result", {}).get("value")


def wait(send, expr: str, timeout: float = 20.0):
    end = time.time() + timeout
    while time.time() < end:
        v = js(send, expr)
        if v:
            return v
        time.sleep(0.1)
    return None


def open_page(send, url: str, ready: str) -> None:
    send("Page.navigate", {"url": url})
    assert wait(send, f"document.readyState === 'complete' && !!document.querySelector({json.dumps(ready)})"), url


#: 在瀏覽器裡算對比：元件的文字顏色（含 opacity）對「側欄頂端或底端的顏色＋元件自己的半透明底」。
#: 換配色後先等轉場（`.sb-item` 的底色有 0.12 秒轉場）跑完再量，不然量到的是上一種配色的底。
#: 滑過的那一格由 `force_hover` 用 DevTools 強制 `:hover`（標了 `data-hover-probe`）。
_MEASURE = r"""(async function () {
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  await sleep(300);
  function parse(c) {
    var m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null;
    var p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number);
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
  }
  function hex(h) { h = h.trim().replace('#', ''); return [parseInt(h.substr(0,2),16), parseInt(h.substr(2,2),16), parseInt(h.substr(4,2),16), 1]; }
  function over(fg, bg) { var a = fg[3]; return [fg[0]*a + bg[0]*(1-a), fg[1]*a + bg[1]*(1-a), fg[2]*a + bg[2]*(1-a), 1]; }
  function lum(c) { var f = function (v) { v /= 255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); };
    return 0.2126*f(c[0]) + 0.7152*f(c[1]) + 0.0722*f(c[2]); }
  function ratio(a, b) { var x = lum(a), y = lum(b); return (Math.max(x,y)+0.05) / (Math.min(x,y)+0.05); }
  function r2(v) { return Math.round(v * 100) / 100; }
  var root = getComputedStyle(document.documentElement);
  var top = hex(root.getPropertyValue('--sb-bg-top')), bottom = hex(root.getPropertyValue('--sb-bg-bottom'));
  function text(el, base, own) {
    var cs = getComputedStyle(el), fg = parse(cs.color), op = 1;
    for (var e = el; e && e !== document.body; e = e.parentElement) op *= parseFloat(getComputedStyle(e).opacity);
    fg[3] *= op;
    var bg = base;
    (own || []).forEach(function (e) { var b = parse(getComputedStyle(e).backgroundColor); if (b && b[3] > 0) bg = over(b, bg); });
    return r2(ratio(over(fg, bg), bg));
  }
  var q = function (s) { return document.querySelector(s); };
  var item = q('.sidebar .sb-item:not(.active):not([data-hover-probe])'), group = q('.sidebar .sb-group > summary');
  var hov = q('[data-hover-probe]');
  var search = q('.sidebar-search');
  var ph = parse(getComputedStyle(search, '::placeholder').color);
  var sbg = over(parse(getComputedStyle(search).backgroundColor), top);
  var bar = q('.sidebar .user-bar');
  // 帳號列的登入來源（認證開著才有）：照原本的樣子放一個進去量
  var src = q('#probeSrc');
  if (!src) { src = document.createElement('span'); src.id = 'probeSrc'; src.className = 'sb-user-src';
    src.setAttribute('data-s', 's5bca4f8e'); src.textContent = '@ldap'; bar.appendChild(src); }
  var out = {
    theme: document.documentElement.getAttribute('data-sb-theme'),
    top: root.getPropertyValue('--sb-bg-top').trim(), bottom: root.getPropertyValue('--sb-bg-bottom').trim(),
    fg: getComputedStyle(q('.sidebar')).color, sidebarBg: getComputedStyle(q('.sidebar')).backgroundImage,
    brand: text(q('.sidebar .brand-text'), top),
    version: text(q('.sidebar .brand-version'), top),
    group: text(group, top, [group]),
    count: text(group.querySelector('.sb-count'), top, [group, group.querySelector('.sb-count')]),
    name: text(item.querySelector('.sb-item-name'), top, [item]),
    desc: text(item.querySelector('.sb-item-desc'), top, [item]),
    anon: text(q('.sidebar .sb-anon-label'), top, [bar]),
    src: text(src, top, [bar]),
    hoverName: hov ? text(hov.querySelector('.sb-item-name'), top, [hov]) : null,
    hoverDesc: hov ? text(hov.querySelector('.sb-item-desc'), top, [hov]) : null,
    placeholder: r2(ratio(over(ph, sbg), sbg)),
    logo: q('.sidebar .brand-logo').getAttribute('src')
  };
  // 浮動捲軸：平常、拖曳中（跟滑過同一條規則）；它沿著整條側欄走，頂端底端取比較差的那一個
  var sb = q('.sidebar'), thumb = q('.sidebar-scrollbar');
  function thumbRatio() { var c = parse(getComputedStyle(thumb).backgroundColor);
    return r2(Math.min(ratio(over(c, top), top), ratio(over(c, bottom), bottom))); }
  out.thumb = thumbRatio();
  sb.classList.add('sb-dragging'); await sleep(300);
  out.thumbHover = thumbRatio();
  sb.classList.remove('sb-dragging');
  // 目前所在的那一格（自己有底色）
  item.classList.add('active'); await sleep(300);
  out.activeName = text(item.querySelector('.sb-item-name'), top, [item]);
  out.activeDesc = text(item.querySelector('.sb-item-desc'), top, [item]);
  item.classList.remove('active'); await sleep(300);
  return out;
})()"""


def force_hover(send) -> None:
    """把第二格工具標成 `data-hover-probe`，用 DevTools 強制它一直是 `:hover`。"""
    js(send, "(function(){var a=document.querySelectorAll('.sidebar .sb-item:not(.active)');"
             "a[1].setAttribute('data-hover-probe','1');return true;})()")
    send("DOM.enable")
    send("CSS.enable")
    root = send("DOM.getDocument", {"depth": 0})["result"]["root"]["nodeId"]
    nid = send("DOM.querySelector", {"nodeId": root, "selector": "[data-hover-probe]"})["result"]["nodeId"]
    send("CSS.forcePseudoState", {"nodeId": nid, "forcedPseudoClasses": ["hover"]})


def _themes():
    sys.path.insert(0, str(ROOT))
    from app.core import branding
    return branding.SIDEBAR_THEMES


def test_every_theme_is_readable_and_the_default_is_unchanged(live):
    base, send, errs = live
    open_page(send, base + "/", ".sidebar .sb-item")
    force_hover(send)
    results = {}
    for tid, _kind, tone, *_ in _themes():
        js(send, f"document.documentElement.setAttribute('data-sb-theme', {json.dumps(tid)}), true")
        results[tid] = js(send, _MEASURE)
    d = results["indigo"]
    assert (d["top"], d["bottom"]) == ("#7c6df0", "#5b4bd9"), d
    assert d["fg"] == "rgb(244, 243, 255)", "預設配色的文字顏色變了：" + d["fg"]
    assert "rgb(124, 109, 240)" in d["sidebarBg"] and "rgb(91, 75, 217)" in d["sidebarBg"], d["sidebarBg"]
    bad = []
    for tid, r in results.items():
        if tid == "indigo":
            continue
        for k in ("brand", "version", "group", "count", "name", "desc", "anon", "src", "hoverName", "hoverDesc",
                  "activeName", "activeDesc"):
            if r[k] < 4.5:
                bad.append(f"{tid} {k} {r[k]}")
        for k in ("placeholder", "thumbHover"):
            if r[k] < 3.0:
                bad.append(f"{tid} {k} {r[k]}")
        if r["thumbHover"] <= r["thumb"]:
            bad.append(f"{tid} 捲軸滑過時沒有變明顯（{r['thumb']} → {r['thumbHover']}）")
    assert not bad, f"這些文字在那一種配色上對比不夠：{bad}"
    # 每一種的背景真的不一樣（不是全部退回預設）
    assert len({(r["top"], r["bottom"]) for r in results.values()}) == len(results)
    assert not errs, errs


def test_picking_a_theme_applies_at_once_and_sticks(live):
    base, send, errs = live
    open_page(send, base + "/admin/branding", "[data-theme-pick]")
    cards = js(send, """Array.from(document.querySelectorAll('[data-theme-pick]')).map(function (b) {
        var mini = b.querySelector('.br-mini');
        return {id: b.dataset.themePick, name: b.querySelector('.br-theme-name').textContent.trim(),
                desc: (b.querySelector('.br-theme-desc') || {}).textContent || '',
                bg: getComputedStyle(mini).backgroundImage, h: mini.getBoundingClientRect().height}; })""")
    assert [c["id"] for c in cards] == [t[0] for t in _themes()]
    assert all(c["name"] and c["desc"].strip() and c["h"] >= 60 for c in cards), cards
    assert len({c["bg"] for c in cards}) == len(_themes()), "預覽的背景要是那一種配色"
    js(send, "document.querySelector('[data-theme-pick=\"silver\"]').click(), true")
    assert wait(send, "document.documentElement.getAttribute('data-sb-theme') === 'silver'", 10), "沒有當場換"
    st = js(send, _MEASURE)
    assert st["top"] == "#e2e8f0" and st["logo"].endswith("logo-on-light.png"), st
    assert js(send, "document.querySelector('[data-theme-pick=\"silver\"]').getAttribute('aria-checked')") == "true"
    assert "淺灰" in js(send, "document.getElementById('themeStatus').textContent")
    # 重新整理之後照舊；其他頁也是
    open_page(send, base + "/", ".sidebar .sb-item")
    st = js(send, _MEASURE)
    assert st["theme"] == "silver" and st["logo"].endswith("logo-on-light.png"), st
    open_page(send, base + "/admin/branding", "[data-theme-pick]")
    js(send, "document.querySelector('[data-theme-pick=\"ocean\"]').click(), true")
    assert wait(send, "document.documentElement.getAttribute('data-sb-theme') === 'ocean'", 10)
    assert js(send, "document.querySelector('.sidebar .brand-logo').getAttribute('src')").endswith("logo-on-dark.png")
    assert not errs, errs


def test_the_document_logo_replaces_the_builtin_one_everywhere(live):
    base, send, errs = live
    open_page(send, base + "/admin/branding", "[data-builtin-logo]")
    js(send, "document.querySelector('[data-builtin-logo=\"doc\"]').click(), true")
    assert wait(send, "document.querySelector('.sidebar .brand-logo').getAttribute('src').indexOf('logo-doc.png') >= 0", 10)
    open_page(send, base + "/", ".sidebar .brand-logo")
    got = js(send, """(async function () {
        var side = document.querySelector('.sidebar .brand-logo'), icon = document.querySelector('link[rel="icon"]');
        var r = await fetch(icon.getAttribute('href'));
        var b = await r.blob();
        return {side: side.getAttribute('src'), icon: icon.getAttribute('href'), ok: r.ok, type: b.type, size: b.size,
                loaded: side.complete && side.naturalWidth > 0}; })()""")
    assert got["side"].endswith("logo-doc.png") and got["icon"].endswith("logo-doc.png"), got
    assert got["ok"] and got["type"] == "image/png" and got["size"] > 1000 and got["loaded"], got
    # 換回 Jason Tools Logo
    open_page(send, base + "/admin/branding", "[data-builtin-logo]")
    js(send, "document.querySelector('[data-builtin-logo=\"jt\"]').click(), true")
    assert wait(send, "document.querySelector('.sidebar .brand-logo').getAttribute('src').indexOf('logo-on-') >= 0", 10)
    assert not errs, errs


def test_phone_width_fits(live):
    base, send, errs = live
    send("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})
    try:
        open_page(send, base + "/admin/branding", "[data-theme-pick]")
        w = js(send, "[document.documentElement.scrollWidth, innerWidth]")
        assert w[0] <= w[1] + 1, f"手機寬度出現橫向捲動：{w}"
    finally:
        send("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 1000, "deviceScaleFactor": 1,
                                                    "mobile": False})
    assert not errs, errs


#: 每一組（漸層 / 純色）的卡片各排在第幾排、每排幾張
_ROWS = r"""Array.from(document.querySelectorAll('.br-themes')).map(function (g) {
  var rows = {};
  g.querySelectorAll('[data-theme-pick]').forEach(function (b) {
    var y = Math.round(b.getBoundingClientRect().top); rows[y] = (rows[y] || 0) + 1; });
  return Object.keys(rows).sort(function (a, b) { return a - b; }).map(function (k) { return rows[k]; }); })"""


def test_each_group_fills_its_rows(live):
    """桌面寬度一組一排 6 張；中等寬度 3 張一排；手機兩欄。不會有一排只剩一張。"""
    base, send, errs = live
    sizes = ((1440, False, [6]), (1100, False, [3, 3]), (390, True, [2, 2, 2]))
    try:
        for width, mobile, want in sizes:
            send("Emulation.setDeviceMetricsOverride", {"width": width, "height": 1000, "deviceScaleFactor": 1,
                                                        "mobile": mobile})
            open_page(send, base + "/admin/branding", "[data-theme-pick]")
            assert js(send, _ROWS) == [want, want], (width, js(send, _ROWS))
    finally:
        send("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 1000, "deviceScaleFactor": 1,
                                                    "mobile": False})
    assert not errs, errs


#: 只攔「套用」那一支：`mode` 是 'error'（伺服器回 403 ＋ 訊息）、'offline'（連不上）、'slow-first'（第一個回應晚 0.8 秒才到）
_STUB_FETCH = r"""(function (mode) {
  if (!window.__realFetch) window.__realFetch = window.fetch;
  var n = 0;
  window.fetch = function (u, o) {
    if (String(u).indexOf('/admin/branding/appearance') < 0) return window.__realFetch(u, o);
    n += 1;
    if (mode === 'error') return Promise.resolve(new Response(JSON.stringify({detail: '模擬的錯誤訊息'}),
      {status: 403, headers: {'Content-Type': 'application/json'}}));
    if (mode === 'offline') return Promise.reject(new TypeError('模擬連不上'));
    var p = window.__realFetch(u, o);
    if (mode === 'slow-first' && n === 1) return p.then(function (r) { return new Promise(function (ok) { setTimeout(function () { ok(r); }, 800); }); });
    return p;
  };
  return true;
})"""

_STATE = r"""(async function () {
  var on = function (sel) { return Array.from(document.querySelectorAll(sel + '.is-on')).map(function (b) {
    return b.dataset.themePick || b.dataset.builtinLogo; }); };
  var saved = await (await (window.__realFetch || window.fetch)('/admin/api/branding')).json();
  return {attr: document.documentElement.getAttribute('data-sb-theme'), themeOn: on('[data-theme-pick]'),
          logoOn: on('[data-builtin-logo]'), saved: [saved.sidebar_theme, saved.builtin_logo],
          side: document.querySelector('.sidebar .brand-logo').getAttribute('src'),
          themeStatus: document.getElementById('themeStatus').textContent,
          themeErr: document.getElementById('themeStatus').classList.contains('is-err'),
          logoStatus: document.getElementById('logoOptStatus').textContent,
          logoErr: document.getElementById('logoOptStatus').classList.contains('is-err')};
})()"""


def test_a_failed_save_leaves_everything_as_it_was(live):
    base, send, errs = live
    for mode, msg in (("error", "模擬的錯誤訊息"), ("offline", "模擬連不上")):
        open_page(send, base + "/admin/branding", "[data-theme-pick]")
        js(send, _STUB_FETCH + "('" + mode + "')")
        before = js(send, _STATE)
        theme = next(t[0] for t in _themes() if t[0] != before["attr"])
        logo = "doc" if before["logoOn"] == ["jt"] else "jt"
        js(send, f"document.querySelector('[data-theme-pick=\"{theme}\"]').click(), true")
        js(send, f"document.querySelector('[data-builtin-logo=\"{logo}\"]').click(), true")
        assert wait(send, "document.getElementById('themeStatus').classList.contains('is-err') && "
                          "document.getElementById('logoOptStatus').classList.contains('is-err')", 10), mode
        after = js(send, _STATE)
        assert msg in after["themeStatus"] and msg in after["logoStatus"], (mode, after)
        for k in ("attr", "themeOn", "logoOn", "saved", "side"):
            assert after[k] == before[k], f"{mode}：套用失敗卻改了 {k}：{before[k]} → {after[k]}"
        assert after["themeOn"] == [before["attr"]], after
    assert not errs, errs


def test_quick_clicks_end_on_the_last_choice(live):
    """第一個回應晚到：不排隊的話畫面會停在第一張，存下來的卻是第二張。"""
    base, send, errs = live
    open_page(send, base + "/admin/branding", "[data-theme-pick]")
    cur = js(send, _STATE)["attr"]
    first, last = [t[0] for t in _themes() if t[0] != cur][:2]
    js(send, _STUB_FETCH + "('slow-first')")
    js(send, f"document.querySelector('[data-theme-pick=\"{first}\"]').click();"
             f"document.querySelector('[data-theme-pick=\"{last}\"]').click(); true")
    assert wait(send, f"document.documentElement.getAttribute('data-sb-theme') === '{last}'", 10)
    time.sleep(1.5)                     # 晚到的那個回應也到了之後再看
    st = js(send, _STATE)
    assert st["attr"] == last and st["themeOn"] == [last] and st["saved"][0] == last, st
    assert not errs, errs


@pytest.mark.parametrize("lang", ["en", "ja"])
def test_cards_fit_in_other_languages(live, lang):
    """英文、日文的名稱與說明比較長：每組照樣排滿，字不會被切掉或撐出卡片，也沒有橫向捲動。"""
    base, send, errs = live
    open_page(send, base + "/", ".sidebar")     # 停在 about:blank 時設不了 cookie（單獨跑這一支就是這樣）
    js(send, "document.cookie = 'jtdt_locale=" + lang + "; path=/'; true")
    try:
        for width, want in ((1440, [6]), (1100, [3, 3]), (390, [2, 2, 2])):
            send("Emulation.setDeviceMetricsOverride", {"width": width, "height": 1000, "deviceScaleFactor": 1,
                                                        "mobile": width < 600})
            open_page(send, base + "/admin/branding", "[data-theme-pick]")
            assert js(send, "document.documentElement.lang") == lang
            assert js(send, _ROWS) == [want, want], (lang, width, js(send, _ROWS))
            got = js(send, r"""(function () {
              var out = {names: [], cut: [], scroll: [document.documentElement.scrollWidth, innerWidth]};
              document.querySelectorAll('[data-theme-pick]').forEach(function (b) {
                var n = b.querySelector('.br-theme-name'), d = b.querySelector('.br-theme-desc');
                out.names.push(n.textContent.trim());
                [n, d].forEach(function (e) { if (e.scrollWidth > e.clientWidth + 1 || e.getBoundingClientRect().right > b.getBoundingClientRect().right)
                  out.cut.push(e.textContent.trim()); });
              });
              return out; })()""")
            assert got["scroll"][0] <= got["scroll"][1] + 1, (lang, width, got["scroll"])
            assert not got["cut"], f"{lang} {width}px 這些字被切掉或撐出卡片：{got['cut']}"
            assert not any(re.search(r"[一-鿿]", n) for n in got["names"] if lang == "en"), got["names"]
    finally:
        js(send, "document.cookie = 'jtdt_locale=; path=/; max-age=0'; true")
        send("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 1000, "deviceScaleFactor": 1,
                                                    "mobile": False})
    assert not errs, errs

