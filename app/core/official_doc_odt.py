"""公文撰擬的匯出：把（可能被使用者改過的）純文字排成 A4 的 ODT，再交給 Office 引擎轉 DOCX / PDF。

**一律從純文字讀回結構**（`official_doc.parse_text`）—— 使用者在畫面上改過的字
才是要交出去的版本，不可以拿模型當初回的 JSON 重排。

**ODT 直接寫 ODF XML，只用標準函式庫**：所以沒有 Office 引擎的機器也下載得到 ODT。
DOCX 與 PDF 才需要 soffice（找不到時 `office_convert.OfficeUnavailableError`
原樣往外丟，全站處理器回 503）。

版面（16pt 為 1em ＝ 0.5644cm）：

* A4，上下左右邊界 2.5cm；內文 16pt、行高 150%。
* 抬頭（「簽　　於…」）18pt、日期行 12pt。
* 「主旨：…」同一行有內容時**懸掛縮排 3em**（換行對齊在「主旨：」之後）；
  段名單獨一行（「說明：」）就是一般段落。
* 項次的懸掛縮排：一、（左邊界起、懸掛 2em）→（一）（2em 起、懸掛 3em）
  → 1、（5em 起、懸掛 2em）→（1）（7em 起、懸掛 3em）。
* 草稿（`draft_mark=True`）：頁首右側一行灰色「草稿」。**本文起點不因頁首下移**
  —— 頁首放在頁面上緣的邊界裡（見 `_page_layout`），有沒有「草稿」本文都從 2.5cm 開始。

**項次 1、（1）的單一位數改成全形數字**（「１、」「（１）」）：懸掛縮排是照
「標記寬度＝懸掛寬度」算的，而半形數字只有 0.5em —— 照原樣寫的話第一行的字
比換行後的字往左半個字，整份對不齊。兩位數（「10、」）本來就剛好 2em，不動。

## 字型替代（2026-10-07 在開發機實測：OxOffice 11.0.5 與 LibreOffice 24.2）

伺服器上通常沒有標楷體，PDF 要退到別的楷體。LibreOffice 的字型名稱欄可以打
分號列替代字型（`A;B;C`）—— **在 Linux 上它沒有作用**：

* 清單**讀得進去**：ODF 寫成 `svg:font-family="'A', 'B'"`（或直接寫 `A;B`），
  soffice 轉回 ODT 時原樣寫回 `A, 'B'`。
* 但**算圖時只看第一個名字**：Linux 版在逐一比對清單之前，先把名字交給
  fontconfig 找「最像的」，fontconfig 一定回得出東西，清單後面的名字就輪不到了。
  實測 `NonExistentFontXYZ;Noto Serif CJK TC` 轉出的 PDF：
  OxOffice 內嵌 Source Han Serif TC（依 generic=roman 退的，不是清單），
  LibreOffice 內嵌 **Noto Sans CJK TC**（黑體）；`…;Noto Sans CJK TC`
  在 OxOffice 上一樣得到明體 —— 清單的第二個名字從來沒被用到。
* 單一名字找不到時，結果由 `style:font-family-generic` ＋ fontconfig 決定：
  OxOffice 自己帶了全字庫正楷體（`share/fonts/truetype/TW-Kai-98_1.ttf`），
  它的 fontconfig 設定還把「名稱含『楷』」對到 TW-Kai，所以 `標楷體`、
  `DFKai-SB`、任何 generic=script 的名字都落到 TW-Kai；
  **LibreOffice 24.2 把 `標楷體` 畫成 Noto Sans CJK TC（黑體）** —— 風格整個錯。

所以 PDF 不能靠清單：`export("pdf")` 在產生 ODT 時**先挑出 soffice 真的看得到的
那一個字型**（`pick_font`：系統字型 ＋ Office 引擎自己帶的字型，依
`PDF_FONT_CANDIDATES` 由楷到明的順序），只寫那一個名字。**上傳到本系統的自訂字型
不算** —— soffice 看不到 `data/fonts`。

Word 不認得分號，所以 DOCX 與 ODT 一律只寫 `標楷體`（`WORD_FONT`）—— 那兩種檔案是
給有標楷體的 Windows 使用者開的。
"""
from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from xml.sax.saxutils import escape as _xml_escape

from . import office_convert
from .official_doc import parse_text

#: Word / ODT 用的字型 —— 公文的標準字型。Word 不認得分號清單，所以只能寫一個名字。
WORD_FONT = "標楷體"

#: 產 PDF 時的候選字型，**由前往後挑第一個 soffice 看得到的**（見 `pick_font`）。
#:
#: * 標楷體 / DFKai-SB —— Windows（`kaiu.ttf`）；BiauKai —— macOS 的標楷體。
#: * TW-Kai —— 全字庫正楷體，**OxOffice 自己帶著**。
#: * AR PL UKai TW / AR PL UKai TW MBE —— Debian / Ubuntu 的 `fonts-arphic-ukai`。
#: * 都沒有楷體時退明體：Noto Serif CJK TC（`fonts-noto-cjk`）、思源宋體、
#:   全字庫正宋體、AR PL UMing、新細明體。公文用明體不算錯；用黑體才算
#:   （LibreOffice 對找不到的「標楷體」就是退成黑體）。
PDF_FONT_CANDIDATES = (
    "標楷體", "DFKai-SB", "BiauKaiTC", "BiauKai", "TW-Kai",
    "AR PL UKai TW", "AR PL UKai TW MBE",
    "Noto Serif CJK TC", "Noto Serif TC", "Source Han Serif TC", "TW-Sung",
    "AR PL UMing TW", "AR PL UMing TW MBE", "新細明體", "PMingLiU",
)

#: 伺服器產 PDF 用的字型 —— 上面那串候選以分號連起來（LibreOffice 的寫法）。
#: **`build_odt` 收到分號清單時會先換成第一個裝了的那一個**，不把清單原樣交給
#: soffice（Linux 上它只看第一個名字，見模組說明）。
PDF_FONT = ";".join(PDF_FONT_CANDIDATES)

FORMATS = ("odt", "docx", "pdf")
MEDIA_TYPES = {
    "odt": "application/vnd.oasis.opendocument.text",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}

ODF_VERSION = "1.3"
_MIME = MEDIA_TYPES["odt"]

#: 內文字級；1em ＝ 16pt ＝ 0.5644cm（16 × 2.54 ÷ 72）。
BODY_PT = 16
EM_CM = BODY_PT * 2.54 / 72

#: 版面數值**照檔案管理局「筆硯公文製作系統」的官方範本**（`函.odt`、`簽.odt`，
#: 政府資料開放平臺資料集 30943；2026-10-07 讀出來的值）：
#: 邊界上下 2.0、左 2.5、右 2.21cm；內文 16pt、固定行高 0.88cm；欄位 12pt；檔號 10pt。
PAGE_MARGINS_CM = {"top": 2.0, "bottom": 2.0, "left": 2.5, "right": 2.21}
LINE_HEIGHT_BODY_CM = 0.88
LINE_HEIGHT_META_CM = 0.6
#: 各層項次的（左邊界，懸掛寬度），單位 em。標記從「左邊界 − 懸掛」開始：
#: 「一、」縮 1 字、文字對齊第 3 字；「(一)」縮 2 字、文字對齊第 4 字（官方範本 1.68 / 2.24cm）。
#: 標記一律兩個字寬（`_display_marker` 把括號改半形、單一位數改全形）。
ITEM_INDENT = {1: (3, 2), 2: (4, 2), 3: (5, 2), 4: (6, 2)}
#: 「主旨：」三個字 —— **簽**的段名懸掛 3 字；**函**的主旨、說明續行回到左邊界（官方範本如此）。
LABEL_HANG = 3
#: 函抬頭右半邊的欄位（檔號、保存年限、地址、承辦人、電話…）從這個位置開始（官方範本約在版心 55%）
RIGHT_BLOCK_CM = 9.0
#: 函的署名：上面留給用印的空間（約三行）與從左邊算起的位置（版心中間偏左，
#: 公司章與負責人章蓋在署名旁邊；照使用者附的自家函）
SIGN_GAP_CM = 2.6
#: 機關的檔案管理欄位：企業發的函不寫（草稿裡沒有這幾行時，範本那幾行整行拿掉）
_AGENCY_ONLY_KEYS = ("檔號", "保存年限", "密等及解密條件或保密期限")
SIGN_LEFT_CM = 6.5

_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
    'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/"'
)


# ------------------------------------------------------------------ 匯出時加在版面上的項目
#
# 2026-10-08 使用者：匯出時可以選要不要加裝訂線、正本、發文方式…（給的是自己公司發出去的函，
# 左邊有裝訂線、左上角「正本／發文方式：郵寄」、頁尾「第1頁　共1頁」）。對照實際的機關公文
# （左上「副本／發文方式：電子交換」、頁尾頁碼、署名下方「本案依分層負責規定授權…決行」、
# 受文者的郵遞區號與地址給開窗信封）與文書處理手冊（兩頁以上每頁下緣加註頁碼）整理出這幾項。
#
# **都不是草稿的內容**：不寫進草稿文字、不受事實檢查（「依分層負責規定」寫進草稿會被當成
# 沒有依據的法規），只在 ODT / DOCX / PDF / 圖片上出現；每一項都是使用者自己選的。

COPY_MARKS = ("正本", "副本", "抄本")
SEND_METHODS = ("電子交換", "郵寄", "掛號郵寄", "專差送達", "親自送達", "傳真", "電子郵件")
#: 「本案依分層負責規定授權○○決行」常見的寫法（畫面上給建議，打別的也收）
DELEGATE_SUGGESTIONS = ("業務主管", "單位主管", "一級主管", "副首長")
MAX_DELEGATE_CHARS = 20
MAX_ADDRESS_CHARS = 80
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_DELEGATE_FULL = re.compile(r"^本案依分層負責規定授權(.+?)決行。?$")


@dataclass(frozen=True)
class PageExtras:
    page_numbers: bool = False      # 頁尾「第○頁　共○頁」
    binding_line: bool = False      # 左側裝訂線（虛線＋「裝」「訂」「線」）
    copy_mark: str = ""             # 左上角：正本 / 副本 / 抄本
    send_method: str = ""           # 左上角：發文方式：…
    delegate: str = ""              # 「本案依分層負責規定授權{delegate}決行」
    receiver_address: str = ""      # 受文者的郵遞區號與地址（開窗信封），放在「受文者」上面
    #: 簽辦意見單獨列印時：上面印「簽辦意見」與來文、下面留承辦人簽章與日期
    #: （2026-10-08 使用者：簽辦意見通常寫在來文空白處或公文系統的簽辦欄，單獨印成一頁時
    #: 看不出是哪一份來文的意見、也沒有地方簽名）。預設不印 —— 貼進公文系統時會多出這幾行。
    endorse_frame: bool = False
    #: 「來文：」那一行的內容。**由伺服器從這份案件整理出來的資料填**，不收畫面送來的字
    endorse_source: str = ""

    @property
    def header_items(self) -> bool:
        return bool(self.binding_line or self.copy_mark or self.send_method)

    def cache_key(self) -> list:
        return [self.page_numbers, self.binding_line, self.copy_mark, self.send_method,
                self.delegate, self.receiver_address, self.endorse_frame, self.endorse_source]


