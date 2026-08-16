"""辦公文件格式目錄 —— 這台機器上的 soffice **實際**支援哪些匯出格式。

## 為什麼要去問 soffice，不自己寫一份清單

`--convert-to <ext>:<濾鏡名稱>` 裡的濾鏡名稱是 soffice 自己註冊的字串。
不同發行版（OxOffice / LibreOffice）、不同版本、不同安裝選項（有沒有裝
Impress 模組）支援的濾鏡並不一樣。而 **soffice 拿到不認得的濾鏡名稱時
不會報錯，只是產不出檔案**（實測回傳碼 0）—— 所以清單寫死的話，使用者
會在畫面上看到一個選得到、按下去卻永遠失敗的目標格式。

因此這裡直接讀安裝目錄的 `share/registry/*.xcd`：那是 soffice 自己的
濾鏡註冊表，`Flags` 含 `EXPORT` 的就是真的匯得出來。

## 版本選擇是真的，不是換個名字

同一個副檔名確實會有多支濾鏡，而且產出**實質不同**（在 OxOffice 11 上實測
`.docx` 兩支的差異）：

| | `MS Word 2007 XML` | `Office Open XML Text` |
|---|---|---|
| 對齊屬性 | `w:jc val="left"` | `val="start"`（strict 寫法） |
| 字型表 | 無 `characterSet` | 有 `w:characterSet` |
| 相容模式 | `compatibilityMode 12` | `15` |

舊系統只吃相容模式 12，所以這個選項要留給使用者。

## 只做「同類互轉」

文書檔↔文書檔、試算表↔試算表、簡報↔簡報。**跨類是做不到的**
（試算表轉不成文書檔），把它列出來只會讓人按下去才發現。
轉 PDF 也不在這裡 —— 那是「辦公文件轉 PDF」的工作，重複做兩份
只會讓兩邊的行為慢慢不一致。

家族刻意只有三個，對齊首頁「本站的格式用語」講的四類（文書檔 / 試算表 /
簡報 / PDF）。Draw 的 `.odg` 雖然 soffice 也支援，但站上沒有「繪圖檔」
這個說法，加進來要連同首頁的用語一起改，屬於另一件事。
"""
from __future__ import annotations

import functools
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import office_convert

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Target:
    """一個可選的目標格式。"""

    id: str          #: 穩定識別碼，前端送回來的就是這個（例：``docx-2007``）
    ext: str         #: 副檔名，不含點
    filter: str      #: soffice 濾鏡名稱
    label: str       #: 給人看的名稱（例：``Word 2007``）
    note: str = ""   #: 補充說明，會顯示在選項下方
    common: bool = False  #: 是否列在「常用」區


@dataclass(frozen=True)
class Family:
    """一個文件家族（文書檔 / 試算表 / 簡報）。"""

    id: str
    name: str
    service: str                 #: soffice 的 DocumentService
    sources: tuple[str, ...]     #: 可以收的來源副檔名
    targets: tuple[Target, ...]

    def target(self, target_id: str) -> Optional[Target]:
        return next((t for t in self.targets if t.id == target_id), None)


_FAMILIES: tuple[tuple[str, str, str], ...] = (
    ("text", "文書檔", "TextDocument"),
    ("sheet", "試算表", "SpreadsheetDocument"),
    ("slides", "簡報", "PresentationDocument"),
)

_SERVICE_OF = {fam: svc for fam, _name, svc in _FAMILIES}

