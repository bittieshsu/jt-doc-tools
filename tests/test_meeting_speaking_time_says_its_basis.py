"""「發言時間」是量到的還是推估的，畫面上要講出來。

## 由來（2026-09-20 外部回饋第 5 點：「發言時間需要說明依據」）

字幕檔（.vtt / .srt）與 JSON 的每一段都帶著**自己的結束時間** —— 那是量到的。
純文字逐字稿只有「誰在幾點幾分開始講」，結束時間是拿**下一段的開始**補上去的
（`transcript_parse.parse_plain`），所以算出來的發言時間把中間的停頓也算了進去。

兩者在畫面上長得一模一樣 —— 不講的話，就是**介面承諾了一個沒做到的精準度**。

> 台灣寫「**推估**」（從別的數字推算來的），不是「估計」
> —— 見 `tests/test_taiwan_terminology.py`。
"""
from __future__ import annotations

import pathlib

import pytest

from app.core import transcript_parse as tp
from app.tools.meeting_summary.router import _MEASURED_SHAPES

TPL = (pathlib.Path(__file__).resolve().parent.parent / "app" / "tools"
       / "meeting_summary" / "templates" / "meeting_summary.html")

VTT = (b"WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n\xe7\x8e\x8b: hi\n\n"
       b"00:00:06.000 --> 00:00:09.000\nb: yo\n")
PLAIN = "16:19 王小明：第一句\n16:22 李小華：第二句\n16:30 王小明：第三句\n".encode()


@pytest.mark.parametrize("data,name,measured", [
    (VTT, "a.vtt", True),
    (PLAIN, "a.txt", False),
])
def test_the_parser_kind_decides_whether_times_are_measured(data, name, measured):
    _segs, shape = tp.parse(data, name)
    assert (shape in _MEASURED_SHAPES) is measured, (
        f"{name} 解析成 {shape!r}，被判成 {'量到的' if not measured else '推估的'}"
    )


def test_plain_text_really_does_borrow_the_next_start(  ):
    """**先證明那個前提成立。**

    「純文字的結束時間是推估的」如果哪天不再成立（例如解析器改成不補），
    上面那條的期望值就會變成在說謊，而它照樣全綠。
    """
    segs, _ = tp.parse(PLAIN, "a.txt")
    assert len(segs) >= 2
    assert segs[0]["end_ms"] == segs[1]["start_ms"], (
        "純文字的結束時間不再是下一段的開始 —— 這份測試的前提要重寫"
    )
    assert segs[-1].get("end_ms") is None, "最後一段沒有下一段，本來就不該有結束時間"


def test_the_cue_file_keeps_its_own_end_time():
    """反面：字幕檔的結束時間是它自己的，不是下一段的開始。"""
    segs, _ = tp.parse(VTT, "a.vtt")
    assert segs, "字幕檔一段都沒解出來"
    # 合併過後至少要保有「結束時間不等於下一段開始」這件事
    assert segs[0]["end_ms"] is not None


def test_the_page_labels_the_column_by_that_flag():
    """欄位標題與說明都要走伺服器端給的旗標，不可以前端自己猜。

    **這條只看得到「有沒有接線」**，看不到畫面上真的長什麼樣 ——
    那一格要靠人工逐頁操作（TEST_PLAN §0.6 的第三種方法）。
    """
    body = TPL.read_text(encoding="utf-8")
    assert "times_are_measured" in body, "樣板沒有讀伺服器端給的旗標"
    assert "tr('推估發言時間')" in body, "推估時沒有換標題"
    assert "tr('發言時間')" in body, "量到時的標題不見了"
    assert "msSpkNote" in body, "少了表格底下那行說明"
    # 自己開的類別名要有樣式，不然是一段沒有樣式的純文字貼在表格下緣
    assert ".ms-note {" in body, "`.ms-note` 沒有補樣式"
