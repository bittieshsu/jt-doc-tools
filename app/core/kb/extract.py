"""把知識庫的原檔變成「一行一行、知道在第幾頁」的文字。

## 收哪些格式

PDF、Word（.docx）、ODF 文字文件（.odt）、純文字（.txt）、Markdown（.md）。
**全部在本行程裡讀，不需要 Office 引擎** —— 知識庫是管理員一次上傳幾十份手冊
的地方，每份都起一次 soffice 的話又慢又佔記憶體；而 .docx / .odt 的段落就在
XML 裡，直接讀比轉成純文字再猜段落準。舊格式（.doc / .rtf）請先另存新格式。

## PDF：為什麼要保留「行」與「頁碼」

切段要靠行首的項次（`壹、`、`一、`、`（一）`、`第 7 條`）判斷層級，
`app/tools/translate_doc` 那支抽字會把行接起來（它要的是句子），這裡不能用。
頁碼是引用定位（「文書處理手冊第 26 頁」），抽字時就要帶著。

抽字本身沿用全站的兩道：`glyph_text.page_text_repaired`（對照表壞掉時從
字形反查）與 `bad_cmap`（OCR 過的 PDF 兩層文字裡那一層亂碼）。

## 每一頁的頁首頁尾

公文手冊的 PDF 每一頁上方都印著書名（「文書處理手冊」）或章名、頁碼。
不拿掉的話：①每一段都多一句雜訊；②**章名當頁首**（「壹、總述」）會被當成
一個新的章節開頭，整個層級就亂了。判準見 `_strip_running_lines`。
"""
from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import Optional

from ..bad_cmap import clean_pdf_text, is_bad_cmap_text
from .. import zip_guard

#: 單一文件抽出來的字數上限（超過就拒絕，不安靜截斷 —— 截斷的話後半本手冊
#: 永遠查不到，而畫面看起來完全正常）。三百萬字約是一千頁的密排手冊。
MAX_CHARS = 3_000_000


class ExtractError(ValueError):
    """抽不出文字的原因（訊息給管理員看）。"""


@dataclass
class Line:
    text: str
    page: Optional[int] = None          # PDF 的頁碼（1 起算）；其他格式 None
    para_start: bool = False            # 這一行是不是新段落的開頭（空行 / 新區塊之後）
    style_rank: Optional[int] = None    # 文書處理器的「標題 N」樣式 / Markdown 的 #


@dataclass
class Extracted:
    lines: list[Line] = field(default_factory=list)
    page_count: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(ln.text) for ln in self.lines)


# ---------------------------------------------------------------- 入口
def extract(data: bytes, ext: str) -> Extracted:
    ext = (ext or "").lower()
    if ext == ".pdf":
        out = _pdf(data)
    elif ext == ".docx":
        out = _docx(data)
    elif ext == ".odt":
        out = _odt(data)
    elif ext in (".txt", ".md"):
        out = _plain(data, markdown=(ext == ".md"))
    else:
        raise ExtractError("不支援的檔案格式。")
    if out.chars > MAX_CHARS:
        raise ExtractError("這份文件的文字量超過上限（三百萬字），請拆成幾份再上傳。")
    if not any(ln.text.strip() for ln in out.lines):
        raise ExtractError("這份文件抽不出文字（可能是掃描的圖片檔，請先用「OCR 文字辨識」轉成可搜尋的 PDF）。")
    return out


# ---------------------------------------------------------------- 格式檢查
def sniff_ok(data: bytes, ext: str) -> bool:
    """內容真的是那個格式嗎（**看內容不看副檔名**）。"""
    ext = (ext or "").lower()
    if ext == ".pdf":
        return data[:1024].lstrip().startswith(b"%PDF")
    if ext in (".docx", ".odt"):
        if not data.startswith(b"PK\x03\x04"):
            return False
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                # 判斷格式也會讀成員（`mimetype`）—— 先過 zip 炸彈防護，過不了就當格式不對
                zip_guard.check(zf)
                names = set(zf.namelist())
                if ext == ".docx":
                    return "word/document.xml" in names
                if "content.xml" not in names or "mimetype" not in names:
                    return False
                return zf.read("mimetype").strip() == b"application/vnd.oasis.opendocument.text"
        except (zipfile.BadZipFile, zip_guard.ZipBombError):
            return False
    if ext in (".txt", ".md"):
        return _decode(data) is not None
    return False