NO_EXTRAS = PageExtras()


def page_extras(raw: Any) -> PageExtras:
    """畫面 / API 送來的 `extras`（dict）→ `PageExtras`。**不可信的輸入**：選項只收清單上的，
    文字去掉控制字元、限長；不合規定丟 `ValueError`（訊息可以給使用者看）。"""
    if raw in (None, ""):
        return NO_EXTRAS
    if not isinstance(raw, dict):
        raise ValueError("extras 要是物件。")

    def flag(k: str) -> bool:
        v = raw.get(k)
        return v is True or (isinstance(v, str) and v.lower() in ("1", "true", "on", "yes"))

    def choice(k: str, allowed: tuple, name: str) -> str:
        v = raw.get(k)
        if v in (None, ""):
            return ""
        if not isinstance(v, str) or v not in allowed:
            raise ValueError(f"「{name}」只接受：{'、'.join(allowed)}。")
        return v

    def text(k: str, limit: int, name: str) -> str:
        v = raw.get(k)
        if v in (None, ""):
            return ""
        if not isinstance(v, str):
            raise ValueError(f"「{name}」要是文字。")
        v = re.sub(r"\s+", " ", _CTRL.sub(" ", v)).strip()
        if len(v) > limit:
            raise ValueError(f"「{name}」超過 {limit} 字的上限。")
        return v

    delegate = text("delegate", MAX_DELEGATE_CHARS + 20, "分層負責決行")
    m = _DELEGATE_FULL.match(delegate)
    if m:                       # 整句貼進來也收：只留「授權」與「決行」之間那幾個字
        delegate = m.group(1).strip()
    if len(delegate) > MAX_DELEGATE_CHARS:
        raise ValueError(f"「分層負責決行」超過 {MAX_DELEGATE_CHARS} 字的上限。")
    return PageExtras(
        page_numbers=flag("page_numbers"), binding_line=flag("binding_line"),
        copy_mark=choice("copy_mark", COPY_MARKS, "正本/副本標示"),
        send_method=choice("send_method", SEND_METHODS, "發文方式"),
        delegate=delegate,
        receiver_address=text("receiver_address", MAX_ADDRESS_CHARS, "受文者地址"),
        endorse_frame=flag("endorse_frame"))


def delegate_line(extras: PageExtras) -> str:
    return f"本案依分層負責規定授權{extras.delegate}決行" if extras.delegate else ""


#: 簽辦意見單獨列印的簽章欄（承辦人簽章、日期）—— 空格留給手寫
ENDORSE_SIGN_LINES = ("承辦人：", "日期：　　　年　　月　　日")


def endorse_frame_paras(extras: PageExtras) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """簽辦意見單獨列印時加在上面與下面的段落 `(上面, 下面)`；沒勾就是兩個空清單。"""
    if not extras.endorse_frame:
        return [], []
    head = [("OD_Title", "簽辦意見")]
    head.append(("OD_Para", "來文：" + (extras.endorse_source or "")))
    head.append(("OD_Gap", ""))
    tail = [("OD_Sign", ENDORSE_SIGN_LINES[0]), ("OD_SignLine", ENDORSE_SIGN_LINES[1])]
    return head, tail


def address_lines(addr: str) -> list[str]:
    """「407662 臺中市…」→ 郵遞區號一行、地址一行（開窗信封的寫法）；沒有郵遞區號就一行。"""
    m = re.match(r"^(\d{3,6})\s*(.+)$", addr or "")
    return [m.group(1), m.group(2)] if m else ([addr] if addr else [])


#: 頁首 / 頁尾的高度與間距（跟「草稿」頁首同一套：放在邊界裡，本文起點不動）
_FOOTER_HEIGHT_CM = 1.0
_FOOTER_GAP_CM = 0.4
#: 裝訂線離版心左緣多遠（政府公文與使用者給的檔案都在左邊界中間附近）
_BIND_X_CM = -1.0
_BIND_TEXT = ("裝", "訂", "線")


def _furniture_styles() -> str:
    """頁首的線與小框要用的圖形樣式（放在 styles.xml 的 automatic-styles；
    虛線的定義 `_dash_xml()` 要放在 office:styles）。"""
    common = ('style:wrap="run-through" style:run-through="foreground" '
              'style:vertical-pos="from-top" style:vertical-rel="paragraph" '
              'style:horizontal-pos="from-left" style:horizontal-rel="paragraph"')
    return (
        '<style:style style:name="OD_grBind" style:family="graphic">'
        '<style:graphic-properties draw:stroke="dash" draw:stroke-dash="OD_BindDash" '
        'svg:stroke-width="0.01cm" svg:stroke-color="#000000" draw:fill="none" '
        f'{common}/></style:style>'
        '<style:style style:name="OD_frBind" style:family="graphic">'
        '<style:graphic-properties draw:stroke="none" draw:fill="solid" draw:fill-color="#ffffff" '
        'fo:background-color="#ffffff" fo:border="none" fo:padding="0cm" '
        f'{common}/></style:style>'
        '<style:style style:name="OD_frMark" style:family="graphic">'
        '<style:graphic-properties draw:stroke="none" draw:fill="none" '
        'fo:background-color="transparent" fo:border="none" fo:padding="0cm" '
        f'{common}/></style:style>'
        + _frame_text_styles()
    )


def _dash_xml() -> str:
    return ('<draw:stroke-dash draw:name="OD_BindDash" draw:display-name="公文裝訂線" '
            'draw:style="rect" draw:dots1="1" draw:dots1-length="0.03cm" draw:distance="0.06cm"/>')


def _furniture_para_styles() -> str:
    """頁尾、分層負責、受文者地址的段落樣式（放 office:styles，內文也用得到）。"""
    return (
        _para_style("OD_Footer", "公文頁尾", pt=10,
                      extra_para=' fo:text-align="center" fo:line-height="100%"')
        + _para_style("OD_Delegate", "公文分層負責", pt=12,
                      extra_para=' fo:text-align="end" fo:margin-top="0.6cm"')
        + _para_style("OD_RecvAddr", "公文受文者地址", pt=12,
                      extra_para=f' fo:line-height="{LINE_HEIGHT_META_CM}cm"')
    )


def _frame_text_styles() -> str:
    """頁首小框裡的字。**要放 styles.xml 的 automatic-styles** —— 放 office:styles 的話，
    LibreOffice 在頁首的框裡不套用字級，一律畫成 16pt（2026-10-08 實測；使用者給的檔案也是
    自動樣式）。"""
    return (
        _para_style("OD_BindChar", "公文裝訂線字", pt=10,
                    extra_para=' fo:text-align="center" fo:line-height="100%"')
        + _para_style("OD_CopyMark", "公文正副本標示", pt=14,
                      extra_para=' fo:text-align="start" fo:line-height="100%"')
        + _para_style("OD_SendMethod", "公文發文方式", pt=10,
                      extra_para=' fo:text-align="start" fo:line-height="100%"')
        + '<style:style style:name="OD_t10" style:family="text">' + _text_props(10) + "</style:style>"
        + '<style:style style:name="OD_t14" style:family="text">' + _text_props(14) + "</style:style>"
    )


def _header_inner(extras: PageExtras, *, rel_y1: float, rel_y2: float) -> str:
    """頁首段落**裡面**的圖形（錨在頁首那一段，座標相對於那一段的左上角）。

    `rel_y1` / `rel_y2`：本文的上緣、下緣相對於頁首段落的位置（裝訂線從本文頂畫到本文底）。"""
    out = ""
    if extras.binding_line:
        out += (f'<draw:line draw:style-name="OD_grBind" text:anchor-type="paragraph" draw:z-index="0" '
                f'svg:x1="{_BIND_X_CM}cm" svg:y1="{rel_y1:.2f}cm" '
                f'svg:x2="{_BIND_X_CM}cm" svg:y2="{rel_y2:.2f}cm"><text:p/></draw:line>')
        span = rel_y2 - rel_y1
        for n, ch in enumerate(_BIND_TEXT, 1):
            y = rel_y1 + span * n / 4 - 0.3
            out += (f'<draw:frame draw:style-name="OD_frBind" text:anchor-type="paragraph" '
                    f'draw:z-index="{n}" svg:x="{_BIND_X_CM - 0.25:.2f}cm" svg:y="{y:.2f}cm" '
                    'svg:width="0.5cm" svg:height="0.6cm"><draw:text-box>'
                    f'<text:p text:style-name="OD_BindChar"><text:span text:style-name="OD_t10">{ch}</text:span></text:p>'
                    '</draw:text-box></draw:frame>')
    if extras.copy_mark or extras.send_method:
        lines = ""
        if extras.copy_mark:
            # 實際的公文寫成「副　本」（兩字中間空一格）
            lines += ('<text:p text:style-name="OD_CopyMark"><text:span text:style-name="OD_t14">'
                      f'{"　".join(extras.copy_mark)}</text:span></text:p>')
        if extras.send_method:
            lines += ('<text:p text:style-name="OD_SendMethod"><text:span text:style-name="OD_t10">'
                      f'發文方式：{_esc(extras.send_method)}</text:span></text:p>')
        out += ('<draw:frame draw:style-name="OD_frMark" text:anchor-type="paragraph" draw:z-index="5" '
                'svg:x="-1.5cm" svg:y="-0.45cm" svg:width="8cm"><draw:text-box fo:min-height="0.4cm">'
                + lines + '</draw:text-box></draw:frame>')
    return out


def _footer_xml(extras: PageExtras) -> str:
    if not extras.page_numbers:
        return ""
    return ('<style:footer><text:p text:style-name="OD_Footer">第'
            '<text:page-number text:select-page="current">1</text:page-number>頁　共'
            '<text:page-count>1</text:page-count>頁</text:p></style:footer>')


# ------------------------------------------------------------------ 文字

