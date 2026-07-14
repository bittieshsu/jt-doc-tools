"""乘車證明解析器單元測試（合成 fixture，不含真實票號 / 統編 / 站名資料）。

用符合台鐵 / 高鐵官方版面的假資料驗證抽取邏輯，不放真實憑證內容進版控。
真實樣本的驗收走部署 / 手動（TEST_PLAN）。
"""
from __future__ import annotations

import pytest

from app.tools.transit_proof import parser


# 高鐵電子車票證明版面：label ：value。假統編 / 假票號 / 任意站名。
_HSR = """統一編號 : 00000000
台灣高鐵電子車票證明
列印日期：2026-07-14
營利事業名稱 : ********
卡號/票號 : 0000000000000
乘車日期 : 2026-06-09 07:20:00
票證類別 : 手機電子票證
起程站 : 高鐵AAA車站
到達站 : 高鐵BBB車站
銷售額 : 700
營業稅額 : 35
票價 : 735
控管編號 : 000000000000000000
"""

# 台鐵購票證明版面：PyMuPDF 打散後的順序（label 群組 + value 群組交錯）。
_TRA = """購票證明
X00000000000000
票號
印製日期
2026/07/14
乘車日
列車資訊
票種
票價
乘車區間
2026/05/19
莒光(700)
123 車次
08:00 CCC > 10:30 DDD
全票
(自由座)
420 元
※ 辦理乘車變更或退票，須併同原票及購票證明辦理。
"""


def test_detect_kind():
    assert parser.detect_kind(_HSR) == "thsrc"
    assert parser.detect_kind(_TRA) == "tra"
    assert parser.detect_kind("隨便的內容") is None


def test_parse_hsr():
    d = parser.parse_text(_HSR)
    assert d["transport"] == "高鐵"
    assert d["date"] == "2026-06-09"
    assert d["depart_time"] == "07:20"
    assert d["arrive_time"] == ""
    assert d["origin"] == "AAA"        # 去「高鐵」前綴 + 「車站」後綴
    assert d["destination"] == "BBB"
    assert d["fare"] == 735
    assert d["amount_untaxed"] == 700
    assert d["tax"] == 35
    assert d["ticket_no"] == "0000000000000"
    assert d["buyer_tax_id"] == "00000000"
    assert d["train"] == ""


def test_parse_tra():
    d = parser.parse_text(_TRA)
    assert d["transport"] == "台鐵"
    assert d["date"] == "2026-05-19"          # 排除印製日期 2026/07/14
    assert d["depart_time"] == "08:00"
    assert d["arrive_time"] == "10:30"
    assert d["origin"] == "CCC"
    assert d["destination"] == "DDD"
    assert d["fare"] == 420
    assert d["ticket_type"] == "全票(自由座)"
    assert d["ticket_no"] == "X00000000000000"
    assert "莒光" in d["train"]
    assert "123車次" in d["train"]
    assert d["amount_untaxed"] is None
    assert d["tax"] is None


def test_tra_excludes_chengche_qujian_from_train():
    """回歸：'乘車區間' label 的「區間」不可被當成車種。"""
    d = parser.parse_text(_TRA)
    assert not d["train"].startswith("區間")


def test_unknown_raises():
    with pytest.raises(parser.ParseError):
        parser.parse_text("這不是乘車證明，只是一段普通文字。")


def test_empty_fields_raises():
    with pytest.raises(parser.ParseError):
        parser.parse_text("台灣高鐵電子車票證明\n（版面全空，無欄位）")


def test_roc_date_edge():
    """民國年換算：西元 2026 → 民國 115（parser 只出 ISO，格式化在 settings）。"""
    d = parser.parse_text(_HSR)
    from app.tools.transit_proof import settings as s
    assert s.apply_format("date", d["date"], {"date": "roc"}) == "115/06/09"