# ---------------------------------------------------------------- PDF
#: 頁碼行：「12」「- 12 -」「第 12 頁」「12 / 160」。
_PAGE_NO_RE = re.compile(r"^[-－—–\s]*(?:第\s*)?\d{1,4}\s*(?:頁|/\s*\d{1,4})?[-－—–\s]*$")
#: 目錄行：「壹、總述……………2」「附件10、…… … 60」「九、………125」。
#: 刪節號「…」一個字就是三個點，兩個就算引導線；英文句點要三個以上。
_TOC_RE = re.compile(r"(?:(?:[…⋯]\s*){2,}|(?:[.．·‧･]\s*){3,})\d{1,4}\s*$")
#: 句子結束的標點：PDF 的下一行從這裡開始才算新段落，其餘都是同一句的換行。
_SENT_END_RE = re.compile(r"[。！？!?」』）)]$")


def _pdf(data: bytes) -> Extracted:
    try:
        import fitz
    except ImportError as e:  # pragma: no cover
        raise ExtractError("伺服器沒有 PDF 剖析元件（PyMuPDF）。") from e
    from .. import glyph_text

    pages: list[list[Line]] = []
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        raise ExtractError("這份 PDF 打不開（可能毀損或加密）。") from e
    with doc:
        if doc.needs_pass:
            raise ExtractError("這份 PDF 有開啟密碼，請先移除密碼再上傳。")
        for pno, page in enumerate(doc, start=1):
            plines: list[Line] = []
            fixed = glyph_text.page_text_repaired(page, doc=doc)
            if fixed is not None:
                blocks = [fixed]
            else:
                # sort=True：照版面由上而下、由左而右，不照內容串流的順序 ——
                # 頁首判斷看的是「每頁最上面兩行」，順序亂了會判錯。
                blocks = [b[4] for b in (page.get_text("blocks", sort=True) or [])
                          if len(b) > 6 and b[6] == 0]
            for blk in blocks:
                for raw in (blk or "").split("\n"):
                    if raw and is_bad_cmap_text(raw):
                        continue
                    t = clean_pdf_text(raw).strip()
                    if t:
                        plines.append(Line(t, page=pno))
            # 先把直排的「第 / 7 / 條」接回來，再判斷頁首 —— 換頁後的第一行若是單獨
            # 一個「第」，會在好幾頁的頁首重複出現，被當成頁首拿掉之後條號就斷了。
            pages.append(_merge_split_markers(plines))
        page_count = doc.page_count
    _strip_running_lines(pages)
    lines = [ln for pl in pages for ln in pl if not _TOC_RE.search(ln.text)]
    # 段落邊界：PDF 的「區塊」靠不住（這批手冊一行就是一個區塊），
    # 改用「上一行是不是一句話的結尾」判斷。有項次的行不靠這個 —— 切段那邊看行首。
    prev = ""
    for ln in lines:
        ln.para_start = bool(_SENT_END_RE.search(prev)) or not prev
        prev = ln.text
    return Extracted(lines=lines, page_count=page_count)


def _sig(text: str) -> str:
    """頁首頁尾比對用的簽名：正規化、拿掉空白、數字一律當成 #。"""
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"\s+", "", t)
    return re.sub(r"\d+", "#", t)


#: 每頁上下各看幾行。
_EDGE = 2
#: 長得像章名或法規名稱的行（雙頁印章名、法規名的頁首只會是這一類）。
_CHAPTER_LIKE_RE = re.compile(
    r"^(?:[壹貳參肆伍陸柒捌玖拾]+\s*、|第\s*\S{1,6}\s*[章編節]|附\s*[件錄])"
    r"|^[^\s，。：；、]{2,28}(?:法|條例|辦法|細則|規則|要點|規範|準則|須知|規程|通則)$")


