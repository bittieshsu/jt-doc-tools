"""Draw 引擎：PDF → LibreOffice/OxOffice Draw 匯入 → 合法 Writer .odt / .docx。

定位：pdf-to-office 的第三種引擎（v1.12.83+），與 pdf2docx-refine / jtdt-reform
**完全隔離**，走 early-return 分支，不修改也不依賴另外兩顆引擎。

原理與取捨
----------
soffice 的 PDF 匯入濾鏡屬 Draw 模組：把每段文字變成「有絕對座標的文字方塊」、
圖片保留、框線變向量形狀，版面幾乎 1:1（LibreOffice/OxOffice 成熟的 PDF 抽取）。
但 Draw 匯入產出的是「繪圖文件」，**沒有 Draw→Writer 直接濾鏡**：

* ``soffice --convert-to docx <pdf>``  → 失敗（no export filter）
* ``soffice --convert-to odt  <odg>``  → 產出「假 odt」（mimetype 其實是 graphics，
  雙擊開進 Draw、也轉不了 docx）

本引擎的解法：拿 Draw 匯入的 ``.odg``（已經把 PDF 拆成定位好的形狀），**自己重組成
一份合法的 Writer ODF**——把每個形狀改成 ``text:anchor-type="page"`` 的頁面錨定物件、
並在 graphic 樣式補上絕對定位屬性。產出的 ``.odt`` 是真正的 Writer 文件（正確
mimetype、可在 Word/Writer 開啟編輯），要 ``.docx`` 再走標準 Writer→Word。

誠實限制：本質是「文字方塊」版面（與 jtdt-reform 同類）——非流動文字、沒有真表格
（表格會變成一格格文字方塊＋框線向量）。適合「要一份長得跟 PDF 一樣、可微調」的
情境，不適合要重排內文/編輯表格。
"""
from __future__ import annotations

import logging
import os
import re
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from lxml import etree

from ....core import office_convert

log = logging.getLogger(__name__)

# ODF 命名空間
_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
}

# 設計感頁面（漸層 / 圖案背景）raster fallback：LibreOffice/OxOffice Draw 的 PDF
# 匯入器會把漸層 / 裁切 / 圖案背景「用純色拼塊近似」，一頁可爆出上千個色塊且色彩
# 失真（實測 OSSII 設計封面：深藍漸層 → 一大塊平橘色）。此類頁面的向量已不可信，
# 改把「原 PDF 該頁整頁」render 成圖嵌入 → 像素級忠於原稿（代價：該頁不可編輯，
# 設計封面本就不會去編）。門檻用頂層形狀數；一般表單 / 文件遠低於此。
_RASTER_SHAPE_THRESHOLD = 400
_RASTER_DPI = 150


# 安全解析器：關掉外部實體（防 XXE）、關網路、限制樹規模（防資源耗盡）。
# odg 的 content.xml 雖由 soffice 產生，仍採 defence-in-depth。
# 注意：lxml parser 非執行緒安全，不可共用 module-global（job_manager 用
# ThreadPoolExecutor(max_workers=2)，兩個 draw job 併發會競爭同一 parser）→
# 每次呼叫都新建一個（成本極低）。
def _safe_parser() -> "etree.XMLParser":
    return etree.XMLParser(
        resolve_entities=False, no_network=True, huge_tree=False, load_dtd=False
    )

# odg content.xml 解壓後大小上限（防惡意 PDF 讓 soffice 產出巨量 XML 撐爆記憶體）
_MAX_CONTENT_BYTES = 80 * 1024 * 1024   # 80MB
# 單一 odt 內 Pictures 總大小上限
_MAX_PICTURES_BYTES = 200 * 1024 * 1024  # 200MB


def _q(prefix: str, local: str) -> str:
    return "{%s}%s" % (_NS[prefix], local)


# ── 字型風格對應（參考 pdf2docx 的 font_normalize / config.FONT_MAPPING）──────
# 把原字型依「明體 / 楷書 / 黑體」風格 → 標準台灣字型名 + ODF generic-family。
# 目的（OS 無關）：輸出檔在有新細明體/標楷體/微軟正黑體的 Windows 客戶 render 正確；
# 任何 OS / reader 找不到原字型時，靠 generic-family fallback 到「同風格」
# （roman=明體 / swiss=黑體 / script=楷書），而非亂替成黑體。
# 關鍵：soffice Draw 匯入的 CJK font-face 常**缺 style:font-family-generic**（例
# MingLiU），沒風格提示就 fallback 成 sans → 明體變黑體（實測踩過）。
try:
    from ..postprocess.config import FONT_MAPPING as _FONT_MAPPING
except Exception:  # pragma: no cover - 保守
    _FONT_MAPPING = {}

# 標準台灣字型名 → ODF generic-family（roman=serif/明宋、swiss=sans/黑、script=楷）
_TW_GENERIC = {
    "新細明體": "roman", "標楷體": "script", "微軟正黑體": "swiss",
    "思源黑體": "swiss", "思源宋體": "roman", "PingFang TC": "swiss",
}


# 只認「明確是 CJK 字型」的 romanized token（避免誤判 Liberation Sans / DejaVu Sans
# / Noto Sans 這種 Latin 字型 —— 它們含 sans/serif 但不是中文字型，不可改）。
_CJK_TOKENS = (
    "mingliu", "pmingliu", "jhenghei", "yahei", "simsun", "simhei", "dfkai",
    "biaukai", "kaiti", "songti", "heiti", "fangsong", "pingfang", "cwtex",
    "ukai", "uming", "tw-kai", "tw-sung", "twkai", "twsung", "stkai", "stsong",
    "stheiti", "stsong", "lihei", "lisong", "mhei", "msung", "mkai", "batang",
    "gulim", "kozuka",
    # 台灣簡報 / 文宣常見的圓體與黑體 —— 認不出來就會原樣輸出字型名，Linux 上
    # 找不到該字型時 fontconfig 會隨便挑一個，實測落到明體，圓潤黑體標題整個走樣
    # （實測臺大圖書館簡報：DFNYuan / GenSenRounded / DFLiKingHei 全部沒被認出）。
    "yuan",          # 華康圓體 DFYuan / DFNYuan（圓體＝圓潤黑體）
    "gensen",        # 源泉圓體 GenSenRounded（justfont / Adobe 開源）
    "genyo",         # 源樣黑體 GenYoGothic
    "genryu", "genwan",   # 源流明體 GenRyuMin / 源雲明體 GenWanMin（明體系）
    "kinghei",       # 華康儷金黑 DFLiKingHei
    "rounded", "gothic", "maru",   # 通用：圓體 / 黑體 / 丸ゴシック
    "dfhei", "dfsong", "dfming", "dfli",
    "cwtexyen",      # cwTeX 圓體
    "genjyuu", "gensekigothic",
)


def _cjk_style_of_token(low: str) -> tuple[str, str]:
    """romanized CJK token → (標準台灣字型名, generic)。

    **圓體歸到黑體**：圓體是「圓潤化的黑體」，開源 CJK 字型沒有對應品項（我們預裝
    的是 Noto Sans/Serif CJK），選黑體是最接近的；若照字面找不到就會 fallback 成
    明體，那才是真的走樣。
    """
    if "kai" in low or "fangsong" in low:
        return ("標楷體", "script")          # 楷書 / 仿宋
    # 明體系要先於通用判斷（GenRyuMin / GenWanMin 都是明體，名稱裡沒有 "ming"）
    if any(t in low for t in ("ming", "sung", "song", "ryumin", "wanmin",
                              "dfsong", "dfming")):
        return ("新細明體", "roman")         # 明體 / 宋體
    return ("微軟正黑體", "swiss")            # 其餘（黑體 / 正黑 / 圓體 / pingfang…）


