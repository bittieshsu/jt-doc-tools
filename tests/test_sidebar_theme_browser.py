"""左側選單配色與內建 Logo —— 真的在瀏覽器裡量（2026-10-11 加入）。

判準：

* 10 種配色逐一套上，**實量**側欄裡每一種文字的顏色與它底下的背景（側欄的頂端顏色疊上該元件自己的半透明底），
  算 WCAG 對比：
  - 新的 9 種：站台名稱、分類列、工具名稱、工具說明、版本都要 4.5:1 以上，搜尋框的提示字 3:1 以上；
  - 預設（靛紫）**跟改版前一模一樣**（背景、文字顏色逐一比對原本寫死的值）。
* 設定頁點一張配色卡：不重新整理，側欄當場換色、`<html data-sb-theme>` 跟著換；重新整理之後照舊。
  淺色背景時 Jason Tools Logo 換成白底用的那一版。
* 每張配色卡都有預覽（迷你側欄的背景就是那一種配色）、名稱、一句說明。
* 內建 Logo 選「文件圖示」：側欄、頁籤圖示都換成 `logo-doc.png`，而且那張圖真的載得到。
* 手機寬度：設定頁沒有橫向捲動。
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


#: 在瀏覽器裡算對比：元件的文字顏色（含 opacity）對「側欄頂端或底端的顏色＋元件自己的半透明底」
_MEASURE = r"""(function () {
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
  var root = getComputedStyle(document.documentElement);
  var top = hex(root.getPropertyValue('--sb-bg-top')), bottom = hex(root.getPropertyValue('--sb-bg-bottom'));
  function text(el, base, own) {
    var cs = getComputedStyle(el), fg = parse(cs.color), op = parseFloat(cs.opacity) || 1;
    fg[3] *= op;
    var bg = base;
    (own || []).forEach(function (e) { var b = parse(getComputedStyle(e).backgroundColor); if (b && b[3] > 0) bg = over(b, bg); });
    return Math.round(ratio(over(fg, bg), bg) * 100) / 100;
  }
  var q = function (s) { return document.querySelector(s); };
  var item = q('.sidebar .sb-item:not(.active)'), group = q('.sidebar .sb-group > summary');
  var search = q('.sidebar-search');
  var ph = parse(getComputedStyle(search, '::placeholder').color);
  var sbg = over(parse(getComputedStyle(search).backgroundColor), top);
  return {
    theme: document.documentElement.getAttribute('data-sb-theme'),
    top: root.getPropertyValue('--sb-bg-top').trim(), bottom: root.getPropertyValue('--sb-bg-bottom').trim(),
    fg: getComputedStyle(q('.sidebar')).color, sidebarBg: getComputedStyle(q('.sidebar')).backgroundImage,
    brand: text(q('.sidebar .brand-text'), top),
    version: text(q('.sidebar .brand-version'), top),
    group: text(group, top, [group]),
    name: text(item.querySelector('.sb-item-name'), top, [item]),
    desc: text(item.querySelector('.sb-item-desc'), top, [item]),
    placeholder: Math.round(ratio(over(ph, sbg), sbg) * 100) / 100,
    logo: q('.sidebar .brand-logo').getAttribute('src')
  };
})()"""


def _themes():
    sys.path.insert(0, str(ROOT))
    from app.core import branding
    return branding.SIDEBAR_THEMES


def test_every_theme_is_readable_and_the_default_is_unchanged(live):
    base, send, errs = live
    open_page(send, base + "/", ".sidebar .sb-item")
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
        for k in ("brand", "version", "group", "name", "desc"):
            if r[k] < 4.5:
                bad.append(f"{tid} {k} {r[k]}")
        if r["placeholder"] < 3.0:
            bad.append(f"{tid} placeholder {r['placeholder']}")
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
    assert len({c["bg"] for c in cards}) == 10, "預覽的背景要是那一種配色"
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
