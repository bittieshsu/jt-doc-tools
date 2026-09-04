"""把辦公文件裡「可翻譯的文字」抽出來，翻好之後原位換回去。

用途：文件翻譯 —— 產出**同一種格式**的檔案，內容換成譯文，而**排版不動**。

做法是直接在檔案的 XML 上動手，不重排版面：
  * OOXML（`.docx` / `.xlsx`）是一個 zip，文字在 `<w:t>` / `<t>` 這些節點裡
  * ODF（`.odt` / `.ods`）也是 zip，文字在 `text:p` / `text:span` / `text:h` 裡

**以「段落」為單位翻譯，譯文寫回第一個文字節點、其餘清空。** 為什麼不逐節點
翻：一個句子常被切成好幾個 run（改個字體、標個粗體就會切開），逐節點翻等於
把半句話丟給 LLM，譯出來的東西會走樣。段落的框、表格的格、頁首頁尾都還在
原位，所以版面不會跑掉。**能依行切開的地方會依行切開**（一格裡的紅色斜體
補充自己一行時，那個顏色留得住）；剩下的限制是同**一行**裡的混合格式
（半句粗體）會統一成該行第一個 run 的樣式。

不碰的東西：欄位碼（`w:instrText`）、樣式定義、關聯檔、圖片、數字與純符號。
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Optional
from xml.etree import ElementTree as ET

# ---- 命名空間 ----------------------------------------------------------
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_XL = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_XML = "http://www.w3.org/XML/1998/namespace"

#: 哪些 zip 內的檔案要掃。頁首頁尾也要翻，不然翻完的文件抬頭還是原文。
#: `document` 後面允許數字 —— **主檔不一定叫 `word/document.xml`**：Word 在某些
#: 編輯之後會寫成 `word/document2.xml`，那種檔案照樣是合法的 .docx。寫死名字
#: 的話那份文件會「找不到可以翻譯的文字」，而使用者完全看不出為什麼。
_DOCX_PARTS = re.compile(
    r"^word/(document\d*|header\d*|footer\d*|footnotes|endnotes)\.xml$")
_XLSX_PARTS = re.compile(r"^xl/(sharedStrings\.xml|worksheets/sheet\d+\.xml)$")
#: 簡報：投影片本身與備忘稿。母片 / 版面配置不翻（那是版型的佔位文字）。
_PPTX_PARTS = re.compile(r"^ppt/(slides|notesSlides)/[a-zA-Z]+\d+\.xml$")
#: ODF：內容一定要掃。`styles.xml` 只有**文書檔**要掃（頁首頁尾的真實文字在
#: 那裡）—— 試算表與簡報的 `styles.xml` 只有「Page 1」「<number>」這種頁碼
#: 佔位字，翻了不但沒用，還會把頁碼欄位變成一句譯文。
_ODF_PARTS = re.compile(r"^content\.xml$")
_ODT_PARTS = re.compile(r"^(content|styles)\.xml$")

#: 純數字 / 日期 / 符號 / 網址 —— 丟給 LLM 只會拿回一句廢話，直接跳過。
_SKIP = re.compile(
    r"^\s*(?:"
    r"[\d\s.,:;/\\%+\-()\[\]{}<>=*#~^&|!?'\"$€£¥￥、，。；：（）【】《》「」…—–·]*"
    r"|https?://\S+"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+"
    # `<number>` / `<date>` 這種是欄位佔位字（頁碼、日期），不是內容
    r"|<[a-zA-Z-]{1,20}>"
    r")\s*$"
)

SUPPORTED_EXTS = (".doc", ".docx", ".odt",
                  ".xls", ".xlsx", ".ods",
                  ".ppt", ".pptx", ".odp")
#: 舊的二進位格式沒辦法直接改 XML —— 先轉成新格式、翻完再轉回去。
LEGACY_TO_MODERN = {".doc": ".docx", ".xls": ".xlsx", ".ppt": ".pptx"}


def is_supported(filename: str) -> bool:
    return (filename or "").lower().endswith(SUPPORTED_EXTS)


def should_translate(text: str) -> bool:
    """這段文字值得送去翻嗎？"""
    t = (text or "").strip()
    if len(t) < 1:
        return False
    return not _SKIP.match(t)


@dataclass
class TextUnit:
    """一段可翻譯的文字，以及它在檔案裡的位置。"""
    part: str                     # zip 裡的哪一個檔案
    index: int                    # 該檔案裡的第幾段
    text: str                     # 原文（同一段所有文字節點接起來）
    nodes: list = field(repr=False, default_factory=list)   # 對應的 XML 節點
    owners: dict = field(repr=False, default_factory=dict)  # 文字節點 → 所屬 run


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _iter_paragraph_nodes(root: ET.Element, kind: str):
    """回傳 [(段落元素, [文字節點, ...])]。"""
    out: list[tuple[ET.Element, list[ET.Element], dict]] = []
    if kind == "docx":
        for para in root.iter(f"{{{_W}}}p"):
            nodes = [n for n in para.iter(f"{{{_W}}}t")]
            if nodes:
                # 記下每個文字節點屬於哪個 run —— 寫進中文時要在 run 上指定
                # 東亞字型，否則 Word 會退到文件預設的東亞字型（實測是日文的
                # MS Mincho）：字形是日文的、行高也跟著變大，看起來就是
                # 「行距怎麼變了」。
                owner = {}
                for run in para.iter(f"{{{_W}}}r"):
                    for tn in run.iter(f"{{{_W}}}t"):
                        owner[id(tn)] = run
                out.append((para, nodes, owner))
    elif kind == "xlsx":
        # sharedStrings：一個 <si> 是一格的內容（可能被切成多個 <r><t>）
        for si in root.iter(f"{{{_XL}}}si"):
            nodes = [n for n in si.iter(f"{{{_XL}}}t")]
            if nodes:
                out.append((si, nodes, {}))
        # 直接寫在工作表裡的 inline string
        for isx in root.iter(f"{{{_XL}}}is"):
            nodes = [n for n in isx.iter(f"{{{_XL}}}t")]
            if nodes:
                out.append((isx, nodes, {}))
    elif kind == "pptx":
        # 簡報的文字在 DrawingML 裡：一個 <a:p> 是一段，切成多個 <a:r><a:t>
        for para in root.iter(f"{{{_A}}}p"):
            nodes = [n for n in para.iter(f"{{{_A}}}t")]
            if nodes:
                out.append((para, nodes, {}))
    else:  # odf
        for tag in ("p", "h"):
            for para in root.iter(f"{{{_TEXT}}}{tag}"):
                nodes = _odf_text_nodes(para)
                if nodes:
                    out.append((para, nodes, {}))
    return out


def _odf_text_nodes(para: ET.Element) -> list[ET.Element]:
    """ODF 的文字散在元素的 text/tail 上，這裡回傳「帶文字的元素」。

    直接回元素、稍後用 `.text` 存取 —— ODF 沒有 `<w:t>` 那種單純的文字節點。
    """
    nodes = []
    if (para.text or "").strip():
        nodes.append(para)
    for el in para.iter():
        if el is para:
            continue
        if _localname(el.tag) in ("s", "tab", "line-break"):
            continue
        if (el.text or "").strip():
            nodes.append(el)
    return nodes


def _node_text(node: ET.Element, kind: str) -> str:
    return node.text or ""


def _set_node_text(node: ET.Element, kind: str, value: str) -> None:
    node.text = value
    if kind in ("docx", "xlsx") and value != value.strip():
        # Word / Excel 預設會吃掉前後空白，要明講保留。**換行也算**：依行分組
        # 之後，組與組之間的換行是寫在後一組的**開頭**，沒有這個屬性那個換行
        # 就可能被吃掉 —— 兩行黏成一行，而且不會有任何錯誤。
        node.set(f"{{{_XML}}}space", "preserve")


def _kind_for(part: str, ext: str) -> str:
    if ext == ".docx":
        return "docx"
    if ext == ".xlsx":
        return "xlsx"
    if ext == ".pptx":
        return "pptx"
    return "odf"


def _parts_for(ext: str) -> re.Pattern:
    return {".docx": _DOCX_PARTS, ".xlsx": _XLSX_PARTS,
            ".pptx": _PPTX_PARTS, ".odt": _ODT_PARTS}.get(ext, _ODF_PARTS)



#: 目標語言 → 該用哪個東亞字型。**不設的話 Word 會退到文件預設的東亞字型**，
#: 而多數英文範本的預設是日文的 MS Mincho：字形是日文的（過 / 直 / 骨 寫法不同）、
#: 行高也比中文字型大，看起來就是「翻完行距變了」。
#: 挑的都是各平台常見、且該語言的正確字型。
_EAST_ASIAN_FONT = {
    "zh-TW": "Microsoft JhengHei", "zh": "Microsoft JhengHei",
    "zh-CN": "Microsoft YaHei",
    "ja": "Yu Gothic",
    "ko": "Malgun Gothic",
}
_CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def east_asian_font_for(target_lang: str) -> str:
    return _EAST_ASIAN_FONT.get(target_lang or "", "")


def _set_run_east_asian_font(run: ET.Element, font: str) -> None:
    """在 run 上指定東亞字型（`w:rPr/w:rFonts/@w:eastAsia`）。

    只補**東亞**那一格，不動 ascii / hAnsi —— 段落裡原本的英文、數字要維持
    原本的字型，那也是版面的一部分。
    """
    if not font:
        return
    rpr = run.find(f"{{{_W}}}rPr")
    if rpr is None:
        rpr = ET.Element(f"{{{_W}}}rPr")
        run.insert(0, rpr)      # rPr 一定要是 run 的第一個子元素
    fonts = rpr.find(f"{{{_W}}}rFonts")
    if fonts is None:
        fonts = ET.SubElement(rpr, f"{{{_W}}}rFonts")
        rpr.remove(fonts)
        rpr.insert(0, fonts)    # rFonts 要排在 rPr 的最前面
    fonts.set(f"{{{_W}}}eastAsia", font)


def _line_groups(nodes: list[ET.Element], kind: str
                 ) -> list[tuple[int, list[ET.Element]]]:
    """把一段文字依「行」切成幾組，回傳 [(這組有幾行, 節點清單), ...]。

    **為什麼要分組**：譯文若全部寫進第一個節點，整段就統一成第一個 run 的樣式
    —— 試算表常見的「說明文字 + 換行 + 紅色斜體的補充」翻完之後那行補充
    **會變成黑色正體**（使用者實測回報：「有些字顏色翻譯後失去了」）。格式沒有
    錯誤訊息、版面也沒跑掉，只是那個「這是新增要求」的紅字提示不見了。

    切點只落在**沒有任何節點跨過去**的換行處：那裡切開，每一行的譯文就能寫回
    它自己的 run，行層級的顏色 / 斜體 / 粗體都留得住。跨行的節點（一個 run 裡
    就含換行）併成同一組 —— 那種情形本來就無法分辨格式屬於哪一行。
    """
    texts = [_node_text(n, kind) for n in nodes]
    full = "".join(texts)
    n_lines = full.count("\n") + 1
    if n_lines == 1 or len(nodes) == 1:
        return [(n_lines, list(nodes))]

    # 每個換行字元的位置（第 i 個換行結束第 i 行）
    nl_pos = [i for i, ch in enumerate(full) if ch == "\n"]
    # 這個換行處可不可以切：擁有該換行字元的那個節點，不能同時含有它前後的字。
    # 兩種寫法都要認 —— Excel 多半把換行放在 run 的**結尾**（`<t>說明&#10;</t>`），
    # 別的產生器則會放在下一個 run 的**開頭**。只認一種的話，另一種檔案就整段
    # 併成一組、格式照樣被吃掉（而且完全看不出來）。
    splittable = [False] * len(nl_pos)
    starts_new = [False] * len(nl_pos)   # 該換行屬於「下一組」的第一個字
    starts: list[int] = []
    pos = 0
    for t in texts:
        starts.append(pos)
        pos += len(t)
    for bi, p_nl in enumerate(nl_pos):
        for ni, st in enumerate(starts):
            en = st + len(texts[ni])
            if st <= p_nl < en:
                if en == p_nl + 1:
                    splittable[bi] = True
                elif st == p_nl:
                    splittable[bi] = True
                    starts_new[bi] = True
                break

    # 依可切的換行處把「行」分組
    line_group: list[int] = []
    g = 0
    for i in range(n_lines):
        line_group.append(g)
        if i < len(splittable) and splittable[i]:
            g += 1
    n_groups = g + 1

    # 節點依「起點落在哪一行」歸到那一行的組
    buckets: list[list[ET.Element]] = [[] for _ in range(n_groups)]
    for ni, node in enumerate(nodes):
        st = starts[ni]
        # 節點以換行開頭而且那個換行是切點時，這個節點屬於**後面**那一組
        line = sum(1 for bi, p_nl in enumerate(nl_pos)
                   if p_nl < st or (p_nl == st and starts_new[bi]))
        buckets[line_group[min(line, n_lines - 1)]].append(node)

    counts = [0] * n_groups
    for gi in line_group:
        counts[gi] += 1

    # **分不到節點的組要併進隔壁，不可以留著也不可以整段放棄。**
    # 真實檔案很常見這種收尾：最後一個 run 只裝一個換行（`<r><rPr 紅色/><t>\n</t></r>`）
    # —— 那個換行會切出一個「空白行」的組，而它前後的節點都歸給別組，於是這一組
    # 一個節點都沒有。早期版本遇到這種就整段退回「全部寫進第一個節點」，結果就是
    # **那份客戶檔案的紅字還是變黑的**（使用者實測回報「有字顏色沒出來啊」）——
    # 修法明明在，卻被這條保險擋掉了。併進前一組就同時保住文字與其他組的格式。
    groups = [[counts[i], buckets[i]] for i in range(n_groups)]
    merged: list[list] = []
    for k, gnodes in groups:
        if not gnodes and merged:
            merged[-1][0] += k          # 併進前一組（那幾行接在前一組後面）
        elif not gnodes and not merged:
            groups_rest = None          # 第一組就是空的 → 留著，等下一組來收
            merged.append([k, gnodes])
        else:
            if merged and not merged[-1][1]:
                # 前面那組（開頭的空組）沒有節點 → 讓這一組一起收走
                k += merged.pop()[0]
            merged.append([k, gnodes])
    return [(k, g) for k, g in merged]


def extract_units(data: bytes, ext: str) -> tuple[list[TextUnit], dict]:
    """抽出所有可翻譯的段落。

    回傳 (units, state)；`state` 要原封不動交給 `rebuild()`。
    """
    ext = ext.lower()
    part_re = _parts_for(ext)
    units: list[TextUnit] = []
    trees: dict[str, ET.ElementTree] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        raw = {n: zf.read(n) for n in names}
    for name in names:
        if not part_re.match(name):
            continue
        try:
            root = ET.fromstring(raw[name])
        except ET.ParseError:
            continue
        kind = _kind_for(name, ext)
        trees[name] = ET.ElementTree(root)
        for i, (_para, nodes, owner) in enumerate(
                _iter_paragraph_nodes(root, kind)):
            text = "".join(_node_text(n, kind) for n in nodes)
            if should_translate(text):
                units.append(TextUnit(part=name, index=i, text=text,
                                      nodes=nodes, owners=owner))
    return units, {"raw": raw, "names": names, "trees": trees, "ext": ext}


def rebuild(state: dict, translations: dict[int, str], units: list[TextUnit],
            target_lang: str = "") -> bytes:
    """把譯文寫回原檔，回傳新的檔案內容。

    `translations` 是 {units 的索引: 譯文}；沒有譯文的段落保持原文。
    給了 `target_lang` 時，寫進東亞文字的 run 會一併指定對應的東亞字型
    （見 `_EAST_ASIAN_FONT`）。
    """
    ext = state["ext"]
    ea_font = east_asian_font_for(target_lang)
    for idx, unit in enumerate(units):
        new = translations.get(idx)
        if new is None or new == unit.text:
            continue
        kind = _kind_for(unit.part, ext)
        groups = _line_groups(unit.nodes, kind)
        lines = new.split("\n")
        if sum(k for k, _ in groups) != len(lines) or any(not g for _, g in groups):
            # 行數對不上（模型自己在譯文裡塞了換行）→ 退回「全部寫進第一個節點」。
            # **不可以硬湊** —— 錯開一行就把 A 行的譯文套上 B 行的格式。
            # （分不到節點的組已經在 `_line_groups` 併進隔壁了，這裡是最後一道保險。）
            groups = [(len(lines), list(unit.nodes))]
        at = 0
        for k, gnodes in groups:
            chunk = "\n".join(lines[at:at + k])
            at += k
            # 組與組之間的換行由「後一組」帶著 —— 前一組的節點會被清空，
            # 分隔的換行字元本來就在那裡面
            if at - k > 0:
                chunk = "\n" + chunk
            _set_node_text(gnodes[0], kind, chunk)
            if kind == "docx" and ea_font and _CJK_RE.search(chunk):
                run = unit.owners.get(id(gnodes[0]))
                if run is not None:
                    _set_run_east_asian_font(run, ea_font)
            for n in gnodes[1:]:
                _set_node_text(n, kind, "")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in state["names"]:
            tree = state["trees"].get(name)
            if tree is not None:
                buf = io.BytesIO()
                tree.write(buf, encoding="UTF-8", xml_declaration=True)
                payload = buf.getvalue()
            else:
                payload = state["raw"][name]
            # mimetype 必須是第一個且不壓縮，否則 ODF 讀不進去
            if name == "mimetype":
                zf.writestr(name, payload, zipfile.ZIP_STORED)
            else:
                zf.writestr(name, payload)
    return out.getvalue()


def register_namespaces() -> None:
    """保留原本的命名空間前綴，免得寫回去變成 ns0: 這種。"""
    ET.register_namespace("w", _W)
    ET.register_namespace("", _XL)
    ET.register_namespace("text", _TEXT)
    ET.register_namespace("a", _A)


register_namespaces()
