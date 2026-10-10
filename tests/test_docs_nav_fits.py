"""介紹站導覽列：每一頁都連得到合規頁，而且多了那一項之後桌機仍然排得下。

## 由來（2026-10-10）

合規支援頁上線時只在首頁「稽核」那一節放了按鈕、頁尾放了連結，**導覽列沒有**
——使用者用手機打開漢堡選單找不到，問「合規的頁從哪裡點」。

加進導覽列之後，英文那一排在容器最寬（1180px）時也塞不下：項目之間 28px，
十一個項目光間距就 280px。所以間距改 20px、收成漢堡選單的切換點 1080 → 1200px。

**判準落在畫面上**（`getBoundingClientRect`）：切換點以上一整排、不超出容器、
跟品牌至少隔 24px；切換點以下是漢堡選單，打開之後看得到合規那一項。
只驗 HTML 裡有那個連結的話，桌機上被擠出容器、手機選單被藏起來都照樣綠。
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

import pytest

from tests.test_docs_nav_controls_line_up import ROOT, _free_port, pytestmark  # noqa: F401
from tools.browser_probe import browser as _browser
from tools.browser_probe import profile_arg as _profile_arg
from tools.repo_paths import public_root

DOCS = public_root(pathlib.Path(ROOT)) / "docs"

#: 四種頁面 × 三種語言。合規頁本身也要有（它借疑難排解頁的版面）。
BASES = ("index", "api", "troubleshooting", "compliance")
SUFFIX = {"zh-Hant": "", "en": "-en", "ja": "-ja"}
PAGES = [f"{b}{s}.html" for b in BASES for s in SUFFIX.values()]

#: 收成漢堡選單的切換點（`docs/style.css` 的 `@media (max-width: …)`）。
def _breakpoint() -> int:
    css = (DOCS / "style.css").read_text(encoding="utf-8")
    m = re.search(r"@media \(max-width: (\d+)px\) \{\s*/\*[^*]*\*/\s*:root \{ --menu-ctl-h", css)
    assert m, "找不到導覽列收成漢堡選單的那條 @media"
    return int(m.group(1))


# ---------------------------------------------------------------- 靜態

@pytest.mark.parametrize("page", PAGES)
def test_every_page_links_to_the_compliance_page_from_its_nav(page):
    html = (DOCS / page).read_text(encoding="utf-8")
    nav = re.search(r'<nav class="topnav" id="topnav">(.*?)</nav>', html, re.S)
    assert nav, f"{page}：找不到導覽列"
    # 同一個語言的合規頁：index-en.html → compliance-en.html
    suffix = page.split(".")[0]
    suffix = suffix[suffix.index("-"):] if "-" in suffix else ""
    want = f"compliance{suffix}.html"
    links = re.findall(r'<a href="([^"]+)" class="nav-link">([^<]+)</a>', nav.group(1))
    hit = [t for h, t in links if h == want]
    assert hit, f"{page}：導覽列沒有連到 {want}（現有 {links}）"
    if suffix:
        assert hit[0] != "合規", f"{page}：導覽列那一項沒有翻譯"
    if suffix == "-en":
        assert not re.search(r"[一-鿿]", hit[0]), f"{page}：導覽列寫的是 {hit[0]!r}"


# ---------------------------------------------------------------- 瀏覽器量

MEASURE = """(() => {
  const n = document.getElementById('topnav');
  const b = document.querySelector('.brand');
  const t = document.getElementById('navToggle');
  const items = [...n.children].filter(e => getComputedStyle(e).display != 'none');
  const mid = e => { const r = e.getBoundingClientRect(); return Math.round((r.top + r.bottom) / 8); };
  const nr = n.getBoundingClientRect(), br = b.getBoundingClientRect();
  const c = document.querySelector('a.nav-link[href^="compliance"]');
  const cr = c ? c.getBoundingClientRect() : null;
  return JSON.stringify({
    rows: new Set(items.map(mid)).size,
    toggle: getComputedStyle(t).display,
    navL: nr.left, navR: nr.right, brandR: br.right,
    edge: document.documentElement.clientWidth
          - parseFloat(getComputedStyle(n.parentElement).paddingRight),
    comp: cr && {w: cr.width, h: cr.height, vis: getComputedStyle(c).visibility,
                 top: cr.top, bottom: cr.bottom, vh: innerHeight}});
})()"""


@pytest.fixture(scope="module")
def look():
    if not (DOCS / "index.html").is_file():
        pytest.skip("找不到介紹站的 docs/")
    port, cdp = _free_port(), _free_port()
    srv = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(DOCS), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [_browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
         _profile_arg(), f"--remote-debugging-port={cdp}", "--remote-allow-origins=*",
         "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws = None
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            pytest.skip("靜態伺服器或瀏覽器起不來")
        import websockets.sync.client as wsc
        req = urllib.request.Request(f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
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

        send("Page.enable")

        def one(page: str, width: int, mobile: bool = False, open_menu: bool = False) -> dict:
            send("Emulation.setDeviceMetricsOverride",
                 {"width": width, "height": 900 if not mobile else 844,
                  "deviceScaleFactor": 2 if mobile else 1, "mobile": mobile})
            send("Page.navigate", {"url": f"http://127.0.0.1:{port}/{page}"})
            for _ in range(40):
                r = send("Runtime.evaluate", {"expression": "document.readyState", "returnByValue": True})
                if r["result"]["result"].get("value") == "complete":
                    break
                time.sleep(0.2)
            time.sleep(0.4)
            if open_menu:
                send("Runtime.evaluate", {"expression": "document.getElementById('navToggle').click()"})
                time.sleep(0.5)
            r = send("Runtime.evaluate", {"expression": MEASURE, "returnByValue": True})
            return json.loads(r["result"]["result"]["value"])

        yield one
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        br.terminate(); srv.terminate()
        for p in (br, srv):
            try:
                p.wait(timeout=6)
            except Exception:  # noqa: BLE001
                p.kill()


@pytest.mark.parametrize("page", PAGES)
def test_just_above_the_breakpoint_the_nav_is_one_row_inside_the_container(look, page):
    bp = _breakpoint()
    m = look(page, bp + 1)
    assert m["toggle"] == "none", f"{page}：{bp + 1}px 還是漢堡選單"
    assert m["rows"] == 1, f"{page}：{bp + 1}px 時導覽列折成 {m['rows']} 列"
    assert m["navR"] <= m["edge"] + 0.5, (
        f"{page}：{bp + 1}px 時導覽列右緣 {m['navR']:.0f} 超出容器 {m['edge']:.0f}")
    assert m["navL"] - m["brandR"] >= 23.5, (
        f"{page}：{bp + 1}px 時導覽列離品牌只有 {m['navL'] - m['brandR']:.0f}px")
    assert m["comp"] and m["comp"]["w"] > 10, f"{page}：桌機導覽列上看不到合規那一項"


@pytest.mark.parametrize("page", ["index.html", "index-en.html", "index-ja.html"])
def test_at_the_breakpoint_it_becomes_the_menu(look, page):
    """反向對照：切換點本身要是漢堡選單。只驗上面那條的話，把切換點拉到很小
    （桌機永遠一整排）也會過 —— 那時窄一點的桌機就整排超出畫面。"""
    m = look(page, _breakpoint())
    assert m["toggle"] != "none", f"{page}：{_breakpoint()}px 沒有收成漢堡選單"


@pytest.mark.parametrize("page", PAGES)
def test_on_a_phone_the_menu_shows_the_compliance_link(look, page):
    m = look(page, 390, mobile=True, open_menu=True)
    c = m["comp"]
    assert c, f"{page}：手機選單裡沒有合規那一項"
    assert c["vis"] == "visible" and c["w"] > 40 and c["h"] > 20, (
        f"{page}：手機選單打開之後合規那一項看不到（{c}）")
    assert c["bottom"] <= c["vh"], f"{page}：合規那一項跑到畫面外（{c}）"