#: 濾鏡名稱 → (家族, 穩定 id, 副檔名, 顯示名稱, 說明, 是否常用)
#:
#: **副檔名要寫明，不可以從 id 推導** —— `doc-xml2003` 的副檔名是 `.xml`
#: 不是 `.doc`，用 id 前綴推會推錯，而且錯了是無聲的（產出檔名不對）。
#:
#: 只有列在這裡的才會有中文說明與穩定 id。沒列到但 soffice 支援的，
#: `_auto_target()` 會自動收成「其他格式」—— 使用者要的是「soffice 支援的
#: 都可以選」，不在這份表裡不代表不能選，只是沒有額外說明。
_CURATED: dict[str, tuple[str, str, str, str, str, bool]] = {
    # ---- 文書檔 ----
    "writer8": ("text", "odt", "odt", "ODF 文字文件",
                "LibreOffice / OxOffice 原生格式", True),
    "MS Word 2007 XML": ("text", "docx-2007", "docx", "Word 2007",
                         "相容模式 12，較舊的系統與轉檔服務接受度高", True),
    "Office Open XML Text": ("text", "docx-365", "docx", "Word 2010–365",
                             "相容模式 15，Microsoft 365 的預設寫法", True),
    "MS Word 97": ("text", "doc", "doc", "Word 97–2003",
                   "二進位 .doc，給很舊的系統", True),
    "Rich Text Format": ("text", "rtf", "rtf", "RTF 格式",
                         "純文字結構，幾乎所有文書軟體都讀得到", True),
    "Text": ("text", "txt", "txt", "純文字",
             "只留文字，格式與圖片會全部消失", True),
    "HTML (StarWriter)": ("text", "html", "html", "HTML 網頁", "", False),
    "EPUB": ("text", "epub", "epub", "EPUB 電子書", "", False),
    "OpenDocument Text Flat XML": ("text", "fodt", "fodt", "ODF 單一檔 XML",
                                   "不壓縮，方便版本比對", False),
    "MS Word 2003 XML": ("text", "doc-xml2003", "xml", "Word 2003 XML", "",
                         False),
    "MS Word 2007 XML VBA": ("text", "docm", "docm", "Word 巨集啟用", "", False),
    "writer8_template": ("text", "ott", "ott", "ODF 文字範本", "", False),
    "MS Word 2007 XML Template": ("text", "dotx", "dotx", "Word 2007 範本", "",
                                  False),
    "MS Word 97 Vorlage": ("text", "dot", "dot", "Word 97–2003 範本", "", False),
    "Office Open XML Text Template": ("text", "dotx-365", "dotx",
                                      "Word 2010–365 範本", "", False),
    "DocBook File": ("text", "docbook", "xml", "DocBook", "", False),

    # ---- 試算表 ----
    "calc8": ("sheet", "ods", "ods", "ODF 試算表",
              "LibreOffice / OxOffice 原生格式", True),
    "Calc MS Excel 2007 XML": ("sheet", "xlsx-2007", "xlsx", "Excel 2007–365",
                               "最通用的 .xlsx 寫法", True),
    "Calc Office Open XML": ("sheet", "xlsx-ooxml", "xlsx",
                             "Office Open XML 試算表",
                             "嚴格版寫法，少數系統才需要", True),
    "MS Excel 97": ("sheet", "xls", "xls", "Excel 97–2003",
                    "二進位 .xls，上限 65,536 列", True),
    "Text - txt - csv (StarCalc)": ("sheet", "csv", "csv", "CSV 逗號分隔",
                                    "只留第一張工作表的值，公式與格式會消失",
                                    True),
    "HTML (StarCalc)": ("sheet", "html-calc", "html", "HTML 網頁", "", False),
    "OpenDocument Spreadsheet Flat XML": ("sheet", "fods", "fods",
                                          "ODF 單一檔 XML", "", False),
    "MS Excel 2003 XML": ("sheet", "xls-xml2003", "xml", "Excel 2003 XML", "",
                          False),
    "Calc MS Excel 2007 VBA XML": ("sheet", "xlsm", "xlsm", "Excel 巨集啟用",
                                   "", False),
    "calc8_template": ("sheet", "ots", "ots", "ODF 試算表範本", "", False),
    "Calc MS Excel 2007 XML Template": ("sheet", "xltx", "xltx",
                                        "Excel 2007–365 範本", "", False),
    "MS Excel 97 Vorlage/Template": ("sheet", "xlt", "xlt",
                                     "Excel 97–2003 範本", "", False),
    "dBase": ("sheet", "dbf", "dbf", "dBASE", "", False),
    "DIF": ("sheet", "dif", "dif", "資料交換格式（DIF）", "", False),
    "SYLK": ("sheet", "slk", "slk", "SYLK", "", False),

    # ---- 簡報 ----
    "impress8": ("slides", "odp", "odp", "ODF 簡報",
                 "LibreOffice / OxOffice 原生格式", True),
    "Impress MS PowerPoint 2007 XML": ("slides", "pptx-2007", "pptx",
                                       "PowerPoint 2007–365",
                                       "最通用的 .pptx 寫法", True),
    "Impress Office Open XML": ("slides", "pptx-ooxml", "pptx",
                                "Office Open XML 簡報",
                                "嚴格版寫法，少數系統才需要", True),
    "MS PowerPoint 97": ("slides", "ppt", "ppt", "PowerPoint 97–2003",
                         "二進位 .ppt", True),
    "OpenDocument Presentation Flat XML": ("slides", "fodp", "fodp",
                                           "ODF 單一檔 XML", "", False),
    "impress8_template": ("slides", "otp", "otp", "ODF 簡報範本", "", False),
    "Impress MS PowerPoint 2007 XML Template": ("slides", "potx", "potx",
                                                "PowerPoint 範本", "", False),
    "Impress Office Open XML Template": ("slides", "potx-ooxml", "potx",
                                         "Office Open XML 簡報範本", "", False),
    "MS PowerPoint 97 Vorlage": ("slides", "pot", "pot",
                                 "PowerPoint 97–2003 範本", "", False),
    "Impress MS PowerPoint 2007 XML AutoPlay": ("slides", "ppsx", "ppsx",
                                                "PowerPoint 自動播放",
                                                "開啟後直接進入播放模式", False),
    "Impress Office Open XML AutoPlay": ("slides", "ppsx-ooxml", "ppsx",
                                         "Office Open XML 自動播放", "", False),
    "MS PowerPoint 97 AutoPlay": ("slides", "pps", "pps",
                                  "PowerPoint 97–2003 自動播放", "", False),
    "Impress MS PowerPoint 2007 XML VBA": ("slides", "pptm", "pptm",
                                           "PowerPoint 巨集啟用", "", False),
    "impress_html_Export": ("slides", "html-impress", "html", "HTML 網頁", "",
                            False),
}

