"""書籤與目錄。

## 由來

標案、年報、結案報告動輒十幾個檔案合成三百頁。合併工具只負責串接，**產出完全
沒有書籤** —— 收件方只能一直捲，而書籤是 PDF 閱讀器唯一的導覽方式。

## 這一份主要在守四個坑

1. **層級規則很嚴**：`set_toc` 要求第一項是 level 1、層級一次只能加一
   （1→3 直接丟 `ValueError`）。使用者手打的清單很容易違反 —— 要先修好再送，
   而且**每一項修改都要講出來**，不能靜靜改掉。
2. **頁碼超出範圍是無聲的**：`set_toc` 對超過總頁數的頁碼不報錯，**默默夾到
   最後一頁**。使用者以為指到第 500 頁，實際上全部指向最後一頁。
3. **插目錄頁會讓所有頁碼位移**。書籤、目錄上顯示的頁碼、目錄的連結三者都要
   一起移，漏掉任何一個都會指錯。
4. **合併時的頁碼偏移**：子文件自己的書籤要加上它在合併結果裡的起始頁。
"""
from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from app.tools.pdf_bookmark import bookmark_core as BC


def _doc(pages: int, label: str = "P") -> fitz.Document:
    d = fitz.open()
    for i in range(pages):
        pg = d.new_page(width=595, height=842)
        pg.insert_text((72, 100), f"{label}{i + 1}", fontsize=11)
    return d


# ------------------------------------------------------------ 層級正規化

def test_first_item_must_be_level_one():
    """第一項不是 level 1 的話 `set_toc` 會直接丟例外 —— 要先修好。"""
    fixed, warns = BC.normalize([BC.BookmarkItem("開頭就第三層", 1, 3)], 5)
    assert fixed[0].level == 1
    assert any("第一筆" in w for w in warns)


def test_level_cannot_jump():
    fixed, _ = BC.normalize([BC.BookmarkItem("A", 1, 1),
                             BC.BookmarkItem("B", 2, 4)], 5)
    assert [i.level for i in fixed] == [1, 2]


def test_every_fix_is_reported():
    """**靜靜改掉最糟** —— 使用者打的層級被改卻沒人講，他只會覺得工具壞了。"""
    _fixed, warns = BC.normalize([BC.BookmarkItem("A", 1, 3),
                                  BC.BookmarkItem("B", 999, 1)], 5)
    assert len(warns) >= 2
    assert any("999" in w for w in warns)


def test_normalized_result_is_accepted_by_pymupdf():
    """正規化的意義就是「送進去不會炸」—— 直接拿真的 API 驗。"""
    d = _doc(5)
    items = [BC.BookmarkItem("跳級開頭", 1, 3), BC.BookmarkItem("A", 2, 1),
             BC.BookmarkItem("跳很多", 3, 6), BC.BookmarkItem("超範圍", 999, 2)]
    warns = BC.apply_bookmarks(d, items)     # 不可以丟例外
    assert d.get_toc()
    assert warns
    d.close()


def test_page_out_of_range_is_clamped_and_reported():
    """`set_toc` 對超範圍頁碼**不報錯、默默夾到最後一頁** —— 要自己擋並講。"""
    fixed, warns = BC.normalize([BC.BookmarkItem("A", 999, 1)], 7)
    assert fixed[0].page == 7
    assert any("7 頁" in w for w in warns)


def test_blank_title_is_dropped():
    fixed, warns = BC.normalize([BC.BookmarkItem("   ", 1, 1)], 3)
    assert fixed == []
    assert warns


# ------------------------------------------------------------ 合併

def test_merge_uses_filenames_as_top_level(tmp_path):
    srcs = []
    for i, (n, name) in enumerate([(3, "投標須知"), (2, "規格書")]):
        p = tmp_path / f"{i}.pdf"
        d = _doc(n, name)
        d.save(str(p))
        d.close()
        srcs.append((p, name))
    total, items = BC.merge_with_bookmarks(srcs, tmp_path / "out.pdf")
    assert total == 5
    assert [(i.title, i.page, i.level) for i in items] == \
        [("投標須知", 1, 1), ("規格書", 4, 1)]


