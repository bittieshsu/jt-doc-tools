"""心智圖節點的文字：縮短可以，但**要看得出來是縮短**。

## 由來（使用者 2026-09-20 截圖回報「有一些整理的內容 後面有字被截斷」）

節點標籤原本是 `str(it["text"])[:60]` —— **硬砍 60 個字、不留任何記號**。
畫面上看到的是「…把環境變數（Environment Variable）加上 L」、「…全部 de」，
使用者分不出這是「顯示時縮短了」還是「分析把後面弄丟了」，
而這兩件事的嚴重度天差地遠（卡片與逐字稿其實都是完整的）。

## ⚠ 為什麼先前所有的量測都看不到

翻出先前跑過的 **868 條**項目：中位數 18 字、**最長 56** —— 一次都沒碰到 60。
那些全是**評測語料**（合成的短會議）。拿真的逐字稿跑就看得到：質詢與技術會議
的項目帶著英文術語與括號註解，**72 字以上很常見**。

**語料再多，形狀不對就照不到** —— 這跟「合成樣本抓不到的那一類」是同一條。
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app.core import meeting_insight as mi

JS = pathlib.Path(__file__).resolve().parent.parent / "static" / "js" / "meeting_charts.js"


#: **測試自己的「識別字」定義** —— 不可以用 `mi._is_token_char`。
#:
#: 第一版就是用它，結果把 `_TOKEN_EXTRA` 清空的變異**完全抓不到**：
#: 判準跟著被變異的那個函式一起移動，永遠自洽。
#: 檢查的判準不可以跟被守的程式共用同一個定義。
_IDENT = re.compile(r"[A-Za-z0-9_.\-/:=+]")


def _looks_like_identifier_char(ch: str) -> bool:
    return bool(_IDENT.fullmatch(ch))


def test_short_text_is_left_alone():
    assert mi.shorten_for_node("短的待辦") == "短的待辦"
    exact = "中" * mi.NODE_LABEL_MAX
    assert mi.shorten_for_node(exact) == exact, "剛好等於上限不該被動到"


def test_shortening_is_always_visible():
    """**這條是整份的重點。**

    只驗「有沒有超過上限」的話，硬砍不留記號也會過 —— 而那正是原本的 bug。
    """
    out = mi.shorten_for_node("中" * (mi.NODE_LABEL_MAX + 40))
    assert out.endswith("…"), f"縮短了卻看不出來：{out[-12:]!r}"
    assert len(out) <= mi.NODE_LABEL_MAX + 1


@pytest.mark.parametrize("tail", [
    "LOG4J_FORMAT_MSG_NO_LOOKUPS=true 後面還有字",   # 底線
    "deployment.yaml 檔案裡面",                      # 點
    "X-Forwarded-Proto 這個標頭",                    # 連字號
    "/bin/sh 立刻斷線",                              # 斜線
])
def test_never_cuts_a_latin_identifier_in_half(tail):
    """`加上 L`、`全部 de` 那種切法看起來像壞掉，比少顯示幾個字糟得多。

    判準要涵蓋識別字裡的 `_` `.` `-` `/` —— 只認英數的話，
    `LOG4J_FORMAT` 會在底線上被判成「詞的邊界」，照樣切在半個詞中間。
    """
    text = "中" * (mi.NODE_LABEL_MAX - 6) + " " + tail
    out = mi.shorten_for_node(text)
    assert out.endswith("…")
    kept = out[:-1]                      # 去掉刪節號才是「原文留下了多少」
    assert text.startswith(kept.rstrip()) or text.startswith(kept)

    # **判準：原文中緊接在留下來那一段後面的字，不可以跟它連成同一個識別字。**
    #
    # 第一版寫成 `tail.startswith(留下來的最後一段)` —— 那是**沒有牙齒的**：
    # `LOG4J` 正好是 `LOG4J_FORMAT…` 的前綴，也就是**壞掉的那種切法會被判成合格**。
    # 變異驗證（把 `_TOKEN_EXTRA` 清空）當場全綠才看到。
    n = len(kept.rstrip())
    if 0 < n < len(text):
        assert not (_looks_like_identifier_char(text[n - 1])
                    and _looks_like_identifier_char(text[n])), (
            f"切在半個識別字中間：…{text[max(0, n - 10):n]!r} | {text[n:n + 10]!r}…"
        )


def test_a_very_long_identifier_is_still_cut_rather_than_losing_a_chunk():
    """整段都是識別字時**寧可硬切** —— 為了對齊邊界讓掉一大截比切開更糟。"""
    text = "中" * (mi.NODE_LABEL_MAX - 40) + "A" * 80
    out = mi.shorten_for_node(text)
    kept = len(out.rstrip("…"))
    assert out.endswith("…")
    assert kept >= mi.NODE_LABEL_MAX - mi._NODE_LABEL_BACKOFF, (
        f"為了邊界只留下 {kept} 字，讓掉太多"
    )


def test_the_limit_is_above_what_real_transcripts_produce():
    """**上限要量過真的逐字稿再定。**

    60 是拿評測語料定的（那些項目中位數 18 字、最長 56），所以從來沒有被觸發過。
    拿真的質詢逐字稿跑一次（54 條）：**中位數 39、p90 = 101、最長 379**。

    | 上限 | 完整顯示 |
    |---:|---:|
    | 60（原本） | **77%** —— 每四條就有一條被截 |
    | 90 | 87% |
    | 120（現在） | **94%** |
    | 160 | 98% |

    120 是這樣選的：94% 不必縮短，剩下的會縮但**看得出來是縮短**，
    完整內容在提示框與下方卡片裡。再往上會讓少數極長的項目把節點撐成三四行。
    """
    assert mi.NODE_LABEL_MAX >= 100, (
        "上限掉到實測 p90（101）以下 —— 會回到「每幾條就有一條被截斷」"
    )


def test_build_mindmap_carries_the_full_text_only_when_it_shortened():
    long_text = "中" * (mi.NODE_LABEL_MAX + 30)
    chapters = [{"title": "章", "segment_ids": [1, 2], "start_seq": 1, "end_seq": 2}]
    items = {k: [] for k in mi.KINDS}
    items["decisions"] = [{"text": long_text, "segment_ids": [1]},
                          {"text": "短的", "segment_ids": [2]}]
    nodes = mi.build_mindmap(chapters, items)
    kids = [n for n in nodes if n.get("parent_id")]
    assert len(kids) == 2
    shortened = [n for n in kids if n["label"].endswith("…")]
    assert len(shortened) == 1
    assert shortened[0]["label_full"] == long_text, "縮短了卻沒帶完整版"
    plain = [n for n in kids if not n["label"].endswith("…")][0]
    assert "label_full" not in plain, "沒縮短就不該多帶一份（白白變大）"


def test_the_tooltip_uses_the_full_text():
    """滑鼠移上去要看得到完整內容 —— 提示框用縮過的那份等於沒有幫助。

    **這條只看得到接線**；畫面上真的顯示什麼要靠人工操作（TEST_PLAN §0.6）。
    """
    body = JS.read_text(encoding="utf-8")
    hits = body.count("label_full")
    assert hits >= 4, f"心智圖的提示框沒有改用完整版（只找到 {hits} 處）"