def _classify_cjk_font(name: str) -> tuple[str | None, str | None]:
    """字型名 → (標準台灣字型名, generic)。**只對確定是 CJK 的字型**回非 None，
    Latin 字型（Liberation Sans / Noto Sans / Arial / Times…）一律回 (None, None)
    不動，避免英文字被塞中文字型。"""
    if not name:
        return (None, None)
    base = name.strip().strip("'\"")
    if "+" in base:  # 去 subset prefix（如 GPDGIG+MingLiU）
        head, _, rest = base.partition("+")
        if len(head) == 6 and head.isalpha():
            base = rest
    # 1) 精確查 FONT_MAPPING（pdf2docx 同款）
    for k, v in _FONT_MAPPING.items():
        if base.lower() == k.lower():
            cjk = v[0] if isinstance(v, (list, tuple)) else v
            return (cjk, _TW_GENERIC.get(cjk))
    # 2) 名稱含 CJK 漢字 → 依 楷/明宋/黑 判風格
    if any("一" <= ch <= "鿿" for ch in base):
        if "楷" in base:
            return ("標楷體", "script")
        if any(ch in base for ch in "明宋"):
            return ("新細明體", "roman")
        if any(ch in base for ch in "黑圓"):
            return ("微軟正黑體", "swiss")
        return (None, None)  # CJK 名但風格不明 → 不動
    # 3) 特定 CJK romanized token（保守，不含通用 sans/serif）
    low = base.lower()
    for t in _CJK_TOKENS:
        if t in low:
            return _cjk_style_of_token(low)
    return (None, None)


def _remap_fonts(root) -> int:
    """把 odg 的 CJK font-face 補上標準台灣字型名 + generic-family。回改動數。"""
    decls = root.find(_q("office", "font-face-decls"))
    if decls is None:
        return 0
    n = 0
    fam_key = _q("svg", "font-family")
    gen_key = _q("style", "font-family-generic")
    name_key = _q("style", "name")
    for ff in decls.findall(_q("style", "font-face")):
        raw = ff.get(fam_key) or ff.get(name_key) or ""
        cjk, generic = _classify_cjk_font(raw)
        if not cjk:
            continue
        ff.set(fam_key, cjk)                 # 標準台灣名（Windows 客戶對）
        if generic:
            ff.set(gen_key, generic)         # OS 無關風格 fallback
        n += 1
    return n


def _shape_has_text(shape) -> bool:
    """該形狀是否含實際文字（draw:text-box 有非空白內容）。"""
    for tb in shape.iter(_q("draw", "text-box")):
        if "".join(tb.itertext()).strip():
            return True
    return False


def _bg_style_name(autostyles, cache: dict, name: str) -> str | None:
    """取得 ``name`` 的「背景層」變體樣式名（需要時才複製一份）。

    為什麼要這個：ODF 的 ``style:run-through`` 分 foreground / background 兩層。
    原本所有形狀都給 foreground，靠文件順序疊放——Writer(.odt) 沒問題，但
    **匯出 .docx 時無文字的形狀走 VML(``w:pict``) 路徑、z-index 整個掉光**
    （含文字的走 DrawingML ``wp:anchor`` 才保留 relativeHeight），結果表單底色
    矩形跑到文字上面把整頁蓋掉（實測美國 IRS 1040 表單）。把「純裝飾形狀」
    （底色塊 / 框線 / 圖片，即無文字者）改判到 **background 層**，兩種格式都
    保證在文字之下，而形狀彼此之間的相對順序仍照文件順序，不受影響。
    """
    if not name:
        return None
    if name in cache:
        return cache[name]
    src = None
    for st in autostyles.findall(_q("style", "style")):
        if st.get(_q("style", "name")) == name:
            src = st
            break
    if src is None:
        cache[name] = None
        return None
    import copy
    dup = copy.deepcopy(src)
    new_name = name + "JtBg"
    dup.set(_q("style", "name"), new_name)
    gp = dup.find(_q("style", "graphic-properties"))
    if gp is None:
        gp = etree.SubElement(dup, _q("style", "graphic-properties"))
    gp.set(_q("style", "run-through"), "background")
    autostyles.append(dup)
    cache[name] = new_name
    return new_name


_VML_NS = "urn:schemas-microsoft-com:vml"
# VML 形狀的背景層起始 z-index（負值 = 在文字層之下，見 _fix_docx_vml_zorder）
_VML_BG_Z_BASE = -30000


