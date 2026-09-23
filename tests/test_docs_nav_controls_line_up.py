"""介紹站導覽列右邊那兩個控制項**高度要一樣**。

語言下拉是 `<select>`、GitHub 是 `<a class="btn">` —— 兩個各自算自己的高度
（下拉 13.5px 字 ＋ 6px padding ＋ 1px 框；按鈕 14px 字 ＋ 9px padding ＋
1.5px 框）。並排時實測差 **12.2 px**，而且垂直也沒對齊（2026-09-23 使用者
截圖回報）。

**為什麼要用瀏覽器量**：這一類「版面長歪」是本專案記過最多次的盲區 ——
元素都在、沒有 JS 例外、也沒有殘留中文，逐頁掃描與截圖檢查全部綠燈。
唯一分得出來的訊號是**畫面上真正佔的位置**（`getBoundingClientRect`）。

**判準是「兩個的上下緣一樣」，不是「padding 寫了多少」**。寫死數字的話，
改一次字級、加一種語言就又會歪掉，而測試還是綠的。修法本身也一樣：
`align-self: stretch` 讓下拉跟著那一列最高的那個走，沒有魔術數字。
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tools.browser_probe import browser as _browser  # noqa: E402
from tools.repo_paths import public_root  # noqa: E402

pytestmark = pytest.mark.skipif(
    _browser() is None
    or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才量得到")

#: 量幾個語言版本。**不要只量中文** —— 英文與日文的字比較寬，
#: 而導覽列歪掉的歷史紀錄裡有一半是換語言才現形的。
PAGES = ("index.html", "index-en.html", "index-ja.html")

#: 容許的誤差（px）。次像素捨入會有零點幾的差，整數 1 已經是肉眼看得到的。
TOL = 1.0

MEASURE = """(() => {
  const s = document.querySelector('.nav-lang');
  const b = document.querySelector('.nav-github');
  if (!s || !b) return JSON.stringify({missing: true});
  const r = (e) => { const x = e.getBoundingClientRect(); const c = getComputedStyle(e);
    return {h: x.height, top: x.top, bottom: x.bottom, w: x.width,
            radius: c.borderTopLeftRadius, bt: c.borderTopWidth,
            bb: c.borderBottomWidth, btc: c.borderTopColor, bbc: c.borderBottomColor}; };
  return JSON.stringify({lang: r(s), gh: r(b)});
})()"""

#: 手機寬度（漢堡選單）。選單要先打開，量到的才是使用者看到的那一格。
PHONE_W = 390


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def measure():
    """把 `docs/` 靜態服起來 ＋ 無頭瀏覽器，回傳「量某一頁」的函式。"""
    docs = public_root(pathlib.Path(ROOT)) / "docs"
    if not (docs / "index.html").is_file():
        pytest.skip("找不到介紹站的 docs/")
    port, cdp = _free_port(), _free_port()
    srv = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(docs), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [_browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
         f"--remote-debugging-port={cdp}", "--remote-allow-origins=*",
         "--window-size=1440,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws = None
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version",
                                       timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.skip("靜態伺服器或瀏覽器起不來")

        import websockets.sync.client as wsc
        req = urllib.request.Request(
            f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            tab = json.loads(r.read())
        ws = wsc.connect(tab["webSocketDebuggerUrl"], max_size=None,
                         open_timeout=10)
        n = [0]

        def send(method, params=None):
            n[0] += 1
            i = n[0]
            ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
            while True:
                m = json.loads(ws.recv())
                if m.get("id") == i:
                    return m

        send("Page.enable")

        def one(page: str, phone: bool = False) -> dict:
            if phone:
                send("Emulation.setDeviceMetricsOverride",
                     {"width": PHONE_W, "height": 844, "deviceScaleFactor": 2,
                      "mobile": True})
            else:
                send("Emulation.clearDeviceMetricsOverride")
            send("Page.navigate", {"url": f"http://127.0.0.1:{port}/{page}"})
            time.sleep(2.0)
            if phone:
                send("Runtime.evaluate", {"expression":
                     "document.getElementById('navToggle').click()"})
                time.sleep(0.5)
            r = send("Runtime.evaluate",
                     {"expression": MEASURE, "returnByValue": True})
            return json.loads(r["result"]["result"]["value"])

        yield one
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        br.terminate(); srv.terminate()
        for p in (br, srv):
            try:
                p.wait(timeout=6)
            except Exception:
                p.kill()


@pytest.mark.parametrize("page", PAGES)
def test_the_language_picker_is_the_same_height_as_the_github_button(measure, page):
    box = measure(page)
    assert not box.get("missing"), f"{page}：導覽列少了語言下拉或 GitHub 按鈕"
    lang, gh = box["lang"], box["gh"]
    assert abs(lang["h"] - gh["h"]) <= TOL, (
        f"{page}：語言下拉 {lang['h']:.1f}px、GitHub 按鈕 {gh['h']:.1f}px "
        f"—— 高度差 {abs(lang['h'] - gh['h']):.1f}px")
    # **上下緣也要對齊** —— 只驗高度的話，兩個一樣高但整體上下錯開照樣難看。
    assert abs(lang["top"] - gh["top"]) <= TOL, (
        f"{page}：兩者上緣差 {abs(lang['top'] - gh['top']):.1f}px")
    assert abs(lang["bottom"] - gh["bottom"]) <= TOL, (
        f"{page}：兩者下緣差 {abs(lang['bottom'] - gh['bottom']):.1f}px")


def test_the_measurement_really_reaches_all_three_pages(measure):
    """**先證明量得到東西** —— 三個語言版本都要真的有那兩個控制項。

    少了哪一頁的話上面那組會 skip 掉它而不是紅，而「跳過」跟「通過」在
    pytest 的輸出裡長得一模一樣（本專案第 N 次）。
    """
    got = [p for p in PAGES if not measure(p).get("missing")]
    assert got == list(PAGES), f"只量到 {got}"


# ---------------------------------------------------------------- 手機的漢堡選單

@pytest.mark.parametrize("page", PAGES)
def test_in_the_phone_menu_github_looks_like_the_language_picker(measure, page):
    """手機選單裡 GitHub 那一格的上緣看起來像被切掉一截（2026-09-23 使用者
    手機截圖回報）。原本的 CSS 想做「上面一條分隔線 ＋ 方角」，但按鈕本身帶著
    四邊的框 —— 兩件事疊在一起就是上緣方角、上框線跟其他三邊不一樣。

    判準落在畫面上：跟上面的語言下拉**同寬、同高、同一種圓角**，而且
    **上下框線一樣**（被切掉的樣子就是上下不一樣）。"""
    box = measure(page, phone=True)
    assert not box.get("missing"), f"{page}：選單裡少了語言下拉或 GitHub 按鈕"
    lang, gh = box["lang"], box["gh"]
    assert gh["h"] > 20, f"{page}：GitHub 按鈕高 {gh['h']}px —— 選單大概沒打開"
    assert abs(lang["h"] - gh["h"]) <= TOL, (
        f"{page}：選單裡語言下拉 {lang['h']:.1f}px、GitHub {gh['h']:.1f}px")
    assert abs(lang["w"] - gh["w"]) <= TOL, f"{page}：兩格不一樣寬"
    assert gh["radius"] == lang["radius"], (
        f"{page}：GitHub 圓角 {gh['radius']}、語言下拉 {lang['radius']}")
    assert (gh["bt"], gh["btc"]) == (gh["bb"], gh["bbc"]), (
        f"{page}：GitHub 上框線 {gh['bt']} {gh['btc']}、下框線 {gh['bb']} {gh['bbc']}"
        " —— 上下不一樣就是那個「被切掉一截」的樣子")
    assert gh["top"] >= lang["bottom"], f"{page}：GitHub 疊到語言下拉上面了"