#: XML 1.0 不允許的字元（控制字元、落單的代理字元）—— 留著的話整份 XML 解析失敗，
#: 使用者只會看到「檔案毀損」。直接拿掉。
_BAD_XML_CHARS = re.compile(r"[^\t\n\r\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_TOKENS = re.compile(r" +|\t|\r\n|\n|\r|[^ \t\r\n]+")


def _esc(s: str) -> str:
    return _xml_escape(s, {'"': "&quot;", "'": "&apos;"})


def _inline(s: str) -> str:
    """一段文字 → 段落內容的 XML。

    ODF 會把連續的半形空白縮成一個、段首的空白直接丟掉 —— 所以第二個以後的空白、
    以及段首段尾的空白，一律寫成 `<text:s text:c="n"/>`。tab 是 `<text:tab/>`、
    換行是 `<text:line-break/>`。全形空白（U+3000）不是 XML 的空白，照原樣留著。
    """
    s = _BAD_XML_CHARS.sub("", s or "")
    toks = [m.group(0) for m in _TOKENS.finditer(s)]
    out: list[str] = []
    for i, tok in enumerate(toks):
        if tok == "\t":
            out.append("<text:tab/>")
        elif tok in ("\n", "\r", "\r\n"):
            out.append("<text:line-break/>")
        elif tok[0] == " ":
            n = len(tok)
            prev_ok = i > 0 and toks[i - 1][0] not in " \t\r\n"
            next_ok = i + 1 < len(toks) and toks[i + 1][0] not in " \t\r\n"
            if prev_ok and next_ok:
                # 兩個字中間：第一個空白照寫，其餘用 text:s
                out.append(" ")
                n -= 1
            if n:
                out.append('<text:s/>' if n == 1 else f'<text:s text:c="{n}"/>')
        else:
            out.append(_esc(tok))
    return "".join(out)


_FW_DIGITS = str.maketrans("0123456789", "０１２３４５６７８９")


def _display_marker(marker: str) -> str:
    """項次標記一律排成兩個字寬，讓它剛好等於懸掛寬度。

    * 括號用**半形**（官方範本寫「(一)」）：「(一)」＝ 0.5 ＋ 1 ＋ 0.5 ＝ 2 字寬；
      全形的「（一）」是 3 字寬，第一行會比續行多出一個字。
    * 單一位數的阿拉伯數字改全形：「1、」是 1.5 字寬、「(1)」也是 1.5 —— 改全形剛好 2。
      兩位數（「10、」）本來就剛好，不動。
    """
    m = (marker or "").replace("（", "(").replace("）", ")")
    digits = re.findall(r"[0-9]+", m)
    if len(digits) == 1 and len(digits[0]) == 1:
        return m.translate(_FW_DIGITS)
    return m


#: 函抬頭欄位 → 樣式。右半邊那一塊（檔號、聯絡資訊）與左邊的發文欄位分開排（照官方範本）。
_META_FILE_KEYS = ("檔號", "保存年限")
_META_CONTACT_KEYS = ("地址", "統一編號", "聯絡人", "承辦人", "聯絡電話", "電話", "傳真", "電子信箱")
#: 左邊那幾個欄位懸掛「欄位名＋冒號」的字數（12pt 的字）—— 只列會出現的寬度，其餘照 3 字
_META_HANGS = (3, 4, 5, 13)


def _meta_style(key: str) -> str:
    if key in _META_FILE_KEYS:
        return "OD_MetaFile"
    if key in _META_CONTACT_KEYS:
        return "OD_Contact"
    if key == "受文者":
        return "OD_Receiver"
    n = len(key) + 1                 # 發文日期：5、速別：3、密等及解密條件或保密期限：13
    return f"OD_Meta{n if n in _META_HANGS else 3}"


def _contact_zone(blocks: list[dict]) -> set[int]:
    """函：標題之後、受文者（或主旨）之前的每一行都是發文者的聯絡資訊，回傳那幾行的位置。

    地址、聯絡人、電話、電子郵件…一律排在右邊的聯絡資訊區塊 —— 使用者寫「連絡人」
    「電子郵件」這種不在 `_META_CONTACT_KEYS` 的字也一樣（2026-10-08 使用者：只有地址排對，
    另外兩行跑成內文大字）。內建版面與套範本兩條路都用這一支。檔號、保存年限不算。
    """
    if not any(b.get("kind") == "title" for b in blocks):
        return set()
    out: set[int] = set()
    state = "before"
    for i, b in enumerate(blocks):
        kind = b.get("kind")
        key = str(b.get("key") or "") if kind == "meta" else ""
        if state == "before":
            if kind == "title":
                state = "contact"
            continue
        if key == "受文者" or kind in ("label", "item", "ending"):
            break
        if kind in ("meta", "para") and key not in _META_FILE_KEYS:
            out.add(i)
    return out


def _paragraphs(text: str) -> list[tuple[str, str]]:
    """純文字 → [(段落樣式名, 段落文字)]。"""
    out: list[tuple[str, str]] = []
    blocks = parse_text(text)
    # 有「○○　函」標題的是函：主旨、說明的續行回到左邊界（官方範本）；簽則懸掛 3 字
    is_letter = any(b.get("kind") == "title" for b in blocks)
    contact = _contact_zone(blocks)
    copies_started = False
    for i, b in enumerate(blocks):
        kind = b.get("kind")
        t = str(b.get("text") or "")
        key = str(b.get("key") or "") if kind == "meta" else ""
        if i in contact:
            out.append(("OD_Contact", t))
            continue
        if is_letter and key in ("正本", "副本") and not copies_started:
            # 正副本跟本文之間空一行（使用者自己的函與機關實際公文都是這樣）
            copies_started = True
            out.append(("OD_Gap", ""))
        if kind == "head":
            out.append(("OD_Head", t))
        elif kind == "title":
            out.append(("OD_Title", t))
        elif kind == "meta":
            out.append((_meta_style(key), t))
        elif kind == "date":
            out.append(("OD_Date", t))
        elif kind == "label":
            label = str(b.get("label") or "")
            # 冒號一律全形：懸掛縮排是照「主旨：」＝ 3 個全形字算的
            if t:
                out.append(("OD_LabelFlat" if is_letter else "OD_Label", f"{label}：{t}"))
            else:
                out.append(("OD_LabelTitle", f"{label}："))
        elif kind == "item":
            try:
                level = int(b.get("level") or 1)
            except (TypeError, ValueError):
                level = 1
            level = min(max(level, 1), 4)
            out.append((f"OD_Item{level}", _display_marker(str(b.get("marker") or "")) + t))
        elif kind == "ending":
            if is_letter and copies_started:
                # 函的署名（「局長　王○○」「○○公司　負責人」）：上面留用印的空間、往右排
                # （2026-10-08 使用者：用印的地方跟上面要留空位，附自己公司的函為例）
                out.append(("OD_Sign", t))
            else:
                # 「敬陳」縮四個字（官方範本），陳核對象與署名頂格
                out.append(("OD_Jingchen" if t == "敬陳" else "OD_Ending", t))
        else:
            out.append(("OD_Para", t))
    return out


# ------------------------------------------------------------------ 樣式

def _cm(em: float) -> str:
    return f"{em * EM_CM:.4f}cm"


# ------------------------------------------------------------------ 字型挑選

_FONT_EXTS = (".ttf", ".otf", ".ttc", ".otc")
#: 只讀檔名看起來像楷 / 明 / 宋的字型檔 —— 全部讀的話 Windows 幾百個字型要好幾秒。
#: 實測開發機（OxOffice ＋ 系統字型）只讀到 32 個檔、0.22 秒。
_FONT_FILE_HINTS = ("kai", "biau", "ming", "song", "sung", "serif")
#: 字型很少變，但管理員裝了新字型不該要重啟才生效 —— 十分鐘重查一次。
_FAMILY_TTL_S = 600.0
_family_cache: dict[str, tuple[float, frozenset]] = {}


def refresh_font_cache() -> None:
    """清掉「裝了哪些字型」的快取（測試與管理員剛裝字型時用）。"""
    _family_cache.clear()


def _bundled_font_dirs(soffice: Optional[str]) -> list[Path]:
    """Office 引擎自己帶的字型（OxOffice 的全字庫正楷體就在這裡）。

    系統的 fontconfig 看不到它們，但 soffice 看得到 —— 只問 `fc-list` 的話，
    OxOffice 上明明有楷體也會被判成沒有。
    """
    if not soffice:
        return []
    try:
        root = Path(soffice).resolve().parent.parent
    except OSError:
        return []
    dirs = (root / "share" / "fonts" / "truetype",            # Linux / Windows
            root / "Resources" / "fonts" / "truetype")        # macOS 的 .app
    return [d for d in dirs if d.is_dir()]


def _system_font_dirs() -> list[Path]:
    """系統字型目錄 —— 跟字型管理用同一份清單，不另寫一份。"""
    try:
        from .font_catalog import _detect_font_dirs
        return list(_detect_font_dirs())
    except Exception:
        return []


def _names_in_file(path: Path) -> set:
    """字型檔自己的家族名（name 表的 1 / 16，所有語言 —— 標楷體同時叫 DFKai-SB）。"""
    try:
        from fontTools.ttLib import TTCollection, TTFont
    except ImportError:
        return set()
    names: set = set()
    holder = None
    try:
        if path.suffix.lower() in (".ttc", ".otc"):
            holder = TTCollection(str(path), lazy=True)
            fonts = list(holder.fonts)
        else:
            holder = TTFont(str(path), lazy=True)
            fonts = [holder]
        for f in fonts:
            for rec in f["name"].names:
                if rec.nameID not in (1, 16):
                    continue
                try:
                    v = rec.toUnicode().strip()
                except Exception:
                    continue
                if v:
                    names.add(v)
    except Exception:
        return names
    finally:
        try:
            if holder is not None:
                holder.close()
        except Exception:
            pass
    return names


def _fc_list_families() -> set:
    """fontconfig 認得的家族名（Linux 上 soffice 就是問它）。沒有 fc-list 回空集合。"""
    exe = shutil.which("fc-list")
    if not exe:
        return set()
    try:
        out = subprocess.run([exe, ":", "family"], capture_output=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    names: set = set()
    for line in out.decode("utf-8", "replace").splitlines():
        for n in line.split(","):
            n = n.replace("\\-", "-").strip()
            if n:
                names.add(n)
    return names


def available_font_families(soffice: Optional[str] = None) -> frozenset:
    """soffice 看得到的字型家族名（`casefold` 過）：系統字型 ＋ Office 引擎自己帶的。

    **上傳到本系統的自訂字型（`data/fonts`）不算** —— soffice 看不到那個目錄。
    """
    key = soffice or ""
    now = time.monotonic()
    hit = _family_cache.get(key)
    if hit is not None and now - hit[0] < _FAMILY_TTL_S:
        return hit[1]
    names = _fc_list_families()
    for d in _bundled_font_dirs(soffice) + _system_font_dirs():
        try:
            files = [p for p in d.rglob("*")
                     if p.suffix.lower() in _FONT_EXTS
                     and any(h in p.name.lower() for h in _FONT_FILE_HINTS)]
        except OSError:
            continue
        for p in files:
            names |= _names_in_file(p)
    result = frozenset(n.casefold() for n in names)
    _family_cache[key] = (now, result)
    return result


def _split_names(font_family: str) -> list[str]:
    names = [n.strip().strip("'\"") for n in re.split(r"[;,]", font_family or "")]
    return [n for n in names if n]


def pick_font(font_family: str, *, soffice: Optional[str] = None) -> str:
    """分號清單 → **第一個 soffice 看得到的**那一個名字；單一名字原樣回。

    一個都沒裝時回清單的第一個（交給 soffice 依 generic 自己退）。
    清單在 Linux 上不能直接交給 soffice（只看第一個名字，見模組說明），所以
    `build_odt` 收到清單一律先經過這裡。
    """
    names = _split_names(font_family)
    if not names:
        return WORD_FONT
    if len(names) == 1:
        return names[0]
    avail = available_font_families(
        soffice if soffice is not None else office_convert.find_soffice())
    for n in names:
        if n.casefold() in avail:
            return n
    return names[0]


# ------------------------------------------------------------------ 樣式（續）

def _font_family_attr(font_family: str) -> str:
    """字型名 → `svg:font-family` 的值。

    單一名字照原樣（Word 讀得懂）；含空白的名字照 ODF / CSS 的寫法加單引號。
    萬一收到清單（`A;B`）就寫成 `'A', 'B'` —— soffice 讀得進去（實測轉回 ODT 會
    原樣寫回），只是 Linux 上算圖時只看第一個。
    """
    names = _split_names(font_family) or [WORD_FONT]
    if len(names) == 1 and not re.search(r"[\s,;'\"]", names[0]):
        return names[0]
    return ", ".join("'" + n.replace("'", "") + "'" for n in names)


def _font_generic(font_family: str) -> str:
    """給 soffice 的風格提示 —— 字型名稱**找不到**時它靠這個挑同風格的字型。

    楷書歸 `script`（同 pdf-to-office 的 `_TW_GENERIC`：roman＝明宋、swiss＝黑、
    script＝楷）。明體 / 宋體 / 其他一律 `roman`。實測 OxOffice 對找不到的
    script 名字會落到它自帶的 TW-Kai；LibreOffice 24.2 不管 generic 是什麼都退
    Noto Sans CJK —— 所以 PDF 才要先 `pick_font`，不能只靠這個提示。
    """
    first = re.split(r"[;,]", font_family or WORD_FONT)[0].lower()
    if "楷" in first or "kai" in first:
        return "script"
    if "黑" in first or "sans" in first or "hei" in first:
        return "swiss"
    return "roman"


def _font_decls(font_family: str) -> str:
    return (
        "<office:font-face-decls>"
        f'<style:font-face style:name="OD_Font" svg:font-family="{_esc(_font_family_attr(font_family))}" '
        f'style:font-family-generic="{_font_generic(font_family)}" style:font-pitch="variable"/>'
        "</office:font-face-decls>"
    )


def _text_props(pt: float, extra: str = "") -> str:
    return (
        f'<style:text-properties style:font-name="OD_Font" style:font-name-asian="OD_Font" '
        f'style:font-name-complex="OD_Font" fo:font-size="{pt}pt" '
        f'style:font-size-asian="{pt}pt" style:font-size-complex="{pt}pt"{extra}/>'
    )


def _para_style(name: str, display: str, *, indent: tuple[float, float] | None = None,
                pt: float | None = None, extra_para: str = "", extra_text: str = "") -> str:
    para = ""
    if indent is not None:
        left, hang = indent
        para += f' fo:margin-left="{_cm(left)}" fo:text-indent="{_cm(-hang)}"'
    para += extra_para
    out = (f'<style:style style:name="{name}" style:display-name="{_esc(display)}" '
           f'style:family="paragraph" style:parent-style-name="OD_Body" style:class="text">')
    if para:
        out += f"<style:paragraph-properties{para}/>"
    if pt is not None or extra_text:
        out += _text_props(pt or BODY_PT, extra_text)
    return out + "</style:style>"


#: 有「草稿」頁首時：頁首放在上緣的邊界裡，本文起點仍是官方範本的 2.0cm。
#: LibreOffice 的版面：頁面上緣 → margin-top → 頁首 → 本文。
#: ⚠ **`fo:min-height` 是含「與本文的間距」的總高度**（實測：寫 0.6cm ＋ 間距 0.4cm，
#: 頁首實際只佔 0.82cm＝字高 0.42 ＋ 間距 0.4，本文往上跑了 1.8mm）。
#: 所以 min-height 直接寫總高度 1.0cm：1.0 ＋ 1.0 ＝ 2.0cm。
_HEADER_TOP_CM = 1.0
_HEADER_HEIGHT_CM = 1.0
_HEADER_GAP_CM = 0.4


def _hang12(n: int) -> str:
    """12pt 的字懸掛 n 個字（函的發文欄位：「發文日期：」5 字、「速別：」3 字…）。"""
    w = n * 12 * 2.54 / 72
    return f' fo:margin-left="{w:.4f}cm" fo:text-indent="{-w:.4f}cm"'


def _page_layout(draft_mark: bool, extras: PageExtras = NO_EXTRAS) -> str:
    m = PAGE_MARGINS_CM
    has_header = draft_mark or extras.header_items
    top = _HEADER_TOP_CM if has_header else m["top"]
    # 頁尾放在下緣的邊界裡（跟頁首同一套）：本文的下緣不動
    bottom = m["bottom"] - _FOOTER_HEIGHT_CM if extras.page_numbers else m["bottom"]
    out = (
        '<style:page-layout style:name="OD_Page">'
        '<style:page-layout-properties fo:page-width="21cm" fo:page-height="29.7cm" '
        'style:print-orientation="portrait" '
        f'fo:margin-top="{top}cm" fo:margin-bottom="{bottom}cm" '
        f'fo:margin-left="{m["left"]}cm" fo:margin-right="{m["right"]}cm" '
        'style:writing-mode="lr-tb"/>'
    )
    if has_header:
        out += (
            '<style:header-style><style:header-footer-properties '
            f'fo:min-height="{_HEADER_HEIGHT_CM}cm" '
            f'fo:margin-bottom="{_HEADER_GAP_CM}cm" style:dynamic-spacing="false"/>'
            '</style:header-style>'
        )
    if extras.page_numbers:
        out += (
            '<style:footer-style><style:header-footer-properties '
            f'fo:min-height="{_FOOTER_HEIGHT_CM}cm" '
            f'fo:margin-top="{_FOOTER_GAP_CM}cm" style:dynamic-spacing="false"/>'
            '</style:footer-style>'
        )
    return out + "</style:page-layout>"


def _styles_xml(font_family: str, draft_mark: bool, extras: PageExtras = NO_EXTRAS) -> str:
    items = "".join(
        _para_style(f"OD_Item{lvl}", f"公文項次{lvl}", indent=ITEM_INDENT[lvl])
        for lvl in (1, 2, 3, 4)
    )
    header = ""
    if draft_mark or extras.header_items:
        m = PAGE_MARGINS_CM
        # 頁首段落在頁面 1.0cm 處；本文從 2.0cm 到「頁高 − 下邊界」
        rel_y1 = _HEADER_HEIGHT_CM
        rel_y2 = 29.7 - m["bottom"] - _HEADER_TOP_CM
        header = ('<style:header><text:p text:style-name="OD_Header">'
                  + ("草稿" if draft_mark else "")
                  + _header_inner(extras, rel_y1=rel_y1, rel_y2=rel_y2)
                  + '</text:p></style:header>')
    footer = _footer_xml(extras)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<office:document-styles {_NS} office:version="{ODF_VERSION}">'
        + _font_decls(font_family)
        + "<office:styles>"
        # 預設：字型、16pt、行高 150%、中文斷行規則（標點不落在行首）。
        # punctuation-wrap="simple"：句末標點**不懸掛**到右邊界外 —— 懸掛時「。」落在版心外，
        # Writer 畫面上常常看不到，看起來像句子少了句號（2026-10-08 使用者看實際檔案）；
        # 不懸掛時行尾那個字連同標點一起移到下一行（Word 的做法）。
        # text-autospace="none"：不在中文與英數之間自動加間距 —— 加了的話 PDF
        # 抽出來的字是「新臺幣180 萬元」，複製、搜尋都對不上原文（實測）。
        '<style:default-style style:family="paragraph">'
        '<style:paragraph-properties fo:line-height="150%" style:line-break="strict" '
        'style:punctuation-wrap="simple" style:text-autospace="none" '
        'style:writing-mode="lr-tb"/>'
        + _text_props(BODY_PT, ' style:language-asian="zh" style:country-asian="TW"')
        + "</style:default-style>"
        '<style:style style:name="Standard" style:family="paragraph" style:class="text"/>'
        '<style:style style:name="OD_Body" style:display-name="公文內文" style:family="paragraph" '
        'style:parent-style-name="Standard" style:class="text">'
        '<style:paragraph-properties fo:margin-top="0cm" fo:margin-bottom="0cm" '
        'fo:margin-left="0cm" fo:margin-right="0cm" fo:text-indent="0cm" '
        f'fo:line-height="{LINE_HEIGHT_BODY_CM}cm" fo:text-align="justify"/>'
        + _text_props(BODY_PT)
        + "</style:style>"
        # 簽頭「簽　於○○」：16pt，「簽」字另用 20pt 的字元樣式（見 `_content_xml`）
        + _para_style("OD_Head", "公文抬頭", extra_para=' fo:keep-with-next="always"')
        # 簽的日期靠右、12pt（官方範本「日期：105年8月4日」在右上）
        + _para_style("OD_Date", "公文日期", pt=12,
                      extra_para=' fo:text-align="end" fo:keep-with-next="always"')
        # 函的標題（機關全銜＋「函」）：置中、20pt、與下一段同頁
        + _para_style("OD_Title", "公文函標題", pt=20,
                      extra_para=' fo:text-align="center" fo:margin-bottom="0.2cm" '
                                 'fo:keep-with-next="always"')
        + "".join(_para_style(f"OD_Meta{n}", f"公文欄位{n}", pt=12,
                              extra_para=_hang12(n) + f' fo:line-height="{LINE_HEIGHT_META_CM}cm"')
                  for n in _META_HANGS)
        + _para_style("OD_MetaFile", "公文檔號", pt=10,
                      extra_para=f' fo:margin-left="{RIGHT_BLOCK_CM}cm" fo:line-height="0.5cm"')
        + _para_style("OD_Contact", "公文聯絡資訊", pt=12,
                      extra_para=f' fo:margin-left="{RIGHT_BLOCK_CM}cm" '
                                 f'fo:line-height="{LINE_HEIGHT_META_CM}cm"')
        + _para_style("OD_Receiver", "公文受文者", indent=(4, 4),
                      extra_para=' fo:margin-top="0.3cm"')
        + _para_style("OD_LabelFlat", "公文段名（函）")
        + _para_style("OD_Jingchen", "公文敬陳", indent=(4, 0))
        + _para_style("OD_Label", "公文段名（懸掛）", indent=(LABEL_HANG, LABEL_HANG))
        # 段名單獨一行（「說明：」）—— 不可以跟下面的第一項分到兩頁
        + _para_style("OD_LabelTitle", "公文段名", extra_para=' fo:keep-with-next="always"')
        + items
        + _para_style("OD_Para", "公文段落")
        + _para_style("OD_Ending", "公文結尾")
        # 函：正副本前的空行（空段落，固定高度，不跟著字級變）
        + _para_style("OD_Gap", "公文空行",
                      extra_para=f' fo:line-height="{LINE_HEIGHT_BODY_CM}cm" fo:keep-with-next="always"')
        # 函的署名：上面空出蓋章的位置（約三行）、從版心偏右開始，跟正副本同一頁
        + _para_style("OD_Sign", "公文署名",
                      extra_para=f' fo:margin-top="{SIGN_GAP_CM}cm" fo:margin-left="{SIGN_LEFT_CM}cm"')
        # 署名下一行（簽辦意見的「日期：」）：跟署名同一條左緣，不再空出蓋章的位置
        + _para_style("OD_SignLine", "公文署名續行",
                      extra_para=f' fo:margin-left="{SIGN_LEFT_CM}cm"')
        + _para_style("OD_Header", "公文頁首", pt=10,
                      extra_para=' fo:text-align="end" fo:line-height="100%"',
                      extra_text=' fo:color="#888888"')
        + _furniture_para_styles()
        + _dash_xml()
        + "</office:styles>"
        "<office:automatic-styles>" + _page_layout(draft_mark, extras) + _furniture_styles()
        + "</office:automatic-styles>"
        '<office:master-styles>'
        '<style:master-page style:name="Standard" style:page-layout-name="OD_Page">'
        + header + footer
        + "</style:master-page></office:master-styles>"
        "</office:document-styles>"
    )


def _para_xml(style: str, t: str) -> str:
    if style == "OD_Head" and t.startswith("簽"):
        # 官方範本：「簽」20pt，其餘 16pt
        return (f'<text:p text:style-name="{style}"><text:span text:style-name="OD_HeadBig">'
                f'簽</text:span>{_inline(t[1:])}</text:p>')
    return f'<text:p text:style-name="{style}">{_inline(t)}</text:p>'


def _content_xml(text: str, font_family: str, extras: PageExtras = NO_EXTRAS) -> str:
    paras = _paragraphs(text)
    if extras.receiver_address:
        # 開窗信封：受文者的郵遞區號與地址放在「受文者」上面
        at = next((i for i, (st, _t) in enumerate(paras) if st == "OD_Receiver"), None)
        if at is not None:
            paras[at:at] = [("OD_RecvAddr", line) for line in address_lines(extras.receiver_address)]
    if extras.delegate:
        paras.append(("OD_Delegate", delegate_line(extras)))
    head, tail = endorse_frame_paras(extras)
    paras = head + paras + tail
    body = "".join(_para_xml(style, t) for style, t in paras) \
        or '<text:p text:style-name="OD_Para"/>'
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<office:document-content {_NS} office:version="{ODF_VERSION}">'
        + _font_decls(font_family)
        + '<office:automatic-styles><style:style style:name="OD_HeadBig" style:family="text">'
        + _text_props(20) + "</style:style></office:automatic-styles>"
        "<office:body><office:text>" + body + "</office:text></office:body>"
        "</office:document-content>"
    )


