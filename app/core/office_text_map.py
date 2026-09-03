"""把辦公文件裡「可翻譯的文字」抽出來，翻好之後原位換回去。

用途：文件翻譯 —— 產出**同一種格式**的檔案，內容換成譯文，而**排版不動**。

做法是直接在檔案的 XML 上動手，不重排版面：
  * OOXML（`.docx` / `.xlsx`）是一個 zip，文字在 `<w:t>` / `<t>` 這些節點裡
  * ODF（`.odt` / `.ods`）也是 zip，文字在 `text:p` / `text:span` / `text:h` 裡

**以「段落」為單位翻譯，譯文寫回第一個文字節點、其餘清空。** 為什麼不逐節點
翻：一個句子常被切成好幾個 run（改個字體、標個粗體就會切開），逐節點翻等於
把半句話丟給 LLM，譯出來的東西會走樣。段落的框、表格的格、頁首頁尾都還在
原位，所以版面不會跑掉；代價是段落**內部**的混合格式（同一段裡一半粗體）
會統一成第一個 run 的樣式。

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
_DOCX_PARTS = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")
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


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _iter_paragraph_nodes(root: ET.Element, kind: str):
    """回傳 [(段落元素, [文字節點, ...])]。"""
    out: list[tuple[ET.Element, list[ET.Element]]] = []
    if kind == "docx":
        for para in root.iter(f"{{{_W}}}p"):
            nodes = [n for n in para.iter(f"{{{_W}}}t")]
            if nodes:
                out.append((para, nodes))
    elif kind == "xlsx":
        # sharedStrings：一個 <si> 是一格的內容（可能被切成多個 <r><t>）
        for si in root.iter(f"{{{_XL}}}si"):
            nodes = [n for n in si.iter(f"{{{_XL}}}t")]
            if nodes:
                out.append((si, nodes))
        # 直接寫在工作表裡的 inline string
        for isx in root.iter(f"{{{_XL}}}is"):
            nodes = [n for n in isx.iter(f"{{{_XL}}}t")]
            if nodes:
                out.append((isx, nodes))
    elif kind == "pptx":
        # 簡報的文字在 DrawingML 裡：一個 <a:p> 是一段，切成多個 <a:r><a:t>
        for para in root.iter(f"{{{_A}}}p"):
            nodes = [n for n in para.iter(f"{{{_A}}}t")]
            if nodes:
                out.append((para, nodes))
    else:  # odf
        for tag in ("p", "h"):
            for para in root.iter(f"{{{_TEXT}}}{tag}"):
                nodes = _odf_text_nodes(para)
                if nodes:
                    out.append((para, nodes))
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
    if kind == "docx" and value != value.strip():
        # Word 預設會吃掉前後空白，要明講保留
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
        for i, (_para, nodes) in enumerate(_iter_paragraph_nodes(root, kind)):
            text = "".join(_node_text(n, kind) for n in nodes)
            if should_translate(text):
                units.append(TextUnit(part=name, index=i, text=text, nodes=nodes))
    return units, {"raw": raw, "names": names, "trees": trees, "ext": ext}


def rebuild(state: dict, translations: dict[int, str], units: list[TextUnit]) -> bytes:
    """把譯文寫回原檔，回傳新的檔案內容。

    `translations` 是 {units 的索引: 譯文}；沒有譯文的段落保持原文。
    """
    ext = state["ext"]
    for idx, unit in enumerate(units):
        new = translations.get(idx)
        if new is None or new == unit.text:
            continue
        kind = _kind_for(unit.part, ext)
        # 譯文全部寫進第一個節點，其餘清空 —— 段落的框 / 格 / 頁首頁尾都還在，
        # 所以版面不動；段落內部的混合格式會統一成第一個 run 的樣式。
        _set_node_text(unit.nodes[0], kind, new)
        for n in unit.nodes[1:]:
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