def _fix_docx_vml_zorder(docx_path: Path) -> int:
    """把 .docx 內「無文字的 VML 形狀」壓到文字層之下（回改動數）。

    為什麼需要：soffice 的 Word 匯出對**含文字**的物件走 DrawingML
    （``wp:anchor``，有 ``relativeHeight`` → 疊放順序完整保留），但對**純圖形**
    （底色矩形、框線、圖片）走 VML ``w:pict``，而 **VML 的 ``z-index`` 在匯出時
    整個不寫**。依 VML 規則，沒有 z-index 等同 ``auto`` → 一律疊在文字層**之上**，
    於是表單底色塊會把整頁文字蓋掉（.odt 開起來正常、轉 .docx 就整頁看不到字，
    實測美國 IRS 1040 表單；ODF 端設 ``draw:z-index`` / ``style:run-through``
    都救不了，因為資訊在匯出時就掉了）。

    修法（通用、與文件內容無關）：依**文件順序**給每個無文字的 VML 形狀一個遞增
    的負 z-index → 全部落在文字層之下，且形狀彼此的相對順序原封不動。有文字的
    VML 形狀不動（它可能本來就該在上層，例如標籤氣泡）。
    """
    import shutil, tempfile
    try:
        with zipfile.ZipFile(docx_path) as zin:
            names = zin.namelist()
            if "word/document.xml" not in names:
                return 0
            data = {n: zin.read(n) for n in names}
    except (OSError, zipfile.BadZipFile):
        return 0
    try:
        root = etree.fromstring(data["word/document.xml"], _safe_parser())
    except etree.XMLSyntaxError:
        return 0
    n = 0
    z = _VML_BG_Z_BASE
    for el in root.iter():
        if not isinstance(el.tag, str) or not el.tag.startswith("{" + _VML_NS + "}"):
            continue
        if etree.QName(el).localname not in ("rect", "shape", "roundrect", "oval",
                                             "line", "polyline", "curve", "group"):
            continue
        if "".join(el.itertext()).strip():
            continue                      # 含文字 → 不動（可能本來就該在上層）
        style = el.get("style") or ""
        if re.search(r"(?:^|;)\s*z-index\s*:", style):
            continue                      # 已有 z-index → 尊重原值
        el.set("style", (style.rstrip("; ") + ";" if style.strip() else "")
                        + "z-index:%d" % z)
        z += 1
        n += 1
    if not n:
        return 0
    data["word/document.xml"] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True)
    fd, tmp = tempfile.mkstemp(suffix=".docx", dir=str(docx_path.parent))
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                zout.writestr(name, data[name])
        shutil.move(tmp, docx_path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return 0
    return n


def _points_to_cm(pt: float) -> float:
    return pt / 28.3465


# 各單位換 cm（ODF svg:width 幾乎都用 cm，其餘保底）
_UNIT_CM = {"cm": 1.0, "mm": 0.1, "in": 2.54, "inch": 2.54, "pt": 1 / 28.3465,
            "pc": 12 / 28.3465, "px": 2.54 / 96}
# 文字方塊加寬：比例餘裕 + 固定餘裕（cm）。防 Writer 重繪字寬略增而換行掉尾字。
_FRAME_WIDTH_FACTOR = 1.18
_FRAME_WIDTH_PAD_CM = 0.15


def _parse_len_cm(val: str) -> tuple[float, str] | None:
    """解析 ODF 長度（如 '0.908cm'）→ (數值cm, 單位)。無法解析回 None。"""
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*([a-z]+)\s*$", val or "", re.I)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2).lower()
    if unit not in _UNIT_CM:
        return None
    return num * _UNIT_CM[unit], unit


def _pad_frame_width(frame, page_w_cm: float | None = None) -> None:
    """把文字 frame 的 svg:width 加一點餘裕（就地改寫），防換行掉尾字。

    安全性：soffice 的 PDF 匯入一律用「絕對定位 + text-align=start」，文字錨在
    svg:x，加寬只是把（無邊框無填色的）隱形框往右延伸，**不會移動可見文字**（右
    對齊數字實測零漂移）。仍加頁寬 clamp，避免框超出頁緣（萬一該框有填色時才不溢）。
    """
    wkey = _q("svg", "width")
    cur = frame.get(wkey)
    parsed = _parse_len_cm(cur) if cur else None
    if not parsed:
        return
    w_cm, _unit = parsed
    new_cm = w_cm * _FRAME_WIDTH_FACTOR + _FRAME_WIDTH_PAD_CM
    if page_w_cm:
        xparsed = _parse_len_cm(frame.get(_q("svg", "x")) or "")
        x_cm = xparsed[0] if xparsed else 0.0
        max_w = page_w_cm - x_cm - 0.05          # 留 0.05cm 不貼頁緣
        # 一律把加寬後的寬度 cap 在頁緣，但絕不縮到比「原寬」還小（否則反而換行）。
        # 舊版寫 `if max_w > w_cm` 會在「框原本就已接近頁寬」時整個跳過 clamp → 套
        # 1.18× 後溢出頁面，Writer render 該框時位移（頁6「現階段使用」往左突出）。
        new_cm = min(new_cm, max(max_w, w_cm))
    frame.set(wkey, "%.3fcm" % new_cm)


def _pdf_page_sizes_cm(pdf_path: Path) -> list[tuple[float, float]]:
    """讀 PDF 每頁「視覺」尺寸（cm）清單。失敗回單一 A4。

    PyMuPDF 的 ``page.rect`` 已把 /Rotate 算進去（90° A4 → 回傳 842×595 橫向），
    正好對得上 Draw 匯入後正規化的視覺頁，故直接用不必再交換寬高。
    """
    try:
        import fitz  # PyMuPDF

        with fitz.open(pdf_path) as doc:
            out = []
            for pg in doc:
                r = pg.rect  # 已含旋轉的視覺尺寸
                out.append((round(_points_to_cm(r.width), 3),
                            round(_points_to_cm(r.height), 3)))
            if out:
                return out
    except Exception:  # pragma: no cover - 保守 fallback
        pass
    return [(21.0, 29.7)]


def _frame_text(frame) -> str:
    """取 draw:frame 內文字方塊的全部文字。"""
    return "".join(frame.itertext())


def _frame_bbox(frame):
    """回 (x0,y0,x1,y1) cm；缺座標回 None。"""
    xp = _parse_len_cm(frame.get(_q("svg", "x")) or "")
    yp = _parse_len_cm(frame.get(_q("svg", "y")) or "")
    wp = _parse_len_cm(frame.get(_q("svg", "width")) or "")
    hp = _parse_len_cm(frame.get(_q("svg", "height")) or "")
    if not (xp and yp):
        return None
    x, y = xp[0], yp[0]
    w = wp[0] if wp else 0.0
    h = hp[0] if hp else 0.0
    return (x, y, x + w, y + h)


def _bbox_overlap(a, b, pad=0.05) -> bool:
    """兩 bbox 是否重疊（加一點 pad 容忍浮點/疊印微位移）。"""
    if not a or not b:
        return False
    return (a[0] < b[2] + pad and b[0] < a[2] + pad
            and a[1] < b[3] + pad and b[1] < a[3] + pad)


def _bbox_colocated(a, b, min_cover: float = 0.6) -> bool:
    """兩 bbox 是否「幾乎疊在同一處」（真正的疊印假粗體會這樣）。

    與 `_bbox_overlap` 的差別很關鍵：疊印是**同一段文字重畫在同一位置**，兩框的
    重疊面積會佔掉小框的絕大部分；而表格裡相鄰儲存格只是邊緣擦到一點點。先前
    只用「有沒有重疊」判定，導致「74.98公頃」旁邊那個獨立的「公頃」被當成
    「170.48公頃」的疊印重複刪掉（實測台北市都發局簡報，每頁少 3 個「公頃」）。
    """
    if not a or not b:
        return False
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    if ix <= 0 or iy <= 0:
        return False
    inter = ix * iy
    area_a = max(1e-9, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1e-9, (b[2] - b[0]) * (b[3] - b[1]))
    # 小框有多少比例被大框蓋住
    return inter / min(area_a, area_b) >= min_cover


def _dedup_overprint(page) -> int:
    """清掉 PDF「疊印假粗體」在 Draw 匯入後產生的重複 / 被覆蓋文字框。回移除數。

    很多政府表單標題用「同段文字疊印 3-4 次」做假粗體，Draw 匯入後拆成重疊的
    小文字框，有的被併成「鄉鄉」「（（」→ render 出來變雙字。清理規則（都要求
    **有重疊鄰框**才動，避免誤刪正常內容如 ○○○○○ / ＿＿＿）：
      ① 完全重複：文字相同 + bbox 重疊 → 只留一個。
      ② 純單字重複框（如「鄉鄉」）：若有重疊鄰框已含該字 → 移除（字由鄰框保留）。
      ③ 被完整包含：某框文字是重疊鄰框文字的子字串 → 移除較短的（鄰框已含）。
    """
    tb_q = _q("draw", "text-box")
    frames = []
    for f in page:
        if isinstance(f.tag, str) and etree.QName(f).localname == "frame" \
                and f.find(tb_q) is not None:
            frames.append((f, _frame_bbox(f), _frame_text(f).strip()))
    removed = 0
    to_remove = set()
    for i, (fi, bi, ti) in enumerate(frames):
        if id(fi) in to_remove or not ti:
            continue
        for j, (fj, bj, tj) in enumerate(frames):
            if i == j or id(fj) in to_remove or not tj:
                continue
            if not _bbox_overlap(bi, bj):
                continue
            # ① 完全重複（留 i 刪 j）。同樣要求幾乎同位置 —— 表格裡兩個相鄰
            #    儲存格可能都是「-」或同一個數字，那是真實內容不是疊印。
            if ti == tj and _bbox_colocated(bi, bj):
                to_remove.add(id(fj))
                continue
            # ②③ 只在「兩框幾乎疊在同一處」時才動 —— 相鄰儲存格只是邊緣擦到，
            #     不算疊印（見 _bbox_colocated）。
            if not _bbox_colocated(bi, bj):
                continue
            # ② j 是純單字重複（「鄉鄉」）且該字在 i 內 → 刪 j
            if len(set(tj)) == 1 and len(tj) >= 2 and tj[0] in ti:
                to_remove.add(id(fj))
                continue
            # ③ j 的文字是 i 的子字串（且較短）→ 刪 j
            if tj != ti and tj in ti and len(tj) < len(ti):
                to_remove.add(id(fj))
                continue
    for f, _b, _t in frames:
        if id(f) in to_remove:
            page.remove(f)
            removed += 1
    return removed


def _render_pdf_page_png(pdf_path: Path, page_index: int,
                         remove_text: bool = False) -> bytes | None:
    """把原 PDF 第 page_index（0-based）頁 render 成 PNG bytes（設計頁 raster fallback）。

    remove_text=True → 先把該頁「真實可抽取文字」移除（**保留圖形 / 圖片 / 外框字
    路徑 / 漸層**）再 render → 這張圖只剩背景設計，真實文字改由上層可編輯 vector 呈現
    （避免文字被烤進圖裡「移開圖才看得到文字」的困擾，且不會雙重文字）。外框化的標題
    （非真實文字、是向量路徑）會留在圖裡，維持設計原貌。"""
    try:
        import fitz  # noqa: PLC0415
        with fitz.open(str(pdf_path)) as doc:
            if page_index >= doc.page_count:
                return None
            page = doc[page_index]
            if remove_text:
                try:
                    for blk in page.get_text("dict").get("blocks", []):
                        if blk.get("type") == 0:  # 文字 block
                            page.add_redact_annot(fitz.Rect(blk["bbox"]), fill=None)
                    # 只移除文字,保留 line-art（漸層 / 外框字路徑）與圖片
                    page.apply_redactions(
                        images=getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0),
                        graphics=getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0),
                    )
                except Exception:  # noqa: BLE001 — 移字失敗就用含字的整頁圖
                    pass
            pix = page.get_pixmap(dpi=_RASTER_DPI, alpha=False)
            return pix.tobytes("png")
    except Exception:  # noqa: BLE001 — render 失敗就退回向量搬移
        return None