#: 這些匯出濾鏡產的是圖片 / PDF，不屬於「格式互轉」
#: （轉 PDF 有專門的工具，轉圖片也有）
_SKIP_EXT = {"pdf", "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp",
             "svg", "svgz", "emf", "wmf", "emz", "wmz", "eps", "swf", "apng"}

#: 刻意不提供的濾鏡。
#:
#: * `writer_layout_dump` / `writer_indexing_export`：soffice 內部除錯用的匯出
#: * `Text (encoded)`：要另外給編碼參數（`FilterOptions`）才有意義，
#:   目前沒開放那個欄位；而且 registry 給它的副檔名是 `.csv`（跟 Calc 共用
#:   同一個 Type），直接列出來會顯示成「文書檔轉 .csv」，看了更困惑
#: * `impress8_draw`：簡報存成 Draw 的 `.odg`，屬於繪圖家族 —— 站上沒有
#:   「繪圖檔」這個說法，見本檔開頭的說明
_SKIP_FILTER = {"writer_layout_dump", "writer_indexing_export",
                "Text (encoded)", "impress8_draw"}

#: 收得下的來源副檔名。
#:
#: **刻意不從 registry 的匯入濾鏡自動推導**：那會連 `.pdf`（pdfimport）、
#: `.png` 之類一起收進來，使用者上傳了才發現轉出來的東西不能看。
#: 這裡只列「本來就是這個家族的文件」。
_SOURCES: dict[str, tuple[str, ...]] = {
    "text": ("odt", "ott", "fodt", "docx", "doc", "docm", "dotx", "dot", "rtf",
             "txt", "html", "htm", "epub", "sxw"),
    "sheet": ("ods", "ots", "fods", "xlsx", "xls", "xlsm", "xltx", "xlt", "csv",
              "dif", "slk", "dbf", "sxc"),
    "slides": ("odp", "otp", "fodp", "pptx", "ppt", "pptm", "potx", "pot",
               "ppsx", "pps", "sxi"),
}

_NODE_RE = re.compile(
    r'<node oor:name="([^"]+)" oor:op="replace">(.*?)</node>\s*(?=<node|</node)',
    re.S)


def _prop(body: str, name: str) -> str:
    m = re.search(r'<prop oor:name="%s"[^>]*>\s*<value>([^<]*)</value>' % name,
                  body)
    return m.group(1) if m else ""


def _registry_dir() -> Optional[Path]:
    """soffice 安裝目錄下的 `share/registry`。"""
    soffice = office_convert.find_soffice()
    if not soffice:
        return None
    prog = Path(soffice).resolve().parent          # <root>/program
    for cand in (prog.parent / "share" / "registry",
                 prog.parent.parent / "share" / "registry"):
        if cand.is_dir():
            return cand
    return None


def _scan_registry() -> dict[str, dict]:
    """讀 registry，回 ``{濾鏡名稱: {"service": ..., "ext": ...}}``。

    讀不到就回空的 —— 呼叫端會改用內建對照表。
    """
    reg = _registry_dir()
    if not reg:
        logger.info("找不到 soffice 的 registry 目錄，格式清單改用內建對照表")
        return {}

    filters: dict[str, dict] = {}
    type_ext: dict[str, str] = {}
    for xcd in sorted(reg.glob("*.xcd")):
        try:
            txt = xcd.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _NODE_RE.finditer(txt):
            name, body = m.group(1), m.group(2)
            ext = _prop(body, "Extensions")
            if ext:
                # **整串都留著**。一個 Type 可以有多個副檔名（`generic_Text`
                # 是 `csv txt`，Writer 與 Calc 共用），取第一個當成「這支濾鏡
                # 的副檔名」會得到 Writer 的純文字匯出是 `.csv` 這種結論。
                # 真正決定輸出副檔名的是我們傳給 `--convert-to <ext>:<濾鏡>`
                # 的那個 ext，這裡的清單只用來驗證我們寫的在不在合法範圍內。
                type_ext.setdefault(name, [e.lower() for e in ext.split()])
            if "EXPORT" in _prop(body, "Flags").split():
                filters[name] = {
                    "service": _prop(body, "DocumentService").rsplit(".", 1)[-1],
                    "type": _prop(body, "Type"),
                }
    for info in filters.values():
        exts = type_ext.get(info.get("type", ""), [])
        info["exts"] = exts
        info["ext"] = exts[0] if exts else ""
    return filters