def _meta_xml(title: str) -> str:
    title = _BAD_XML_CHARS.sub("", title or "").strip()
    t = f"<dc:title>{_esc(title)}</dc:title>" if title else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<office:document-meta {_NS} office:version="{ODF_VERSION}">'
        "<office:meta><meta:generator>jt-doc-tools</meta:generator>"
        f"{t}<dc:language>zh-TW</dc:language></office:meta>"
        "</office:document-meta>"
    )


_PARTS = ("content.xml", "styles.xml", "meta.xml")


def _manifest_xml() -> str:
    entries = "".join(
        f'<manifest:file-entry manifest:full-path="{p}" manifest:media-type="text/xml"/>'
        for p in _PARTS
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
        f'manifest:version="{ODF_VERSION}">'
        f'<manifest:file-entry manifest:full-path="/" manifest:version="{ODF_VERSION}" '
        f'manifest:media-type="{_MIME}"/>'
        + entries
        + "</manifest:manifest>"
    )


# ------------------------------------------------------------------ 公開介面

def build_odt(text: str, *, title: str = "", font_family: str = WORD_FONT,
              draft_mark: bool = True, extras: PageExtras = NO_EXTRAS) -> bytes:
    """把公文純文字排成 ODT（位元組）。不需要 Office 引擎。

    `font_family` 是分號清單（例如 `PDF_FONT`）時，先換成第一個裝了的字型
    （`pick_font`）—— 只寫一個名字進檔案。
    """
    import io
    font_family = pick_font(font_family)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        # ODF 規定：mimetype 是第一個檔、不壓縮（讀取端靠固定位移認格式）
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        z.writestr(info, _MIME)
        files = {
            "content.xml": _content_xml(text, font_family, extras),
            "styles.xml": _styles_xml(font_family, draft_mark, extras),
            "meta.xml": _meta_xml(title),
            "META-INF/manifest.xml": _manifest_xml(),
        }
        for name, data in files.items():
            z.writestr(name, data.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def export(text: str, fmt: str, *, title: str = "", draft_mark: bool = True,
           template: Optional[bytes] = None, extras: PageExtras = NO_EXTRAS) -> tuple[bytes, str]:
    """匯出成 ODT / DOCX / PDF，回 `(檔案位元組, media_type)`。

    * odt：直接產生，**不需要 Office 引擎**。
    * docx：ODT（標楷體）→ soffice。
    * pdf：ODT（`PDF_FONT` 經 `pick_font` 換成這台裝了的那一個楷體 / 明體）→ soffice。
    * `template`：範本 .odt 的位元組 —— 改用 `build_from_template` 就地填入。
      範本讀不懂時 `TemplateError` **原樣往外丟**，由呼叫端決定退回內建版面並**講出來**
      （安靜地退回的話，使用者以為套了機關範本，拿到的卻是另一種版面）。

    找不到 soffice 時 `office_convert.OfficeUnavailableError` 原樣往外丟；
    `fmt` 不認得是 `ValueError`。

    ⚠ docx / pdf 會起 soffice（數秒），是**阻塞呼叫** —— async 端點要用
    `asyncio.to_thread(export, ...)`，不可以直接呼叫（會卡住整個事件迴圈）。
    """
    fmt = str(fmt or "").strip().lower()
    if fmt not in FORMATS:
        raise ValueError(f"不支援的匯出格式：{fmt!r}（只收 {', '.join(FORMATS)}）")
    if template is not None:
        # ODT / DOCX 保留範本自己的字型；PDF 換成這台裝了的那一個（範本寫的標楷體多半沒有）
        tfont = pick_font(PDF_FONT) if fmt == "pdf" else ""
        data = build_from_template(text, template, title=title, draft_mark=draft_mark,
                                   font_family=tfont, extras=extras)
        if fmt == "odt":
            return data, MEDIA_TYPES["odt"]
    elif fmt == "odt":
        return build_odt(text, title=title, font_family=WORD_FONT,
                         draft_mark=draft_mark, extras=extras), MEDIA_TYPES["odt"]
    else:
        font = WORD_FONT if fmt == "docx" else PDF_FONT
        data = build_odt(text, title=title, font_family=font, draft_mark=draft_mark, extras=extras)
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "official-doc.odt"
        src.write_bytes(data)
        dst = Path(td) / f"out.{fmt}"
        if fmt == "docx":
            office_convert.convert_to_docx(src, dst)
        else:
            office_convert.convert_to_pdf(src, dst)
        return dst.read_bytes(), MEDIA_TYPES[fmt]


# ------------------------------------------------------------------ 套用範本（第二期）
#
# 管理員下載（或上傳）的範本 —— 官方「筆硯」的函.odt、簽.odt，或機關自己的範本 ——
# **就地填入**：保留範本的頁面設定、字型、頁首頁尾、表格（簽的決行欄），只換文字。
# 本文（主旨到正本之前）用範本裡那幾段當樣板逐段複製；欄位依「欄位名：」找段落填值；
# **範本裡的範例值一律清掉**（「承辦人：王○華」「臺北市政府等」不可以跟著出去），
# 範例性質的句子（「本案依分層負責規定授權業務主管決行」）也拿掉 —— 那是在替使用者主張一件事。

_ODF_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
}
_TP = "{%s}p" % _ODF_NS["text"]
_TH = "{%s}h" % _ODF_NS["text"]
_TSPAN = "{%s}span" % _ODF_NS["text"]
_TSTYLE = "{%s}style-name" % _ODF_NS["text"]
#: 範本裡出現就整句拿掉的範例句（替使用者主張了一件不一定成立的事）
_SAMPLE_SENTENCES = re.compile(r"^本案依分層負責規定")
_PLACEHOLDER_O = re.compile(r"○{2,}|○+[、，]○+")