def _page_has_large_image(page, page_w_cm: float, page_h_cm: float,
                          frac: float = 0.55) -> bool:
    """頁面是否有單一「圖片」(draw:image) 覆蓋 > frac 的頁面面積。

    抓「全出血照片 / 設計封面」——這種頁面上面疊的向量文字框，Draw 匯入常搞壞
    透明度 / 位置（實測 ISO 封面：疊字下方多出黑框、半透明字變實心放大），改走整頁
    raster。**只認 draw:image（照片 / 點陣圖），不認純填色矩形**——否則表單 / 內文的
    整頁底色會被誤判成設計頁。"""
    if not (page_w_cm and page_h_cm):
        return False
    page_area = page_w_cm * page_h_cm
    for shape in page:
        if not isinstance(shape.tag, str):
            continue
        if next(shape.iter(_q("draw", "image")), None) is None:
            continue  # 該頂層形狀（或其後代）不含點陣圖 → 跳過
        bbox = _frame_bbox(shape)
        if not bbox:
            continue
        if (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) >= frac * page_area:
            return True
    return False


def _text_frame_count(page) -> int:
    """該頁「含實際文字」的頂層形狀數（draw:text-box 有非空白內容）。用來分辨
    **密集表單（大量文字框）** 與 **漸層設計頁（幾乎無文字、只有色塊）**——兩者
    形狀數都可能上千，但表單的文字框比例高（IRS 1040：1010 形狀 / 503 文字框 =
    50%），漸層封面極低（OxOffice 封面：1255 / 9 = 1%）。"""
    n = 0
    for shape in page:
        if not isinstance(shape.tag, str):
            continue
        tb = next(shape.iter(_q("draw", "text-box")), None)
        if tb is not None and "".join(tb.itertext()).strip():
            n += 1
    return n


def _page_has_transformed_image(page) -> bool:
    """頁面是否有「被斜切 / 旋轉」的圖片（draw:transform 含 skew / rotate + 內含
    draw:image）。設計年報常把照片嵌進等角（isometric）幾何造型 → **Writer 無法渲染
    斜切 / 裁切的圖片 → 整塊變黑**（實測國藝會年報目錄的照片幾何塊）。這種頁面改走
    整頁 raster。一般表單 / 文件的圖片不會斜切，不受影響。"""
    for shape in page:
        if not isinstance(shape.tag, str):
            continue
        tr = shape.get(_q("draw", "transform")) or ""
        if ("skew" in tr or "rotate" in tr) and \
                next(shape.iter(_q("draw", "image")), None) is not None:
            return True
    return False


def _emit_raster_page(para, autostyles, pics: dict, png: bytes,
                      i: int, w: float, h: float, z: int = 0) -> None:
    """把整頁 PNG 以頁面錨定 frame 塞進段落，放在**最底層（背景、低 z-index）**——
    這張圖是「已移除真實文字、只剩漸層 / 圖形 / 外框字」的設計背景（見 remove_text）；
    真實文字改由上層可編輯 vector 呈現。點頁面選到的是文字（圖在背景不礙事），視覺
    仍保真、且不會雙重文字。"""
    pic_name = "Pictures/jtraster_%d.png" % i
    pics[pic_name] = png
    gstyle = etree.SubElement(autostyles, _q("style", "style"))
    gstyle.set(_q("style", "name"), "JtRaster%d" % i)
    gstyle.set(_q("style", "family"), "graphic")
    ggp = etree.SubElement(gstyle, _q("style", "graphic-properties"))
    ggp.set(_q("style", "horizontal-pos"), "from-left")
    ggp.set(_q("style", "horizontal-rel"), "page")
    ggp.set(_q("style", "vertical-pos"), "from-top")
    ggp.set(_q("style", "vertical-rel"), "page")
    ggp.set(_q("style", "wrap"), "run-through")
    ggp.set(_q("style", "run-through"), "background")
    frame = etree.SubElement(para, _q("draw", "frame"))
    frame.set(_q("draw", "style-name"), "JtRaster%d" % i)
    frame.set(_q("draw", "z-index"), str(z))  # 背景層,文字框的 z 比它大
    # 錨定：用「段落錨定 + 位置相對於頁面」而非 ODF 專有的「錨定到第 N 頁」——
    # OOXML(.docx) **沒有 anchor-to-page-N 的概念**,匯出時會把頁面錨定物件全部
    # 重新掛到同一個段落 → 所有頁的內容擠成一頁(實測任何多頁文件轉 docx 都踩)。
    # 每頁本來就各自有一個段落,錨到該段落即可對到正確頁,座標仍相對頁面故視覺不變。
    frame.set(_q("text", "anchor-type"), "paragraph")
    frame.set(_q("svg", "x"), "0cm")
    frame.set(_q("svg", "y"), "0cm")
    frame.set(_q("svg", "width"), "%.3fcm" % w)
    frame.set(_q("svg", "height"), "%.3fcm" % h)
    img = etree.SubElement(frame, _q("draw", "image"))
    img.set(_q("xlink", "href"), pic_name)
    img.set(_q("xlink", "type"), "simple")
    img.set(_q("xlink", "show"), "embed")
    img.set(_q("xlink", "actuate"), "onLoad")


