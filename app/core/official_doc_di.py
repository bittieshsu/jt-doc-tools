"""公文撰擬的草稿 → 政府電子公文的 DI 檔（文書本文檔，XML）。

## 為什麼是 DI 不是接公文系統

各機關的公文系統（筆硯公文製作、各家廠商的線上簽核系統）都收「匯入 DI 檔」—— 認得的欄位填進
對應欄位，認不得的放本文。所以公文撰擬**不接任何公文系統的 API**：產出標準格式，承辦人匯入自己的
系統，照常取號、會辦、決行、發文（2026-10-09 使用者：「規劃一下匯出 DI 檔功能」）。

## 格式

檔案管理局〈文書及檔案管理電腦化作業規範〉附錄 5，現行 104 版 DTD（109 年 12 月修正）：
函 `104_2_utf8.dtd`、簽 `104_5_utf8.dtd`，一律 UTF-8。DTD 跟著程式放在 `di_dtd/`（行政規則的附錄，
依著作權法第 9 條不是著作權的標的；出處見那個資料夾的說明），**匯出前與預覽時都拿它驗一次**。

* **元素順序固定**（DTD 的內容模型），順序錯了交換時會被退件 —— 由這支依順序組，不靠模板。
* **來源是畫面上目前的文字**（`official_doc.parse_text`，跟 ODT / PDF 匯出同一套），使用者改過的字照樣進來。
* **機關代碼**：先用案件存著的「名稱 → 代碼」（使用者從地址簿挑的），沒有才照名稱查地址簿
  （**名稱完全相同而且只有一筆**才填）；都沒有就留空並講出來。**填錯代碼比空著糟**。
* 發文日期、發文字號、文號留空就留空 —— 公文系統取號時給；有填就照填，**不拆、不猜**
  （發文字號整串放進 `<文字>`，DTD 允許這一種）。
* **不丟字**：認不出欄位的行併進前一段的文字，並在注意事項講出幾行。〔待補〕〔待確認〕原樣留著
  （匯入後在公文系統裡看得到要補什麼）。
* 簽辦意見不是一種文別（寫在公文系統的簽辦欄裡），不出 DI。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Optional

from . import official_doc as od

DTD_DIR = Path(__file__).resolve().parent / "di_dtd"
DTD_FILES = {"letter": "104_2_utf8.dtd", "sign": "104_5_utf8.dtd"}
ROOTS = {"letter": "函", "sign": "簽"}
MEDIA_TYPE = "application/xml"

#: 注意事項：樣板＋參數（前端 `tr(template)` 再填參數；組好的中文句子英日介面翻不動）
MESSAGES = {
    "codes_missing": "這些機關沒有對到機關代碼：{0}。匯入後請在公文系統裡選機關。",
    "book_missing": "機關地址簿還沒下載，機關代碼都是空的。",
    "placeholders": "還有 {0} 個〔待補〕〔待確認〕，匯入後要在公文系統裡補上。",
    "unplaced": "有 {0} 行認不出是哪個欄位，已經併進前一段。",
    "receiver_many": "受文者寫了好幾個機關，DI 檔的受文者只放得下一個：照原文放，機關代碼留空。",
}

_SPEEDS = ("普通件", "速件", "最速件")
_SECRECY = ("密", "機密", "極機密", "絕對機密")
_SEP_RE = re.compile(r"[、，,；;]")
_BAD_XML_CHARS = re.compile(r"[^\t\n\r -퟿-�\U00010000-\U0010ffff]")
_ATTACHED_RE = re.compile(r"[（(]\s*(?:均)?含附件\s*[）)]\s*$")
_FILE_KEYS = ("檔號", "保存年限")
_DATE_RE = re.compile(r"^(?:中華民國)?\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日$")


class DiNotApplicable(ValueError):
    """這份草稿出不了 DI（簽辦意見、找不到主旨）。訊息是給使用者看的中文（我們自己寫的）。"""


def _clean(s: str) -> str:
    return _BAD_XML_CHARS.sub("", str(s or "")).strip()


def _sub(parent: ET.Element, tag: str, text: Optional[str] = None, **attrs) -> ET.Element:
    e = ET.SubElement(parent, tag, {k: v for k, v in attrs.items() if v is not None})
    if text is not None:
        e.text = _clean(text)
    return e


def _marker(m: str) -> str:
    """項次標記照官方範例：括號用半形（「(一)」「(1)」），其餘照原樣。"""
    return (m or "").replace("（", "(").replace("）", ")")


def _roc_date(v: str) -> str:
    v = (v or "").strip()
    m = _DATE_RE.match(v)
    if not m:
        return v
    return f"中華民國{int(m.group(1))}年{int(m.group(2))}月{int(m.group(3))}日"


def _names(v: str) -> list[str]:
    return [p.strip() for p in _SEP_RE.split(v or "") if p.strip()]


class _Codes:
    """名稱 → 機關代碼：先用使用者挑的，沒有才照名稱查地址簿；記下沒對到的。"""

    def __init__(self, chosen: Optional[dict], lookup: Optional[Callable[[str], str]]):
        self.chosen = {str(k).strip(): str(v).strip() for k, v in (chosen or {}).items()
                       if isinstance(k, str) and isinstance(v, str) and v.strip()}
        self.lookup = lookup
        self.missing: list[str] = []

    def __call__(self, name: str) -> str:
        name = (name or "").strip()
        # 〔待補〕還沒填；「本局資訊安全科」「本公司」是自己內部的單位 —— 本來就沒有機關代碼，不算沒對到
        if not name or od.PLACEHOLDER_RE.search(name) or name.startswith("本"):
            return ""
        code = self.chosen.get(name, "")
        if not code and self.lookup:
            try:
                code = self.lookup(name) or ""
            except Exception:  # noqa: BLE001 — 查不到就是沒有代碼
                code = ""
        if not code and name not in self.missing:
            self.missing.append(name)
        return code


class _Body:
    """主旨之後的段落：段名 → 條列（照層級巢狀）→ 認不出的行併進前一段。"""

    def __init__(self, root: ET.Element):
        self.root = root
        self.para: Optional[ET.Element] = None
        self.stack: list[ET.Element] = []        # 目前每一層的條列
        self.last: Optional[ET.Element] = None   # 最後一個有「文字」可以接的元素
        self.unplaced = 0

    def label(self, name: str, text: str) -> None:
        self.para = _sub(self.root, "段落", None, **{"段名": f"{name}："})
        self.stack = []
        if text:
            _sub(self.para, "文字", text)
        self.last = self.para

    def item(self, level: int, marker: str, text: str) -> None:
        if self.para is None:            # 主旨之後直接接項次（少見）：開一段沒有段名的
            self.para = _sub(self.root, "段落", None, **{"段名": ""})
            self.stack = []
        level = max(1, min(int(level or 1), 4))
        del self.stack[level - 1:]
        parent = self.stack[-1] if self.stack else self.para
        it = _sub(parent, "條列", None, **{"序號": _marker(marker)})
        _sub(it, "文字", text)
        self.stack.append(it)
        self.last = it

    def append(self, text: str) -> bool:
        """認不出的行併進前一個元素的文字（中文接在後面，不加空白）。"""
        if self.last is None:
            return False
        node = self.last.find("文字")
        if node is None:
            node = ET.Element("文字")
            self.last.insert(0, node)
            node.text = ""
        node.text = (node.text or "") + _clean(text)
        self.unplaced += 1
        return True


def _blocks(text: str) -> list[dict]:
    return od.parse_text(text)


def _contact_lines(blocks: list[dict]) -> set[int]:
    from .official_doc_odt import _contact_zone
    return _contact_zone(blocks)


def _ordered_letter(blocks: list[dict], codes: _Codes, notes: list) -> ET.Element:
    root = ET.Element("函")
    contact = _contact_lines(blocks)
    meta = {b["key"]: str(b.get("value") or "").strip() for b in blocks if b.get("kind") == "meta"}
    title = next((b["text"] for b in blocks if b.get("kind") == "title"), "")
    org = re.sub(r"[\s　]*函$", "", title).strip()

    sender = _sub(root, "發文機關")
    _sub(sender, "全銜", org)
    _sub(sender, "機關代碼", codes(org))
    _sub(root, "函類別", None, **{"代碼": "函"})

    address = ""
    lines: list[str] = []
    for i in sorted(contact):
        b = blocks[i]
        if b.get("kind") == "meta" and b.get("key") == "地址":
            address = str(b.get("value") or "")
        else:
            lines.append(str(b.get("text") or ""))
    _sub(root, "地址", address)
    for ln in lines or [""]:
        _sub(root, "聯絡方式", ln)

    recv = _sub(root, "受文者")
    rv = meta.get("受文者", "")
    many = len(_names(rv)) > 1
    _sub(recv, "全銜", rv)
    _sub(recv, "機關代碼", "" if many else codes(rv))
    if many:
        notes.append({"code": "receiver_many", "args": []})

    date = _sub(root, "發文日期")
    _sub(date, "年月日", _roc_date(meta.get("發文日期", "")))
    no = _sub(root, "發文字號")
    if meta.get("發文字號"):
        _sub(no, "文字", meta["發文字號"])
    else:
        _sub(no, "字", "")
        num = _sub(no, "文號")
        for t in ("年度", "流水號", "支號"):
            _sub(num, t, "")
    speed = meta.get("速別", "")
    if speed in _SPEEDS:
        _sub(root, "速別", None, **{"代碼": speed})
    if "密等及解密條件或保密期限" in meta:
        v = meta["密等及解密條件或保密期限"]
        sec = _sub(root, "密等及解密條件或保密期限")
        if v in _SECRECY:
            _sub(sec, "密等", None, **{"代碼": v})
            _sub(sec, "解密條件或保密期限", "")
        else:
            _sub(sec, "密等")
            _sub(sec, "解密條件或保密期限", v)
    if "附件" in meta:
        att = _sub(root, "附件")
        _sub(att, "文字", meta["附件"])
    return root


def _fill_body(root: ET.Element, blocks: list[dict], *, mode: str, start: int) -> int:
    """主旨與段落；回傳認不出、併進前一段的行數。主旨找不到丟 DiNotApplicable。"""
    subj_i = next((i for i, b in enumerate(blocks) if b.get("kind") == "label"
                   and b.get("label") == "主旨"), None)
    if subj_i is None:
        raise DiNotApplicable("找不到「主旨：」那一行，產生不了 DI 檔（主旨是必填欄位）。")
    subj = _sub(root, "主旨")
    _sub(subj, "文字", str(blocks[subj_i].get("text") or ""))
    body = _Body(root)
    body.last = subj
    for b in blocks[subj_i + 1:]:
        kind = b.get("kind")
        if kind == "label":
            body.label(str(b.get("label") or ""), str(b.get("text") or ""))
        elif kind == "item":
            body.item(b.get("level") or 1, str(b.get("marker") or ""), str(b.get("text") or ""))
        elif kind == "para":
            body.append(str(b.get("text") or ""))
        else:   # meta / ending / date：主旨之後的框（正副本、署名、敬陳）由呼叫端處理
            break
    return body.unplaced


def _letter(text: str, codes: _Codes, notes: list) -> ET.Element:
    blocks = _blocks(text)
    root = _ordered_letter(blocks, codes, notes)
    unplaced = _fill_body(root, blocks, mode="letter", start=0)
    meta = {b["key"]: str(b.get("value") or "").strip() for b in blocks if b.get("kind") == "meta"}
    copies = _sub(root, "正本")
    names = _names(meta.get("正本", "")) or [""]
    for n in names:
        _sub(copies, "全銜", n)
    cc_names = _names(meta.get("副本", ""))
    if "副本" in meta:
        cc = _sub(root, "副本")
        for n in cc_names:
            attached = bool(_ATTACHED_RE.search(n))
            _sub(cc, "全銜", _ATTACHED_RE.sub("", n).strip())
            if attached:
                _sub(cc, "含附件", "含附件")
    # 正本、副本的機關代碼不在 DI 本文裡（交換表單 SW 才有）—— 這一期不查也不提醒，免得以為漏了東西
    after = False
    for b in blocks:
        if b.get("kind") == "meta" and b.get("key") in ("正本", "副本"):
            after = True
            continue
        if after and b.get("kind") == "ending":
            _sub(root, "署名", str(b.get("text") or ""))
    if unplaced:
        notes.append({"code": "unplaced", "args": [str(unplaced)]})
    return root


def _sign(text: str, notes: list) -> ET.Element:
    blocks = _blocks(text)
    root = ET.Element("簽")
    head = next((b["text"] for b in blocks if b.get("kind") == "head"), "")
    unit = re.sub(r"^簽[\s　]*於", "", head).strip()
    sender = _sub(root, "發文機關")
    _sub(sender, "全銜", unit)
    _sub(sender, "機關代碼", "")
    num = _sub(root, "文號")
    for t in ("年度", "流水號", "支號"):
        _sub(num, t, "")
    unplaced = _fill_body(root, blocks, mode="sign", start=0)
    titles: list[str] = []
    in_ending = False
    for b in blocks:
        if b.get("kind") == "ending":
            t = str(b.get("text") or "").strip()
            if t == "敬陳":
                in_ending = True
                continue
            if in_ending and t:
                titles.append(t)
    if titles:
        jc = _sub(root, "敬陳")
        for t in titles:
            _sub(jc, "職稱", t)
    _sub(root, "署名", "")
    date = next((b["text"] for b in blocks if b.get("kind") == "date"), "")
    _sub(root, "年月日", _roc_date(date))
    if unplaced:
        notes.append({"code": "unplaced", "args": [str(unplaced)]})
    return root


def build(text: str, mode: str, *, org_codes: Optional[dict] = None,
          lookup: Optional[Callable[[str], str]] = None,
          book_available: bool = True) -> tuple[bytes, list[dict]]:
    """草稿文字 → (DI 檔的位元組, 注意事項 `[{code, args}]`)。

    `org_codes`：案件存著的「名稱 → 代碼」；`lookup(name) -> code`：照名稱查地址簿（名稱完全相同
    而且只有一筆才回代碼）。簽辦意見、找不到主旨 → `DiNotApplicable`。"""
    if mode not in ROOTS:
        raise DiNotApplicable("簽辦意見不是一種文別（寫在公文系統的簽辦欄裡），沒有 DI 檔；"
                              "請用「複製純文字」貼進公文系統。")
    notes: list[dict] = []
    codes = _Codes(org_codes, lookup if book_available else None)
    root = _letter(text, codes, notes) if mode == "letter" else _sign(text, notes)
    if mode == "letter":
        if not book_available:
            notes.insert(0, {"code": "book_missing", "args": []})
        elif codes.missing:
            notes.insert(0, {"code": "codes_missing", "args": ["、".join(codes.missing)]})
    n = len(od.PLACEHOLDER_RE.findall(text or ""))
    if n:
        notes.append({"code": "placeholders", "args": [str(n)]})
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           f'<!DOCTYPE {ROOTS[mode]} SYSTEM "{DTD_FILES[mode]}">\n' + body + "\n")
    return xml.encode("utf-8"), notes


def validate(data: bytes, mode: str) -> tuple[bool, list[str]]:
    """拿跟著程式的 104 版 DTD 驗一次：(通過嗎, 前幾條錯誤)。

    DTD 要用**檔案路徑**載入 —— 它用 `SYSTEM "104_basic_utf8.ent"` 參照標籤集，相對路徑照 DTD 檔
    所在的位置解析；用檔案物件載入的話標籤集一個都讀不到，全部報「沒有宣告」。
    解析我們自己的 XML 時也不載外部實體、不連網路。"""
    from lxml import etree
    dtd = etree.DTD(str(DTD_DIR / DTD_FILES[mode]))
    parser = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False)
    try:
        doc = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as e:
        return False, [str(e)[:200]]
    ok = dtd.validate(doc)
    return bool(ok), [str(e)[:200] for e in dtd.error_log.filter_from_errors()[:5]]