def _strip_running_lines(pages: list[list[Line]]) -> None:
    """拿掉每一頁的頁首、頁尾、頁碼（就地修改）。

    三條判準，任何一條成立就拿掉（**只看每頁最上面與最下面兩行**）：

    1. 頁碼行（`_PAGE_NO_RE`）。
    2. 同一個簽名（數字當萬用字元）在兩頁以上的頁首 / 頁尾出現 ——
       書名、機關名這種逐頁重複的字。**門檻是 2 不是比例**：附錄只有五六頁、
       每頁都印同一行，用比例的話一百多頁的手冊永遠到不了。
       **長得像章名的行不適用這條**：一章的第一頁，真正的章標題本來就落在頁首，
       後面幾頁又印成頁首 —— 用次數判斷的話，連真正的那一個都一起拿掉
       （第一版就是這樣，整本手冊的「壹、總述」不見了，第一章的內容全掛到
       前面的附件底下）。
    3. 頁首那一行**長得像章名**（`壹、`、`第三章`、`附件`），而且跟前面已經出現過的
       一行一模一樣 —— 雙頁印章名的頁首（第 8 頁上方的「壹、總述」）。它只在那一章
       的偶數頁出現，次數不夠多，但它前面一定以真正的章標題出現過一次。不拿掉的話
       會被當成新的一章。**只限長得像章名的行**：「說明：」這種短句也常常重複出現，
       剛好落在頁首時不可以被當成頁首拿掉。
    """
    from collections import Counter

    def _edges(pl: list[Line]) -> tuple[set[int], set[int]]:
        """(頁碼行, 頁首頁尾候選)。頁碼行**不佔**上下各兩行的名額 ——
        頁首常常是「頁碼 ＋ 書名 ＋ 章名」三行，頁碼佔掉一個名額的話章名就看不到了。"""
        nums: set[int] = set()
        cand: set[int] = set()
        for order in (range(len(pl)), range(len(pl) - 1, -1, -1)):
            took = 0
            for i in order:
                if _PAGE_NO_RE.match(pl[i].text):
                    nums.add(i)
                    continue
                cand.add(i)
                took += 1
                if took >= _EDGE:
                    break
        return nums, cand - nums

    edges = [_edges(pl) for pl in pages]
    edge_count: Counter = Counter()
    for pl, (_nums, cand) in zip(pages, edges):
        edge_count.update({_sig(pl[i].text) for i in cand})
    seen: set[str] = set()
    for pl, (nums, cand) in zip(pages, edges):
        top = set(sorted(cand)[:_EDGE])
        drop: set[int] = set(nums)
        for i in cand:
            ln = pl[i]
            s = _sig(ln.text)
            chapter_like = bool(_CHAPTER_LIKE_RE.match(
                unicodedata.normalize("NFKC", ln.text.strip())))
            if ((edge_count[s] >= 2 and len(s) <= 40 and not chapter_like)
                    or (i in top and chapter_like and ln.text.strip() in seen
                        and len(ln.text.strip()) <= 30)):
                drop.add(i)
        for i, ln in enumerate(pl):
            if i not in drop:
                seen.add(ln.text.strip())
        if drop:
            pl[:] = [ln for i, ln in enumerate(pl) if i not in drop]


_SPLIT_HEAD_RE = re.compile(r"^[0-9０-９一二三四五六七八九十百千零〇\s]{1,6}$")
_SPLIT_TAIL_RE = re.compile(r"^(條|點|章|節|編|款)")


def _merge_split_markers(lines: list[Line]) -> list[Line]:
    """法規 PDF 常把「第 7 條」直排成三行（`第` / `7` / `條`）—— 接回一行。"""
    out: list[Line] = []
    i = 0
    while i < len(lines):
        t = lines[i].text.strip()
        if (t == "第" and i + 2 < len(lines)
                and _SPLIT_HEAD_RE.match(lines[i + 1].text.strip())
                and _SPLIT_TAIL_RE.match(lines[i + 2].text.strip())):
            merged = "第" + re.sub(r"\s+", "", lines[i + 1].text) + lines[i + 2].text.strip()
            out.append(Line(merged, page=lines[i].page, para_start=True))
            i += 3
            continue
        out.append(lines[i])
        i += 1
    return out


# ---------------------------------------------------------------- DOCX / ODT
def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ExtractError("檔案打不開（不是有效的 Office 文件，或已毀損）。") from e
    try:
        zip_guard.check(zf)
    except zip_guard.ZipBombError as e:
        zf.close()
        raise ExtractError(str(e)) from e
    return zf


def _parse_xml(blob: bytes):
    from defusedxml import ElementTree as DET
    try:
        return DET.fromstring(blob)
    except Exception as e:
        raise ExtractError("文件內容的格式不對（XML 解析失敗）。") from e


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_HEADING_STYLE_RE = re.compile(r"(?i)^(?:heading|標題|title)\s*(\d)$|^(\d)$")


def _docx(data: bytes) -> Extracted:
    with _open_zip(data) as zf:
        try:
            root = _parse_xml(zf.read("word/document.xml"))
        except KeyError as e:
            raise ExtractError("這不是 Word 文件（找不到文件本體）。") from e
    body = root.find(_W + "body")
    lines: list[Line] = []
    if body is None:
        return Extracted(lines=lines)
    for el in list(body):
        if el.tag == _W + "p":
            t = _docx_para_text(el)
            if t.strip():
                lines.append(Line(t.strip(), para_start=True, style_rank=_docx_heading(el)))
        elif el.tag == _W + "tbl":
            for tr in el.iter(_W + "tr"):
                cells = []
                for tc in tr.findall(_W + "tc"):
                    ct = " ".join(_docx_para_text(p).strip() for p in tc.iter(_W + "p"))
                    cells.append(ct.strip())
                row = " ｜ ".join(c for c in cells if c)
                if row:
                    lines.append(Line(row, para_start=True))
    return Extracted(lines=lines)