def _move_text_frames_over_raster(page, para, i: int, page_w_cm: float,
                                  z_start: int = 1) -> int:
    """raster 頁：把原始「含文字」的框搬到段落（錨定第 i 頁、**高 z-index 在背景圖
    之上**）——文字直接可見 / 可選取 / 可編輯（背景圖已移除真實文字,不會雙重）。不搬
    非文字形狀（漸層色塊 / 失真向量 / 外框字路徑,那些已在背景圖裡）。回搬移文字框數。"""
    n = 0
    for shape in list(page):
        if not isinstance(shape.tag, str):
            continue
        tb = next(shape.iter(_q("draw", "text-box")), None)
        if tb is None or not "".join(tb.itertext()).strip():
            continue  # 只保留有文字的框
        tag = etree.QName(shape).localname
        shape.set(_q("text", "anchor-type"), "paragraph")  # 見 _emit_raster_page 註解
        shape.set(_q("draw", "z-index"), str(z_start + n))  # 在背景 raster 圖之上
        if tag == "frame":
            _pad_frame_width(shape, page_w_cm=page_w_cm)
        para.append(shape)
        n += 1
    return n


def _build_writer_odt(odg_path: Path, odt_out: Path,
                      page_sizes: list[tuple[float, float]],
                      pdf_path: Path | None = None) -> tuple[int, int]:
    """把 Draw 的 .odg 重組成合法 Writer .odt（支援每頁不同尺寸）。回 (頁數, 圖片數)。

    pdf_path 有給且某頁色塊數超過 `_RASTER_SHAPE_THRESHOLD`（設計感漸層 / 圖案頁，
    Draw 匯入必失真）→ 該頁改嵌原 PDF 整頁圖（像素級還原）。"""
    with zipfile.ZipFile(odg_path) as zin:
        names = zin.namelist()
        if "content.xml" not in names:
            raise RuntimeError("odg 缺 content.xml")
        # 大小防護：用 ZipInfo.file_size 先看解壓後大小，避免 zip bomb
        ci = zin.getinfo("content.xml")
        if ci.file_size > _MAX_CONTENT_BYTES:
            raise RuntimeError(
                "PDF 版面過於龐大（Draw 匯入後 content.xml %d MB 超過上限），"
                "請改用 pdf2docx / jtdt-reform 引擎" % (ci.file_size // 1048576)
            )
        content_bytes = zin.read("content.xml")
        # Pictures：逐張累計大小，超上限即停
        pics = {}
        total_pic = 0
        for n in names:
            if not n.startswith("Pictures/"):
                continue
            info = zin.getinfo(n)
            total_pic += info.file_size
            if total_pic > _MAX_PICTURES_BYTES:
                raise RuntimeError("PDF 內嵌圖片總量過大，請改用其他引擎")
            pics[n] = zin.read(n)

    root = etree.fromstring(content_bytes, _safe_parser())

    # ODF 版本一致性（關鍵！）：新版 LibreOffice（26.2+）產的 odg content.xml 標
    # office:version="1.4"，但本函式自組的 styles.xml + manifest.xml 是 "1.3" →
    # **版本不一致 → LibreOffice 直接拒絕載入整份**（"source file could not be
    # loaded"），舊版 LO / OxOffice 產 1.3 才不會踩到。強制把 content 版本壓成 1.3
    # 與 styles/manifest 對齊。（實測 LO 26.2 踩過，花很久才抓到。）
    root.set(_q("office", "version"), "1.3")

    # 0) 字型：CJK font-face 補標準台灣字型名 + generic-family（明/楷/黑風格對應）。
    _remap_fonts(root)

    # 1) automatic-styles：把每個 graphic 樣式補上「頁面絕對定位」屬性。
    #    這一步同時修好文字方塊、線條、矩形（保留各自 stroke/fill 不動）。
    autostyles = root.find(_q("office", "automatic-styles"))
    if autostyles is not None:
        for st in autostyles.findall(_q("style", "style")):
            if st.get(_q("style", "family")) != "graphic":
                continue
            gp = st.find(_q("style", "graphic-properties"))
            if gp is None:
                gp = etree.SubElement(st, _q("style", "graphic-properties"))
            gp.set(_q("style", "horizontal-pos"), "from-left")
            gp.set(_q("style", "horizontal-rel"), "page")
            gp.set(_q("style", "vertical-pos"), "from-top")
            gp.set(_q("style", "vertical-rel"), "page")
            gp.set(_q("style", "wrap"), "run-through")
            gp.set(_q("style", "run-through"), "foreground")
            # 文字方塊隨內容自動長寬：svg:width/height 當「最小值」，Writer 重繪時
            # 字寬若略增不會裁掉尾字（Draw 定的框剛好，換算字型後常差一兩 px → 掉尾字）。
            gp.set(_q("draw", "auto-grow-width"), "true")
            gp.set(_q("draw", "auto-grow-height"), "true")
            # 透明背景：Draw 的無填色框（draw:fill="none"）搬到 Writer 當文字框時，
            # Writer 會**預設補白色不透明底**，遮住底下的灰底表頭 / 綠底等背景。
            # 關鍵：Writer 文字框的透明度要用 **style:background-transparency="100%"**
            # （fo:background-color / draw:fill="none" 在 Writer 文字框 context 都無效，
            # 實測踩過）。**只對「本來就無填色（none / 未設）」的框套 100% 透明**——
            # 絕不能碰 gradient / bitmap / solid 填色！舊版寫 `!= "solid"` 會把裝飾用的
            # **漸層 / 點陣填色一起清成 none** → 國藝會年報目錄的漸層幾何圖案變成實心黑塊。
            _fill = gp.get(_q("draw", "fill"))
            if _fill in (None, "none"):
                gp.set(_q("draw", "fill"), "none")
                gp.set(_q("style", "background-transparency"), "100%")

    if autostyles is None:
        autostyles = etree.SubElement(root, _q("office", "automatic-styles"))

    # 2) body：office:drawing → office:text，每個 draw:page 的形狀改成頁面錨定
    body = root.find(_q("office", "body"))
    drawing = body.find(_q("office", "drawing")) if body is not None else None
    if body is None or drawing is None:
        raise RuntimeError("odg 缺 office:drawing 結構，無法轉 Writer")

    pages = drawing.findall(_q("draw", "page"))
    if page_sizes and len(pages) != len(page_sizes):
        # soffice 匯入頁數與 PyMuPDF 讀到的不一致（罕見：毀損 / 特殊註解 PDF）→
        # _sz 會把超界頁夾到最後一個尺寸，可能尺寸略錯（不 crash）。記一筆好追。
        log.warning("draw engine：odg 頁數 %d ≠ PDF 頁數 %d（尺寸可能對不齊）",
                    len(pages), len(page_sizes))
    # 每頁尺寸 → 去重成「尺寸群組」，每群一個 master-page + page-layout。
    # 段落樣式帶 style:master-page-name → 自動換頁並套該頁尺寸（含同尺寸連續頁）。
    def _sz(i):
        if page_sizes and i - 1 < len(page_sizes):
            return page_sizes[i - 1]
        return page_sizes[-1] if page_sizes else (21.0, 29.7)

    size_groups: list[tuple[float, float]] = []
    size_index: dict[tuple[float, float], int] = {}
    per_page_size: list[tuple[float, float]] = []   # 每頁實際尺寸（各頁一個 master page）
    text_el = etree.Element(_q("office", "text"))
    n_pages = 0
    prev_group = -1          # 前一頁的尺寸群組（保留供空文件 fallback 用）
    # 全域疊放順序計數器：**必須明確給每個形狀 draw:z-index**。odg 內的形狀本來
    # 就照「繪製順序」排列（底色矩形在前、文字在後），Writer 依文件順序疊放沒問題，
    # 但**匯出 .docx 時順序不保證**（實測 IRS 表單：薄荷綠底色矩形跑到文字上面，
    # 整頁文字被蓋掉）。ODF 的 draw:z-index 會對應到 OOXML 的 relativeHeight，
    # 明確編號後兩種格式的疊放才一致。
    z_counter = 0
    bg_style_cache: dict = {}   # 原樣式名 → 背景層變體名（見 _bg_style_name）
    for i, page in enumerate(pages, start=1):
        n_pages += 1
        w, h = _sz(i)
        key = (round(w, 2), round(h, 2))
        if key not in size_index:
            size_index[key] = len(size_groups)
            size_groups.append((w, h))
        g = size_index[key]
        # 分頁：**每一頁都用自己的 master page**（style:master-page-name），不論
        # 尺寸是否與前頁相同。原因是 .docx 相容性：
        #   * `fo:break-before` 會被匯出成「段落內的 <w:br w:type="page">」——分頁符
        #     跟前一頁的物件擠在同一個段落裡，Word/Writer 算「頁面錨定物件屬於哪一
        #     頁」時就會全部歸到同一頁 → 第 1 頁空白、內容整體位移（實測 odt 正確、
        #     docx [0,5,10]）。
        #   * 換 master page 則會匯出成 **section break**（Word 真正的分頁 / 換版單
        #     位），每頁自成一個 section，錨定物件才會落在正確頁。
        # 代價：styles.xml 多出「每頁一個 page-layout + master-page」（數十～數百個，
        # 檔案略大但無妨）。任何多頁文件皆適用，非針對特定檔案。
        pstyle = etree.SubElement(autostyles, _q("style", "style"))
        pstyle.set(_q("style", "name"), "JtPg%d" % i)
        pstyle.set(_q("style", "family"), "paragraph")
        pstyle.set(_q("style", "master-page-name"), "JtMP_p%d" % i)
        per_page_size.append((w, h))
        prev_group = g
        para = etree.SubElement(text_el, _q("text", "p"))
        para.set(_q("text", "style-name"), "JtPg%d" % i)
        # 設計感頁面 raster fallback → 改嵌原 PDF 整頁圖，像素級還原（該頁不可編輯，
        # 設計封面本就不會去編）。三種觸發：
        #  ① 色塊爆量 **且文字框比例極低**（漸層 / 圖案被 Draw 拆成上千純色拼塊、
        #     幾乎無文字）——**密集表單雖形狀多但文字框比例高（IRS 1040 = 50%），
        #     必須排除、保留可編輯 vector**（否則整份表單變不可填的圖片）；
        #  ② 全出血大圖 + 疊字（照片封面）；③ 斜切 / 旋轉圖片（Writer 渲染變黑）。
        shape_count = sum(1 for c in page if isinstance(c.tag, str))
        gradient_heavy = (shape_count > _RASTER_SHAPE_THRESHOLD
                          and _text_frame_count(page) < shape_count * 0.10)
        if pdf_path is not None and (
                gradient_heavy
                or _page_has_large_image(page, w, h)
                or _page_has_transformed_image(page)):
            png = _render_pdf_page_png(pdf_path, i - 1, remove_text=True)
            if png is not None:
                _emit_raster_page(para, autostyles, pics, png, i, w, h,
                                  z=z_counter)
                z_counter += 1
                # 真實文字框放在背景圖之上 → 直接可見 / 可選取 / 可編輯（背景圖已移
                # 除真實文字,不會雙重;漸層 / 外框字標題留在背景圖裡保真）。
                z_counter += _move_text_frames_over_raster(page, para, i, w,
                                                          z_start=z_counter)
                continue  # 非文字形狀（漸層色塊 / 外框路徑）不搬,已在背景圖
        # 先清掉 PDF 疊印假粗體造成的重複 / 被覆蓋文字框（見 _dedup_overprint）
        _dedup_overprint(page)
        # 搬移該頁所有頂層形狀（frame / line / rect / custom-shape / …）
        for shape in list(page):
            # 跳過非元素節點（comment / PI）— 否則 etree.QName 會丟 ValueError
            if not isinstance(shape.tag, str):
                continue
            tag = etree.QName(shape).localname
            if not tag:
                continue
            shape.set(_q("text", "anchor-type"), "paragraph")  # 見 _emit_raster_page 註解
            # 明確編號疊放順序（沿用 odg 的繪製順序）→ 匯出 .docx 後底色矩形才不會
            # 蓋到文字（見 z_counter 的說明）。
            shape.set(_q("draw", "z-index"), str(z_counter))
            z_counter += 1
            # 無文字的純裝飾形狀 → 背景層（見 _bg_style_name 的說明：.docx 匯出
            # 時這類形狀走 VML 會掉 z-index，不丟到背景層就會蓋掉整頁文字）。
            if autostyles is not None and not _shape_has_text(shape):
                _sn = shape.get(_q("draw", "style-name"))
                _bg = _bg_style_name(autostyles, bg_style_cache, _sn)
                if _bg:
                    shape.set(_q("draw", "style-name"), _bg)
            # 文字方塊防裁尾：Draw 把 svg:width 定成剛好容納原字寬，Writer 重繪時
            # 字型 metric 略增就換行、再被矮高度垂直裁掉最後一字（auto-grow 對錨定
            # 框無效）。給含文字的 frame 加一點寬度餘裕（比例＋固定），防換行掉字。
            if tag == "frame" and shape.find(_q("draw", "text-box")) is not None:
                _pad_frame_width(shape, page_w_cm=w)
            para.append(shape)  # 從 draw:page 搬到 text:p

    # 零頁（空白 / 無法解析的 PDF）：至少放一個空段落，保證是合法非空 Writer body。
    # 段落綁 JtPg0 → JtMP0，讓空文件仍套正確頁面尺寸（否則 fallback 成 Letter）。
    if n_pages == 0:
        if not size_groups:
            size_groups.append(page_sizes[0] if page_sizes else (21.0, 29.7))
        p0 = etree.SubElement(autostyles, _q("style", "style"))
        p0.set(_q("style", "name"), "JtPg0")
        p0.set(_q("style", "family"), "paragraph")
        p0.set(_q("style", "master-page-name"), "JtMP0")
        ep = etree.SubElement(text_el, _q("text", "p"))
        ep.set(_q("text", "style-name"), "JtPg0")

    # 用新的 office:text 取代 office:drawing
    body.remove(drawing)
    body.append(text_el)

    content_out = etree.tostring(root, xml_declaration=True, encoding="UTF-8")

    # 3) 自組乾淨的 Writer styles.xml。
    #    **每一頁一個 master-page + page-layout**（JtMP_p{i}）——換 master page 是
    #    Writer 的換頁機制，且匯出 .docx 時會變成 section break（Word 的分頁單位），
    #    頁面錨定物件才會落在正確的頁（見上方段落樣式處的說明）。
    #    另保留尺寸群組版 JtMP{g}（零頁 fallback 用）。
    masters = "".join(
        '<style:master-page style:name="JtMP_p%d" style:page-layout-name="JtPL_p%d"/>' % (i, i)
        for i in range(1, len(per_page_size) + 1)
    ) + "".join(
        '<style:master-page style:name="JtMP%d" style:page-layout-name="JtPL%d"/>' % (g, g)
        for g in range(len(size_groups))
    )
    layouts = "".join(
        '<style:page-layout style:name="JtPL_p%d"><style:page-layout-properties '
        'fo:page-width="%.3fcm" fo:page-height="%.3fcm" fo:margin="0cm"/></style:page-layout>'
        % (i, w, h) for i, (w, h) in enumerate(per_page_size, start=1)
    ) + "".join(
        '<style:page-layout style:name="JtPL%d"><style:page-layout-properties '
        'fo:page-width="%.3fcm" fo:page-height="%.3fcm" fo:margin="0cm"/></style:page-layout>'
        % (g, w, h) for g, (w, h) in enumerate(size_groups)
    )
    # 注意 ODF schema 要求固定順序：office:styles → automatic-styles → master-styles，
    # 且要有 <office:styles/>。順序錯或缺 office:styles → LibreOffice 嚴格 parser
    # 拒收整份 styles.xml → 頁面尺寸 fallback 成內建 Letter（踩過）。
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-styles '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
        'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
        'office:version="1.3">'
        '<office:styles/>'
        '<office:automatic-styles>%s</office:automatic-styles>'
        '<office:master-styles>%s</office:master-styles>'
        '</office:document-styles>' % (layouts, masters)
    ).encode("utf-8")

    # 4) manifest：mimetype = text，保留 Pictures
    man = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<manifest:manifest '
        'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
        'manifest:version="1.3">',
        '<manifest:file-entry manifest:full-path="/" '
        'manifest:media-type="application/vnd.oasis.opendocument.text"/>',
        '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>',
        '<manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>',
    ]
    _mt = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
           "gif": "image/gif", "bmp": "image/bmp", "svg": "image/svg+xml"}
    # zip-slip / 注入 防禦（defence-in-depth；Pictures 名稱來自 soffice 產的 odg）：
    # ① 過濾 .. / 絕對路徑 / 反斜線（路徑穿越）、控制字元；
    # ② full-path 一律 XML-escape（惡意名稱含 " & < > 會破壞 manifest.xml，
    #    導致整份 .odt 無法開啟 / 圖片無聲掉落）。
    def _pic_ok(name: str) -> bool:
        if ".." in name or name.startswith("/") or "\\" in name:
            return False
        return all(ord(ch) >= 0x20 for ch in name)  # 無控制字元

    safe_pics = {p: d for p, d in pics.items() if _pic_ok(p)}
    for p in safe_pics:
        ext = p.rsplit(".", 1)[-1].lower() if "." in p else ""
        man.append('<manifest:file-entry manifest:full-path="%s" manifest:media-type="%s"/>'
                   % (_xml_escape(p, {'"': "&quot;"}),
                      _mt.get(ext, "application/octet-stream")))
    man.append("</manifest:manifest>")
    manifest_xml = "".join(man).encode("utf-8")

    # 5) 打包 Writer .odt（mimetype 必須第一個且不壓縮）。
    #    原子寫入：先寫 .tmp 再 os.replace，避免中途失敗（MemoryError / 磁碟滿）
    #    在 out_path 留下半截毀損檔被後續流程誤用。
    odt_out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = odt_out.with_name(odt_out.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as zo:
            zo.writestr("mimetype", "application/vnd.oasis.opendocument.text",
                        compress_type=zipfile.ZIP_STORED)
            zo.writestr("content.xml", content_out)
            zo.writestr("styles.xml", styles_xml)
            zo.writestr("META-INF/manifest.xml", manifest_xml)
            for p, data in safe_pics.items():
                zo.writestr(p, data)
        # os.replace 原子覆蓋；Windows 上 AV / 索引器可能瞬間鎖住目的檔 →
        # 短暫重試幾次（每次 backoff 遞增），避免偶發 PermissionError 變成整個轉換失敗。
        for attempt in range(4):
            try:
                os.replace(tmp_out, odt_out)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.1 * (attempt + 1))
    except BaseException:
        try:
            if tmp_out.exists():
                tmp_out.unlink()
        except OSError:
            pass
        raise

    return n_pages, len(safe_pics), z_counter


