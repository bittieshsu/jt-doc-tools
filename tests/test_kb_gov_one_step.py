"""政府公開資料：「下載並匯入」一步完成、整份清單翻頁看、全選（2026-10-08 使用者：
「請改為下載並匯入 不要分兩步」「這邊說169項，可是我看只有四項可以勾」「要有個全選吧」）。

要守住的事：

* `update`（畫面上唯一的按鈕）＝重新下載清單 ＋ 匯入勾選的項目。這次**全部下載失敗、但這台有上次的
  清單**時照那份匯入並講明（`summary.stale_list`）；連一份清單都沒有就整件失敗（不可以假裝成功）。
* 上傳＝離線版的下載並匯入：換上清單之後一樣接著匯入。
* `download` / `import` 兩個動作 API 照舊收（腳本不壞）。
* 搜尋 `browse`：沒有關鍵字時回**整份清單**（翻頁），已廢止的排最後；`level` 篩選；`levels` 是各位階幾項。
  不帶 `browse` 的舊呼叫照舊只回選取的。
* `keys_only`：「全選 / 取消全選」要的整個範圍；超過選取上限時不附名稱（畫面據此擋下全選）。
"""
from __future__ import annotations

import time

import pytest

from app.core.kb import gov, store
from tests._kb_support import kb_isolated  # noqa: F401
from tests.test_kb_gov_import import (  # noqa: F401
    _download, _serve_moj, allow_fake, g, law_zip, order_zip, srv)


