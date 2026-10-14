"""歷史案件表格 —— 真的在瀏覽器裡跑一次（2026-10-10 使用者：「案件名稱如果字多 要截斷搭配... 移過去才顯示全文」
「表格要題 要可以按下排序 要可以挑選要顯示的欄位 如果過多筆，會不會分頁?」）。

* 名稱太長：截斷加「…」（量 `scrollWidth > clientWidth`）、`title` 是全文；表格不會撐出外框。
* 欄位標題按下去排序（再按一次反過來），`aria-sort` 跟著變；重新整理之後照舊。
* 「顯示欄位」藏掉一欄：標題與每一格都不見；重新整理之後照舊。
* 分頁：每頁 20 件，第 2 頁剩下的；改成每頁 50 件全部出來。全選只全選這一頁，翻頁之後原本勾的照樣算。

伺服器、假的語言模型沿用 `test_official_doc_e2e_kb` 的 `live`（每個測試檔各自一份全新的實例）。
"""
from __future__ import annotations

import httpx

from app.core import official_doc_di as di
from tests.test_official_doc_cases_table import LONG
from tests.test_official_doc_di import _letter
from tests.test_official_doc_e2e_kb import _js, _wait, live  # noqa: F401

N = 25


def _seed(port: int) -> None:
    files = []
    for i in range(N):
        subj = LONG if i == 7 else f"第{i:02d}號測試案件函送資料"
        text = _letter(content={"subject": subj, "explanation": ["依規定辦理。"], "measures": []})
        files.append(("files", (f"{i:02d}.di", di.build(text, "letter")[0], "application/xml")))
    r = httpx.post(f"http://127.0.0.1:{port}/tools/official-doc/cases/import", files=files, timeout=60)
    assert r.status_code == 200 and len(r.json()["imported"]) == N, r.text[:500]


def _open(send, port: int) -> None:
    send("Runtime.enable")
    send("Page.enable")
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/official-doc/cases"})
    assert _wait(send, "document.readyState === 'complete' && !!document.querySelector('.odc-tbl')"), \
        "歷史案件頁沒有載入"


_VISIBLE = "Array.from(document.querySelectorAll('tr[data-case-id]')).filter(function (r) { return !r.hidden; })"


def _names(send) -> list[str]:
    return _js(send, _VISIBLE + ".map(function (r) { return r.querySelector('.odc-name-text').textContent; })")