#: 段落裡出現這些以外的東西（框、圖、表格…）就不算「空白段落」，不可以動
_BLANK_INLINE = {"span", "s", "tab", "line-break", "soft-page-break", "bookmark",
                 "bookmark-start", "bookmark-end"}
_BLOCK_TAGS = {"p", "h", "table", "list", "frame", "section"}
_TAIL_STYLE = "JT_TailTiny"


def _is_blank_para(p) -> bool:
    from lxml import etree
    if "".join(p.itertext()).strip(" \t\u3000\xa0"):
        return False
    return all(etree.QName(e).localname in _BLANK_INLINE for e in p.iterdescendants()
               if isinstance(e.tag, str))


def _trim_trailing_blank(root) -> int:
    """拿掉文件最後的空白段落，回傳處理了幾段。

    範本最後常留著幾行空白（官方「簽」範本最後一段是五個全形空白）—— 草稿一長，那幾行被擠到
    第二頁，就多出一張**只有頁首頁尾的空白頁**（2026-10-08 使用者看預覽圖）。只動文件最尾端、
    不在框或表格裡、而且什麼都沒有的段落；那一節只剩它時不刪（節不可以是空的），改成 1pt 高。
    """
    from lxml import etree
    O, T, S, FO = (_ODF_NS[k] for k in ("office", "text", "style", "fo"))
    body = root.find(".//{%s}text" % O)
    if body is None:
        return 0
    done = 0
    for _ in range(50):
        order = [e for e in body.iter() if isinstance(e.tag, str)]
        last = None
        for e in reversed(order):
            name = etree.QName(e).localname
            if name in ("p", "h"):
                if e.get("{%s}style-name" % T) == _TAIL_STYLE:
                    continue                      # 已經縮成 1pt 的那段：往前找
                if any(etree.QName(a).localname in ("frame", "text-box", "table-cell", "note")
                       for a in e.iterancestors()):
                    continue
                last = e
                break
        if last is None or not _is_blank_para(last):
            break
        # 它後面不可以還有表格、框、有字的段落（那它就不是尾端）
        tail_start = order.index(last) + 1 + sum(1 for _ in last.iterdescendants())
        if any(etree.QName(e).localname in _BLOCK_TAGS - {"section"}
               and e.get("{%s}style-name" % T) != _TAIL_STYLE
               and not any(a is last for a in e.iterancestors())
               for e in order[tail_start:]):
            break
        parent = last.getparent()
        siblings = [c for c in parent if isinstance(c.tag, str) and c is not last
                    and etree.QName(c).localname in _BLOCK_TAGS]
        if siblings:
            parent.remove(last)
            done += 1
            continue
        auto = root.find("{%s}automatic-styles" % O)
        if auto is None:
            auto = etree.SubElement(root, "{%s}automatic-styles" % O)
            root.remove(auto)
            root.insert(1, auto)
        if auto.find("{%s}style[@{%s}name='%s']" % (S, S, _TAIL_STYLE)) is None:
            st = etree.SubElement(auto, "{%s}style" % S)
            st.set("{%s}name" % S, _TAIL_STYLE)
            st.set("{%s}family" % S, "paragraph")
            pp = etree.SubElement(st, "{%s}paragraph-properties" % S)
            for k, v in (("margin-top", "0cm"), ("margin-bottom", "0cm"), ("line-height", "0.04cm")):
                pp.set("{%s}%s" % (FO, k), v)
            tp = etree.SubElement(st, "{%s}text-properties" % S)
            tp.set("{%s}font-size" % FO, "1pt")
            tp.set("{%s}font-size-asian" % S, "1pt")
            tp.set("{%s}font-size-complex" % S, "1pt")
        for c in list(last):
            last.remove(c)
        last.text = None
        last.set("{%s}style-name" % T, _TAIL_STYLE)
        done += 1
    return done


