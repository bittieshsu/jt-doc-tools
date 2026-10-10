"""公文撰擬：機關名稱從地址簿挑、DI 檔的匯出預覽 —— 真的在瀏覽器裡跑一次。

這一類的 bug 在本專案一律長成「元素都在、沒有例外、按了沒反應」（或清單是瀏覽器原生那一塊黑色
提示框），只有真的跑一次 JS 才看得到。要驗的：

* 打字出現的是**本站樣式**的清單（白底、每列名稱＋機關代碼），不是原生 datalist；方向鍵＋Enter 選得到。
* 選了會記住機關代碼（欄位下方看得到），**改了字代碼就不算數**；自己打完整名稱也對得到代碼。
* 正本、副本一格好幾個機關：只查、只換游標所在的那一段。
* 送出草稿時代碼跟著案件存；DI 預覽打得開，三種檢視都有內容，XML 有上色，下載的就是預覽的那一份。

伺服器、假的語言模型、地址簿都沿用 `test_official_doc_e2e_kb` 的 `live`（另起一支服務、另一個資料目錄）。
"""
from __future__ import annotations

import json

from tests.test_official_doc_e2e_kb import _js, _open, _wait, live  # noqa: F401


def _type(send, el_id: str, text: str) -> None:
    _js(send, f"""(function () {{
        var e = document.getElementById({json.dumps(el_id)});
        e.focus(); e.value = {json.dumps(text)};
        e.setSelectionRange(e.value.length, e.value.length);
        e.dispatchEvent(new Event('input', {{bubbles: true}}));
        return true; }})()""")


def _key(send, el_id: str, key: str) -> None:
    _js(send, f"document.getElementById({json.dumps(el_id)}).dispatchEvent("
              f"new KeyboardEvent('keydown', {{key: {json.dumps(key)}, bubbles: true}})), true")


def _panel(el_id: str) -> str:
    return f"document.getElementById({json.dumps(el_id + 'OrgList')})"


def _code_line(send, el_id: str) -> str:
    return _js(send, f"(function(){{var l = document.getElementById({json.dumps(el_id)}).parentNode"
                     f".querySelector('.op-codeline'); return l && !l.hidden ? l.textContent : '';}})()")


def test_picker_uses_the_site_list_and_keeps_the_code(live):  # noqa: F811
    port, send, errs = live
    _open(send, port)
    _js(send, "document.querySelector('input[name=\"odMode\"][value=\"letter\"]').click(), true")
    assert not _js(send, "!!document.querySelector('datalist#odOrgList') || "
                         "!!document.getElementById('odReceiver').getAttribute('list')"), "原生 datalist 還在"
    assert _js(send, "!!document.getElementById('odReceiver').parentNode.querySelector('.op-btn')"), \
        "輸入框裡要有 ▼ 可以打開清單"

    _type(send, "odReceiver", "嘉禾")
    assert _wait(send, f"!{_panel('odReceiver')}.hidden && "
                       f"{_panel('odReceiver')}.querySelectorAll('.op-opt').length >= 2", 10), "清單沒出現"
    rows = _js(send, f"Array.from({_panel('odReceiver')}.querySelectorAll('.op-opt'))"
                     ".map(function(r){return r.querySelector('.op-name').textContent + '|' + "
                     "r.querySelector('.op-code').textContent;})")
    assert "嘉禾市政府|Q1000000" in rows and "嘉禾市東湖區公所|Q20000000B" in rows, rows
    # 符合處標亮（2026-10-09 使用者：「搜尋有符合的 該字串要高亮」）：每一筆的「嘉禾」那兩個字
    marks = _js(send, f"Array.from({_panel('odReceiver')}.querySelectorAll('.op-opt .op-name'))"
                      ".map(function(n){return Array.from(n.querySelectorAll('mark.op-hl'))"
                      ".map(function(m){return m.textContent;}).join('+');})")
    assert marks and all(m == "嘉禾" for m in marks), marks
    assert _js(send, f"getComputedStyle({_panel('odReceiver')}.querySelector('mark.op-hl'))"
                     ".backgroundColor") == "rgb(253, 230, 138)"
    # 本站樣式：白底、有框（不是瀏覽器原生那一塊）
    bg = _js(send, f"getComputedStyle({_panel('odReceiver')}).backgroundColor")
    assert bg in ("rgb(255, 255, 255)", "rgba(255, 255, 255, 1)"), bg
    assert not _js(send, "!!document.querySelector('.modal-overlay')"), "地址簿是新的，不該跳提醒"

    # 方向鍵 ＋ Enter 選第一筆（全銜短的排前面：嘉禾市政府）
    _key(send, "odReceiver", "ArrowDown")
    _key(send, "odReceiver", "Enter")
    assert _js(send, "document.getElementById('odReceiver').value") == "嘉禾市政府"
    assert _js(send, f"{_panel('odReceiver')}.hidden")
    assert "Q1000000" in _code_line(send, "odReceiver")

    # 改了字：代碼不算數
    _type(send, "odReceiver", "嘉禾市政府秘書室")
    assert _wait(send, "(function(){var l=document.getElementById('odReceiver').parentNode"
                       ".querySelector('.op-codeline'); return l.hidden;})()", 5), "名稱改了代碼還掛著"
    # 自己打完整名稱（沒從清單挑）：名稱完全相同而且只有一筆，也對得到代碼
    _type(send, "odReceiver", "嘉禾市東湖區公所")
    assert _wait(send, "document.getElementById('odReceiver').parentNode.querySelector('.op-codeline')"
                       ".textContent.indexOf('Q20000000B') >= 0", 10), "打完整名稱沒有對到代碼"

    # 正本：一格好幾個機關，只換游標那一段
    _type(send, "odCopies", "嘉禾市東湖區公所、嘉禾市政")
    assert _wait(send, f"!{_panel('odCopies')}.hidden", 10)
    _js(send, f"Array.from({_panel('odCopies')}.querySelectorAll('.op-opt')).find(function(r){{"
              "return r.querySelector('.op-name').textContent === '嘉禾市政府';}).click(), true")
    assert _js(send, "document.getElementById('odCopies').value") == "嘉禾市東湖區公所、嘉禾市政府"
    line = _code_line(send, "odCopies")
    assert "Q20000000B" in line and "Q1000000" in line, line
    got = _js(send, "window.OrgPicker.collect()")
    assert got == {"嘉禾市東湖區公所": "Q20000000B", "嘉禾市政府": "Q1000000"}, got
    assert not errs, errs