def test_merge_keeps_inner_bookmarks_with_offset(tmp_path):
    """子文件自己的書籤要**加上偏移並降一層** —— 標案的子文件常常自己就有結構。"""
    a = tmp_path / "a.pdf"
    d = _doc(3, "A")
    d.save(str(a))
    d.close()
    b = tmp_path / "b.pdf"
    d = _doc(4, "B")
    d.set_toc([[1, "第一節", 1], [1, "第二節", 3]])
    d.save(str(b))
    d.close()
    _total, items = BC.merge_with_bookmarks([(a, "甲"), (b, "乙")],
                                            tmp_path / "out.pdf")
    got = [(i.title, i.page, i.level) for i in items]
    assert ("乙", 4, 1) in got
    assert ("第一節", 4, 2) in got, f"偏移或層級錯了：{got}"
    assert ("第二節", 6, 2) in got


def test_merge_can_drop_inner_bookmarks(tmp_path):
    b = tmp_path / "b.pdf"
    d = _doc(2)
    d.set_toc([[1, "內部", 1]])
    d.save(str(b))
    d.close()
    _t, items = BC.merge_with_bookmarks([(b, "乙")], tmp_path / "o.pdf",
                                        keep_inner=False)
    assert [i.title for i in items] == ["乙"]


def test_merged_file_really_has_the_bookmarks(tmp_path):
    """寫進檔案才算數 —— 回傳的 list 對但沒寫進去是最容易漏的。"""
    a = tmp_path / "a.pdf"
    d = _doc(2, "A")
    d.save(str(a))
    d.close()
    out = tmp_path / "out.pdf"
    BC.merge_with_bookmarks([(a, "甲")], out)
    assert fitz.open(str(out)).get_toc() == [[1, "甲", 1]]


# ------------------------------------------------------------ 目錄頁

def _router():
    import sys

    import app.tools.pdf_bookmark.router  # noqa: F401 — 讓子模組進 sys.modules
    return sys.modules["app.tools.pdf_bookmark.router"]


def test_toc_page_shifts_bookmarks(tmp_path):
    """**這是最容易錯的地方**：插了目錄頁，所有書籤都要往後移。"""
    src = tmp_path / "s.pdf"
    d = _doc(6)
    d.set_toc([[1, "第一章", 1], [1, "第二章", 4]])
    d.save(str(src))
    d.close()
    dst = tmp_path / "o.pdf"
    items = BC.read_bookmarks(fitz.open(str(src)))
    info = _router()._write_result(src, dst, items, toc_page=True,
                                   toc_title="目錄", toc_max_level=3,
                                   toc_dots=True)
    assert info["toc_pages"] >= 1
    out = fitz.open(str(dst))
    assert out.page_count == 6 + info["toc_pages"]
    toc = out.get_toc()
    assert toc[0][2] == 1 + info["toc_pages"], f"書籤沒有跟著位移：{toc}"


def test_toc_page_numbers_match_the_bookmarks(tmp_path):
    """目錄上印的頁碼要跟書籤指的頁碼一致 —— 兩邊各算一次很容易差一頁。"""
    src = tmp_path / "s.pdf"
    d = _doc(8)
    d.set_toc([[1, "甲", 2], [1, "乙", 5]])
    d.save(str(src))
    d.close()
    dst = tmp_path / "o.pdf"
    _router()._write_result(src, dst, BC.read_bookmarks(fitz.open(str(src))),
                            toc_page=True, toc_title="目錄", toc_max_level=3,
                            toc_dots=False)
    out = fitz.open(str(dst))
    text = out[0].get_text()
    for _lvl, title, page in out.get_toc():
        assert title in text
        assert str(page) in text, f"目錄上找不到「{title}」的頁碼 {page}"


def test_toc_links_point_at_the_right_page(tmp_path):
    """目錄要點得動，而且要點到對的地方。"""
    src = tmp_path / "s.pdf"
    d = _doc(6)
    d.set_toc([[1, "甲", 3]])
    d.save(str(src))
    d.close()
    dst = tmp_path / "o.pdf"
    _router()._write_result(src, dst, BC.read_bookmarks(fitz.open(str(src))),
                            toc_page=True, toc_title="目錄", toc_max_level=3,
                            toc_dots=True)
    out = fitz.open(str(dst))
    links = out[0].get_links()
    assert links, "目錄頁沒有任何連結"
    target = links[0]["page"] + 1           # 0-based → 1-based
    assert target == out.get_toc()[0][2], "連結與書籤指到不同頁"


def test_toc_max_level_is_respected(tmp_path):
    src = tmp_path / "s.pdf"
    d = _doc(5)
    d.set_toc([[1, "一層", 1], [2, "二層", 2], [3, "三層", 3]])
    d.save(str(src))
    d.close()
    dst = tmp_path / "o.pdf"
    _router()._write_result(src, dst, BC.read_bookmarks(fitz.open(str(src))),
                            toc_page=True, toc_title="目錄", toc_max_level=1,
                            toc_dots=False)
    text = fitz.open(str(dst))[0].get_text()
    assert "一層" in text and "二層" not in text


