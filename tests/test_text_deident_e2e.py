"""文字去識別化：走完整條路徑的驗收。

**這支工具原本一條測試都沒有** —— 盤點「哪些工具驗到產出內容」時才發現的。
它與文件去識別化共用同一份偵測式子（`doc_deident.patterns`），所以 issue #51
那個「大部分縣市的地址從來沒抓到過」的 bug，**這支工具也一起中招**，
而且同樣無聲：畫面會顯示「找到 0 筆」，使用者以為文字本來就沒有個資。

驗收一路走到最後：偵測 → 處理 → **產出的文字裡不可以再看到那段個資**。
"""
from __future__ import annotations

import pytest


ADDR_NEW_TAIPEI = "新北市板橋區文化路一段88號3樓"   # 舊版完全抓不到
ADDR_HSINCHU = "新竹縣竹北市光明六路1號"            # 「縣」也一樣
ADDR_TAIPEI = "臺北市大安區信義路四段1號"           # 舊版就抓得到 —— 防止修壞
TW_ID = "A123456789"
PHONE = "0912-345-678"

SAMPLE = (
    "當事人：王大明\n"
    f"戶籍地址：{ADDR_NEW_TAIPEI}\n"
    f"通訊地址：{ADDR_HSINCHU}\n"
    f"公司地址：{ADDR_TAIPEI}\n"
    f"身分證字號：{TW_ID}\n"
    f"聯絡電話：{PHONE}\n"
)


@pytest.fixture(scope="module")
def detected(client):
    r = client.post("/tools/text-deident/detect",
                    json={"text": SAMPLE, "types": ["addr", "tw_id", "phone_mobile"]})
    assert r.status_code == 200, r.text
    return r.json()


def test_detect_finds_addresses_in_every_kind_of_county(detected):
    found = [f["value"] for f in detected["findings"] if f["type"] == "addr"]
    joined = " | ".join(found)
    assert any("新北市板橋區" in v for v in found), f"漏抓新北市：{joined}"
    assert any("新竹縣竹北市" in v for v in found), f"漏抓新竹縣：{joined}"
    assert any("臺北市大安區" in v for v in found), f"連舊版抓得到的都漏了：{joined}"


def test_detect_finds_the_id_number(detected):
    assert TW_ID in [f["value"] for f in detected["findings"]]


@pytest.mark.parametrize("mode", ["mask", "redact"])
def test_processed_text_no_longer_contains_the_personal_data(client, detected, mode):
    """**最後一關**：處理完的文字裡不可以再看到那些個資。"""
    r = client.post("/tools/text-deident/process",
                    json={"text": SAMPLE, "mode": mode,
                          "selections": detected["findings"]})
    assert r.status_code == 200, r.text
    out = r.json()["text"]
    for needle in ("新北市板橋區", "新竹縣竹北市", "臺北市大安區", TW_ID):
        assert needle not in out, f"{mode} 模式處理完還看得到「{needle}」：{out!r}"
    # 不相干的內容要留著 —— 全部塗掉不算處理成功
    assert "當事人" in out and "戶籍地址" in out


def test_public_api_does_the_same(client):
    """對外 API 也要走同一條路（本專案的規矩：每個功能都要有 API）。"""
    r = client.post("/tools/text-deident/api/text-deident",
                    json={"text": SAMPLE, "mode": "mask",
                          "types": ["addr", "tw_id"]})
    assert r.status_code == 200, r.text
    out = r.json().get("text") or ""
    assert "新北市板橋區" not in out, f"API 路徑沒有處理掉地址：{out!r}"
