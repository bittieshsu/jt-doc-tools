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

第三次（2026-10-09，使用者截圖：「這區看起來還是有點雜亂」）：每張卡片同時攤開兩個標籤、
一整條百分比編碼的長網址、兩行顯名文字和三顆不同顏色的按鈕。現在每張卡片只留三層：

* 標題：類型圖示＋名稱，名稱下一行小字寫類型、「內建」與資料集網頁，右邊「啟用」開關；
* 狀態與主要動作同一列：狀態點（綠／紅／灰）＋數量＋最後下載時間｜「更新」「上傳檔案」；
* 收起來的「列出範本」與「來源設定」（下載網址、刪除；下載失敗時自動打開），
  最後一行出處小字。「新增來源」「還原預設」移到卡片下面，「重新整理」拿掉。

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

#: 每一張卡片的幾何：內容區、名稱、類型行、開關、狀態、主要按鈕、來源設定、出處。
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
  var dot = main ? main.querySelector('.od-dot') : null;
  var cfg = r.querySelector('details.od-cfg');
  var body = r.querySelector('.od-card-body');
  var prov = r.querySelector('.od-prov');
  function btn(b) {
    var box = b.getBoundingClientRect(), st = getComputedStyle(b);
    var svg = b.querySelector('svg'), sb = svg ? svg.getBoundingClientRect() : null;
    var label = b.querySelector('.od-btn-label');
    return { text: b.textContent.trim(), act: b.dataset.act, cls: b.className,
             lines: window.__lines(label || b),
             icon: !!(svg && svg.childElementCount > 0 && sb.width > 0 && sb.height > 0),
             bg: st.backgroundColor, color: st.color, border: st.borderTopColor,
             left: box.left, right: box.right, top: box.top, bottom: box.bottom,
             width: box.width, height: box.height }; }
  return {
    sid: r.dataset.sid,
    isCard: r.classList.contains('od-card'),
    card: rect(r), inner: inner,
    overflow: td.scrollWidth - td.clientWidth,
    name: [name ? name.textContent : null, window.__lines(name)], nameBox: rect(name),
    sub: rect(r.querySelector('.od-src-sub')),
    subText: (r.querySelector('.od-src-sub') || {}).textContent || '',
    badges: r.querySelectorAll('.od-badge').length,
    toggle: rect(r.querySelector('label.od-switch')),
    toggleText: (r.querySelector('label.od-switch') || {}).textContent || '',
    toggleInput: !!r.querySelector('label.od-switch input[type=checkbox][data-act="toggle"][data-sid="' + r.dataset.sid + '"]'),
    meta: (r.querySelector('.od-meta') || {}).textContent || '',
    metaBox: rect(r.querySelector('.od-meta')),
    main: main ? [main.textContent, window.__lines(main)] : null,
    dot: dot ? dot.className : null,
    when: when ? [when.textContent, window.__lines(when)] : null,
    metaBits: Array.from(r.querySelectorAll('.od-meta *')).map(function (e) {
      return e.childElementCount ? null : e.textContent.trim(); }).filter(Boolean),
    cfgOpen: cfg ? cfg.open : null,
    cfgSummary: cfg ? cfg.querySelector('summary').textContent : null,
    urlInCfg: !!(cfg && cfg.querySelector('input.od-url-input[data-sid="' + r.dataset.sid + '"]')),
    urlOutsideCfg: Array.from(r.querySelectorAll('input.od-url-input')).filter(function (i) {
      return !i.closest('details.od-cfg'); }).length,
    url: rect(r.querySelector('input.od-url-input')),
    urlValue: (r.querySelector('input.od-url-input') || {}).value,
    del: (function () { var d = r.querySelector('button[data-act="delete"]');
      return d ? Object.assign(btn(d), { inCfg: !!d.closest('details.od-cfg') }) : null; })(),
    prov: rect(prov),
    provText: prov ? prov.textContent : '',
    provIsLast: !!(prov && body && prov.parentElement === body && !prov.nextElementSibling),
    actions: rect(r.querySelector('.od-card-main .od-actions')),
    buttons: Array.from(r.querySelectorAll('.od-card-main .od-actions button')).map(btn)
  }; })"""


def _cards(js):
    return {c["sid"]: c for c in js(CARDS_JS)}


def _page_overflow(js):
    return js("document.documentElement.scrollWidth - document.documentElement.clientWidth")


def _rgba(s: str) -> tuple:
    import re
    nums = [float(x) for x in re.findall(r"[\d.]+", s)]
    return tuple(nums + [1.0] * (4 - len(nums)))[:4]


def _check_every_card(js, extra):
    """每一種寬度都要成立的：一個來源一張卡片、只有三層、主要按鈕兩顆、網址與刪除收在
    「來源設定」裡、出處在最後、文字不在不該折的地方折。"""
    cards = _cards(js)
    assert len(cards) == 4, list(cards)
    assert js("document.querySelectorAll('#odRows > tr').length") == 4, \
        "一個來源只能有一列（出處、範本清單都在同一張卡片裡，不再有第二列）"
    for sid, c in cards.items():
        assert c["isCard"], sid
        assert c["overflow"] <= 1, f"{sid} 卡片內容比卡片寬 {c['overflow']}px"
        assert c["name"][1] <= 2, f"{sid} 名稱折成 {c['name'][1]} 行"
        assert c["badges"] == 0, f"{sid} 名稱旁邊不再放標籤（類型寫在名稱下面那一行）"
        assert c["sub"] and c["sub"]["top"] >= c["nameBox"]["bottom"] - 1, f"{sid} 類型那一行要在名稱下面"
        assert c["toggleInput"] and "啟用" in c["toggleText"], c["toggleText"]
        # 主要按鈕：兩顆（更新／下載、上傳檔案），各有圖示、不折行、一樣高、都在卡片裡
        bs = c["buttons"]
        assert [b["act"] for b in bs] == ["download", "upload"], bs
        assert "btn-primary" in bs[0]["cls"] and _rgba(bs[0]["bg"])[3] > 0.5, bs[0]
        assert all(b["icon"] for b in bs), f"{sid} 動作按鈕沒有圖示"
        assert all(b["lines"] == 1 for b in bs), f"{sid} 按鈕文字在按鈕裡折行：{bs}"
        assert abs(bs[0]["height"] - bs[1]["height"]) < 1, f"{sid} 兩顆按鈕要一樣高"
        assert all(b["right"] <= c["inner"]["right"] + 1 and b["left"] >= c["inner"]["left"] - 1
                   for b in bs), f"{sid} 按鈕跑出卡片"
        # 網址與刪除收在「來源設定」裡，不在卡片上攤開
        assert c["cfgSummary"] == "來源設定", c["cfgSummary"]
        assert c["urlInCfg"] and c["urlOutsideCfg"] == 0, f"{sid} 網址框要收在來源設定裡"
        assert c["urlValue"].startswith("https://"), sid
        assert c["del"] and c["del"]["inCfg"], f"{sid} 刪除要在來源設定裡，不跟主要按鈕並排"
        # 出處是卡片最後一行，在按鈕下面
        assert c["provIsLast"], f"{sid} 出處要在卡片最下面"
        assert c["prov"]["top"] >= max(b["bottom"] for b in bs), f"{sid} 出處要在按鈕下面"
    # 下載失敗的那一個「來源設定」自動打開（多半要改網址）；其他的收著
    for sid, c in cards.items():
        assert c["cfgOpen"] is (sid == extra["custom_bad"]), (sid, c["cfgOpen"])
    for sid, unit in ((T, "88 份範本"), (A, "2 個機關")):
        text, lines = cards[sid]["main"]
        assert unit in text and "ok" in (cards[sid]["dot"] or ""), (text, cards[sid]["dot"])
        assert lines == 1, f"{sid} 的狀態折成 {lines} 行：{text}"
    # 工具列在卡片下面，「重新整理」拿掉了
    tools = js("""(function () { var a = document.getElementById('odAddToggle');
        var rows = document.querySelectorAll('#odRows > tr');
        return [a.getBoundingClientRect().top, rows[rows.length - 1].getBoundingClientRect().bottom,
                !!document.getElementById('odRefresh'),
                !!document.getElementById('odRestore')]; })()""")
    assert tools[0] >= tools[1], f"「新增來源」要在卡片下面：{tools}"
    assert tools[2] is False and tools[3] is True, tools
    assert _page_overflow(js) <= 0, "頁面出現橫向捲軸"
    return cards


def _check_wide(js, extra):
    """寬的時候：開關在名稱同一行的右邊；按鈕跟狀態同一列、靠右。"""
    cards = _check_every_card(js, extra)
    for sid, c in cards.items():
        assert c["name"][1] == 1, f"{sid} 名稱折行：{c['name']}"
        t, n = c["toggle"], c["nameBox"]
        assert t["left"] > n["right"] and t["top"] < c["sub"]["bottom"], \
            f"{sid} 啟用開關要在名稱右邊：{t} vs {n}"
        assert abs(t["right"] - c["inner"]["right"]) <= 2, f"{sid} 開關要靠卡片右緣"
        bs = c["buttons"]
        assert len({round(b["top"]) for b in bs}) == 1, f"{sid} 兩顆按鈕要排成一列：{bs}"
        assert abs(c["actions"]["right"] - c["inner"]["right"]) <= 2, f"{sid} 按鈕要靠右"
        # 按鈕跟狀態同一列（垂直範圍重疊），在狀態右邊
        m = c["metaBox"]
        assert bs[0]["top"] < m["bottom"] and bs[0]["bottom"] > m["top"], f"{sid} 按鈕要跟狀態同一列"
        assert min(b["left"] for b in bs) >= m["left"], sid
    return cards


# ---------------------------------------------------------------- 1440 寬

@pytest.fixture(scope="module")
def live():
    with live_admin_page(1440, 900) as h:
        yield h


def test_one_card_per_source_with_three_layers(live):
    send, js, errs, extra = live
    _check_wide(js, extra)
    # 不再是有表頭的表格
    assert js("document.querySelectorAll('#odRows th, .od-cards thead').length") == 0
    assert js("document.querySelector('.od-cards').getAttribute('role')") == "presentation"


def test_the_status_is_a_dot_a_count_and_the_time(live):
    send, js, errs, extra = live
    cards = _cards(js)
    # 「略過 N 個檔案」是狀態那一行的另一段，不擠進「88 份範本」
    skip = js(f"""(function () {{
        var e = document.querySelector('#odRows > tr[data-sid="{T}"] .od-status-skip');
        return e ? [e.textContent, window.__lines(e)] : null; }})()""")
    assert skip and "略過 1 個檔案" in skip[0] and skip[1] == 1, skip
    assert "略過" not in cards[T]["main"][0]
    # 伺服器那一整句「已取得 88 份範本」不再出現；成功不再另外寫一個「成功」
    assert "已取得" not in cards[T]["meta"]
    assert "成功" not in cards[T]["main"][0], cards[T]["main"]
    # 狀態點真的看得到（有大小、有顏色）
    dot = js(f"""(function () {{
        var d = document.querySelector('#odRows > tr[data-sid="{T}"] .od-status-main .od-dot');
        var b = d.getBoundingClientRect();
        return [b.width, b.height, getComputedStyle(d).backgroundColor]; }})()""")
    assert dot[0] >= 6 and dot[1] >= 6, dot
    g = _rgba(dot[2])
    assert g[1] > g[0] and g[1] > g[2], f"成功的點要是綠色：{dot[2]}"


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
    assert "失敗" in bad["main"][0] and "err" in (bad["dot"] or ""), bad
    assert "先前的資料照常可用" not in bad["meta"], bad["meta"]
    assert js(f"""!!document.querySelector('#odRows > tr[data-sid="{extra["custom_bad"]}"]'
                + ' .od-meta .od-msg-err')""")
    never = [c for k, c in cards.items() if k not in (T, A, extra["custom_bad"])]
    assert len(never) == 1, list(cards)
    assert never[0]["meta"].strip() == "尚未下載" and never[0]["when"] is None, never[0]["meta"]
    assert "ok" not in (never[0]["dot"] or "") and "err" not in (never[0]["dot"] or "")


def test_attribution_is_the_last_line_and_the_dataset_link_sits_under_the_name(live):
    send, js, errs, extra = live
    cards = _cards(js)
    st = {s["id"]: s for s in json.loads(
        js("fetch('/admin/official-doc/status').then(function(r){return r.text()})"))["sources"]}
    assert set(cards) == set(st)
    for sid, c in cards.items():
        attr = st[sid]["attribution"]
        assert c["provText"] == "出處：" + attr, (attr, c["provText"])
        assert attr not in c["meta"]
    # 類型行：類型、內建、資料集網頁
    assert cards[T]["subText"].startswith("公文範本") and "內建" in cards[T]["subText"]
    assert "內建" not in cards[extra["custom_bad"]]["subText"]
    link = js(f"""(function () {{
        var a = document.querySelector('#odRows > tr[data-sid="{T}"] .od-src-sub a');
        return a ? [a.getAttribute('href'), a.textContent, a.target] : null; }})()""")
    assert link == ["https://data.gov.tw/dataset/30943", "資料集網頁", "_blank"], link
    assert js(f"document.querySelectorAll('#odRows > tr[data-sid=\"{T}\"] .od-prov a').length") == 0
    # 出處是小字、比名稱與狀態都小
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
    js(f"document.querySelector('#odRows > tr[data-sid=\"{T}\"] details.od-details summary')"
       ".click(); true")


def test_delete_is_a_subtle_outline_inside_the_source_settings(live):
    send, js, errs, extra = live
    # 打開範本那一張的來源設定，量刪除按鈕
    js(f"document.querySelector('#odRows > tr[data-sid=\"{T}\"] details.od-cfg summary').click(); true")
    time.sleep(0.2)
    c = _cards(js)[T]
    dele = c["del"]
    assert dele["text"] == "刪除這個來源" and dele["icon"], dele
    assert "btn-danger" not in dele["cls"], "刪除不要用實心紅色按鈕"
    bg, color = _rgba(dele["bg"]), _rgba(dele["color"])
    assert bg[3] == 0 or min(bg[:3]) >= 240, f"刪除按鈕的底色要是透明或淺色：{dele['bg']}"
    assert color[0] > 150 and color[1] < 100 and color[2] < 100, \
        f"刪除按鈕的字要是紅色：{dele['color']}"
    note = js(f"""document.querySelector('#odRows > tr[data-sid="{T}"] .od-cfg-note').textContent""")
    assert "還原預設" in note, note          # 內建的：講得出怎麼放回來
    js(f"document.querySelector('#odRows > tr[data-sid=\"{T}\"] details.od-cfg summary').click(); true")


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


def test_address_book_trial_search_marks_the_matching_characters(live):
    """試查機關地址簿：符合的字標亮（2026-10-09 使用者：「搜尋有符合的 該字串要高亮」）。
    好幾個詞都標；只打代碼開頭時標代碼那一段。"""
    send, js, errs, extra = live

    def search(q):
        js(f"var q = document.getElementById('odOrgQ'); q.value = {json.dumps(q)};"
           " q.dispatchEvent(new Event('input')); true")
        time.sleep(0.8)            # 打字之後 250ms 才查（不要讀到上一次的清單）
        end = time.time() + 10
        rows = []
        while time.time() < end:
            rows = js("""Array.from(document.querySelectorAll('#odOrgs li')).map(function (li) {
                var s = li.querySelectorAll(':scope > span');
                function m(e) { return Array.from(e.querySelectorAll('mark.od-hl')).map(function (x) {
                    return x.textContent; }); }
                return {id: s[0].textContent, name: s[1].textContent, idm: m(s[0]), nm: m(s[1])}; })""")
            if rows:
                return rows
            time.sleep(0.2)
        return rows

    rows = search("嘉禾 公所")
    assert [r["name"] for r in rows] == ["嘉禾市東湖區公所"], rows
    assert rows[0]["nm"] == ["嘉禾", "公所"] and rows[0]["idm"] == [], rows
    rows = search("Q2000")
    assert rows and rows[0]["idm"] == ["Q2000"] and rows[0]["nm"] == [], rows
    bg = js("getComputedStyle(document.querySelector('#odOrgs mark.od-hl')).backgroundColor")
    assert bg == "rgb(253, 230, 138)", bg
    js("var q = document.getElementById('odOrgQ'); q.value = ''; q.dispatchEvent(new Event('input')); true")


def test_no_javascript_errors(live):
    send, js, errs, extra = live
    js("document.getElementById('odAddToggle').click(); true")
    time.sleep(0.5)
    js("document.getElementById('odAddToggle').click(); true")
    js("true")
    assert not errs, errs


# ---------------------------------------------------------------- 1100 寬（使用者上一次回報的寬度）

@pytest.fixture(scope="module")
def medium():
    with live_admin_page(1100, 900) as h:
        yield h


def test_cards_at_1100_keep_buttons_beside_the_status(medium):
    send, js, errs, extra = medium
    _check_wide(js, extra)
    assert not errs, errs


def test_url_edit_save_and_toggle_still_work(medium):
    """改版只動畫面：打開來源設定改網址、儲存、啟用開關照樣送得出去；
    改了還沒存的網址在重畫之後來源設定照樣開著。"""
    send, js, errs, extra = medium
    sid = T
    new_url = "https://example.com/moved/t.zip"
    js(f"document.querySelector('#odRows > tr[data-sid=\"{sid}\"] details.od-cfg summary').click(); true")
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
                bb.top < ib.bottom && bb.left >= ib.right - 1, ib.width]; }})()""")
    assert btn and btn[:3] == [True, 1, True], f"「儲存網址」要出現在網址框右邊：{btn}"
    assert btn[3] >= 300, f"網址框太窄：{btn[3]}"
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
    # 停用之後畫面也看得出來（卡片變灰）；重畫之後使用者打開的來源設定照樣開著
    end = time.time() + 5
    off = False
    while time.time() < end and not off:
        off = js(f"document.querySelector('#odRows > tr[data-sid=\"{sid}\"]')"
                 ".classList.contains('is-off')")
        time.sleep(0.2)
    assert off, "停用的來源卡片要有 is-off"
    assert js(f"document.querySelector('#odRows > tr[data-sid=\"{sid}\"] details.od-cfg').open") is True
    assert not errs, errs


