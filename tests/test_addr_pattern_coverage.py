"""台灣地址的涵蓋率（GitHub issue #51）。

原本 `RE_ADDR` 的泛用分支寫成字面 `<縣市>`（本來要寫字元類別 `[縣市]`），
所以那條分支**從來沒有成立過** —— 實際生效的只剩寫死的 `[台臺][北中南東]市`，
新北 / 桃園 / 高雄 / 基隆 / 新竹 與**所有「縣」**的地址全部漏抓。

對去識別化工具而言這是最糟的失效方式：不報錯、不警告，
「已處理但沒有地址」和「根本沒去找」在畫面上長得一模一樣。

另一半是空白：這個工具是 PDF 優先，而 PDF 文字層常在數字前後插入空白
（CJK 與數字混排、字距調整、表格儲存格），同一個地址在 Word 裡沒有空白、
轉成 PDF 之後就有了。
"""
from __future__ import annotations

import pytest

from app.tools.doc_deident import patterns as P


HIT = [
    # 六都
    "臺北市大安區信義路四段1號5樓",
    "新北市板橋區文化路一段88號",
    "桃園市中壢區中央西路二段50號",
    "臺中市西區台灣大道二段2號",
    "臺南市中西區中正路1號",
    "高雄市三民區建工路415號",
    # 市
    "基隆市仁愛區愛四路1號",
    "新竹市東區光復路二段101號",
    "嘉義市西區垂楊路100號",
    # 縣
    "新竹縣竹北市光明六路1號",
    "彰化縣員林市中山路二段10號",
    "屏東縣屏東市自由路100號",
    "宜蘭縣宜蘭市中山路三段1號",
    # 「台」的異體字
    "台北市中正區重慶南路一段122號",
    # PDF 抽出來常見的空白樣態 —— 半形、不斷行空白（PyMuPDF 實測就是這個）、全形
    "臺北市大安區信義路四段 1 號 5 樓",
    "高雄市三民區建工大道\xa0415\xa0號",
    "臺中市西區台灣大道二段　2　號",
    "新北市板橋區文化路一段 88 號 3 樓",
    "高雄市三民區建工路 415 號",
]

MISS = [
    "臺灣新北地方法院民事判決",
    "本院認為被告應給付原告新臺幣十萬元",
    "中華民國一一五年八月三十日",
    "臺灣高等法院高雄分院",
    "新竹縣政府函 竹府社字第1140001號",
    "臺北市政府警察局大安分局",
]


@pytest.mark.parametrize("text", HIT)
def test_address_is_detected(text: str):
    assert P.RE_ADDR.search(text), f"漏抓地址：{text}"


@pytest.mark.parametrize("text", MISS)
def test_non_address_is_not_detected(text: str):
    m = P.RE_ADDR.search(text)
    assert not m, f"誤抓成地址：{text} →「{m.group(0) if m else ''}」"


def test_literal_county_placeholder_is_gone():
    """釘死那個 bug 本身：`<縣市>` 曾經是字面字串。

    含那四個字元的字串**不可以**命中；正常地址要能命中。
    """
    assert not P.RE_ADDR.search("新北<縣市>板橋區文化路一段88號")
    assert P.RE_ADDR.search("新北市板橋區文化路一段88號")


def test_address_does_not_span_line_breaks():
    """放寬空白時只能吃**同一行**的空白。

    `\\s` 含換行，而 PDF 抽出來的文字是用換行接的 —— 用 `\\s` 的話
    「地址開頭 + 換行 + 別人的姓名 + 換行 + 門牌」會被兜成同一筆地址，
    遮蔽框跟著跨行畫，蓋掉的範圍不是使用者看到的那一段。
    """
    m = P.RE_ADDR.search("臺北市大安區信義路\n王小明\n1號")
    assert not m, f"跨行兜成一筆地址：{m.group(0)!r}" if m else ""
