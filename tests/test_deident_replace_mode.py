"""文件去識別化的第三種模式：替換。

編修是塗黑、遮罩是打星號，替換是**換成另一個看起來正常但不是真的值**——
用途是「文件還要能當成正常文件用」：拿去測試系統、給外部看的報表、教學範例。

最重要的判準只有一條：**原值要真的從檔案裡消失**。畫面上看不到不算數，
文字抽得出來就等於沒有去識別化（這類工具最要命的失敗是「看起來處理過了」）。
"""
from __future__ import annotations

import fitz
import pytest

from app.tools.doc_deident import patterns as P
from app.tools.doc_deident.fake_values import Replacer


# --- 假值產生器 ------------------------------------------------------

def test_same_value_always_maps_to_the_same_fake():
    """少了這條，一份報表裡同一個客戶會變成三個不同的人，文件就沒法用了。"""
    r = Replacer()
    a = r.for_value("tw_id", "A123456789")
    b = r.for_value("tw_id", "A123456789")
    assert a == b
    assert r.for_value("tw_id", "B222222222") != a


def test_safe_mode_never_produces_a_valid_number():
    """預設要「明顯是假的」—— 算得出正確檢查碼的號碼可能剛好是某個真人的。"""
    r = Replacer(valid_checksum=False)
    assert not P._tw_id_valid(r.for_value("tw_id", "A123456789"))
    assert not P._twbiz_valid(r.for_value("tw_biz", "12345678"))
    assert not P._luhn_valid(r.for_value("cc", "4111111111111111"))


def test_valid_mode_produces_numbers_that_pass_validation():
    """打開開關就要真的通過 —— 不然「適合拿去測試系統」是空話。"""
    r = Replacer(valid_checksum=True)
    assert P._tw_id_valid(r.for_value("tw_id", "A123456789"))
    assert P._twbiz_valid(r.for_value("tw_biz", "12345678"))
    assert P._luhn_valid(r.for_value("cc", "4111111111111111"))


def test_reserved_ranges_are_used_for_email_and_ip():
    """Email / IP 用文件專用的保留範圍，兩種設定都不會撞到真的東西。"""
    for vc in (False, True):
        r = Replacer(valid_checksum=vc)
        assert r.for_value("email", "someone@real.com").endswith("@example.com")
        assert r.for_value("ip", "10.1.2.3").startswith("192.0.2.")


def test_format_is_preserved():
    """替換的重點是「看起來仍然是那種資料」，長度與分隔符號要保住。"""
    r = Replacer()
    assert len(r.for_value("tw_id", "A123456789")) == 10
    assert len(r.for_value("cc", "4111111111111111")) == 16
    got = r.for_value("bank_account", "012-345-6789")
    assert len(got) == len("012-345-6789") and got.count("-") == 2


# --- 端到端 ----------------------------------------------------------

SENSITIVE = "身分證 A123456789 電話 0912345678"


@pytest.fixture
def pdf_bytes():
    from app.core.font_catalog import best_cjk_path, embeddable_font
    path, idx = best_cjk_path("sans", "traditional")
    if not path:
        pytest.skip("這台機器沒有 CJK 字型")
    fontfile, fontbuffer = embeddable_font(str(path), idx, SENSITIVE)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="F0", fontfile=fontfile, fontbuffer=fontbuffer)
    page.insert_text((60, 100), SENSITIVE, fontname="F0", fontsize=12)
    raw = doc.tobytes()
    doc.close()
    return raw


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("JTDT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JTDT_CSRF_DISABLE", "1")
    from fastapi.testclient import TestClient
    import app.main as app_main
    return TestClient(app_main.app)


def test_detect_offers_both_kinds_of_suggested_fakes(client, pdf_bytes):
    d = client.post("/tools/doc-deident/detect",
                    files={"file": ("t.pdf", pdf_bytes, "application/pdf")},
                    data={"types": "tw_id,mobile"}).json()
    assert d["findings"], "沒偵測到東西，後面都不算數"
    for f in d["findings"]:
        assert f["fake_safe"] and f["fake_valid"], "兩種形式都要先算好一起送"
        assert f["fake_safe"] != f["value"]


