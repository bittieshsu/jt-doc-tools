"""公文撰擬設定頁：資料來源的版面 —— 在真的瀏覽器裡量。

## 由來

第一次（1440 寬，有側欄）使用者截圖回報資料來源那張表很亂：「名稱與出處」一格
塞了名稱、兩個標籤、提供機關與授權、資料集連結、整段顯名文字；「結果」一格折成
四行又跟「數量」重複；「上傳檔案」在按鈕**裡面**折成兩行。那一版改成六欄的表
＋每列底下一列整寬的出處。

第二次（2026-10-08，1100 寬）回報仍然不好看：網址框被擠到只剩半截、時間底下多一個
孤零零的「下載」、三顆按鈕疊成寬度不一的一柱（實心紅色的「刪除」最搶眼）、出處那一列
看起來跟它的來源脫節。改成**一個來源一張卡片**：

* 標題列：名稱＋類型標籤＋「內建」，右邊是「啟用」開關（同一個 checkbox）；
* 狀態一行：「成功 · 88 份範本 · 略過 1 個檔案 · 最後下載：…」，失敗原因另起一行；
* 網址框整張卡片寬；
* 「列出範本」留在卡片裡；
* 頁腳：左邊顯名小字（含「資料集網頁」連結），右邊一排同高的按鈕，
  「刪除」是紅框不是一整塊紅；卡片窄了（手機）就上下疊、按鈕換行。

外層仍是 `#odRows > tr[data-sid]`（別支測試靠它找來源），只是改成 block 排版。

## 為什麼要量

版面好不好**讀 CSS 判斷不出來**。這裡的判準一律是瀏覽器算出來的幾何：文字實際
佔了幾行（Range 的 client rects 取不同的 top）、元素在哪裡、多寬、多高。

資料在**另一個資料目錄**裡種（另起一支服務），不碰開發機與測試共用的資料庫。
只收自己起的那兩支行程（用 Popen 的把手，不用名字批次殺 —— 這台機器上還有別的專案
在跑 chromium）。
"""
from __future__ import annotations

import base64
import contextlib
import json
import time
from pathlib import Path

import pytest

from tests.test_official_doc_tool import ROOT, _free_port

T = "archives-templates"
A = "archives-address-book"

BOOK = [{"orgId": "Q1000000", "orgName": "嘉禾市政府", "statusCode": "T"},
        {"orgId": "Q20000000B", "orgName": "嘉禾市東湖區公所", "statusCode": "T"}]

#: 四種狀態各一個來源：已下載（88 份範本、略過 1 個檔案 —— 跟正式資料同一個量級）、
#: 已上傳、失敗、從來沒下載過。
SEED = r'''
import io, json, sys, zipfile
sys.path.insert(0, ".")
from app.core import official_doc_sources as ods
from tests.test_official_doc_template import SIGN_TPL, LETTER_TPL

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("一般公文表單/簽.odt", SIGN_TPL)
    z.writestr("一般公文表單/函.odt", LETTER_TPL)
    for i in range(86):
        z.writestr(f"其他表單/範例表單{i + 1:02d}.odt", SIGN_TPL)
    z.writestr("一般公文表單/說明.txt", "這不是範本")
assert ods.install_upload("archives-templates", buf.getvalue(), "t.zip")["count"] == 88
assert ods.install_upload("archives-address-book", sys.argv[1].encode("utf-8"),
                          "book.json")["count"] == 2
bad = ods.add_source({"kind": ods.KIND_ADDRESS_BOOK, "name": "嘉禾市地址簿鏡像",
                      "url": "https://example.com/book.json",
                      "publisher": "嘉禾市政府資訊處", "license": "內部使用"})
try:
    ods.install_upload(bad["id"], b"this is not json", "book.json")
except ods.SourceDataError:
    pass
ods.add_source({"kind": ods.KIND_TEMPLATES, "name": "嘉禾市自訂範本",
                "url": "https://example.com/t.zip"})
# 範本那一個改成「下載」來的（正式機上的樣子；種資料不連外，所以用上傳再改來源）
st_p = ods._status_path("archives-templates")
st = json.loads(st_p.read_text(encoding="utf-8"))
st["last_attempt"]["origin"] = "download"
st_p.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
print("seeded", bad["id"])
'''