def _wait_idle(c, timeout=60.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        st = c.get("/admin/knowledge/api/gov/status").json()
        if not st["running"]:
            return st
        time.sleep(0.1)
    raise AssertionError("背景作業沒有在時限內結束")


def _moj(st: dict) -> dict:
    return {x["id"]: x for x in st["groups"]}["moj"]


def test_download_and_import_is_one_step(g, allow_fake, srv, admin_session):
    c, _, _ = admin_session
    _serve_moj(srv)
    r = c.post("/admin/knowledge/api/gov/moj/update")
    assert r.status_code == 200, r.text
    m = _moj(_wait_idle(c))
    assert m["available"] == 4 and m["last"]["ok"] and m["last"]["action"] == "update"
    assert m["last"]["summary"]["new"] == 0, "預設建議（真的代碼）在假清單裡都沒有 —— 這次不會匯入任何東西"
    assert c.post("/admin/knowledge/api/gov/moj/selection", json={"keys": ["T0000001"]}).status_code == 200
    assert c.post("/admin/knowledge/api/gov/moj/update").status_code == 200
    m = _moj(_wait_idle(c))
    assert m["last"]["ok"] and m["last"]["summary"]["new"] == 1 and m["imported_count"] == 1
    assert not m["last"]["summary"].get("stale_list")


def test_a_failed_download_imports_from_the_list_already_here(g, allow_fake, srv, admin_session):
    c, _, _ = admin_session
    _serve_moj(srv)
    _download("moj")
    gov.set_selection("moj", ["T0000001"])
    srv.routes["/law.zip"] = (500, b"down")
    srv.routes["/order.zip"] = (500, b"down")
    assert c.post("/admin/knowledge/api/gov/moj/update").status_code == 200
    m = _moj(_wait_idle(c))
    sm = m["last"]["summary"]
    assert sm and sm["stale_list"] is True and sm["new"] == 1, m["last"]
    assert m["last"]["ok"] is False, "下載失敗要看得出來（不是一切正常）"
    assert m["imported_count"] == 1
    assert any(not p["last"]["ok"] for p in m["packages"]), "原因要記在檔案的欄位上"


def test_without_any_list_a_failed_download_still_fails(g, allow_fake, srv, admin_session):
    c, _, _ = admin_session
    _serve_moj(srv)
    srv.routes["/law.zip"] = (500, b"down")
    srv.routes["/order.zip"] = (500, b"down")
    assert c.post("/admin/knowledge/api/gov/moj/update").status_code == 200
    m = _moj(_wait_idle(c))
    assert m["last"]["ok"] is False and "全部下載失敗" in m["last"]["message"]
    assert not (m["last"].get("summary") or {}).get("new")
    assert m["imported_count"] == 0


def test_upload_also_imports_the_selection(g, allow_fake, srv, admin_session):
    c, _, _ = admin_session
    _serve_moj(srv)
    _download("moj")
    gov.set_selection("moj", ["T0000010"])        # 在 moj-order 那一包
    r = c.post("/admin/knowledge/api/gov/packages/moj-order/upload",
               files={"file": ("ChOrder.zip", order_zip(), "application/zip")})
    assert r.status_code == 200
    m = _moj(_wait_idle(c))
    assert m["last"]["action"] == "install" and m["last"]["ok"], m["last"]
    assert m["last"]["summary"]["new"] == 1 and m["imported_count"] == 1
    assert store.gov_item("moj", "T0000010")["dataset_id"]


def test_download_and_import_stay_available_to_api_callers(g, allow_fake, srv, admin_session):
    c, _, _ = admin_session
    _serve_moj(srv)
    assert c.post("/admin/knowledge/api/gov/moj/import").status_code == 400
    assert "下載並匯入" in gov.MESSAGES["not_downloaded"]
    assert c.post("/admin/knowledge/api/gov/moj/download").status_code == 200
    m = _moj(_wait_idle(c))
    assert m["available"] == 4 and m["imported_count"] == 0, "只下載不匯入"


def test_browse_lists_the_whole_list_page_by_page(g, allow_fake, srv):
    _serve_moj(srv)
    _download("moj")
    gov.set_selection("moj", ["T0000010"])
    old = gov.search("moj", "")
    assert [i["key"] for i in old["items"]] == ["T0000010"], "不帶 browse 的舊呼叫照舊只回選取的"
    full = gov.search("moj", "", browse=True, limit=50)
    keys = [i["key"] for i in full["items"]]
    assert full["total"] == 4 and set(keys) == {"T0000001", "T0000002", "T0000003", "T0000010"}
    assert keys[-1] == "T0000003", "已廢止的排最後"
    assert [i["selected"] for i in full["items"] if i["key"] == "T0000010"] == [True]
    p1 = gov.search("moj", "", browse=True, limit=2, offset=0)
    p2 = gov.search("moj", "", browse=True, limit=2, offset=2)
    assert [i["key"] for i in p1["items"]] + [i["key"] for i in p2["items"]] == keys
    assert p2["offset"] == 2 and p2["total"] == 4
    assert full["levels"] == {"法律": 3, "命令": 1}
    cmd = gov.search("moj", "", browse=True, level="命令")
    assert cmd["total"] == 1 and cmd["items"][0]["key"] == "T0000010"
    assert gov.search("moj", "範例", browse=True, level="法律")["total"] == 3


def test_keys_only_covers_the_whole_view_for_select_all(g, allow_fake, srv, monkeypatch):
    _serve_moj(srv)
    _download("moj")
    r = gov.search("moj", "", keys_only=True)
    assert r["total"] == 4 and len(r["keys"]) == 4 and not r["truncated"]
    assert {i["key"] for i in r["items"]} == set(r["keys"]) and all("chars" in i for i in r["items"])
    assert gov.search("moj", "", keys_only=True, level="命令")["keys"] == ["T0000010"]
    monkeypatch.setattr(gov, "MAX_SELECTED", 2)
    big = gov.search("moj", "", keys_only=True)
    assert "items" not in big and len(big["keys"]) == 4, "超過選取上限：只給代碼（取消全選用），不給全選的資料"
    monkeypatch.setattr(gov, "KEYS_ONLY_MAX", 3)
    assert gov.search("moj", "", keys_only=True)["truncated"] is True


def test_search_route_passes_the_new_parameters(g, allow_fake, srv, admin_session):
    c, _, _ = admin_session
    _serve_moj(srv)
    _download("moj")
    j = c.get("/admin/knowledge/api/gov/moj/search",
              params={"browse": "1", "offset": "1", "limit": "2", "level": "法律"}).json()
    assert j["total"] == 3 and j["offset"] == 1 and len(j["items"]) == 2 and j["level"] == "法律"
    k = c.get("/admin/knowledge/api/gov/moj/search", params={"keys_only": "1", "q": "範例"}).json()
    assert len(k["keys"]) == 4 and k["max_selected"] == gov.MAX_SELECTED
