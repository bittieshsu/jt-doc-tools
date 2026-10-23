"""統編資料庫收進公開資料的其他欄位：總機構統一編號、資本額、設立日期、使用統一發票（v1.16.91）。

* 欄位名稱照財政部「全國營業（稅籍）登記資料集」BGMOPEN1 的檔頭（2026-10-15 實際抓檔頭確認）。
* 設立日期是民國年月日（`1040413`）→ 存西元 `2015-04-13`；資本額存整數；使用統一發票 Y / N → True / False。
* **既有站台升級**：舊資料表沒有這些欄位 → 啟動時補上（值是空的），查詢照常；
  下次排程下載（全量重匯）後就有值 —— 使用者 2026-10-15「更新 jtdt 後，下次排程下載就會長出這些」。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.core import vat_db

#: BGMOPEN1 的檔頭（照原檔順序）；資料列是杜撰的
HEADER = ("營業地址,統一編號,總機構統一編號,營業人名稱,資本額,設立日期,組織別名稱,使用統一發票,"
          "行業代號,名稱,行業代號1,名稱1,行業代號2,名稱2,行業代號3,名稱3")
ROWS = [
    "嘉禾市中正區範例路1號,12345675,,範例實業股份有限公司,50000000,1040413,股份有限公司,Y,"
    "471913,電腦及其週邊設備零售,,,,,,",
    "嘉禾市北區測試街2號,23456781,12345675,範例實業股份有限公司北區分公司,0,0991231,分公司,N,"
    "471913,電腦及其週邊設備零售,,,,,,",
    "嘉禾市東區樣本巷3號,34567893,,樣本小吃店,\"1,000,000\",991231,獨資,N,561111,餐館,,,,,,",
    "嘉禾市西區4號,45678904,123,壞資料商行,abc,1041340,獨資,X,,,,,,,,",
]


@pytest.fixture
def vat_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.vat_db.app_settings", type("S", (), {"data_dir": tmp_path})())
    vat_db._lookup_cache.clear()
    return tmp_path


def _csv() -> bytes:
    return ("\n".join([HEADER] + ROWS) + "\n").encode("utf-8")


def test_parse_keeps_every_bgmopen_field():
    recs = {r["vat"]: r for r in vat_db.parse_csv_to_records(_csv())}
    hq = recs["12345675"]
    assert (hq["hq_vat"], hq["capital"], hq["founded"], hq["uses_invoice"]) == (None, 50000000, "2015-04-13", True)
    br = recs["23456781"]
    assert (br["hq_vat"], br["capital"], br["founded"], br["uses_invoice"]) == ("12345675", 0, "2010-12-31", False)
    sm = recs["34567893"]
    assert (sm["capital"], sm["founded"]) == (1000000, "2010-12-31"), "千分位逗號、六碼民國日期都要接"
    bad = recs["45678904"]
    assert (bad["hq_vat"], bad["capital"], bad["founded"], bad["uses_invoice"]) == (None, None, None, None), \
        "壞值存空的，不可以猜、也不可以讓整份匯入失敗"


@pytest.mark.parametrize("raw,want", [
    ("1040413", "2015-04-13"), ("0991231", "2010-12-31"), ("991231", "2010-12-31"),
    ("0010101", "1912-01-01"), ("20150413", "2015-04-13"), ("1150229", None), ("1041340", None),
    ("", None), ("abc", None), ("12", None), ("10404133", None),
])
def test_roc_dates(raw, want):
    assert vat_db._parse_founded(raw) == want


def test_lookup_and_search_return_the_new_fields(vat_tmp):
    vat_db.ingest_csv(_csv(), source="test")
    r = vat_db.lookup_vat("23456781")
    assert {k: r[k] for k in ("hq_vat", "capital", "founded", "uses_invoice")} == \
        {"hq_vat": "12345675", "capital": 0, "founded": "2010-12-31", "uses_invoice": False}
    hits = {h["vat"]: h for h in vat_db.search_companies("範例")}
    assert hits["12345675"]["capital"] == 50000000 and hits["12345675"]["founded"] == "2015-04-13"
    assert hits["12345675"]["uses_invoice"] is True


def _old_schema_db(path) -> None:
    """模擬升級前的站台：資料表沒有新欄位，裡面已經有資料。"""
    conn = sqlite3.connect(str(path / "vat_db.sqlite"))
    conn.executescript("""
        CREATE TABLE vat_registry (vat TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT, owner TEXT,
                                   org_type TEXT, status TEXT, raw TEXT, category TEXT, industries TEXT);
        CREATE TABLE vat_meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO vat_registry (vat, name, address, category) VALUES ('12345675', '舊資料公司', '嘉禾市', '企業');
    """)
    conn.commit()
    conn.close()


def test_upgrade_adds_columns_and_next_download_fills_them(vat_tmp):
    _old_schema_db(vat_tmp)
    vat_db.init_db()
    r = vat_db.lookup_vat("12345675")
    assert r["name"] == "舊資料公司", "升級後舊資料要照常查得到"
    assert (r["hq_vat"], r["capital"], r["founded"], r["uses_invoice"]) == (None, None, None, None)
    assert vat_db.search_companies("舊資料")[0]["capital"] is None
    # 下次排程下載＝全量重匯（ingest_csv 換表）→ 新欄位有值
    vat_db.ingest_csv(_csv(), source="BGMOPEN1")
    r = vat_db.lookup_vat("12345675")
    assert r["name"] == "範例實業股份有限公司" and r["capital"] == 50000000 and r["founded"] == "2015-04-13"


def test_supplement_sources_without_these_columns_still_append(vat_tmp):
    vat_db.ingest_csv(_csv(), source="BGMOPEN1")
    out = vat_db.append_csv("統一編號,單位名稱,機關所在縣市\n56789012,嘉禾市立範例國小,嘉禾市\n".encode(),
                            source="學校", category="學校")
    assert out["added"] == 1
    r = vat_db.lookup_vat("56789012")
    assert r["name"] == "嘉禾市立範例國小" and r["capital"] is None and r["uses_invoice"] is None
    assert vat_db.lookup_vat("12345675")["capital"] == 50000000, "補充來源不可以蓋掉主檔的值"


def test_api_returns_the_new_fields(vat_tmp):
    from fastapi.testclient import TestClient
    from app.main import app
    vat_db.ingest_csv(_csv(), source="test")
    r = TestClient(app).get("/api/vat-lookup/23456781")
    assert r.status_code == 200, r.text
    j = r.json()
    body = j.get("result", j)
    assert body["hq_vat"] == "12345675" and body["capital"] == 0 and body["founded"] == "2010-12-31"
    assert body["uses_invoice"] is False


def test_card_label_column_fits_the_longest_label():
    """欄名欄寬不可以寫死（70px 放不下「使用統一發票」，折成兩行；使用者 2026-10-16 截圖）。"""
    import re
    from pathlib import Path
    tpl = (Path(__file__).resolve().parents[1] / "app/tools/vat_lookup/templates/vat_lookup.html").read_text(encoding="utf-8")
    rule = re.search(r"\.vl-card-meta \{([^}]*)\}", tpl).group(1)
    cols = re.search(r"grid-template-columns:\s*([^;]+);", rule).group(1)
    assert "fit-content(" in cols and not re.match(r"\d+px", cols.strip()), cols
