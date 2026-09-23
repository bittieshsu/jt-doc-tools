"""一個標籤對應到**多個** canonical key 是刻意支援的，不要「修掉」。

台灣的表單常有一格標籤同時管兩件事 —— `發票聯數 & 種類` 就同時屬於
`發票種類` 與 `稅別`，讓兩個欄位的值各自去勾自己的選項。
`pdf_form_detect._build_synonym_index` 的說明寫著這件事。

## 為什麼要有這一條

2026-09-16 我原本打算讓 `add_synonym()`「偵測到同一個標籤已經對應別的 key
就拒絕」——直覺上那是在防「使用者按一次學習就影響到別張表」。
**去看 `_build_synonym_index` 才發現前提是錯的**：那個形狀是功能不是缺陷，
改掉會讓那類表單的勾選整排失效，而且**不會有任何測試變紅**。

這條把它釘住。真正要處理的是「按學習時要把後果講出來」，那是介面的事。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.pdf_form_detect import _build_synonym_index  # noqa: E402


def test_a_label_shared_by_two_keys_yields_both():
    idx = _build_synonym_index({
        "發票種類": ["發票聯數 & 種類"],
        "稅別": ["發票聯數 & 種類"],
        "負責人": ["負責人"],
    })
    shared = [v for k, v in idx.items() if len(v) > 1]
    assert shared, "共用標籤應該對應到多個 key"
    assert set(shared[0]) == {"發票種類", "稅別"}


def test_an_ordinary_label_still_maps_to_exactly_one_key():
    """**反向對照** —— 只驗「可以多對一」的話，把它改成「永遠回全部的 key」
    也會過。"""
    idx = _build_synonym_index({"負責人": ["負責人"], "聯絡人": ["聯絡人"]})
    for key, got in idx.items():
        assert len(got) == 1, f"{key} 不該對應到多個 key：{got}"


def test_the_real_catalog_actually_contains_a_shared_label():
    """**先證明這件事在真的資料裡成立** —— 不然這條只是在驗一個假設。

    預設表裡沒有共用標籤的話，這一條就該重新檢討（而不是靜靜地通過）。
    """
    from app.core.pdf_form_detect import DEFAULT_LABEL_MAP
    idx = _build_synonym_index(DEFAULT_LABEL_MAP)
    shared = {k: v for k, v in idx.items() if len(v) > 1}
    assert shared, ("預設對照表裡一個共用標籤都沒有 —— "
                    "那這條檢查守的東西可能已經不存在了，要重新檢討")