def test_no_toc_page_when_not_requested(tmp_path):
    src = tmp_path / "s.pdf"
    d = _doc(4)
    d.save(str(src))
    d.close()
    dst = tmp_path / "o.pdf"
    info = _router()._write_result(src, dst, [BC.BookmarkItem("甲", 1, 1)],
                                   toc_page=False, toc_title="目錄",
                                   toc_max_level=3, toc_dots=True)
    assert info["toc_pages"] == 0
    assert fitz.open(str(dst)).page_count == 4


def test_toc_page_uses_a_cjk_font(tmp_path):
    """目錄是中文的 —— 用錯字型會整排缺字方框，而且畫面上看不出來。"""
    src = tmp_path / "s.pdf"
    d = _doc(4)
    d.save(str(src))
    d.close()
    dst = tmp_path / "o.pdf"
    _router()._write_result(src, dst, [BC.BookmarkItem("第一章 緒論", 1, 1)],
                            toc_page=True, toc_title="目錄", toc_max_level=3,
                            toc_dots=False)
    text = fitz.open(str(dst))[0].get_text()
    assert "第一章" in text and "目錄" in text


# ------------------------------------------------------------ 貼上清單

@pytest.mark.parametrize("line,title,page,level", [
    ("第一章 緒論      3", "第一章 緒論", 3, 1),
    ("  1.1 研究背景   5", "1.1 研究背景", 5, 2),
    ("    1.1.1 細節  7", "1.1.1 細節", 7, 3),
    ("第二章…………14", "第二章", 14, 1),
    ("附錄 A .......... 99", "附錄 A", 99, 1),
])
def test_parse_text_list(line, title, page, level):
    items, _w = BC.parse_text_list(line)
    assert len(items) == 1
    assert (items[0].title, items[0].page, items[0].level) == (title, page, level)


def test_parse_reports_unparseable_lines():
    """看不出頁碼的行要**講出來**，不能安靜吃掉 —— 使用者會以為貼上去了。"""
    items, warns = BC.parse_text_list("這行沒有頁碼\n第一章 3")
    assert len(items) == 1
    assert warns and "這行沒有頁碼" in warns[0]


def test_parse_ignores_blank_lines():
    items, warns = BC.parse_text_list("\n\n第一章 3\n\n")
    assert len(items) == 1 and not warns


# ------------------------------------------------------------ 自動偵測

def test_auto_detect_finds_larger_text(tmp_path):
    d = fitz.open()
    for i in range(3):
        pg = d.new_page(width=595, height=842)
        pg.insert_text((72, 90), f"CHAPTER {i + 1}", fontsize=22)
        for k in range(6):
            pg.insert_text((72, 140 + k * 16),
                           "body text line that is long enough", fontsize=10)
    items = BC.auto_detect(d)
    d.close()
    assert len(items) == 3
    assert all(i.level == 1 for i in items)
    assert items[0].page == 1 and items[2].page == 3


def test_auto_detect_returns_nothing_when_uniform(tmp_path):
    """字級沒有差異時要**回空**，不要硬湊 —— 硬湊出來的書籤比沒有更礙事。"""
    d = fitz.open()
    for _ in range(3):
        pg = d.new_page()
        for k in range(8):
            pg.insert_text((72, 100 + k * 16), "same size everywhere",
                           fontsize=11)
    items = BC.auto_detect(d)
    d.close()
    assert items == []


# ------------------------------------------------------------ 工具註冊

def test_tool_is_registered_and_granted():
    from app.core.roles import SEED_ROLES
    from app.tool_registry import discover_tools
    ids = {t.metadata.id for t in discover_tools()}
    assert "pdf-bookmark" in ids
    granted = {r["id"] for r in SEED_ROLES if "pdf-bookmark" in (r.get("tools") or [])}
    assert "default-user" in granted, "一般使用者看不到這個工具"
    assert "clerk" in granted, "文管看不到 —— 這正是文管的活"


def test_backfill_migration_exists():
    """新工具要**主動補給既有客戶** —— seed 的 bootstrap 缺口會讓舊安裝永遠看不到。"""
    from app.core import auth_db
    assert any(f.__name__.endswith("pdf_bookmark") for f in auth_db.MIGRATIONS)


def test_search_keywords_exist():
    from app.main import _TOOL_ALIASES
    kw = _TOOL_ALIASES.get("pdf-bookmark", "")
    assert "書籤" in kw and "bookmark" in kw and "目錄" in kw