# ── 大型文件自動分段 ────────────────────────────────────────────────────────
# 為什麼要分段：soffice 的 odt→docx 匯出耗時隨物件數 **超線性（約 O(n²)）** 成長。
# 實測 berkshire 年報（152 頁）：整份 18,882 個物件 → 匯出超過 15 分鐘仍未完成；
# 同一份切成 38 頁一段後，2,641 物件段只要 11 秒、7,208 物件段 45 秒、9,497 物件段
# 88 秒，全部都轉得出來。tw_twsa（78 頁 / 22MB）更是連 PDF→Draw 匯入都逾時。
#
# 分段是**內部實作細節**：轉完之後在 Python 端把各段合併回單一檔案（見 doc_merge），
# 使用者拿到的仍是一份完整 .odt / .docx。合併不經過 soffice，所以不會再踩 O(n²)。
_CHUNK_OBJ_TARGET = 2500      # 每段估算物件數上限
_CHUNK_EST_FACTOR = 2.0       # PyMuPDF 行數 → 實際物件數的保守倍率（實測 1.4~2.7）


def estimate_page_objects(pdf_path: Path) -> list[int]:
    """估每頁會產生多少物件（文字方塊 / 圖形）。**不呼叫 soffice**，只用 PyMuPDF
    數文字行數再乘保守倍率，成本 0.1~3 秒，可在轉換前先跑來決定要不要分段。"""
    import fitz

    counts: list[int] = []
    try:
        with fitz.open(str(pdf_path)) as doc:
            for page in doc:
                lines = 0
                try:
                    for b in page.get_text("dict").get("blocks", []):
                        lines += len(b.get("lines", []))
                except Exception:  # noqa: BLE001 — 個別頁抽不出來就當 0，不影響整體估算
                    pass
                counts.append(int(lines * _CHUNK_EST_FACTOR))
    except Exception as e:  # noqa: BLE001 — 估算失敗 → 回空，呼叫端退回不分段
        log.warning("estimate_page_objects 失敗 %s: %s", pdf_path.name, e)
        return []
    return counts