def test_sort_columns_paging_and_long_names(live):  # noqa: F811
    port, send, errs = live
    _seed(port)
    _open(send, port)
    _js(send, "localStorage.removeItem('jtdt.odc.view'), true")
    _open(send, port)

    # 分頁：預設每頁 20 件
    assert _js(send, _VISIBLE + ".length") == 20
    assert not _js(send, "document.getElementById('odcPager').hidden"), "超過一頁要有分頁"
    assert "1–20" in _js(send, "document.getElementById('odcRange').textContent")
    assert _js(send, "document.getElementById('odcPrev').disabled")
    _js(send, "document.getElementById('odcNext').click(), true")
    assert _js(send, _VISIBLE + ".length") == N - 20
    assert "2 / 2" in _js(send, "document.getElementById('odcPageNo').textContent")
    _js(send, "document.getElementById('odcPrev').click(), true")

    # 全選只全選這一頁；翻頁之後原本勾的照樣算
    _js(send, "document.getElementById('odcAll').click(), true")
    assert _js(send, "document.querySelectorAll('input[data-odc-pick]:checked').length") == 20
    _js(send, "document.getElementById('odcNext').click(), true")
    assert not _js(send, "document.getElementById('odcAll').checked"), "第 2 頁還沒勾，全選框不可以是勾著的"
    assert "20" in _js(send, "document.getElementById('odcSel').textContent")
    assert not _js(send, "document.getElementById('odcBatch').disabled")
    _js(send, "document.getElementById('odcPrev').click(), true")
    _js(send, "document.getElementById('odcAll').click(), true")          # 取消

    # 每頁 50 件：全部出來
    _js(send, """(function () { var s = document.getElementById('odcPageSize'); s.value = '50';
        s.dispatchEvent(new Event('change', {bubbles: true})); return true; })()""")
    assert _js(send, _VISIBLE + ".length") == N

    # 排序：預設最後修改新的在前；按「案件」照名稱排，再按一次反過來
    assert _js(send, "document.querySelector('[data-sort=\"updated\"]').closest('th').getAttribute('aria-sort')") \
        == "descending"
    _js(send, "document.querySelector('[data-sort=\"name\"]').click(), true")
    # 中文的先後由瀏覽器的排序規則決定（筆畫或注音），驗編號那一組的順序就好
    numbered = [x for x in _names(send) if x.startswith("第")]
    assert numbered == sorted(numbered), numbered[:5]
    assert _js(send, "document.querySelector('[data-sort=\"name\"]').closest('th').getAttribute('aria-sort')") \
        == "ascending"
    _js(send, "document.querySelector('[data-sort=\"name\"]').click(), true")
    desc = [x for x in _names(send) if x.startswith("第")]
    assert desc == sorted(desc, reverse=True), desc[:5]

    # 名稱太長：截斷、title 是全文、表格不撐出外框
    long = _js(send, """(function () {
        var a = Array.from(document.querySelectorAll('.odc-name-text')).find(function (x) {
            return x.title.indexOf('汰換預算') >= 0; });
        if (!a) return null;
        var tbl = document.querySelector('.odc-tbl'), panel = document.getElementById('odcPanel');
        return {cut: a.scrollWidth > a.clientWidth + 1, title: a.title, text: a.textContent,
                ellipsis: getComputedStyle(a).textOverflow,
                fits: tbl.getBoundingClientRect().right <= panel.getBoundingClientRect().right + 1,
                h: a.getBoundingClientRect().height,
                lh: parseFloat(getComputedStyle(a).lineHeight) || 20}; })()""")
    assert long, "找不到那件長主旨的案件"
    assert long["cut"] and long["ellipsis"] == "ellipsis", long
    assert "以利統籌編列下年度汰換預算" in long["title"], "滑鼠移過去要看得到全文：" + long["title"]
    assert long["fits"], "長名稱把表格撐出外框"
    # 量名稱本身（不是整格）：別的欄位窄的時候可以折一次，整列變兩行不算名稱折行
    assert long["h"] < long["lh"] * 1.6, f"長名稱折成好幾行（高 {long['h']}）"

    # 顯示欄位：藏掉「版本」
    _js(send, "document.querySelector('#odcCols > summary').click(), true")
    _js(send, "document.querySelector('[data-odc-col=\"rev\"]').click(), true")
    hidden = _js(send, """(function () {
        var th = document.querySelector('th[data-col="rev"]');
        var tds = Array.from(document.querySelectorAll('td[data-col="rev"]'));
        return getComputedStyle(th).display === 'none' &&
               tds.every(function (t) { return getComputedStyle(t).display === 'none'; }); })()""")
    assert hidden, "藏掉的欄位還看得到"

    # 重新整理：排序、藏的欄位、每頁幾件都照舊
    _open(send, port)
    assert _js(send, "document.querySelector('[data-sort=\"name\"]').closest('th').getAttribute('aria-sort')") \
        == "descending"
    assert _js(send, "getComputedStyle(document.querySelector('th[data-col=\"rev\"]')).display") == "none"
    assert not _js(send, "document.querySelector('[data-odc-col=\"rev\"]').checked")
    assert _js(send, _VISIBLE + ".length") == N, "每頁幾件沒有記住"
    # 打開回來
    _js(send, "document.querySelector('#odcCols > summary').click(), true")
    _js(send, "document.querySelector('[data-odc-col=\"rev\"]').click(), true")
    assert _js(send, "getComputedStyle(document.querySelector('th[data-col=\"rev\"]')).display") != "none"

    # 手機寬度：一件一張卡片，長名稱照樣截斷、不撐出畫面
    send("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2,
                                                "mobile": True})
    try:
        _open(send, port)
        phone = _js(send, """(function () {
            var a = Array.from(document.querySelectorAll('.odc-name-text')).find(function (x) {
                return x.title.indexOf('汰換預算') >= 0; });
            if (!a) return null;
            return {cut: a.scrollWidth > a.clientWidth + 1, w: a.getBoundingClientRect().width,
                    page: document.documentElement.scrollWidth, vw: window.innerWidth}; })()""")
    finally:
        send("Emulation.clearDeviceMetricsOverride", {})
    assert phone and phone["cut"] and phone["w"] > 100, phone
    assert phone["page"] <= phone["vw"] + 1, f"手機寬度出現橫向捲動：{phone}"
    assert not errs, errs


def test_the_doc_number_column_can_be_filled_sorted_and_hidden(live):  # noqa: F811
    """文號欄（2026-10-14 使用者：「加個 文號 欄位」）：在清單上補填 → 存檔 → 重新整理照舊；
    可以排序、可以在「顯示欄位」藏起來。"""
    port, send, errs = live
    if not httpx.get(f"http://127.0.0.1:{port}/tools/official-doc/api/cases", timeout=30).json()["cases"]:
        _seed(port)
    _open(send, port)
    _js(send, "localStorage.removeItem('jtdt.odc.view'), true")
    _open(send, port)
    assert _js(send, "!!document.querySelector('th[data-col=\"docno\"]')"), "沒有文號欄"
    cid = _js(send, _VISIBLE + "[0].dataset.caseId")
    _js(send, "document.querySelector('tr[data-case-id=\"%s\"] [data-odc-docno]').click(), true" % cid)
    assert _wait(send, "!!document.querySelector('.modal-input')"), "文號對話框沒有出現"
    _js(send, "(function(){var i=document.querySelector('.modal-input'); i.value='府資字第1150077777號';"
              "document.querySelector('.modal-ok').click(); return true;})()")
    cell = "document.querySelector('tr[data-case-id=\"%s\"] [data-odc-docno-text]').textContent" % cid
    assert _wait(send, cell + " === '府資字第1150077777號'"), _js(send, cell)
    # 重新整理：存在伺服器上
    _open(send, port)
    assert _js(send, cell) == "府資字第1150077777號"
    # 依文號排序（大到小）：有文號的排最前面
    _js(send, "document.querySelector('[data-sort=\"docno\"]').click(), true")
    _js(send, "document.querySelector('[data-sort=\"docno\"]').click(), true")
    first = _js(send, _VISIBLE + "[0].dataset.caseId")
    assert first == cid, "依文號排序沒有作用"
    # 顯示欄位：藏掉文號
    _js(send, "document.querySelector('#odcCols > summary').click(), true")
    _js(send, "document.querySelector('[data-odc-col=\"docno\"]').click(), true")
    assert _js(send, "getComputedStyle(document.querySelector('th[data-col=\"docno\"]')).display") == "none"
    _js(send, "document.querySelector('[data-odc-col=\"docno\"]').click(), true")
    _js(send, "localStorage.removeItem('jtdt.odc.view'), true")
    assert not errs, errs


