"""書籤（PDF outline）與目錄頁的核心邏輯。

## 為什麼要有這支工具

標案、年報、結案報告動輒是十幾個檔案合成三百頁。合併工具只負責串接，
**產出完全沒有書籤** —— 收件方只能一直捲。而書籤是 PDF 閱讀器唯一的導覽方式。

## 這裡處理的四個坑

1. **層級規則很嚴**：PyMuPDF 的 `set_toc` 要求第一項必須是 level 1、且層級一次
   只能加一（1→3 直接丟 `ValueError`）。使用者手打的清單很容易違反，所以要先
   正規化再送進去，不是讓它炸。
2. **頁碼超出範圍是無聲的**：`set_toc` 對超過總頁數的頁碼**不會報錯，會默默夾到
   最後一頁**。使用者以為書籤指到第 500 頁，實際上全部指向最後一頁 —— 要自己擋
   並回報。
3. **插入目錄頁會讓所有頁碼位移一頁**。先建書籤再插目錄頁的話，每一個書籤都會
   指錯位置。順序與 +1 都要在同一個地方處理，不能散在呼叫端。
4. **合併時的頁碼偏移**：每一份來源檔的書籤都要加上它在合併結果裡的起始頁。
   來源檔自己的書籤要往下降一層，掛在「檔名」那一層底下。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import fitz


@dataclass
class BookmarkItem:
    """一筆書籤。`page` 是 **1 起算**的頁碼（與畫面一致）。"""
    title: str
    page: int
    level: int = 1

    def as_toc_row(self) -> list:
        return [self.level, self.title, self.page]


# --------------------------------------------------------------- 讀 / 寫 --

def read_bookmarks(doc: fitz.Document) -> list[BookmarkItem]:
    """讀出既有書籤。沒有就回空 list。"""
    out: list[BookmarkItem] = []
    for row in doc.get_toc(simple=True) or []:
        try:
            lvl, title, page = int(row[0]), str(row[1]), int(row[2])
        except (ValueError, IndexError, TypeError):
            continue
        out.append(BookmarkItem(title=title, page=max(1, page), level=max(1, lvl)))
    return out


def normalize(items: list[BookmarkItem], page_count: int
              ) -> tuple[list[BookmarkItem], list[str]]:
    """把使用者給的書籤修成 PyMuPDF 收得下的形狀。回 (修好的, 警告訊息)。

    **不是靜靜修掉就算了** —— 每一項修改都回一句話，畫面上要顯示。使用者打的
    層級被改掉卻沒人告訴他，他只會覺得工具壞了。
    """
    fixed: list[BookmarkItem] = []
    warns: list[str] = []
    prev_level = 0
    for it in items:
        title = (it.title or "").strip()
        if not title:
            warns.append("略過一筆沒有標題的書籤")
            continue
        page = int(it.page or 1)
        if page < 1:
            warns.append(f"「{title}」的頁碼 {page} 小於 1，改為第 1 頁")
            page = 1
        elif page > page_count:
            # set_toc 對超出範圍的頁碼**不報錯，默默夾到最後一頁** —— 要講出來
            warns.append(f"「{title}」指到第 {page} 頁，但文件只有 "
                         f"{page_count} 頁，改為最後一頁")
            page = page_count
        level = max(1, int(it.level or 1))
        if prev_level == 0 and level != 1:
            warns.append(f"「{title}」是第一筆但層級為 {level}，改為第 1 層")
            level = 1
        elif level > prev_level + 1:
            warns.append(f"「{title}」的層級從 {prev_level} 跳到 {level}，"
                         f"改為第 {prev_level + 1} 層")
            level = prev_level + 1
        fixed.append(BookmarkItem(title=title[:300], page=page, level=level))
        prev_level = level
    return fixed, warns


def apply_bookmarks(doc: fitz.Document, items: list[BookmarkItem]) -> list[str]:
    """把書籤寫進文件（會先正規化）。回警告訊息。"""
    fixed, warns = normalize(items, doc.page_count)
    doc.set_toc([i.as_toc_row() for i in fixed] if fixed else [])
    return warns


def shift_pages(items: list[BookmarkItem], delta: int,
                from_page: int = 1) -> list[BookmarkItem]:
    """書籤頁碼平移。插入目錄頁、合併時都要用。

    **`from_page` 之前的書籤不動** —— 目錄可以插在封面後面（第 2 頁），
    那時指向封面的書籤仍然是第 1 頁；跟著一起平移的話就會指到目錄頁去。
    """
    return [BookmarkItem(title=i.title,
                         page=i.page + (delta if i.page >= from_page else 0),
                         level=i.level)
            for i in items]


# ------------------------------------------------------------- 合併建書籤 --

def merge_with_bookmarks(sources: list[tuple[Path, str]], dst: Path,
                         *, keep_inner: bool = True
                         ) -> tuple[int, list[BookmarkItem]]:
    """把多份 PDF 串起來，**每一份的檔名成為第一層書籤**。

    `sources` 是 [(路徑, 顯示名稱)]。`keep_inner=True` 時，來源檔自己的書籤會
    往下降一層掛在檔名底下 —— 標案的子文件常常自己就有結構，丟掉很可惜。

    回 (總頁數, 書籤清單)。
    """
    out = fitz.open()
    items: list[BookmarkItem] = []
    for path, label in sources:
        with fitz.open(str(path)) as src:
            if not src.page_count:
                continue
            start = out.page_count + 1          # 這一份在合併結果裡的第一頁
            inner = read_bookmarks(src) if keep_inner else []
            out.insert_pdf(src)
        items.append(BookmarkItem(title=label, page=start, level=1))
        for b in inner:
            # 來源檔自己的書籤：頁碼加偏移、層級往下推一層
            items.append(BookmarkItem(title=b.title,
                                      page=b.page + start - 1,
                                      level=b.level + 1))
    total = out.page_count
    if total:
        apply_bookmarks(out, items)
        out.save(str(dst), garbage=3, deflate=True)
    out.close()
    return total, items


# --------------------------------------------------------------- 自動偵測 --

#: 判定為標題的字級門檻：比整份文件的**內文字級**大多少倍。
_HEADING_RATIO = 1.15
#: 一份文件最多自動抓幾個標題。抓太多等於沒有導覽價值。
_MAX_AUTO = 300


def auto_detect(doc: fitz.Document, max_items: int = _MAX_AUTO
                ) -> list[BookmarkItem]:
    """依字級大小猜標題，產生書籤草稿。

    **這是草稿不是結果** —— 一定要讓使用者在畫面上改。字級啟發式對排版規矩的
    文件很準，對簡報或表單很不準，而工具無法自己分辨。

    做法：先統計全文最常見的字級（=內文），比它大 15% 以上的行視為標題，
    再依字級由大到小分層。
    """
    sizes: dict[float, int] = {}
    lines: list[tuple[float, str, int]] = []      # (size, text, page)
    for pno in range(doc.page_count):
        try:
            d = doc[pno].get_text("dict")
        except Exception:  # noqa: BLE001
            continue
        for block in d.get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(s.get("text", "") for s in spans).strip()
                if not text or len(text) > 80:      # 太長的不是標題
                    continue
                size = round(max(float(s.get("size", 0)) for s in spans), 1)
                sizes[size] = sizes.get(size, 0) + len(text)
                lines.append((size, text, pno + 1))
    if not sizes or not lines:
        return []
    body = max(sizes.items(), key=lambda kv: kv[1])[0]     # 字數最多 = 內文
    heads = [(s, t, p) for s, t, p in lines if s >= body * _HEADING_RATIO]
    if not heads:
        return []
    # 字級由大到小 → 層級 1, 2, 3…（最多三層，再深就沒有導覽意義）
    tiers = sorted({s for s, _, _ in heads}, reverse=True)[:3]
    out: list[BookmarkItem] = []
    seen: set[tuple[str, int]] = set()
    for size, text, page in heads:
        if size not in tiers:
            continue
        key = (text, page)
        if key in seen:                 # 同一頁重複的標題（頁首頁尾）只留一次
            continue
        seen.add(key)
        out.append(BookmarkItem(title=text, page=page,
                                level=tiers.index(size) + 1))
        if len(out) >= max_items:
            break
    return out


# ------------------------------------------------------------- 文字清單 --

#: 行尾的頁碼。**只錨在尾端、不含任何萬用比對**，所以是線性的。
#:
#: 原本寫成 `^(indent)(title.*?)[\s.…·]*(page\d+)\s*$` —— `.*?` 與後面的
#: `[\s.…·]*` **可以吃到同一批字元**，遇到不匹配的行（例如整行都是點、
#: 結尾沒有數字）就會逐一回溯，變成多項式時間。實測一行 16,000 個點要
#: **5.4 秒**，而這裡的輸入是使用者貼上的目錄清單、每一行都會跑一次 ——
#: 貼一百行就是好幾分鐘的 CPU（CodeQL 也標了 ReDoS）。
#:
#: 改成「尾端抓數字，其餘用字串處理」：沒有歧義、跑幾次就是幾次。
_PAGE_TAIL_RE = re.compile(r"(\d+)[\s　]*$")

#: 標題與頁碼之間的引導點 —— 使用者多半是從既有目錄複製貼上的。
_LEADER_CHARS = " \t.…·　"


def parse_text_list(text: str) -> tuple[list[BookmarkItem], list[str]]:
    """把「標題 + 頁碼」的文字清單解析成書籤。

    支援兩種寫法（可以混用）：
      * `第一章 緒論    3`      —— 行尾的數字是頁碼
      * `    1.1 背景   5`      —— **開頭的縮排決定層級**（每 2 個空白一層）

    目錄常見的引導點（`……`、`....`）會被吃掉 —— 使用者多半是直接從既有目錄
    複製貼上的。
    """
    items: list[BookmarkItem] = []
    warns: list[str] = []
    for raw in (text or "").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        m = _PAGE_TAIL_RE.search(line)
        if not m:
            warns.append(f"看不出頁碼，略過：{line.strip()[:40]}")
            continue
        head = line[:m.start(1)]
        indent = head[:len(head) - len(head.lstrip("\t 　"))].replace("　", "  ")
        title = head.strip().rstrip(_LEADER_CHARS)
        if not title:
            warns.append(f"只有頁碼沒有標題，略過：{line.strip()[:40]}")
            continue
        items.append(BookmarkItem(title=title,
                                  page=int(m.group(1)),
                                  level=len(indent) // 2 + 1))
    return items, warns


# ----------------------------------------------------------------- 目錄頁 --

@dataclass
class TocPageSpec:
    """目錄頁的樣子。"""
    title: str = "目錄"
    font_size: float = 11.0
    title_size: float = 18.0
    max_level: int = 3            # 只列到第幾層
    dot_leader: bool = True       # 標題與頁碼之間的引導點
    link: bool = True             # 目錄項可以點


def build_toc_page(doc: fitz.Document, items: list[BookmarkItem],
                   spec: Optional[TocPageSpec] = None,
                   at_page: int = 1) -> int:
    """插入目錄頁，回**插入了幾頁**。

    `at_page` 是要插在第幾頁（1 = 最前面）。**很多文件有封面**，目錄要放在
    封面後面，也就是 `at_page=2`。

    頁碼有兩套要一起對：目錄上印的頁碼、以及目錄項目的連結目標。**插入點之前
    的頁不會位移**，所以只有 `page >= at_page` 的項目要加上目錄頁數 ——
    整體平移會讓封面那一筆指到目錄自己身上。
    """
    spec = spec or TocPageSpec()
    listed = [i for i in items if i.level <= spec.max_level]
    if not listed:
        return 0

    at_page = max(1, min(int(at_page or 1), doc.page_count + 1))
    # 版面沿用插入位置那一頁的尺寸，讓目錄跟內文放在一起不突兀
    ref = (doc[min(at_page - 1, doc.page_count - 1)].rect
           if doc.page_count else fitz.Rect(0, 0, 595, 842))
    margin = 56.0
    line_h = spec.font_size * 1.9
    usable = ref.height - margin * 2 - spec.title_size * 2.2
    per_page = max(1, int(usable // line_h))
    n_pages = (len(listed) + per_page - 1) // per_page

    fontname, fontbuf = _toc_font(spec.title + "".join(i.title for i in listed))

    for pi in range(n_pages):
        page = doc.new_page(pno=at_page - 1 + pi,
                            width=ref.width, height=ref.height)
        if fontbuf is not None:
            page.insert_font(fontname=fontname, fontbuffer=fontbuf)
        y = margin + spec.title_size
        if pi == 0:
            page.insert_text((margin, y), spec.title, fontname=fontname,
                             fontsize=spec.title_size)
        y += spec.title_size * 1.2
        for it in listed[pi * per_page:(pi + 1) * per_page]:
            y += line_h
            indent = margin + (it.level - 1) * spec.font_size * 1.6
            # **只有插入點之後的頁才會位移** —— 目錄插在封面後面時，
            # 封面仍然是第 1 頁；一律加上去的話目錄會指到自己身上。
            shown = it.page + (n_pages if it.page >= at_page else 0)
            page_label = str(shown)
            page.insert_text((indent, y), it.title, fontname=fontname,
                             fontsize=spec.font_size)
            pw = _text_width(page_label, fontname, fontbuf, spec.font_size)
            page.insert_text((ref.width - margin - pw, y), page_label,
                             fontname=fontname, fontsize=spec.font_size)
            if spec.dot_leader:
                tw = _text_width(it.title, fontname, fontbuf, spec.font_size)
                x0 = indent + tw + 4
                x1 = ref.width - margin - pw - 4
                if x1 > x0:
                    page.draw_line(fitz.Point(x0, y - spec.font_size * 0.3),
                                   fitz.Point(x1, y - spec.font_size * 0.3),
                                   color=(0.7, 0.7, 0.7), width=0.4,
                                   dashes="[0.5 2.5] 0")
            if spec.link:
                rect = fitz.Rect(indent, y - spec.font_size,
                                 ref.width - margin, y + spec.font_size * 0.3)
                page.insert_link({"kind": fitz.LINK_GOTO, "from": rect,
                                  "page": shown - 1, "to": fitz.Point(0, 0)})
    return n_pages


def _toc_font(sample: str):
    """目錄頁要用的字型。回 `(fontname, fontbuffer|None)`。

    走 `font_catalog` 而不是自己找檔案 —— 那裡會挑對 `.ttc` 的**繁中**子字型
    （第 0 套通常是日文），也會只嵌用到的字（不然整支中文字型十幾 MB 會塞進
    這份 PDF）。
    """
    try:
        from app.core import font_catalog
        best = font_catalog.best_cjk_path("sans", "traditional")
        if best:
            path, idx = best[0], (best[1] if len(best) > 1 else 0)
            _ff, buf = font_catalog.embeddable_font(path, idx, text=sample)
            if buf is not None:
                return "jttoc", buf
            return "jttoc", Path(path).read_bytes()
    except Exception:  # noqa: BLE001
        pass
    return "china-t", None      # 內建繁中，至少不會缺字


def _text_width(text: str, fontname: str, fontbuf, size: float) -> float:
    try:
        f = fitz.Font(fontbuffer=fontbuf) if fontbuf is not None \
            else fitz.Font(fontname)
        return f.text_length(text, fontsize=size)
    except Exception:  # noqa: BLE001
        return len(text) * size * 0.6
