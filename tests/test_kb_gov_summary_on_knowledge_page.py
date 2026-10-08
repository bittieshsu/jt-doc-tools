"""「知識庫」頁的政府公開資料卡片要講出數字（2026-10-08 使用者：
「這裡要顯示 總共可以匯入的有 XXXXX　目前已下載與匯入 XXX」）。

* 總共可以匯入：已下載清單的來源加起來有幾項。**清單還沒下載的來源不知道有幾項** ——
  要講出來，不可以安靜地算成 0（那會讓人以為那個來源沒有東西）。
* 目前已下載並匯入：已經建成資料集的項目數。
* 讀不到狀態時整頁照常、按鈕照常（卡片只是少了數字）。
"""
from __future__ import annotations

import re


def _page(c) -> str:
    r = c.get("/admin/knowledge")
    assert r.status_code == 200, r.text[:300]
    return r.text


def _num(html: str, attr: str) -> str:
    m = re.search(r"%s>([^<]*)<" % attr, html)
    assert m, f"卡片上沒有 {attr} 那個數字"
    return m.group(1)


def test_nothing_downloaded_says_so_instead_of_counting_zero(admin_session):
    """真的呼叫 `gov.status()`（不 mock）：測試環境沒有下載任何清單。"""
    c, _, _ = admin_session
    from app.core.kb import gov
    n = len(gov.status()["groups"])
    assert n >= 2, "前提：至少兩個來源"
    html = _page(c)
    assert _num(html, "data-gov-available") == "0"
    assert _num(html, "data-gov-imported") == "0"
    assert f"還有 {n} 個來源沒下載清單" in html
    assert html.count("還沒下載清單") >= n
    assert 'id="kbGovGo"' in html


def test_the_totals_add_up_across_sources(admin_session, monkeypatch):
    c, _, _ = admin_session
    from app.core.kb import gov

    def fake():
        return {"running": None, "groups": [
            {"name": "全國法規資料庫", "downloaded": True, "available": 11798,
             "imported_count": 12, "needs_update_count": 2},
            {"name": "國家發展委員會主管行政規則", "downloaded": True, "available": 240,
             "imported_count": 3, "needs_update_count": 0},
            {"name": "行政院文書處理相關釋例", "downloaded": False, "available": 0,
             "imported_count": 0, "needs_update_count": 0},
        ]}
    monkeypatch.setattr(gov, "status", fake)
    html = _page(c)
    assert _num(html, "data-gov-available") == "12,038"
    assert _num(html, "data-gov-imported") == "15"
    assert "還有 1 個來源沒下載清單" in html
    assert "其中 2 項來源有更新" in html
    assert "可以匯入 11,798 項" in html and "已匯入 12 項" in html


def test_page_still_works_when_the_status_cannot_be_read(admin_session, monkeypatch):
    c, _, _ = admin_session
    from app.core.kb import gov

    def boom():
        raise OSError("disk")
    monkeypatch.setattr(gov, "status", boom)
    html = _page(c)
    assert 'id="kbGovSum"' not in html
    assert 'id="kbGovGo"' in html