def _docx_para_text(p) -> str:
    out = []
    for el in p.iter():
        if el.tag == _W + "t" and el.text:
            out.append(el.text)
        elif el.tag == _W + "tab":
            out.append(" ")
        elif el.tag in (_W + "br", _W + "cr"):
            out.append("\n")
    return "".join(out)


def _docx_heading(p) -> Optional[int]:
    ppr = p.find(_W + "pPr")
    if ppr is None:
        return None
    ps = ppr.find(_W + "pStyle")
    if ps is not None:
        m = _HEADING_STYLE_RE.match(ps.get(_W + "val") or "")
        if m:
            n = int(m.group(1) or m.group(2))
            if 1 <= n <= 6:
                return n
    ol = ppr.find(_W + "outlineLvl")
    if ol is not None:
        try:
            n = int(ol.get(_W + "val") or "") + 1
        except ValueError:
            return None
        if 1 <= n <= 6:
            return n
    return None


_O_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_O_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_O_OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"


def _odt(data: bytes) -> Extracted:
    with _open_zip(data) as zf:
        try:
            root = _parse_xml(zf.read("content.xml"))
        except KeyError as e:
            raise ExtractError("這不是 ODF 文字文件（找不到 content.xml）。") from e
    body = root.find(_O_OFFICE + "body")
    text_root = body.find(_O_OFFICE + "text") if body is not None else None
    lines: list[Line] = []
    if text_root is not None:
        _odt_walk(text_root, lines)
    return Extracted(lines=lines)


def _odt_walk(el, lines: list[Line]) -> None:
    for ch in list(el):
        tag = ch.tag
        if tag == _O_TEXT + "h":
            t = _odt_text(ch).strip()
            if t:
                try:
                    lvl = int(ch.get(_O_TEXT + "outline-level") or "1")
                except ValueError:
                    lvl = 1
                lines.append(Line(t, para_start=True, style_rank=max(1, min(lvl, 6))))
        elif tag == _O_TEXT + "p":
            t = _odt_text(ch).strip()
            if t:
                lines.append(Line(t, para_start=True))
        elif tag == _O_TABLE + "table":
            for row in ch.iter(_O_TABLE + "table-row"):
                cells = []
                for cell in row.findall(_O_TABLE + "table-cell"):
                    cells.append(" ".join(_odt_text(p).strip()
                                          for p in cell.iter(_O_TEXT + "p")).strip())
                r = " ｜ ".join(c for c in cells if c)
                if r:
                    lines.append(Line(r, para_start=True))
        elif tag in (_O_TEXT + "list", _O_TEXT + "list-item", _O_TEXT + "section",
                     _O_TEXT + "list-header"):
            _odt_walk(ch, lines)


def _odt_text(el) -> str:
    out = [el.text or ""]
    for ch in list(el):
        tag = ch.tag
        if tag == _O_TEXT + "s":
            try:
                out.append(" " * max(1, int(ch.get(_O_TEXT + "c") or "1")))
            except ValueError:
                out.append(" ")
        elif tag == _O_TEXT + "tab":
            out.append(" ")
        elif tag == _O_TEXT + "line-break":
            out.append("\n")
        elif tag in (_O_TEXT + "note", ):
            pass                      # 註腳本文不併進正文
        else:
            out.append(_odt_text(ch))
        out.append(ch.tail or "")
    return "".join(out)


# ---------------------------------------------------------------- 純文字 / Markdown
def _decode(data: bytes) -> Optional[str]:
    for enc in ("utf-8-sig", "big5", "cp950"):
        try:
            s = data.decode(enc)
        except UnicodeDecodeError:
            continue
        # 控制字元（換行、tab 以外）一多就不是文字檔
        bad = sum(1 for ch in s[:20000] if ord(ch) < 32 and ch not in "\r\n\t\f")
        if bad <= 2:
            return s
    return None


_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def _plain(data: bytes, *, markdown: bool) -> Extracted:
    s = _decode(data)
    if s is None:
        raise ExtractError("讀不出這份文字檔（不是 UTF-8 或 Big5 編碼的純文字）。")
    lines: list[Line] = []
    para = True
    in_code = False
    for raw in s.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        t = raw.strip()
        if markdown and t.startswith("```"):
            in_code = not in_code
            para = True
            continue
        if not t:
            para = True
            continue
        rank = None
        if markdown and not in_code:
            m = _MD_HEADING_RE.match(t)
            if m:
                rank, t = len(m.group(1)), m.group(2).strip()
                para = True
        lines.append(Line(t, para_start=para, style_rank=rank))
        para = False
    return Extracted(lines=lines)