def _derive_para_style(root, p, name: str, para_props: dict) -> None:
    """把段落 `p` 換成一個新的自動樣式：照它原本的樣式、改幾個段落屬性。

    原本是自動樣式（content.xml 裡的 P5 這種）就複製一份改名 —— 自動樣式不能當別人的父樣式；
    原本是一般樣式就新增一個以它為父樣式的自動樣式。同名的已經有了就直接用。"""
    import copy
    from lxml import etree
    O, S, T, FO = (_ODF_NS[k] for k in ("office", "style", "text", "fo"))
    auto = root.find("{%s}automatic-styles" % O)
    if auto is None:
        auto = etree.Element("{%s}automatic-styles" % O)
        root.insert(1, auto)
    if auto.find("{%s}style[@{%s}name='%s']" % (S, S, name)) is None:
        cur = p.get("{%s}style-name" % T) or ""
        src = auto.find("{%s}style[@{%s}name='%s']" % (S, S, cur)) if cur else None
        if src is not None:
            st = copy.deepcopy(src)
            st.set("{%s}name" % S, name)
        else:
            st = etree.Element("{%s}style" % S)
            st.set("{%s}name" % S, name)
            st.set("{%s}family" % S, "paragraph")
            if cur:
                st.set("{%s}parent-style-name" % S, cur)
        pp = st.find("{%s}paragraph-properties" % S)
        if pp is None:
            pp = etree.Element("{%s}paragraph-properties" % S)
            st.insert(0, pp)
        for k, v in para_props.items():
            pp.set("{%s}%s" % (FO, k), v)
        auto.append(st)
    p.set("{%s}style-name" % T, name)


class TemplateError(ValueError):
    """範本讀不進來或不是公文範本 —— 訊息可以給使用者看。"""


def _safe_parser():
    from lxml import etree
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                           huge_tree=False, remove_blank_text=False)


def _ptext_of(p) -> str:
    return "".join(p.itertext()).strip()


def _first_span_style(p) -> Optional[str]:
    for sp in p.iter(_TSPAN):
        if sp.get(_TSTYLE):
            return sp.get(_TSTYLE)
    return None


def _set_text(p, s: str) -> None:
    """換掉一個段落的文字，**保留段落樣式與第一個字元樣式**（官方範本的字級在字元樣式上）。"""
    from lxml import etree
    span_style = _first_span_style(p)
    for child in list(p):
        p.remove(child)
    p.text = None
    if not s:
        return
    inner = _inline(s)
    ns = f'xmlns:text="{_ODF_NS["text"]}"'
    if span_style:
        frag = etree.fromstring(f'<text:span {ns} text:style-name="{_esc(span_style)}">{inner}</text:span>',
                                _safe_parser())
        p.append(frag)
    else:
        frag = etree.fromstring(f'<text:span {ns}>{inner}</text:span>', _safe_parser())
        p.text = frag.text
        for c in list(frag):
            p.append(c)


def _leaf_paras(body) -> list:
    """**只取最內層的段落**：官方範本把檔號、聯絡資訊、簽頭放在文字框（draw:frame）裡，
    外面再包一個段落 —— 那個外層段落的文字是所有框的字接起來，改它會把裡面的框整個清掉
    （第一版就是這樣把簽頭、承辦人、電話弄不見的）。"""
    out = []
    for p in body.iter(_TP, _TH):
        if any(d is not p and d.tag in (_TP, _TH) for d in p.iter()):
            continue
        out.append(p)
    return out


def _key_of(t: str) -> str:
    m = re.match(r"^([^：:]{1,14})[：:]", t)
    return re.sub(r"[\s　]", "", m.group(1)) if m else ""