# ------------------------------------------------------------ 目錄插入位置

def test_toc_can_be_inserted_after_a_cover_page():
    """很多文件有封面，目錄要排在封面**後面**（第 2 頁）。"""
    d = fitz.open()
    for _ in range(5):
        d.new_page(width=595, height=842)
    items = [BC.BookmarkItem(title="封面", page=1, level=1),
             BC.BookmarkItem(title="第一章", page=3, level=1)]
    n = BC.build_toc_page(d, items, BC.TocPageSpec(title="目錄"), at_page=2)
    assert n == 1
    assert d.page_count == 6
    # 目錄在第 2 頁（索引 1），第 1 頁仍是原本的封面
    assert "目錄" in d[1].get_text()


def test_bookmarks_before_the_insert_point_do_not_move():
    """**插入點之前的書籤不可以平移** —— 封面那筆跟著移就會指到目錄自己身上。"""
    items = [BC.BookmarkItem(title="封面", page=1, level=1),
             BC.BookmarkItem(title="第一章", page=3, level=1),
             BC.BookmarkItem(title="第二章", page=5, level=1)]
    out = BC.shift_pages(items, 1, from_page=2)
    assert [i.page for i in out] == [1, 4, 6], (
        "封面應停在第 1 頁，其餘往後推一頁")


def test_shift_pages_defaults_to_shifting_everything():
    """插在最前面時（from_page=1）行為與原本相同。"""
    items = [BC.BookmarkItem(title="a", page=1, level=1),
             BC.BookmarkItem(title="b", page=2, level=1)]
    assert [i.page for i in BC.shift_pages(items, 2)] == [3, 4]


def test_toc_page_numbers_account_for_the_insert_point():
    """目錄上印的頁碼：插入點之前的不加、之後的要加上目錄頁數。"""
    d = fitz.open()
    for _ in range(6):
        d.new_page(width=595, height=842)
    items = [BC.BookmarkItem(title="封面說明", page=1, level=1),
             BC.BookmarkItem(title="正文開始", page=4, level=1)]
    BC.build_toc_page(d, items, BC.TocPageSpec(title="目錄"), at_page=2)
    txt = d[1].get_text()
    # 封面仍是 1；正文原本第 4 頁，插一頁目錄後變第 5 頁
    assert "1" in txt and "5" in txt


def test_insert_point_beyond_the_document_is_clamped():
    """填超過總頁數不可以炸掉（PyMuPDF 對頁碼越界是無聲的）。"""
    d = fitz.open()
    for _ in range(3):
        d.new_page(width=595, height=842)
    n = BC.build_toc_page(d, [BC.BookmarkItem(title="x", page=1, level=1)],
                          BC.TocPageSpec(), at_page=999)
    assert n == 1 and d.page_count == 4


# ------------------------------------------------------------ 貼上清單的效能

def test_pasted_list_does_not_blow_up_on_pathological_input():
    """**貼上的清單是使用者可控的輸入，解析必須是線性的。**

    原本的式子把 `(title.*?)` 接在 `[\\s.…·]*` 前面 —— 兩者可以吃到同一批
    字元，遇到不匹配的行就逐一回溯。實測一行 16,000 個點要 5.4 秒，而每一行
    都會跑一次：貼一百行就是好幾分鐘的 CPU（CodeQL 標為 ReDoS）。
    """
    import time
    # 整行都是點、結尾沒有數字 —— 最壞情況
    evil = "\n".join(["." * 20000] * 20)
    t0 = time.time()
    items, warns = BC.parse_text_list(evil)
    elapsed = time.time() - t0
    assert elapsed < 1.0, f"解析花了 {elapsed:.2f} 秒 —— 回溯又回來了"
    assert not items and len(warns) == 20


def test_pasted_list_still_parses_normally():
    """效能修正不可以改變解析結果。"""
    items, warns = BC.parse_text_list(
        "第一章 緒論      3\n"
        "  1.1 研究背景 ......... 5\n"
        "　　1.2 方法   9\n"
        "第二章 文獻探討……14\n"
        "沒有頁碼的一行\n"
        "   42")
    assert [(i.level, i.title, i.page) for i in items] == [
        (1, "第一章 緒論", 3),
        (2, "1.1 研究背景", 5),
        (3, "1.2 方法", 9),
        (1, "第二章 文獻探討", 14),
    ]
    assert len(warns) == 2      # 沒頁碼的、只有頁碼沒標題的