# ---------------------------------------------------------------- 390 寬（手機）

@pytest.fixture(scope="module")
def phone():
    with live_admin_page(390, 844) as h:
        yield h


def test_phone_width_stacks_without_horizontal_scroll(phone):
    """手機：圖示收起來、名稱與開關同一行、類型那一行在名稱下面、狀態一項一行、
    按鈕在狀態下面平分整列、出處在最後；頁面兩側至少 16px，沒有橫向捲軸。"""
    send, js, errs, extra = phone
    cards = _check_every_card(js, extra)
    vw = js("document.documentElement.clientWidth")
    for sid, c in cards.items():
        assert c["card"]["left"] >= 16 and c["card"]["right"] <= vw - 16, \
            f"{sid} 卡片貼到螢幕邊（{c['card']['left']:.0f}～{c['card']['right']:.0f}／{vw}）"
        n, t = c["nameBox"], c["toggle"]
        assert t["top"] < n["bottom"] and t["left"] >= n["right"] - 1, \
            f"{sid} 開關要在名稱右邊：{t} vs {n}"
        assert c["sub"]["top"] >= n["bottom"] - 1, f"{sid} 類型那一行要在名稱下面"
        bs = c["buttons"]
        assert min(b["top"] for b in bs) >= c["metaBox"]["bottom"] - 1, f"{sid} 按鈕要在狀態下面"
        assert sum(b["width"] for b in bs) >= 0.85 * c["inner"]["width"], \
            f"{sid} 按鈕要平分整列：{[b['width'] for b in bs]}"
    icon_shown = js("Array.from(document.querySelectorAll('.od-card-icon')).some(function (e) {"
                    " return e.getBoundingClientRect().width > 0; })")
    assert icon_shown is False, "手機上類型圖示要收起來（把寬度留給名稱）"
    # 狀態一項一行（「最後下載」不跟「88 份範本」擠在同一行）
    when_top = js(f"document.querySelector('#odRows > tr[data-sid=\"{T}\"] .od-when')"
                  ".getBoundingClientRect().top")
    main_bottom = js(f"document.querySelector('#odRows > tr[data-sid=\"{T}\"] .od-status-main')"
                     ".getBoundingClientRect().bottom")
    assert when_top >= main_bottom - 1, (when_top, main_bottom, cards[T]["meta"])
    # 失敗那一張自動打開的來源設定：網址框幾乎整張卡片寬
    bad = cards[extra["custom_bad"]]
    assert bad["url"]["width"] >= 0.8 * bad["inner"]["width"], bad["url"]
    assert not errs, errs
