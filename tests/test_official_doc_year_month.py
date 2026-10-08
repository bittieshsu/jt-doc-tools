"""「115年12月底前」這種**有年有月、沒寫日**的日期（2026-10-08 拍介紹站截圖時看到）。

範例寫「今年12月底前完成」，程式照規則把草稿的「今年12月」換成「115年12月」（檢查結果另有一條提示），
檢查卻把「115年」當成一個**數量**去比、標成紅色的「找不到依據」—— 程式自己換的字被自己判錯。
原因是日期的規則只認「年月日」，「年＋月」不算日期，年份就被拆成數量了。

要守住的事：

* 「115年12月」是日期：原文「今年12月」（年份照今天算）支持它；寫錯年份（「111年12月」）、
  寫錯月份（「115年11月」）照樣標出來（不可以為了消掉誤報就放鬆）。
* 草稿只寫「12月」照樣有依據（原文的「今年12月」現在算日期，月份要另外記著）；「11月」照樣標。
* 原文「今年12月」草稿沒寫到 → 漏寫提示照舊；寫了「115年12月」就不提示。
* 「使用10年」這種期間、「115年12月20日」完整日期的行為不變。
"""
from __future__ import annotations

import pytest

from app.core import official_doc as od

SRC = ["今年12月底前完成安裝、資料移轉跟測試"]


def _codes(text: str, src=SRC) -> list[tuple[str, tuple]]:
    return [(i.code, tuple(i.args)) for i in od.check_draft(text, src) if i.code != "missing_section"]


@pytest.fixture(autouse=True)
def _year_2026(monkeypatch):
    import datetime
    monkeypatch.setattr(od, "_today", lambda: datetime.date(2026, 10, 8))


def test_resolved_year_month_is_supported():
    assert _codes("主旨：預計於115年12月底前完成安裝。") == []


def test_the_whole_pipeline_does_not_flag_its_own_conversion():
    text, done = od.resolve_relative_years("主旨：預計於今年12月底前完成安裝。")
    assert "115年12月" in text and done
    assert _codes(text) == [], "程式自己換的年份不可以被自己判成找不到依據"


@pytest.mark.parametrize("draft, raw", [("主旨：預計於111年12月底前完成。", "111年12月"),
                                        ("主旨：預計於115年11月底前完成。", "115年11月")])
def test_wrong_year_or_month_is_still_flagged(draft, raw):
    assert _codes(draft) == [("date_unsupported", (raw,))]


def test_month_only_draft():
    assert _codes("主旨：預計於12月底前完成。") == []
    assert _codes("主旨：預計於11月底前完成。") == [("qty_unsupported", ("11月",))]


def test_year_month_is_a_date_not_two_quantities():
    ds = [(d.raw, d.year, d.month, d.day) for d in od.find_dates("預計於115年12月底前、民國114年3月")]
    assert ds == [("115年12月", 2026, 12, None), ("民國114年3月", 2025, 3, None)]
    assert od.find_quantities("預計於115年12月底前") == []
    rel = [(d.raw, d.month) for d in od.find_dates("今年12月底、明年3月、12月份")]
    assert rel == [("12月", 12), ("3月", 3)], "只有前面是今年 / 明年的才算日期"


def test_omission_hint_for_year_month():
    assert not od.omission_issues("主旨：預計於115年12月底前完成。", SRC[0])
    assert [i.args for i in od.omission_issues("主旨：盡快完成。", SRC[0])] == [("12月",)]
    # 原文只寫到月：草稿寫了那個月的某一天也算有寫到（多出來的日由事實檢查另外標）
    assert not od.omission_issues("主旨：預計於115年12月31日前完成。", SRC[0])


def test_a_full_date_in_the_source_is_not_matched_by_a_month():
    """原文「12月20日」、草稿「12月底」是期限被改了 —— 不可以因為同月就算有依據。"""
    assert _codes("主旨：預計於115年12月底前完成。", ["今年12月20日前完成"]) == \
        [("date_unsupported", ("115年12月",))]


def test_other_shapes_unchanged():
    assert [(d.raw, d.day) for d in od.find_dates("115年12月20日前")] == [("115年12月20日", 20)]
    assert od.find_dates("已使用10年") == []
    assert _codes("說明：設備已使用6年。", ["設備用了6年"]) == []