_FIT = """(function () {
  var p = document.getElementById('odcPanel'), tbl = p.querySelector('.odc-tbl');
  var cs = getComputedStyle(p), room = p.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  var min = parseFloat(getComputedStyle(tbl).fontSize) * 8;
  var rows = Array.from(document.querySelectorAll('tr[data-case-id]')).filter(function (r) { return !r.hidden; });
  var short = rows.map(function (r) { var a = r.querySelector('.odc-name-text');
      return a.clientWidth + 1 >= Math.min(min, a.scrollWidth) ? null : a.clientWidth; })
    .filter(function (x) { return x !== null; });
  var lh = parseFloat(getComputedStyle(tbl).lineHeight) || 20;
  var docTall = rows.filter(function (r) {
      return r.querySelector('[data-odc-docno-text]').getBoundingClientRect().height > lh * 1.6; }).length;
  var labels = Array.from(p.querySelectorAll('tr[data-case-id]:not([hidden]) .odc-btn-label'))
    .filter(function (s) { return s.getBoundingClientRect().width <= 1; }).length;
  return {fit: Array.from(p.classList).filter(function (c) { return c.indexOf('odc-fit') === 0; }),
          over: tbl.offsetWidth > room + 1, page: document.documentElement.scrollWidth, vw: innerWidth,
          short: short, docTall: docTall, hiddenLabels: labels};
})()"""


def test_names_stay_readable_at_common_widths_in_every_locale(live):  # noqa: F811
    """多了文號這一欄之後，固定的寬度門檻在 1280 寬名稱只剩 15px、日文 1440 寬只剩兩個字（瀏覽器實量）。
    改成由頁面**實量**一段一段收（折文號與時間 → 只留圖示 → 標籤折行 → 卡片）。判準：

    * 每一列的名稱至少拿到 8 個字寬（或整個看得到）；表格不超出外框；頁面沒有橫向捲動。
    * 中文 1440 以上什麼都不必收 —— 不可以為了窄的情況讓寬的也一律折行（以前實際看到過）。
    * 變成卡片時按鈕要有文字（卡片放得下）。
    """
    port, send, errs = live
    base = f"http://127.0.0.1:{port}"
    cases = httpx.get(f"{base}/tools/official-doc/api/cases", timeout=30).json()["cases"]
    if not cases:
        _seed(port)
        cases = httpx.get(f"{base}/tools/official-doc/api/cases", timeout=30).json()["cases"]
    for c, v in zip(cases[:3], ["嘉資字第1150004321號", "府授教字第11500987654321號", "X" * 60]):
        assert httpx.post(f"{base}/tools/official-doc/case/{c['case_id']}/doc-no",
                          json={"doc_no": v}, timeout=30).status_code == 200
    send("Network.enable")
    bad = []
    try:
        for loc in ("zh-Hant", "en", "ja"):
            send("Network.setCookie", {"name": "jtdt_locale", "value": loc, "url": base})
            for w in (1180, 1280, 1366, 1440, 1920):
                send("Emulation.setDeviceMetricsOverride", {"width": w, "height": 900, "deviceScaleFactor": 1,
                                                            "mobile": False})
                _open(send, port)
                _js(send, "localStorage.removeItem('jtdt.odc.view'), true")
                _open(send, port)
                m = _js(send, _FIT)
                where = f"{loc} {w}"
                if m["over"]:
                    bad.append(f"{where}：表格超出外框 {m}")
                if m["page"] > m["vw"] + 1:
                    bad.append(f"{where}：頁面出現橫向捲動 {m}")
                if m["short"]:
                    bad.append(f"{where}：名稱被擠到 {m['short']}px {m['fit']}")
                if "odc-fit-cards" in m["fit"] and m["hiddenLabels"]:
                    bad.append(f"{where}：卡片裡的按鈕沒有文字")
                if loc == "zh-Hant" and w >= 1440 and (m["fit"] or m["docTall"]):
                    bad.append(f"{where}：寬度夠了還在收 {m['fit']}，文號折行 {m['docTall']} 列")
    finally:
        send("Network.setCookie", {"name": "jtdt_locale", "value": "zh-Hant", "url": base})
        send("Emulation.clearDeviceMetricsOverride", {})
    assert not bad, "\n".join(bad)
    assert not errs, errs