def plan_page_chunks(per_page: list[int],
                     target: int = _CHUNK_OBJ_TARGET) -> list[tuple[int, int]]:
    """依每頁估算物件數把頁面打包成 [(起頁, 迄頁)]（0-based、含迄頁）。

    用貪婪打包而非固定頁數，因為**密度差很大**：同一份年報 38 頁一段可能是 2,641
    物件，下一段同樣 38 頁卻是 9,497。單頁就超標時自成一段（不可能再切更細）。
    """
    if not per_page:
        return []
    chunks: list[tuple[int, int]] = []
    start = 0
    acc = 0
    for i, n in enumerate(per_page):
        if acc and acc + n > target:
            chunks.append((start, i - 1))
            start, acc = i, 0
        acc += n
    chunks.append((start, len(per_page) - 1))
    return chunks


def convert_via_draw_chunked(pdf_path: Path, out_path: Path,
                             output_format: str = "odt",
                             timeout: float = 180.0,
                             target: int = _CHUNK_OBJ_TARGET,
                             progress_cb=None) -> dict:
    """把 PDF 分段轉換後**合併回單一** .odt / .docx。

    分段只是為了繞開 soffice 的 O(n²) 匯出與匯入逾時，屬內部實作細節；使用者拿到
    的仍是一份完整文件。合併在 Python 端做（見 doc_merge），不再經過 soffice。

    Returns:
        dict: {"ok", "pages", "images", "objects", "parts", "chunks":[{...}],
               "engine": "draw-chunked", "error"}
    """
    import tempfile

    import fitz

    from .doc_merge import merge_docx, merge_odt

    pdf_path, out_path = Path(pdf_path), Path(out_path)
    ext = "odt" if output_format == "odt" else "docx"
    per_page = estimate_page_objects(pdf_path)
    ranges = plan_page_chunks(per_page, target)
    if not ranges:
        return {"ok": False, "pages": 0, "images": 0, "objects": 0, "parts": 0,
                "chunks": [], "engine": "draw-chunked", "error": "無法讀取 PDF 頁面"}

    stem = re.sub(r"[^\w.\-]", "_", pdf_path.stem)[:60] or "part"
    total_pages = total_imgs = total_objs = 0
    infos: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="jtdt-chunk-") as td:
        tdp = Path(td)
        with fitz.open(str(pdf_path)) as doc:
            n_digits = max(3, len(str(doc.page_count)))
            for idx, (a, b) in enumerate(ranges, 1):
                part_pdf = tdp / f"src{idx}.pdf"
                sub = fitz.open()
                try:
                    sub.insert_pdf(doc, from_page=a, to_page=b)
                    sub.save(str(part_pdf))
                finally:
                    sub.close()
                name = "%s_p%0*d-%0*d.%s" % (stem, n_digits, a + 1,
                                             n_digits, b + 1, ext)
                # 分段進度：整體 0.1~0.9 之間依段數平均分配，段內再細分階段
                lo = 0.1 + 0.8 * (idx - 1) / len(ranges)
                span = 0.8 / len(ranges)

                def _sub(msg, frac, _lo=lo, _span=span, _i=idx,
                         _a=a, _b=b):
                    if progress_cb:
                        try:
                            progress_cb("第 %d/%d 段（第 %d-%d 頁）— %s"
                                        % (_i, len(ranges), _a + 1, _b + 1, msg),
                                        _lo + _span * max(0.0, min(1.0, frac)))
                        except Exception:  # noqa: BLE001
                            pass

                res = convert_via_draw(part_pdf, tdp / name, output_format,
                                       timeout=timeout, progress_cb=_sub)
                if not res.get("ok"):
                    return {"ok": False, "pages": total_pages, "images": total_imgs,
                            "objects": total_objs, "parts": len(infos),
                            "chunks": infos, "engine": "draw-chunked",
                            "error": "第 %d-%d 頁轉換失敗：%s"
                                     % (a + 1, b + 1, res.get("error") or "未知錯誤")}
                total_pages += res.get("pages") or 0
                total_imgs += res.get("images") or 0
                total_objs += res.get("objects") or 0
                infos.append({"name": name, "from_page": a + 1, "to_page": b + 1,
                              "objects": res.get("objects") or 0})

        # 合併回單一檔案（使用者要的是一份文件，不是一包分段檔）
        if progress_cb:
            try:
                progress_cb("合併 %d 段為單一檔案…" % len(infos), 0.92)
            except Exception:  # noqa: BLE001
                pass
        part_paths = [tdp / info["name"] for info in infos]
        try:
            if ext == "odt":
                merge_odt(part_paths, out_path)
            else:
                merge_docx(part_paths, out_path)
        except Exception as e:  # noqa: BLE001 — 合併失敗要能明確回報，不吞掉
            log.warning("分段合併失敗 %s: %s", pdf_path.name, e)
            return {"ok": False, "pages": total_pages, "images": total_imgs,
                    "objects": total_objs, "parts": len(infos), "chunks": infos,
                    "engine": "draw-chunked", "error": "分段合併失敗：%s" % e}
        if not out_path.exists():
            return {"ok": False, "pages": total_pages, "images": total_imgs,
                    "objects": total_objs, "parts": len(infos), "chunks": infos,
                    "engine": "draw-chunked", "error": "分段合併後沒有產出檔案"}

    return {"ok": True, "pages": total_pages, "images": total_imgs,
            "objects": total_objs, "parts": len(infos), "chunks": infos,
            "engine": "draw-chunked", "error": ""}