def _fallback_filters() -> dict[str, dict]:
    """registry 讀不到時的退路 —— 用對照表裡的標準濾鏡。

    這些是 LibreOffice 很久以前就有的濾鏡，絕大多數安裝都在。萬一某一支
    真的不在，轉換那一端會**明確報錯**（`convert_with_filter` 會確認產出
    存在），不會無聲失敗。
    """
    return {name: {"service": _SERVICE_OF[fam], "ext": ext, "exts": [ext]}
            for name, (fam, _tid, ext, *_rest) in _CURATED.items()}


def _auto_target(filter_name: str, ext: str) -> Target:
    """沒列在對照表、但 soffice 支援的濾鏡 —— 收成「其他格式」。

    使用者要的是「soffice 支援的都可以選」，所以不能因為我們沒寫說明
    就把它藏起來。id 由濾鏡名稱推導，同一台機器上是穩定的。
    """
    slug = re.sub(r"[^a-z0-9]+", "-", filter_name.lower()).strip("-")
    return Target(id=f"x-{slug}", ext=ext, filter=filter_name,
                  label=f".{ext}（{filter_name}）")


@functools.lru_cache(maxsize=1)
def catalogue() -> tuple[Family, ...]:
    """這台機器上可用的家族與目標格式。

    結果會快取 —— soffice 不會在執行中被換掉，而每次都重讀十幾個 xcd
    檔（合計數 MB）會讓工具頁明顯變慢。
    """
    filters = _scan_registry() or _fallback_filters()

    families = []
    for fam_id, fam_name, service in _FAMILIES:
        seen: set[str] = set()
        targets: list[Target] = []
        for filter_name, info in sorted(filters.items()):
            if info.get("service") != service or filter_name in _SKIP_FILTER:
                continue
            curated = _CURATED.get(filter_name)
            # 副檔名以對照表為準（已逐一確認過），沒收錄的才用 registry 的
            ext = (curated[2] if curated else (info.get("ext") or "")).lower()
            if not ext or ext in _SKIP_EXT:
                continue
            t = (Target(id=curated[1], ext=ext, filter=filter_name,
                        label=curated[3], note=curated[4], common=curated[5])
                 if curated else _auto_target(filter_name, ext))
            if t.id in seen:          # 同一支濾鏡在多個 xcd 裡出現過
                continue
            seen.add(t.id)
            targets.append(t)

        if not targets:
            # 缺該模組（例如沒裝 Impress）—— 整個家族不要出現在畫面上，
            # 比出現一個永遠轉不出來的家族好。
            logger.info("soffice 沒有 %s 的匯出濾鏡，略過「%s」", service, fam_name)
            continue

        # **排序照對照表的順序，不要按名稱** —— 按名稱排的話試算表的第一個
        # 會是「CSV」（C 開頭），但那是最不保真的目標，不該擺在最前面。
        # 對照表是刻意由「原生 → 通用 → 舊版 → 破壞性」排的。
        order = {name: i for i, name in enumerate(_CURATED)}
        targets.sort(key=lambda t: (not t.common,
                                    order.get(t.filter, 10_000), t.label))
        families.append(Family(id=fam_id, name=fam_name, service=service,
                               sources=_SOURCES[fam_id], targets=tuple(targets)))
    return tuple(families)


def refresh() -> None:
    """丟掉快取（換了 office 引擎之後用）。"""
    catalogue.cache_clear()


def family_for_ext(ext: str) -> Optional[Family]:
    """依副檔名判斷屬於哪個家族。"""
    ext = ext.lower().lstrip(".")
    return next((f for f in catalogue() if ext in f.sources), None)


def resolve(target_id: str) -> Optional[tuple[Family, Target]]:
    """把前端送來的 target_id 換回 (家族, 目標)。"""
    for fam in catalogue():
        t = fam.target(target_id)
        if t:
            return fam, t
    return None


def accepted_extensions() -> tuple[str, ...]:
    """上傳欄位的 accept 清單（含點）。"""
    exts = [f".{e}" for fam in catalogue() for e in fam.sources]
    return tuple(sorted(set(exts)))


def as_dict() -> list[dict]:
    """給前端 / API 用的 JSON 結構。"""
    return [{
        "id": fam.id,
        "name": fam.name,
        "sources": list(fam.sources),
        "targets": [{"id": t.id, "ext": t.ext, "label": t.label,
                     "note": t.note, "common": t.common} for t in fam.targets],
    } for fam in catalogue()]