def build_from_template(text: str, template: bytes, *, title: str = "",
                        draft_mark: bool = True, font_family: str = "",
                        extras: PageExtras = NO_EXTRAS) -> bytes:
    """把草稿填進範本（.odt），回新的 .odt。範本讀不懂就丟 `TemplateError`（呼叫端退回內建版面）。"""
    import copy
    from lxml import etree
    from .zip_guard import check as _zip_check
    try:
        zin = zipfile.ZipFile(io.BytesIO(template))
        _zip_check(zin)
        root = etree.fromstring(zin.read("content.xml"), _safe_parser())
        sroot = etree.fromstring(zin.read("styles.xml"), _safe_parser())
    except (zipfile.BadZipFile, KeyError, etree.XMLSyntaxError, ValueError) as e:
        raise TemplateError("範本讀不進來（不是有效的 ODT）。") from e
    body = root.find(".//office:text", _ODF_NS)
    if body is None:
        raise TemplateError("範本裡沒有文字內容。")
    paras = _leaf_paras(body)
    texts = [_ptext_of(p) for p in paras]
    blocks = parse_text(text)
    filled: set = set()           # 我們填過的段落 —— 清範本殘留的範例時不可以碰

    def put(p, s: str) -> None:
        _set_text(p, s)
        filled.add(id(p))

    def find(pred, start: int = 0):
        for i in range(start, len(paras)):
            if pred(texts[i]):
                return i
        return None

    anchor = find(lambda t: t.startswith("主旨"))
    if anchor is None:
        raise TemplateError("範本裡找不到「主旨：」那一段，不像是公文範本。")
    is_letter = any(b["kind"] == "title" for b in blocks)

    # ---- 欄位：依「欄位名：」填值；範本有、草稿沒有的清成空值（不讓範例值跟出去）
    ours = {}
    for b in blocks:
        if b["kind"] == "meta":
            ours.setdefault(b["key"], []).append(b["text"])
    contact_keys = set(_META_CONTACT_KEYS)
    tmpl_contact = [i for i, t in enumerate(texts[:anchor]) if _key_of(t) in contact_keys]
    zone = _contact_zone(blocks)
    our_contact = [blocks[i]["text"] for i in sorted(zone)]
    for i, t in enumerate(texts):
        k = _key_of(t)
        if not k or k in contact_keys or i in tmpl_contact:
            continue
        if k in ("主旨", "說明", "擬辦", "辦法", "日期"):
            continue
        label = t.split("：", 1)[0].split(":", 1)[0]
        has_value = bool(re.split(r"[：:]", t, 1)[1].strip()) if re.search(r"[：:]", t) else False
        if k in ours:
            mine = ours[k][0]
            mine_value = re.split(r"[：:]", mine, 1)[1].strip() if re.search(r"[：:]", mine) else mine
            if mine_value:
                put(paras[i], f"{label}：{mine_value}")      # 欄位名照範本的寫法
            elif has_value:
                put(paras[i], f"{label}：")                  # 範本的範例值清掉
        elif is_letter and k in _AGENCY_ONLY_KEYS:
            # 草稿沒有這一欄＝企業發的函（機關的函一定有，值是空的）：檔號、保存年限、密等是
            # 機關的檔案管理欄位，企業的函不寫 —— 連欄位名一起拿掉，不留一個空的「檔號：」
            put(paras[i], "")
        elif has_value and k in ("檔號", "保存年限", "受文者", "發文日期", "發文字號", "速別",
                                 "密等及解密條件或保密期限", "附件", "正本", "副本", "會辦單位"):
            put(paras[i], f"{label}：")
    # 聯絡資訊：依序放進範本的聯絡欄位，多的複製最後一段、少的拿掉
    if tmpl_contact:
        last = paras[tmpl_contact[-1]]
        for n, i in enumerate(tmpl_contact):
            if n < len(our_contact):
                put(paras[i], our_contact[n])
            else:
                paras[i].getparent().remove(paras[i])
        for line in our_contact[len(tmpl_contact):]:
            p = copy.deepcopy(last)
            put(p, line)
            last.addnext(p)
            last = p
    # 函的標題
    tb = next((b for b in blocks if b["kind"] == "title"), None)
    ti = find(lambda t: bool(_TITLE_LIKE.match(t)))
    if tb and ti is not None and ti < anchor:
        put(paras[ti], tb["text"])
    # 範本沒有聯絡欄位（機關自己的範本常這樣）：聯絡資訊接在標題下面、排在右半邊，
    # 不可以就這樣不見（地址、承辦人、電話是對方回覆要用的）
    if our_contact and not tmpl_contact:
        ri = find(lambda t: t.startswith("受文者"))
        at = paras[ti] if (ti is not None and ti < anchor) else (paras[ri] if ri is not None else None)
        proto = paras[ri] if ri is not None else paras[anchor]
        if at is not None:
            after_title = ti is not None and ti < anchor
            prev = at
            for line in our_contact:
                q = copy.deepcopy(proto)
                put(q, line)
                _derive_para_style(root, q, "JT_ContactR",
                                   {"margin-left": f"{RIGHT_BLOCK_CM}cm", "text-indent": "0cm",
                                    "margin-top": "0cm", "margin-bottom": "0cm", "text-align": "start"})
                if after_title:
                    prev.addnext(q)          # 標題下面，照順序往下接
                    prev = q
                else:
                    at.addprevious(q)        # 沒有標題：放在受文者上面，照順序
    # 簽：單位（「於」前一段）與日期
    head = next((b for b in blocks if b["kind"] == "head"), None)
    yu = find(lambda t: t == "於")
    if head and yu is not None and yu > 0 and yu < anchor:
        unit = re.sub(r"^簽[\s　]*於[\s　]*", "", head["text"])
        put(paras[yu - 1], unit)
    date_b = next((b for b in blocks if b["kind"] == "date"), None)
    di = find(lambda t: t.startswith("日期"))
    if di is not None and di < anchor:
        d = re.sub(r"^中華民國", "", date_b["text"]) if date_b else ""
        put(paras[di], f"日期：{d}")

    # ---- 本文：主旨到（函）正本／（簽）敬陳之前，用範本那幾段當樣板逐段放
    end = find(lambda t: t.startswith("正本") or t == "敬陳" or t.startswith("敬陳"), anchor + 1)
    end_el = paras[end] if end is not None else None
    proto_label = paras[anchor]
    li = find(lambda t: re.fullmatch(r"(說明|擬辦|辦法)[：:]", t) is not None, anchor + 1)
    proto_title = paras[li] if li is not None else proto_label
    i1 = find(lambda t: re.match(r"^[一二三四五六七八九十]{1,2}、", t) is not None, anchor + 1)
    i2 = find(lambda t: re.match(r"^[（(][一二三四五六七八九十]{1,2}[）)]", t) is not None, anchor + 1)
    proto_i1 = paras[i1] if i1 is not None else proto_label
    proto_i2 = paras[i2] if i2 is not None else proto_i1
    ni = find(lambda t: t.startswith("擬辦"), anchor + 1)
    proto_ni = paras[ni] if ni is not None and paras[ni] is not proto_title else proto_label
    parent = proto_label.getparent()
    stop = end if end is not None else len(paras)
    to_remove = [paras[i] for i in range(anchor, stop)
                 if paras[i].getparent() is parent]
    # 結尾的空白間距段（主旨區塊與正本 / 敬陳之間的空行）留著
    while len(to_remove) > 1 and not _ptext_of(to_remove[-1]):
        to_remove.pop()
    protos = {id(x): copy.deepcopy(x) for x in (proto_label, proto_title, proto_i1, proto_i2, proto_ni)}
    insert_at = to_remove[0]
    marker = etree.SubElement(parent, _TP)
    insert_at.addprevious(marker)
    for el in to_remove:
        parent.remove(el)
    body_blocks = [b for n, b in enumerate(blocks)
                   if b["kind"] in ("label", "item", "para") and n not in zone]
    cur = marker
    for b in body_blocks:
        if b["kind"] == "label":
            src = proto_ni if b["label"] == "擬辦" and b["text"] else (proto_label if b["text"] else proto_title)
            s = f"{b['label']}：{b['text']}"
        elif b["kind"] == "item":
            src = proto_i1 if int(b.get("level") or 1) == 1 else proto_i2
            s = _display_marker(b.get("marker") or "") + b["text"]
        else:
            src = proto_i1
            s = b["text"]
        p = copy.deepcopy(protos[id(src)])
        put(p, s)
        cur.addnext(p)
        cur = p
    parent.remove(marker)

    # ---- 結尾：函的署名（正副本之後第一個有字的段）、簽的陳核對象（敬陳之後）
    endings = [b["text"] for b in blocks if b["kind"] == "ending"]
    paras = _leaf_paras(body)
    texts = [_ptext_of(p) for p in paras]
    if is_letter:
        ci = max([i for i, t in enumerate(texts) if t.startswith(("正本", "副本"))] or [-1])
        sig = [i for i in range(ci + 1, len(paras)) if texts[i] and not _SAMPLE_SENTENCES.match(texts[i])]
        if sig:
            put(paras[sig[0]], endings[0] if endings else "")
            # 署名上面留用印的空間（2026-10-08 使用者）：左右位置照範本，只補上方的距離；
            # 範本自己在正副本與署名之間留了空行的，扣掉那幾行（每行約 0.6 公分）
            if ci >= 0 and endings:
                blanks = sum(1 for i in range(ci + 1, sig[0]) if not texts[i])
                gap = round(SIGN_GAP_CM - 0.6 * blanks, 2)
                if gap > 0.2:
                    _derive_para_style(root, paras[sig[0]], "JT_SignGap", {"margin-top": f"{gap}cm"})
    else:
        ji = find(lambda t: t == "敬陳" or t.startswith("敬陳"))
        if ji is not None:
            addr = [e for e in endings if e != "敬陳"]
            tail = [i for i in range(ji + 1, len(paras))
                    if texts[i] and not _key_of(texts[i]) and "決行" not in texts[i]
                    and "單位" not in texts[i]]
            for n, i in enumerate(tail):
                put(paras[i], addr[n] if n < len(addr) else "")
            if len(addr) > len(tail) and tail:
                last = paras[tail[-1]]
                for line in addr[len(tail):]:
                    p = copy.deepcopy(last)
                    put(p, line)
                    last.addnext(p)
                    last = p
    # ---- 範本殘留的範例：○○○ 佔位、範例句。「本案依分層負責規定…」是範例，**清掉**；
    # 使用者選了分層負責決行，就用範本那一段的格式寫使用者選的那一句（找不到那一段就接在最後）
    dline = delegate_line(extras) if is_letter else ""
    for p in _leaf_paras(body):
        if id(p) in filled:
            continue
        t = _ptext_of(p)
        if _SAMPLE_SENTENCES.match(t.strip()):
            put(p, dline)
            dline = ""
        elif _PLACEHOLDER_O.search(t):
            _set_text(p, _PLACEHOLDER_O.sub("", t))
    if dline:
        last = _leaf_paras(body)[-1]
        p = copy.deepcopy(last)
        put(p, dline)
        last.addnext(p)
    # 開窗信封：受文者的郵遞區號與地址，用範本「速別」那一段的格式放在「受文者」上面
    if is_letter and extras.receiver_address:
        cur = _leaf_paras(body)
        ri = next((i for i, q in enumerate(cur) if _ptext_of(q).startswith("受文者")), None)
        proto = next((q for q in cur if _ptext_of(q).startswith("速別")), None)
        if ri is not None:
            for line in address_lines(extras.receiver_address):
                q = copy.deepcopy(proto if proto is not None else cur[ri])
                put(q, line)
                cur[ri].addprevious(q)

    _loosen_frames(root)

    # ---- 字型（產 PDF 時換成這台有的）與「草稿」頁首
    def _swap_fonts(r):
        if not font_family:
            return
        for ff in r.iter("{%s}font-face" % _ODF_NS["style"]):
            if "楷" in (ff.get("{%s}font-family" % _ODF_NS["svg"]) or "") or "楷" in (ff.get("{%s}name" % _ODF_NS["style"]) or ""):
                ff.set("{%s}font-family" % _ODF_NS["svg"], _font_family_attr(font_family))
    _swap_fonts(root)
    _swap_fonts(sroot)
    _add_template_page_extras(sroot, draft_mark, extras, body_fonts=_template_body_fonts(root))

    # ---- 中繼資料與縮圖：**範本的不跟出去**。機關範本的 meta.xml 常帶著範本作者、
    # 原機關名稱；縮圖是範本範例內容的畫面（檔案總管會顯示成「臺北市政府 函」那一頁）。
    MF = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
    try:
        man = etree.fromstring(zin.read("META-INF/manifest.xml"), _safe_parser())
    except (KeyError, etree.XMLSyntaxError):
        man = etree.fromstring(_manifest_xml().encode("utf-8"), _safe_parser())
    if not man.get("{%s}version" % MF):
        man.set("{%s}version" % MF, ODF_VERSION)
    paths = []
    for fe in list(man):
        fp = fe.get("{%s}full-path" % MF) or ""
        if fp.startswith("Thumbnails/"):
            man.remove(fe)
        else:
            paths.append(fp)
    # 範本的清單不完整時補齊 —— soffice 對缺根項目或缺 content.xml 的清單直接說「讀不進來」
    for fp, mt in (("/", _MIME), ("content.xml", "text/xml"), ("styles.xml", "text/xml"),
                   ("meta.xml", "text/xml")):
        if fp not in paths:
            fe = etree.SubElement(man, "{%s}file-entry" % MF)
            fe.set("{%s}full-path" % MF, fp)
            fe.set("{%s}media-type" % MF, mt)
            if fp == "/":
                fe.set("{%s}version" % MF, ODF_VERSION)

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zout:
        zout.writestr(zipfile.ZipInfo("mimetype"), _MIME, compress_type=zipfile.ZIP_STORED)
        for info in zin.infolist():
            if info.filename in ("mimetype", "content.xml", "styles.xml", "meta.xml",
                                 "META-INF/manifest.xml") or info.filename.startswith("Thumbnails/"):
                continue
            zout.writestr(info, zin.read(info.filename), compress_type=zipfile.ZIP_DEFLATED)
        zout.writestr("meta.xml", _meta_xml(title), compress_type=zipfile.ZIP_DEFLATED)
        zout.writestr("META-INF/manifest.xml",
                      etree.tostring(man, xml_declaration=True, encoding="UTF-8"),
                      compress_type=zipfile.ZIP_DEFLATED)
        _trim_trailing_blank(root)
        zout.writestr("content.xml", etree.tostring(root, xml_declaration=True, encoding="UTF-8"),
                      compress_type=zipfile.ZIP_DEFLATED)
        zout.writestr("styles.xml", etree.tostring(sroot, xml_declaration=True, encoding="UTF-8"),
                      compress_type=zipfile.ZIP_DEFLATED)
    return out.getvalue()


