"""**預覽只有前幾頁時，畫面一定要講出整份有幾頁。**

由來（2026-09-08，客戶回報）：一份 11 頁的文件用文件翻譯翻完，客戶回報
「只會翻到第六頁，第六頁之後就無法產生」。實際上 **182 段翻了 179 段、
產出 11 頁全都有內容** —— 只是畫面上的預覽是 `PREVIEW_PAGES = 6`。

他們同時說「整體字數跟原始檔案接近」，正好印證下載的檔案是完整的。
**畫面上唯一看得到的東西停在第 6 頁，使用者就只能那樣判斷。**

這不是翻譯的 bug，是我們沒把話講清楚 —— 而「使用者只能從畫面判斷」這件事
每一支有預覽的工具都適用。
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

DT = importlib.import_module("app.tools.doc_translate.router")
TPL = Path("app/tools/doc_translate/templates/doc_translate.html").read_text(
    encoding="utf-8")


def test_the_job_reports_the_real_page_count():
    """光有「預覽幾頁」不夠 —— 要有「整份幾頁」才對比得出來。"""
    src = Path(DT.__file__).read_text(encoding="utf-8")
    assert '"total_pages"' in src, "作業摘要沒有回報整份的總頁數"


def test_make_preview_returns_the_total_not_just_the_shown_count():
    import inspect
    sig = inspect.signature(DT._make_preview)
    assert "tuple" in str(sig.return_annotation), \
        "_make_preview 要同時回報「顯示幾張」與「整份幾頁」"


def test_the_page_says_the_preview_is_not_the_whole_thing():
    """判準是**畫面上真的寫得出來**，不是我們心裡知道。"""
    assert "以下只是預覽，不是完整內容" in TPL
    assert "完整內容請按上面的下載鈕" in TPL


def test_the_page_shows_both_numbers():
    """「共 N 頁，這裡只顯示前 M 頁」—— 兩個數字都要在，只有一個看不出差別。"""
    assert "total_pages" in TPL, "畫面沒有用到總頁數"
    m = re.search(r"共 \{1\} 頁，這裡只顯示前 \{0\} 頁", TPL)
    assert m, "預覽區沒有同時寫出總頁數與預覽張數"


def test_the_summary_says_everything_was_translated():
    assert "全部都已翻譯完成" in TPL, \
        "摘要沒有明說整份都翻好了 —— 使用者會從預覽推論"


def test_the_preview_limit_is_still_a_small_number():
    """預覽本來就該只出幾頁（算圖很貴）——**所以更要把話講清楚**。"""
    assert 1 <= DT.PREVIEW_PAGES <= 10, DT.PREVIEW_PAGES


#: 只算前幾頁預覽的工具。**每一支都要在預覽結束的位置擋一下** ——
#: 使用者是捲到最後一張才產生誤會的，寫在上面的說明他早就捲過去了。
PARTIAL_PREVIEW_TOOLS = {
    "app/tools/doc_translate/templates/doc_translate.html": "dt-prev-end",
    "app/tools/pdf_to_office/templates/pdf_to_office.html": "pv-end",
}


@pytest.mark.parametrize("tpl,cls", sorted(PARTIAL_PREVIEW_TOOLS.items()))
def test_every_partial_preview_blocks_the_scroll_with_a_notice(tpl, cls):
    """判準有三個，缺一個就還是會被誤會：

    1. 擋板真的長出來（`className` 設在那個類別上）
    2. 它**不是灰色小字**（有自己的樣式：框線＋底色）
    3. 上面寫得出「共 N 頁 / 後面還有幾頁」
    """
    html = Path(tpl).read_text(encoding="utf-8")
    assert f"'{cls}'" in html or f'"{cls}"' in html, f"{tpl}：沒有建立擋板元素"
    assert f".{cls} {{" in html, f"{tpl}：擋板沒有自己的樣式（會變成一行灰字）"
    assert "border" in html.split(f".{cls} {{", 1)[1][:200],         f"{tpl}：擋板沒有框線，看起來還是像普通說明"
    assert "預覽到這裡為止" in html, f"{tpl}：沒有明說預覽結束"
    assert "這份文件共" in html, f"{tpl}：沒有寫出整份的總頁數"


def test_the_translation_end_block_offers_the_download_right_there():
    """擋板上就要有下載鈕 —— 讓他往回捲找按鈕，還是有人會放棄。"""
    assert "下載翻譯後的檔案（共 {0} 頁）" in TPL
