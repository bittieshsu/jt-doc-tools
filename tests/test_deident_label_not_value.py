"""跨格配對時，欄位標籤不可以被當成值（GitHub issue #50）。

第二輪配對（issue #43 的修正）處理「標籤與值被拆到不同儲存格」的版面。
表格型文件（公文、法律書狀、申請表）常常**一整欄都是標籤**，於是上下相鄰的
兩個標籤格被配成一對：

    「被告代表人」＋「銀行帳號」 → 兜成 "被告代表人 銀行帳號"
    RE_PERSON 看到前綴「代表人」後面接 2–4 個漢字 → 把「銀行帳號」當人名

回報者實際踩到的是三層連鎖：欄位標籤被當人名 → 人工覆核時一起放行 →
「銀行帳號」變成假名對照表裡的條目 → 後續稽核把統計欄位名稱報成資料外洩。
"""
from __future__ import annotations

import pytest

import importlib

from app.tools.doc_deident import patterns as P

# `from app.tools.doc_deident import router` 拿到的是套件匯出的 APIRouter 物件，
# 不是模組 —— 要測模組層級的 helper 得用 import_module。
R = importlib.import_module("app.tools.doc_deident.router")


def _unit(joined: str) -> dict:
    """做一個「兩格接起來」的虛擬單位 —— 跟 `_join_units` 產出的形狀一致。

    左右兩格各是一個 span（表格裡本來就是不同儲存格），中間一個空白。
    """
    left, right = joined.split(" ", 1)
    w = 10.0
    spans = [
        {"text": left, "bbox": [0.0, 0.0, w * len(left), 12.0], "size": 11.0, "color": 0},
        {"text": right, "bbox": [w * len(left) + w, 0.0,
                                 w * (len(left) + len(right)) + w, 12.0],
         "size": 11.0, "color": 0},
    ]
    span_map = [0] * len(left) + [0] + [1] * len(right)
    return {
        "text": joined, "spans": spans, "span_map": span_map,
        "span_starts": [0, len(left) + 1],
        "bbox": [0.0, 0.0, spans[1]["bbox"][2], 12.0],
        "junction": len(left),
    }


@pytest.mark.parametrize("joined", [
    "被告代表人 銀行帳號",
    "代表人 銀行帳號",
    "聯絡人 出生日期",
    "申請人 手機號碼",
])
def test_label_cell_is_not_reported_as_a_name(joined: str):
    hits = R._scan_unit(_unit(joined), {"person_name"}, [], labelled_only=True)
    assert not hits, f"欄位標籤被當成人名：{joined} → {[h['value'] for h in hits]}"


@pytest.mark.parametrize("joined,expect", [
    ("被告代表人 王小明", "王小明"),
    ("聯絡人 陳大文", "陳大文"),
])
def test_real_name_still_detected(joined: str, expect: str):
    """擋掉標籤不可以順手把真的人名也擋掉。"""
    hits = R._scan_unit(_unit(joined), {"person_name"}, [], labelled_only=True)
    assert [h["value"] for h in hits] == [expect], f"真人名漏抓：{joined}"


def test_label_vocabulary_comes_from_the_catalog():
    """標籤詞彙要**從註冊表算出來**，不可以另外維護一份寫死的清單。

    寫死的清單一定會跟註冊表漂掉 —— 本專案記過很多次。
    """
    vocab = R._label_words()
    assert "銀行帳號" in vocab and "出生日期" in vocab
    for pat in P.CATALOG:
        assert pat.label in vocab, f"註冊表有但詞彙表沒有：{pat.label}"