def test_the_di_preview_shows_three_views_and_downloads_the_same_file(live):  # noqa: F811
    port, send, errs = live
    # 接著上一條（同一頁）：受文者、正本都挑好了；補發文機關、行文關係、需求敘述，產生草稿
    _js(send, """(function () {
        var s = document.getElementById('odRelation'); s.value = 'down'; s.dispatchEvent(new Event('change'));
        var o = document.getElementById('odOrg'); o.value = '嘉禾市政府'; o.dispatchEvent(new Event('input'));
        var n = document.getElementById('odLetterNarrative');
        n.value = '請各區公所於期限內回復資訊設備盤點結果。'; n.dispatchEvent(new Event('input'));
        return true; })()""")
    assert _wait(send, "!document.getElementById('odLetterClosing').disabled", 10)
    _js(send, "document.getElementById('odGo').click(), true")
    assert _wait(send, "!document.getElementById('odResult').hidden && "
                       "/主旨：/.test(document.getElementById('odDraft').value)", 90), "草稿沒有產生"
    assert _wait(send, "!document.getElementById('odDiKind').hidden", 10), "函要有「電子公文」那一組"
    assert _js(send, "!document.getElementById('odDiPreview').disabled")

    _js(send, "document.getElementById('odDiPreview').click(), true")
    assert _wait(send, "!document.getElementById('odDi').hidden", 30), "匯出預覽沒有打開"
    assert _js(send, "!!document.querySelector('#odDiStatus .od-di-badge.ok')"), \
        _js(send, "document.getElementById('odDiStatus').textContent")
    # 公文預覽：照公文排、內容取自 DI 檔
    doc = _js(send, "document.getElementById('odDiDoc').textContent")
    assert "嘉禾市政府　函" in doc and "主旨：" in doc and "受文者：嘉禾市東湖區公所" in doc, doc
    # 欄位：每個欄位一列，代碼看得到
    _js(send, "document.querySelector('[data-od-di-tab=\"fields\"]').click(), true")
    assert _js(send, "!document.querySelector('[data-od-di-pane=\"fields\"]').hidden")
    fields = _js(send, "document.getElementById('odDiFields').textContent")
    assert "Q20000000B" in fields and "Q1000000" in fields, fields
    assert _js(send, "document.querySelectorAll('#odDiFields tr').length") >= 10
    # XML：上色（標籤、屬性、值各自一種顏色）、有行號
    _js(send, "document.querySelector('[data-od-di-tab=\"xml\"]').click(), true")
    assert _js(send, "document.querySelectorAll('#odDiXml .x-tag').length") > 20
    assert _js(send, "document.querySelectorAll('#odDiXml .x-attr').length") > 0
    colors = _js(send, "['x-tag','x-attr','x-val','x-text'].map(function(c){"
                       "return getComputedStyle(document.querySelector('#odDiXml .' + c)).color;})")
    assert len(set(colors)) == 4, colors
    assert _js(send, "document.querySelectorAll('#odDiXml .x-ln').length") > 20
    shown = _js(send, "document.getElementById('odDiXml').textContent")

    # 下載的就是預覽的那一份（攔下那一次匯出的回應內容）
    body = _js(send, """(async function () {
        var orig = window.fetch, got = null;
        window.fetch = async function (u, o) {
            var r = await orig(u, o);
            if (String(u).indexOf('/export') >= 0) got = await r.clone().text();
            return r; };
        document.getElementById('odDiDownload').click();
        for (var i = 0; i < 100 && got === null; i++) await new Promise(function (r) { setTimeout(r, 100); });
        window.fetch = orig;
        return got; })()""")
    assert body and body.startswith('<?xml version="1.0" encoding="UTF-8"?>'), body and body[:80]
    assert "<機關代碼>Q20000000B</機關代碼>" in body
    assert body.replace("\n", "") == shown.replace("\n", "").replace(" ", " "), "預覽與下載不是同一份"

    # Esc 關掉；拿掉「主旨：」那一行之後兩顆都反灰
    _js(send, "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})), true")
    assert _js(send, "document.getElementById('odDi').hidden")
    _js(send, """(function () { var d = document.getElementById('odDraft');
        d.value = d.value.replace(/^主旨：.*$/m, ''); d.dispatchEvent(new Event('input')); return true; })()""")
    assert _js(send, "document.querySelector('[data-od-dl=\"di\"]').disabled && "
                     "document.getElementById('odDiPreview').disabled"), "沒有主旨卻還能匯出 DI"
    assert not errs, errs