#: 數一個節點裡的文字實際排成幾行：Range 的每個 client rect 取 top，
#: 相鄰兩個 top 差超過 6px 才算換行（同一行裡不同字型的框 top 會差一兩個像素）。
LINES_JS = r"""
window.__lines = function (node) {
  if (!node) return -1;
  var r = document.createRange();
  r.selectNodeContents(node);
  var tops = [];
  Array.prototype.forEach.call(r.getClientRects(), function (q) {
    if (q.width > 0 && q.height > 0) tops.push(q.top);
  });
  if (!tops.length) return 0;
  tops.sort(function (a, b) { return a - b; });
  var n = 1;
  for (var i = 1; i < tops.length; i++) if (tops[i] - tops[i - 1] > 6) n++;
  return n;
};
true;
"""


@contextlib.contextmanager
def live_admin_page(width: int = 1440, height: int = 900, locale: str = ""):
    """起一支關掉認證的拋棄式實例 ＋ 無頭瀏覽器，打開 /admin/official-doc。

    回 `(send, js, errs, extra)`：`extra["custom_bad"]` 是失敗的那個自訂來源的 id。
    `locale` 給了就先設介面語言的 cookie（量英日文版面用）。
    """
    import os
    import shutil
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

    data = tempfile.mkdtemp(prefix="odadmin-layout-")
    port, cdp = _free_port(), _free_port()
    (Path(data) / "auth_settings.json").write_text(json.dumps({"backend": "off"}),
                                                   encoding="utf-8")
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    seed = subprocess.run([sys.executable, "-c", SEED,
                           json.dumps(BOOK, ensure_ascii=False)],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert "seeded" in seed.stdout, seed.stderr[-2000:]
    custom_bad = seed.stdout.split("seeded", 1)[1].strip()

    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [br_path, "--headless=new", "--no-sandbox", "--disable-gpu",
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

        def js(expr: str):
            r = send("Runtime.evaluate", {"expression": expr, "awaitPromise": True,
                                          "returnByValue": True})
            res = r.get("result", {})
            if "exceptionDetails" in res:
                raise AssertionError(res["exceptionDetails"])
            return res.get("result", {}).get("value")

        send("Runtime.enable")
        send("Page.enable")
        send("Emulation.setDeviceMetricsOverride",
             {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
        if locale:
            send("Network.enable")
            send("Network.setCookie", {"name": "jtdt_locale", "value": locale,
                                       "url": f"http://127.0.0.1:{port}/"})
        send("Page.navigate", {"url": f"http://127.0.0.1:{port}/admin/official-doc"})
        end = time.time() + 30
        while time.time() < end:
            if js("document.readyState === 'complete' && "
                  "document.querySelectorAll('#odRows > tr[data-sid]').length === 4"):
                break
            time.sleep(0.2)
        else:
            raise AssertionError("管理頁沒有畫出四個來源")
        js("document.fonts ? document.fonts.ready.then(function(){return true}) : true")
        js(LINES_JS)
        yield send, js, errs, {"custom_bad": custom_bad, "port": port}
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
        shutil.rmtree(data, ignore_errors=True)


def screenshot(send, js, path: str) -> None:
    """整頁截圖（給人看用，不是判準）。"""
    h = js("Math.ceil(document.documentElement.scrollHeight)")
    w = js("Math.ceil(document.documentElement.clientWidth)")
    shot = send("Page.captureScreenshot", {
        "format": "png", "captureBeyondViewport": True,
        "clip": {"x": 0, "y": 0, "width": w, "height": h, "scale": 1}})
    Path(path).write_bytes(base64.b64decode(shot["result"]["data"]))


# ---------------------------------------------------------------- 量測用的 JS

#: 每一張卡片的幾何：卡片內容區、名稱、標籤、開關、狀態、網址框、顯名、頁腳、按鈕。
CARDS_JS = r"""Array.from(document.querySelectorAll('#odRows > tr[data-sid]')).map(function (r) {
  function rect(e) { if (!e) return null; var b = e.getBoundingClientRect();
    return { left: b.left, right: b.right, top: b.top, bottom: b.bottom,
             width: b.width, height: b.height }; }
  var td = r.querySelector(':scope > td');
  var cs = getComputedStyle(td);
  var inner = rect(td);
  inner.left += parseFloat(cs.paddingLeft); inner.right -= parseFloat(cs.paddingRight);
  inner.width = inner.right - inner.left;
  var name = r.querySelector('.od-src-name');
  var when = r.querySelector('.od-meta .od-when');
  var main = r.querySelector('.od-status-main');
  return {
    sid: r.dataset.sid,
    isCard: r.classList.contains('od-card'),
    card: rect(r), inner: inner,
    overflow: td.scrollWidth - td.clientWidth,
    name: [name ? name.textContent : null, window.__lines(name)], nameBox: rect(name),
    badges: Array.from(r.querySelectorAll('.od-badge')).map(rect),
    toggle: rect(r.querySelector('label.od-switch')),
    toggleText: (r.querySelector('label.od-switch') || {}).textContent || '',
    toggleInput: !!r.querySelector('label.od-switch input[type=checkbox][data-act="toggle"][data-sid="' + r.dataset.sid + '"]'),
    meta: (r.querySelector('.od-meta') || {}).textContent || '',
    main: main ? [main.textContent, window.__lines(main)] : null,
    when: when ? [when.textContent, window.__lines(when)] : null,
    metaBits: Array.from(r.querySelectorAll('.od-meta *')).map(function (e) {
      return e.childElementCount ? null : e.textContent.trim(); }).filter(Boolean),
    url: rect(r.querySelector('input.od-url-input')),
    urlValue: (r.querySelector('input.od-url-input') || {}).value,
    prov: rect(r.querySelector('.od-card-foot .od-prov')),
    provText: (r.querySelector('.od-card-foot .od-prov') || {}).textContent || '',
    foot: rect(r.querySelector('.od-card-foot')),
    footIsLast: !!(r.querySelector('.od-card-foot') && !r.querySelector('.od-card-foot').nextElementSibling),
    actions: rect(r.querySelector('.od-card-foot .od-actions')),
    buttons: Array.from(r.querySelectorAll('.od-actions button')).map(function (b) {
      var box = b.getBoundingClientRect(), st = getComputedStyle(b);
      var svg = b.querySelector('svg'), sb = svg ? svg.getBoundingClientRect() : null;
      var label = b.querySelector('.od-btn-label');
      return { text: b.textContent.trim(), act: b.dataset.act, cls: b.className,
               lines: window.__lines(label || b),
               icon: !!(svg && svg.childElementCount > 0 && sb.width > 0 && sb.height > 0),
               bg: st.backgroundColor, color: st.color, border: st.borderTopColor,
               left: box.left, right: box.right, top: box.top, bottom: box.bottom,
               width: box.width, height: box.height }; })
  }; })"""


def _cards(js):
    return {c["sid"]: c for c in js(CARDS_JS)}


def _page_overflow(js):
    return js("document.documentElement.scrollWidth - document.documentElement.clientWidth")


def _rgba(s: str) -> tuple:
    import re
    nums = [float(x) for x in re.findall(r"[\d.]+", s)]
    return tuple(nums + [1.0] * (4 - len(nums)))[:4]


def _check_every_card(js):
    """每一種寬度都要成立的：一個來源一張卡片、該有的都在卡片裡、文字不在不該折的地方折。"""
    cards = _cards(js)
    assert len(cards) == 4, list(cards)
    assert js("document.querySelectorAll('#odRows > tr').length") == 4, \
        "一個來源只能有一列（出處、範本清單都在同一張卡片裡，不再有第二列）"
    for sid, c in cards.items():
        assert c["isCard"], sid
        assert c["overflow"] <= 1, f"{sid} 卡片內容比卡片寬 {c['overflow']}px"
        assert c["name"][1] <= 2, f"{sid} 名稱折成 {c['name'][1]} 行"
        assert c["toggleInput"] and "啟用" in c["toggleText"], c["toggleText"]
        assert c["url"] and c["urlValue"].startswith("https://"), sid
        assert c["footIsLast"], f"{sid} 頁腳要在卡片最下面"
        # 按鈕：三顆、各有圖示、文字不在按鈕裡折行、一樣高、都在卡片裡
        bs = c["buttons"]
        assert [b["act"] for b in bs] == ["download", "upload", "delete"], bs
        assert all(b["icon"] for b in bs), f"{sid} 動作按鈕沒有圖示"
        assert all(b["lines"] == 1 for b in bs), f"{sid} 按鈕文字在按鈕裡折行：{bs}"
        assert max(b["height"] for b in bs) - min(b["height"] for b in bs) < 1, \
            f"{sid} 三顆按鈕要一樣高：{[b['height'] for b in bs]}"
        assert all(b["right"] <= c["inner"]["right"] + 1 and b["left"] >= c["inner"]["left"] - 1
                   for b in bs), f"{sid} 按鈕跑出卡片"
        # 按鈕在網址框下面（卡片底部），顯名在頁腳裡
        assert min(b["top"] for b in bs) >= c["url"]["bottom"], sid
        assert c["prov"] and c["foot"]["top"] >= c["url"]["bottom"], sid
    for sid, unit in ((T, "88 份範本"), (A, "2 個機關")):
        text, lines = cards[sid]["main"]
        assert "成功" in text and unit in text, text
        assert lines == 1, f"{sid} 的狀態折成 {lines} 行：{text}"
    assert _page_overflow(js) <= 0, "頁面出現橫向捲軸"
    return cards


def _check_wide(js):
    """寬的時候：開關在名稱同一行的右邊、按鈕排成一列靠右、網址框幾乎整張卡片寬。"""
    cards = _check_every_card(js)
    for sid, c in cards.items():
        assert c["name"][1] == 1, f"{sid} 名稱折行：{c['name']}"
        t, n = c["toggle"], c["nameBox"]
        assert t["left"] > n["right"] and t["top"] < n["bottom"], \
            f"{sid} 啟用開關要在名稱同一行的右邊：{t} vs {n}"
        assert abs(t["right"] - c["inner"]["right"]) <= 2, f"{sid} 開關要靠卡片右緣"
        assert len({round(b["top"]) for b in c["buttons"]}) == 1, \
            f"{sid} 三顆按鈕要排成一列：{c['buttons']}"
        assert abs(c["actions"]["right"] - c["inner"]["right"]) <= 2, f"{sid} 按鈕要靠右"
        # 顯名在按鈕左邊（同一個頁腳裡並排），不是另外一整列
        assert c["prov"]["right"] <= min(b["left"] for b in c["buttons"]), sid
        # 網址框：舊版的表格裡只剩 180 幾 px，看不到網域後面
        assert c["url"]["width"] >= 0.7 * c["inner"]["width"], \
            f"{sid} 網址框只有 {c['url']['width']:.0f}px（卡片 {c['inner']['width']:.0f}px）"
    return cards


# ---------------------------------------------------------------- 1440 寬

@pytest.fixture(scope="module")
def live():
    with live_admin_page(1440, 900) as h:
        yield h


def test_one_card_per_source_with_everything_inside(live):
    send, js, errs, extra = live
    _check_wide(js)
    # 不再是有表頭的表格
    assert js("document.querySelectorAll('#odRows th, .od-cards thead').length") == 0
    assert js("document.querySelector('.od-cards').getAttribute('role')") == "presentation"


def test_the_status_merges_result_and_count(live):
    send, js, errs, extra = live
    cards = _cards(js)
    # 「略過 N 個檔案」是狀態那一行的另一段，不擠進「成功 · 88 份範本」
    skip = js(f"""(function () {{
        var e = document.querySelector('#odRows > tr[data-sid="{T}"] .od-status-skip');
        return e ? [e.textContent, window.__lines(e)] : null; }})()""")
    assert skip and "略過 1 個檔案" in skip[0] and skip[1] == 1, skip
    assert "略過" not in cards[T]["main"][0]
    # 伺服器那一整句「已取得 88 份範本」不再出現
    assert "已取得" not in cards[T]["meta"]


def test_last_download_is_one_phrase_without_an_orphan_word(live):
    """舊版在時間底下另起一行小字「下載」，看起來像掉了一個標籤。"""
    send, js, errs, extra = live
    cards = _cards(js)
    when, lines = cards[T]["when"]
    assert when.startswith("最後下載："), when
    assert lines == 1, f"「最後下載」那一段折成 {lines} 行：{when}"
    when, lines = cards[A]["when"]
    assert when.startswith("最後上傳：") and lines == 1, when
    for c in cards.values():
        assert "下載" not in c["metaBits"] and "手動上傳" not in c["metaBits"], c["metaBits"]


def test_failed_and_never_downloaded_sources_say_so(live):
    send, js, errs, extra = live
    cards = _cards(js)
    bad = cards[extra["custom_bad"]]
    assert "失敗" in bad["main"][0] and "先前的資料照常可用" not in bad["meta"], bad["meta"]
    assert js(f"""!!document.querySelector('#odRows > tr[data-sid="{extra["custom_bad"]}"]'
                + ' .od-meta .od-msg-err')""")
    never = [c for k, c in cards.items() if k not in (T, A, extra["custom_bad"])]
    assert len(never) == 1, list(cards)
    assert never[0]["meta"].strip() == "尚未下載" and never[0]["when"] is None, never[0]["meta"]


def test_attribution_is_a_footnote_inside_the_card(live):
    send, js, errs, extra = live
    cards = _cards(js)
    st = {s["id"]: s for s in json.loads(
        js("fetch('/admin/official-doc/status').then(function(r){return r.text()})"))["sources"]}
    assert set(cards) == set(st)
    for sid, c in cards.items():
        attr = st[sid]["attribution"]
        assert c["provText"].startswith("顯名：" + attr), (attr, c["provText"])
        assert attr not in c["meta"]
    # 資料集網頁的連結接在顯名後面
    link = js(f"""(function () {{
        var a = document.querySelector('#odRows > tr[data-sid="{T}"] .od-prov a');
        return a ? [a.getAttribute('href'), a.textContent, a.target] : null; }})()""")
    assert link == ["https://data.gov.tw/dataset/30943", "資料集網頁", "_blank"], link
    # 顯名是小字、比名稱與狀態都小
    sizes = js(f"""(function () {{ var r = document.querySelector('#odRows > tr[data-sid="{T}"]');
        function fs(s) {{ return parseFloat(getComputedStyle(r.querySelector(s)).fontSize); }}
        return [fs('.od-prov'), fs('.od-meta'), fs('.od-src-name')]; }})()""")
    assert sizes[0] < sizes[1] < sizes[2], sizes
    # 範本清單在同一張卡片裡，展開得了
    js(f"document.querySelector('#odRows > tr[data-sid=\"{T}\"] details.od-details summary')"
       ".click(); true")
    end = time.time() + 15
    n = 0
    while time.time() < end:
        n = js(f"document.querySelectorAll('#odRows > tr[data-sid=\"{T}\"] .od-tpl-list li').length")
        if n:
            break
        time.sleep(0.2)
    assert n == 88, n


def test_delete_is_a_subtle_outline_not_a_solid_red_block(live):
    send, js, errs, extra = live
    for c in _cards(js).values():
        upd, up, dele = c["buttons"]
        assert "btn-primary" in upd["cls"], upd
        assert "btn-danger" not in dele["cls"], "刪除不要用實心紅色按鈕"
        bg, color = _rgba(dele["bg"]), _rgba(dele["color"])
        assert bg[3] == 0 or min(bg[:3]) >= 240, f"刪除按鈕的底色要是透明或淺色：{dele['bg']}"
        assert color[0] > 150 and color[1] < 100 and color[2] < 100, \
            f"刪除按鈕的字要是紅色：{dele['color']}"
        # 主要按鈕的底色不可以是透明（它才是最醒目的那一顆）
        assert _rgba(upd["bg"])[3] > 0.5, upd["bg"]


def test_usage_panel_explains_where_the_data_is_used(live):
    send, js, errs, extra = live
    got = js("""(function () {
        var p = document.getElementById('odUsage'); if (!p) return null;
        var h = p.querySelector('h2');
        var a = p.querySelector('a[href="/tools/official-doc/"]');
        var src = document.querySelector('.auth-form');
        return { heading: h ? h.textContent.trim() : '',
                 text: p.textContent,
                 link: a ? a.textContent.trim() : null,
                 icon: !!(a && a.querySelector('svg') && a.querySelector('svg').childElementCount),
                 before: !!(src && (p.compareDocumentPosition(src) & 4)) }; })()""")
    assert got, "沒有「下載之後會用在哪裡」那一張"
    assert got["heading"].endswith("下載之後會用在哪裡"), got["heading"]
    assert "公文範本" in got["text"] and "機關地址簿" in got["text"]
    assert got["link"] == "前往公文撰擬" and got["icon"], got
    assert got["before"], "這一張要放在「資料來源」前面"


def test_no_javascript_errors(live):
    send, js, errs, extra = live
    js("document.getElementById('odRefresh').click(); true")
    time.sleep(1.0)
    js("true")
    assert not errs, errs


# ---------------------------------------------------------------- 1100 寬（使用者這次回報的寬度）

@pytest.fixture(scope="module")
def medium():
    with live_admin_page(1100, 900) as h:
        yield h


def test_cards_at_1100_keep_one_row_of_buttons_and_a_wide_url_field(medium):
    send, js, errs, extra = medium
    _check_wide(js)
    assert not errs, errs


def test_url_edit_save_and_toggle_still_work(medium):
    """改版只動畫面：網址編輯、儲存、啟用開關照樣送得出去。"""
    send, js, errs, extra = medium
    sid = extra["custom_bad"]
    new_url = "https://example.com/moved/book.json"
    js(f"""(function () {{
        var inp = document.querySelector('input.od-url-input[data-sid="{sid}"]');
        inp.value = {json.dumps(new_url)};
        inp.dispatchEvent(new Event('input'));
        return true; }})()""")
    btn = js(f"""(function () {{
        var b = document.querySelector('button.od-url-save[data-sid="{sid}"]');
        var inp = document.querySelector('input.od-url-input[data-sid="{sid}"]');
        if (!b || b.hidden) return null;
        var bb = b.getBoundingClientRect(), ib = inp.getBoundingClientRect();
        return [!!b.querySelector('svg'), window.__lines(b.querySelector('.od-btn-label')),
                bb.top < ib.bottom && bb.left >= ib.right - 1]; }})()""")
    assert btn == [True, 1, True], f"「儲存網址」要出現在網址框右邊：{btn}"
    js(f"document.querySelector('button.od-url-save[data-sid=\"{sid}\"]').click(); true")
    js(f"document.querySelector('input[data-act=\"toggle\"][data-sid=\"{sid}\"]').click(); true")
    end = time.time() + 15
    src = None
    while time.time() < end:
        st = json.loads(js("fetch('/admin/official-doc/status').then(function(r){return r.text()})"))
        src = next(s for s in st["sources"] if s["id"] == sid)
        if src["url"] == new_url and src["enabled"] is False:
            break
        time.sleep(0.3)
    assert src["url"] == new_url and src["enabled"] is False, src
    # 停用之後畫面也看得出來（卡片變灰）
    end = time.time() + 5
    off = False
    while time.time() < end and not off:
        off = js(f"document.querySelector('#odRows > tr[data-sid=\"{sid}\"]')"
                 ".classList.contains('is-off')")
        time.sleep(0.2)
    assert off, "停用的來源卡片要有 is-off"
    assert not errs, errs


# ---------------------------------------------------------------- 390 寬（手機）

@pytest.fixture(scope="module")
def phone():
    with live_admin_page(390, 844) as h:
        yield h


def test_phone_width_stacks_without_horizontal_scroll(phone):
    """手機：標籤整列在名稱下面、狀態一項一行、顯名在按鈕上面、按鈕換行不出卡片，
    頁面兩側至少 16px，沒有橫向捲軸。"""
    send, js, errs, extra = phone
    cards = _check_every_card(js)
    vw = js("document.documentElement.clientWidth")
    for sid, c in cards.items():
        assert c["card"]["left"] >= 16 and c["card"]["right"] <= vw - 16, \
            f"{sid} 卡片貼到螢幕邊（{c['card']['left']:.0f}～{c['card']['right']:.0f}／{vw}）"
        n, t = c["nameBox"], c["toggle"]
        assert t["top"] < n["bottom"] and t["left"] >= n["right"] - 1, \
            f"{sid} 開關要在名稱右邊：{t} vs {n}"
        assert all(b["top"] >= n["bottom"] - 1 for b in c["badges"]), f"{sid} 標籤要在名稱下面"
        assert c["prov"]["bottom"] <= min(b["top"] for b in c["buttons"]), \
            f"{sid} 窄的時候顯名在按鈕上面"
        assert c["url"]["width"] >= 0.95 * c["inner"]["width"], sid
    # 狀態一項一行（「最後下載」不跟「成功 · 88 份範本」擠在同一行）
    t = cards[T]
    when_top = js(f"document.querySelector('#odRows > tr[data-sid=\"{T}\"] .od-when')"
                  ".getBoundingClientRect().top")
    main_bottom = js(f"document.querySelector('#odRows > tr[data-sid=\"{T}\"] .od-status-main')"
                     ".getBoundingClientRect().bottom")
    assert when_top >= main_bottom - 1, (when_top, main_bottom, t["meta"])
    assert not errs, errs