def test_replace_actually_removes_the_original(client, pdf_bytes):
    """這條是整個功能的重點：處理完之後檔案裡不可以還找得到原值。"""
    d = client.post("/tools/doc-deident/detect",
                    files={"file": ("t.pdf", pdf_bytes, "application/pdf")},
                    data={"types": "tw_id,mobile"}).json()
    uid = d["upload_id"]
    sels = [{**f, "replacement": f["fake_safe"]} for f in d["findings"]]
    r = client.post("/tools/doc-deident/process",
                    json={"upload_id": uid, "mode": "replace", "selections": sels})
    assert r.status_code == 200, r.text

    out = client.get(f"/tools/doc-deident/download/{uid}")
    assert out.status_code == 200
    with fitz.open(stream=out.content, filetype="pdf") as doc:
        text = doc[0].get_text()
    for f in d["findings"]:
        assert f["value"] not in text, f"原值 {f['value']!r} 還在檔案裡"
        assert f["fake_safe"] in text, f"假值 {f['fake_safe']!r} 沒寫進去"


def test_user_supplied_replacement_wins(client, pdf_bytes):
    d = client.post("/tools/doc-deident/detect",
                    files={"file": ("t.pdf", pdf_bytes, "application/pdf")},
                    data={"types": "mobile"}).json()
    uid = d["upload_id"]
    sels = [{**f, "replacement": "0900000000"} for f in d["findings"]]
    client.post("/tools/doc-deident/process",
                json={"upload_id": uid, "mode": "replace", "selections": sels})
    out = client.get(f"/tools/doc-deident/download/{uid}")
    with fitz.open(stream=out.content, filetype="pdf") as doc:
        assert "0900000000" in doc[0].get_text()


def test_find_endpoint_locates_a_user_supplied_term(client, pdf_bytes):
    """使用者在偵測結果裡自己加要替換的字詞。"""
    d = client.post("/tools/doc-deident/detect",
                    files={"file": ("t.pdf", pdf_bytes, "application/pdf")},
                    data={"types": "tw_id"}).json()
    r = client.post("/tools/doc-deident/find",
                    json={"upload_id": d["upload_id"], "term": "身分證"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    hit = body["findings"][0]
    assert hit["value"] == "身分證"
    assert len(hit["bbox"]) == 4
    assert hit["fake_safe"], "自訂字詞也要有建議的替換值"


def test_find_rejects_someone_elses_upload(client, pdf_bytes):
    r = client.post("/tools/doc-deident/find",
                    json={"upload_id": "0" * 32, "term": "x"})
    assert r.status_code in (403, 404), "別人的 upload_id 不可以搜得到東西"


def test_find_rejects_a_malformed_id(client):
    r = client.post("/tools/doc-deident/find",
                    json={"upload_id": "../../etc/passwd", "term": "x"})
    assert r.status_code == 400


def test_long_replacement_is_shrunk_to_fit():
    """使用者想填多長就多長 —— 不縮字會壓到隔壁欄位，而且是無聲的。"""
    from app.tools.doc_deident.router import _fit_font_size
    assert _fit_font_size("abc", 12.0, 200.0) == 12.0
    assert _fit_font_size("這是一段很長的替換文字", 12.0, 40.0) < 12.0
    assert _fit_font_size("x" * 500, 12.0, 10.0) >= 4.0     # 有下限，不會縮到看不見


def test_api_supports_replace(client, pdf_bytes):
    """每個功能都要有 API —— 網頁做得到的，API 也要做得到。"""
    r = client.post("/tools/doc-deident/api/doc-deident",
                    files={"file": ("t.pdf", pdf_bytes, "application/pdf")},
                    data={"types": "tw_id,mobile", "mode": "replace"})
    assert r.status_code == 200, r.text
    with fitz.open(stream=r.content, filetype="pdf") as doc:
        text = doc[0].get_text()
    assert "A123456789" not in text and "0912345678" not in text


def test_api_replacements_map_is_honoured(client, pdf_bytes):
    import json
    r = client.post("/tools/doc-deident/api/doc-deident",
                    files={"file": ("t.pdf", pdf_bytes, "application/pdf")},
                    data={"types": "mobile", "mode": "replace",
                          "replacements": json.dumps({"0912345678": "0955555555"})})
    assert r.status_code == 200
    with fitz.open(stream=r.content, filetype="pdf") as doc:
        assert "0955555555" in doc[0].get_text()


def test_api_rejects_bad_replacements_json(client, pdf_bytes):
    r = client.post("/tools/doc-deident/api/doc-deident",
                    files={"file": ("t.pdf", pdf_bytes, "application/pdf")},
                    data={"mode": "replace", "replacements": "not json"})
    assert r.status_code == 400


def test_unknown_mode_still_rejected(client, pdf_bytes):
    r = client.post("/tools/doc-deident/api/doc-deident",
                    files={"file": ("t.pdf", pdf_bytes, "application/pdf")},
                    data={"mode": "delete-everything"})
    assert r.status_code == 400
