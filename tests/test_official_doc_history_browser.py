"""公文撰擬「參考歷史案件」—— 真的在瀏覽器裡跑一次。

伺服器那一側（只查自己的、不算依據、門檻）在 `test_official_doc_history_refs.py`；這支驗畫面的接線：

* 還沒有自己的案件時勾選框不出現；寫過一件之後重新開頁就出現。
* 勾了真的送出 `use_history`，參考資料一節列出那件案件：標「歷史案件」、標題連回那個案件
  （`?case=<編號>`，新分頁）、旁邊寫第幾版。
* 重新開那件草稿時，勾選狀態照當時的帶回來。

伺服器、假的語言模型沿用 `test_official_doc_e2e_kb` 的 `live`（每個測試檔各自一份全新的實例）。
"""
from __future__ import annotations

import json

from tests.test_official_doc_e2e_kb import NARRATIVE, _js, _open, _wait, live  # noqa: F401


def _generate(send, narrative: str, *, history: bool) -> None:
    _js(send, f"""(function () {{
        var ta = document.getElementById('odNarrative');
        ta.value = {json.dumps(narrative)};
        ta.dispatchEvent(new Event('input'));
        var h = document.getElementById('odUseHistory');
        if (h) h.checked = {json.dumps(history)};
        document.getElementById('odGo').click();
        return true; }})()""")


def _case_ids(send) -> list[str]:
    got = _js(send, """(async function () {
        var r = await fetch('/tools/official-doc/api/cases');
        return (await r.json()).cases.map(function (c) { return c.case_id; }); })()""")
    return list(got or [])


def test_the_checkbox_appears_after_your_first_case_and_links_back(live):  # noqa: F811
    port, send, errs = live
    _open(send, port)
    assert not _js(send, "!!document.getElementById('odUseHistory')"), \
        "還沒有自己的案件：勾了也查不到，勾選框不該出現"

    _generate(send, NARRATIVE, history=False)
    assert _wait(send, "!document.getElementById('odResult').hidden && "
                       "/主旨：/.test(document.getElementById('odDraft').value)", 90), "第一份草稿沒有產生"
    first = _case_ids(send)
    assert len(first) == 1, first
    first = first[0]

    _open(send, port)
    assert _js(send, "!!document.getElementById('odUseHistory')"), "寫過一件之後要出現「參考歷史案件」"
    _generate(send, NARRATIVE + "今年再辦一次。", history=True)
    assert _wait(send, "!document.getElementById('odResult').hidden && "
                       "document.querySelectorAll('#odRefs li').length > 0", 90), \
        "勾了參考歷史案件，結果要列出那一件"
    second = [c for c in _case_ids(send) if c != first]
    assert len(second) == 1, second
    refs = _js(send, """Array.from(document.querySelectorAll('#odRefs li')).map(function (li) {
        var a = li.querySelector('a.od-ref-title');
        return {badge: li.querySelector('.od-badge').textContent,
                href: a ? a.getAttribute('href') : '', target: a ? a.target : '',
                where: (li.querySelector('.od-ref-where') || {}).textContent || ''}; })""")
    assert refs, refs
    r = refs[0]
    assert r["badge"] == "歷史案件" and r["href"] == f"/tools/official-doc/?case={first}", refs
    assert r["target"] == "_blank" and "第 1 版" in r["where"], refs
    assert _js(send, "!!document.querySelector('#odRefs .od-b-past')")

    # 重新開這件草稿（歷史案件清單、我的作業的「開啟」都是這個網址）：勾選照當時的帶回來
    send("Page.navigate", {"url": f"http://127.0.0.1:{port}/tools/official-doc/?case={second[0]}"})
    assert _wait(send, "document.readyState === 'complete' && "
                       "!!document.getElementById('odUseHistory') && "
                       "!document.getElementById('odResult').hidden", 60)
    assert _wait(send, "document.getElementById('odUseHistory').checked", 10), "重新開啟時勾選沒有帶回來"
    assert not errs, errs