def convert_via_draw(pdf_path: Path, out_path: Path,
                     output_format: str = "odt",
                     timeout: float = 180.0,
                     progress_cb=None) -> dict:
    """PDF → Draw 版面重現的 .odt / .docx。

    Args:
        pdf_path: 來源 PDF
        out_path: 輸出檔（副檔名依 output_format）
        output_format: "odt" 或 "docx"
        timeout: 每次 soffice 呼叫的逾時秒數
        progress_cb: ``cb(message, frac)`` — 回報目前階段給 UI。轉檔動輒數分鐘，
            沒有階段回饋使用者會以為當掉（三個階段的耗時比例差很大，實測匯出最久）。

    Returns:
        dict: {"ok": bool, "pages": int, "images": int, "engine": "draw", "error": str}
    """
    pdf_path = Path(pdf_path)
    out_path = Path(out_path)
    work = out_path.parent
    work.mkdir(parents=True, exist_ok=True)
    # 中間檔名帶 output_format，避免同一 uid 同時轉 odt + docx 時互踩 .draw.odg / .draw.odt
    tag = "odt" if output_format == "odt" else "docx"
    odg = work / (pdf_path.stem + ".draw.%s.odg" % tag)
    odt = out_path if output_format == "odt" else work / (pdf_path.stem + ".draw.%s.odt" % tag)

    def _tick(msg: str, frac: float) -> None:
        if progress_cb:
            try:
                progress_cb(msg, frac)
            except Exception:  # noqa: BLE001 — 進度回報失敗絕不可影響轉檔
                pass

    try:
        # 1) PDF → odg（Draw 匯入）
        _tick("匯入 PDF 版面…", 0.15)
        try:
            office_convert.convert_to_odg(pdf_path, odg, timeout=timeout)
        except RuntimeError as e:
            # soffice 回 0 但沒產出 .odg 多半是「加密/需密碼/非 PDF/格式無法解析」
            msg = str(e)
            if "找不到輸出" in msg:
                msg = ("無法用 Draw 匯入此 PDF（可能已加密需密碼、不是有效 PDF、"
                       "或含 LibreOffice/OxOffice 無法解析的內容）。請改用 pdf2docx / "
                       "jtdt-reform 引擎，或先解除密碼。")
            return {"ok": False, "pages": 0, "images": 0, "engine": "draw", "error": msg}
        # 2) odg → 合法 Writer odt（每頁尺寸）
        page_sizes = _pdf_page_sizes_cm(pdf_path)
        _tick("重組版面（共 %d 頁）…" % len(page_sizes), 0.45)
        n_pages, n_imgs, n_objs = _build_writer_odt(odg, odt, page_sizes,
                                                    pdf_path=pdf_path)
        # 3) 要 docx 再走標準 Writer→Word
        if output_format == "docx":
            _tick("產生 Word 檔（%d 頁 / %d 個物件）…" % (n_pages, n_objs), 0.7)
            office_convert.convert_to_docx(odt, out_path, timeout=timeout)
            if not out_path.exists():
                return {"ok": False, "pages": n_pages, "images": n_imgs,
                        "objects": n_objs, "engine": "draw",
                        "error": "Writer odt → docx 失敗"}
            # 修正 Word 匯出掉失的 VML 疊放順序（見 _fix_docx_vml_zorder）
            _fix_docx_vml_zorder(out_path)
        return {"ok": True, "pages": n_pages, "images": n_imgs,
                "objects": n_objs, "engine": "draw", "error": ""}
    except Exception as e:  # noqa: BLE001 — 統一回報，不讓引擎例外炸掉 job
        log.warning("draw engine 失敗 %s: %s", pdf_path.name, e)
        return {"ok": False, "pages": 0, "images": 0, "engine": "draw", "error": str(e)}
    finally:
        try:
            if odg.exists():
                odg.unlink()
            if output_format == "docx" and odt.exists() and odt != out_path:
                odt.unlink()
        except OSError:
            pass