def _loosen_frames(root) -> None:
    """範本的文字框：底色改透明、固定高度改成「至少這麼高」。

    2026-10-08 使用者回報「有些字被截斷」：官方「簽」範本的檔號、會辦單位、決行表的欄名都是
    **固定高度的文字框**，而且上下相鄰的框故意疊一點點。範本照「標楷體」排，這台換成別的楷體
    （PDF 用 `pick_font` 挑的那一套）字比較高，字的下緣就伸進下一個框 —— **下一個框的白色底色
    後畫、把它蓋掉**（PDF 裡看得到：字畫完之後有一塊白色矩形壓在它上面）。
    框的底色沒有人需要（紙是白的），透明之後重疊的框不會再互相蓋；高度改成最小高度，
    內容較長（承辦單位名稱很長）時框跟著長、不會被切。位置與寬度都不動。
    """
    D, S, FO, SVG = ("urn:oasis:names:tc:opendocument:xmlns:drawing:1.0", _ODF_NS["style"],
                     _ODF_NS["fo"], _ODF_NS["svg"])
    names = set()
    for fr in root.iter("{%s}frame" % D):
        box = fr.find("{%s}text-box" % D)
        if box is None:
            continue                      # 圖片框不動
        if fr.get("{%s}style-name" % D):
            names.add(fr.get("{%s}style-name" % D))
        h = fr.get("{%s}height" % SVG)
        if h and not box.get("{%s}min-height" % FO):
            box.set("{%s}min-height" % FO, h)
            del fr.attrib["{%s}height" % SVG]
    auto = root.find("office:automatic-styles", _ODF_NS)
    if auto is None:
        return
    from lxml import etree
    for st in auto.findall("style:style", _ODF_NS):
        if st.get("{%s}family" % S) != "graphic" or st.get("{%s}name" % S) not in names:
            continue
        gp = st.find("style:graphic-properties", _ODF_NS)
        if gp is None:
            gp = etree.SubElement(st, "{%s}graphic-properties" % S)
        gp.set("{%s}fill" % D, "none")
        gp.set("{%s}background-color" % FO, "transparent")
        gp.set("{%s}background-transparency" % S, "100%")


def _to_cm(v: str) -> Optional[float]:
    m = re.fullmatch(r"\s*([0-9.]+)\s*(cm|mm|in|pt)\s*", v or "")
    if not m:
        return None
    n = float(m.group(1))
    return {"cm": n, "mm": n / 10, "in": n * 2.54, "pt": n * 2.54 / 72}[m.group(2)]


def _template_body_fonts(root) -> dict:
    """範本本文用的字型（內文段落最常用的那一個；西文與中文各一）。

    官方「簽」「函」範本的預設樣式**沒有任何字型**，字型是寫在每一段本文的自動樣式上
    （「標楷體」）。我們加上去的「草稿」、裝訂線的字、頁碼要跟本文同一個字型 ——
    跟著預設樣式走的話，轉 PDF 時會退到 Office 自己的預設黑體（2026-10-09 使用者截圖：
    「左邊裝訂線跟右上草稿兩字的字型是不是正確」）。回 `{"western": 名稱, "asian": 名稱,
    "faces": {名稱: font-face 元素}}`；範本沒寫字型就回空的（照舊跟著預設）。"""
    import collections
    S = _ODF_NS["style"]
    faces = {ff.get("{%s}name" % S): ff for ff in root.iter("{%s}font-face" % S) if ff.get("{%s}name" % S)}
    cnt = {"western": collections.Counter(), "asian": collections.Counter()}
    for tp in root.iter("{%s}text-properties" % S):
        w = tp.get("{%s}font-name" % S)
        a = tp.get("{%s}font-name-asian" % S)
        if w in faces:
            cnt["western"][w] += 1
        if a in faces:
            cnt["asian"][a] += 1
    out: dict = {"faces": {}}
    for k in ("western", "asian"):
        if cnt[k]:
            name = cnt[k].most_common(1)[0][0]
            out[k] = name
            out["faces"][name] = faces[name]
    if "asian" in out and "western" not in out:
        out["western"] = out["asian"]
    return out


def _add_template_page_extras(sroot, draft_mark: bool, extras: PageExtras,
                              body_fonts: Optional[dict] = None) -> None:
    """範本的頁面設定加上頁首（「草稿」、裝訂線、正本／副本、發文方式）與頁尾（頁碼）。

    **只在頁面設定裡加 `header-style` / `footer-style` 才會畫出來** —— 第一版只在 master-page
    底下塞 `<style:header>`，官方範本的 page-layout 沒有 header-style，LibreOffice 就整個不畫
    （算圖看得到：頁面上沒有「草稿」）。頁首、頁尾放在上下緣的邊界裡（跟內建版面同一套），
    本文起點與底線不跟著移；邊界不夠放就照樣加（本文往內縮一點，總比沒有好）。
    範本自己已經有頁首（或頁尾）的，那一邊不動 —— 不蓋掉機關自己的版面。"""
    import copy
    from lxml import etree
    S, FO = _ODF_NS["style"], _ODF_NS["fo"]
    need_header = draft_mark or extras.header_items
    if not (need_header or extras.page_numbers):
        return
    mp = sroot.find(".//office:master-styles/style:master-page", _ODF_NS)
    if mp is None:
        return
    has_header = mp.find("style:header", _ODF_NS) is not None
    has_footer = mp.find("style:footer", _ODF_NS) is not None
    pl_name = mp.get("{%s}page-layout-name" % S)
    pl = None
    for cand in sroot.iter("{%s}page-layout" % S):
        if cand.get("{%s}name" % S) == pl_name:
            pl = cand
            break
    props = pl.find("style:page-layout-properties", _ODF_NS) if pl is not None else None

    def margin(k: str, default: float) -> float:
        v = _to_cm(props.get("{%s}%s" % (FO, k), "")) if props is not None else None
        return default if v is None else v

    page_h = margin("page-height", 29.7)
    top0, bottom0 = margin("margin-top", PAGE_MARGINS_CM["top"]), margin("margin-bottom", PAGE_MARGINS_CM["bottom"])

    # 樣式：段落樣式與虛線放 office:styles，圖形樣式放 automatic-styles（頁首頁尾在 styles.xml 裡）
    frag = etree.fromstring(
        f'<x {_NS}><office:styles>{_furniture_para_styles()}{_dash_xml()}'
        f'<style:style style:name="OD_TplHeader" style:family="paragraph">'
        '<style:paragraph-properties fo:text-align="end" fo:line-height="100%"/>'
        '<style:text-properties fo:font-size="10pt" fo:color="#888888"/></style:style>'
        f'</office:styles><office:automatic-styles>{_furniture_styles()}</office:automatic-styles></x>',
        _safe_parser())
    # 範本沒有宣告我們的字型（OD_Font）：換成範本本文用的字型（字級留著）。
    # 範本本文沒寫字型才拿掉名稱、跟著範本預設走。
    bf = body_fonts or {}
    for tp in list(frag.iter("{%s}text-properties" % S)):
        for k in ("font-name", "font-name-asian", "font-name-complex"):
            tp.attrib.pop("{%s}%s" % (S, k), None)
        if bf.get("asian"):
            tp.set("{%s}font-name" % S, bf["western"])
            tp.set("{%s}font-name-asian" % S, bf["asian"])
            tp.set("{%s}font-name-complex" % S, bf["asian"])
    if bf.get("faces"):
        # styles.xml 的樣式只認 styles.xml 自己宣告的字型（範本只在 content.xml 宣告）
        decls = sroot.find("office:font-face-decls", _ODF_NS)
        if decls is None:
            decls = etree.Element("{%s}font-face-decls" % _ODF_NS["office"])
            first = next((el for el in sroot if el.tag in (
                "{%s}styles" % _ODF_NS["office"], "{%s}automatic-styles" % _ODF_NS["office"],
                "{%s}master-styles" % _ODF_NS["office"])), None)
            if first is not None:
                first.addprevious(decls)
            else:
                sroot.append(decls)
        have = {ff.get("{%s}name" % S) for ff in decls}
        for name, ff in bf["faces"].items():
            if name not in have:
                decls.append(copy.deepcopy(ff))
    for part in ("office:styles", "office:automatic-styles"):
        dst = sroot.find(part, _ODF_NS)
        if dst is None:
            dst = etree.SubElement(sroot, "{%s}%s" % (_ODF_NS["office"], part.split(":")[1]))
        have = {el.get("{%s}name" % S) or el.get("{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}name")
                for el in dst}
        for el in frag.find(part, _ODF_NS):
            name = el.get("{%s}name" % S) or el.get("{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}name")
            if name not in have:
                dst.append(el)

    if need_header and not has_header:
        top = top0
        if pl is not None and pl.find("style:header-style", _ODF_NS) is None:
            if props is not None and top0 - _HEADER_HEIGHT_CM >= 0.5:
                top = top0 - _HEADER_HEIGHT_CM
                props.set("{%s}margin-top" % FO, f"{top:.2f}cm")
            hs = etree.SubElement(pl, "{%s}header-style" % S)
            hp = etree.SubElement(hs, "{%s}header-footer-properties" % S)
            hp.set("{%s}min-height" % FO, f"{_HEADER_HEIGHT_CM}cm")
            hp.set("{%s}margin-bottom" % FO, f"{_HEADER_GAP_CM}cm")
            hp.set("{%s}dynamic-spacing" % S, "false")
        rel_y1 = top0 - top if top0 - top > 0 else _HEADER_HEIGHT_CM
        rel_y2 = page_h - bottom0 - top
        hdr = etree.fromstring(
            f'<style:header {_NS}><text:p text:style-name="OD_TplHeader">{"草稿" if draft_mark else ""}'
            + _header_inner(extras, rel_y1=rel_y1, rel_y2=rel_y2) + '</text:p></style:header>',
            _safe_parser())
        mp.insert(0, hdr)
    if extras.page_numbers and not has_footer:
        if pl is not None and pl.find("style:footer-style", _ODF_NS) is None:
            if props is not None and bottom0 - _FOOTER_HEIGHT_CM >= 0.5:
                props.set("{%s}margin-bottom" % FO, f"{bottom0 - _FOOTER_HEIGHT_CM:.2f}cm")
            fs = etree.SubElement(pl, "{%s}footer-style" % S)
            fp = etree.SubElement(fs, "{%s}header-footer-properties" % S)
            fp.set("{%s}min-height" % FO, f"{_FOOTER_HEIGHT_CM}cm")
            fp.set("{%s}margin-top" % FO, f"{_FOOTER_GAP_CM}cm")
            fp.set("{%s}dynamic-spacing" % S, "false")
        mp.append(etree.fromstring(_footer_xml(extras).replace("<style:footer>", f"<style:footer {_NS}>", 1),
                                   _safe_parser()))


#: 範本裡的函標題（「臺北市政府　函」）
_TITLE_LIKE = re.compile(r"^[^，。：:；、]{1,40}?[\s　]*函$")
