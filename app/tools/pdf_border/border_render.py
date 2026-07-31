"""在每一頁畫上框線。

## 為什麼獨立成一個模組

畫框本身是純函式（給一個 page 與一組設定，畫上去），跟 HTTP 沒有關係。抽出來之後
預覽、送出、對外 API 三條路徑用的是**同一段程式**，不會出現「預覽跟實際輸出不一樣」
那種最難查的 bug。

## 兩種定位方式

* `page`（預設）—— 從**頁面邊緣**往內縮固定距離。投影片加外框最常見的就是這種：
  每一頁的框線位置完全一致，看起來才整齊。
* `content` —— 貼齊該頁**實際內容**的外接矩形。適合文件（每頁內容量不同時，框線
  跟著內容走）。內容範圍要同時看文字、圖片與向量繪圖，只看文字會把整頁的背景圖
  或表格框線漏掉。

## 旋轉頁要換座標系

`page.rect` 是**視覺**矩形（含 `/Rotate`），但 `draw_rect` 吃的是**內容**座標。
旋轉過的頁（90/180/270）如果直接拿視覺座標去畫，框線會跑到頁面外或只畫到一半。
一律先在視覺空間算好，再乘 `page.derotation_matrix` 轉回內容空間。
（同一個雷 pdf-pageno 在 issue #21 踩過。）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import fitz

from ...core.unit_convert import mm_to_pt

#: 虛線 / 點線的樣式（PyMuPDF 的 dashes 字串，單位是 pt，會依線寬縮放）
_DASH_PATTERNS = {
    "solid": None,
    "dashed": "[{a} {b}] 0",
    "dotted": "[{a} {b}] 0",
}


@dataclass
class BorderSpec:
    """一組框線設定。所有長度單位都是 mm（UI 上使用者填的就是 mm）。"""
    mode: str = "page"              # page | content
    margin_mm: float = 5.0
    width_pt: float = 1.5
    color: str = "#333333"
    style: str = "solid"            # solid | dashed | dotted
    radius_mm: float = 0.0
    opacity: float = 1.0

    #: 雙線：在主框線內側再畫一條，兩線之間留 `double_gap_mm`
    double: bool = False
    double_gap_mm: float = 1.5
    double_inner_width_pt: float = 0.0   # 0 = 與主線同粗

    #: 陰影 / 外光暈：在框線外側疊幾圈半透明筆畫
    shadow: bool = False
    shadow_color: str = "#000000"
    shadow_blur_mm: float = 1.2
    shadow_offset_mm: float = 0.6
    shadow_opacity: float = 0.25

    #: 要畫哪些頁（1 起算，None = 全部）；`skip_first` 專門給投影片封面用
    pages: Optional[set[int]] = None
    skip_first: bool = False


def hex_to_rgb01(hex_color: str) -> tuple[float, float, float]:
    h = (hex_color or "#000000").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return (int(h[0:2], 16) / 255.0,
                int(h[2:4], 16) / 255.0,
                int(h[4:6], 16) / 255.0)
    except (ValueError, IndexError):
        return (0.0, 0.0, 0.0)


def parse_pages(text: str, page_count: int) -> Optional[set[int]]:
    """把 "1,3,5-8" 解析成頁碼集合。空字串 / 解析不出東西回 None（＝全部）。

    回 None 而不是空集合 —— 空集合的語意是「一頁都不畫」，那是使用者打錯字時
    最糟的結果（送出去什麼都沒發生，還以為工具壞了）。
    """
    s = (text or "").strip()
    if not s:
        return None
    out: set[int] = set()
    for part in s.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo
            for n in range(max(1, lo), min(page_count, hi) + 1):
                out.add(n)
        else:
            try:
                n = int(part)
            except ValueError:
                continue
            if 1 <= n <= page_count:
                out.add(n)
    return out or None


def content_bbox(page) -> Optional[fitz.Rect]:
    """該頁實際內容的外接矩形（視覺座標）。整頁空白時回 None。

    文字、圖片、向量繪圖**三種都要看**：只看文字會漏掉整頁的背景圖與表格框線，
    框線就會縮到只剩文字那一塊。
    """
    r = fitz.Rect()
    r.x0, r.y0, r.x1, r.y1 = 1e9, 1e9, -1e9, -1e9
    found = False

    def _take(rect) -> None:
        nonlocal found
        try:
            q = fitz.Rect(rect)
        except (TypeError, ValueError):
            return
        if q.is_empty or q.is_infinite:
            return
        r.x0, r.y0 = min(r.x0, q.x0), min(r.y0, q.y0)
        r.x1, r.y1 = max(r.x1, q.x1), max(r.y1, q.y1)
        found = True

    for b in page.get_text("blocks") or []:
        _take(b[:4])
    try:
        for info in page.get_image_info() or []:
            _take(info.get("bbox"))
    except Exception:  # noqa: BLE001 — 取不到圖片資訊不該讓整個工具失敗
        pass
    try:
        for d in page.get_drawings() or []:
            _take(d.get("rect"))
    except Exception:  # noqa: BLE001
        pass
    if not found:
        return None
    # get_* 回的是內容座標；轉成視覺座標再回傳，讓呼叫端統一在視覺空間算
    return (r * page.rotation_matrix).normalize()


def _dashes_for(spec: BorderSpec, width: float) -> Optional[str]:
    if spec.style == "dashed":
        a = max(2.0, width * 3)
        return f"[{a:.2f} {a * 0.6:.2f}] 0"
    if spec.style == "dotted":
        # 點線＝很短的線段配圓端點；長度取線寬本身才會是圓點而不是短線
        a = max(0.1, width * 0.01)
        return f"[{a:.2f} {max(1.5, width * 2):.2f}] 0"
    return None


def _radius_frac(spec: BorderSpec, rect: fitz.Rect) -> Optional[float]:
    """PyMuPDF 的 radius 是「佔短邊的比例」不是絕對長度，要換算。"""
    if spec.radius_mm <= 0:
        return None
    short = min(rect.width, rect.height)
    if short <= 0:
        return None
    frac = mm_to_pt(spec.radius_mm) / short
    return max(0.0, min(0.5, frac))


def target_rect(page, spec: BorderSpec) -> Optional[fitz.Rect]:
    """算出這一頁要畫的框線矩形（**視覺**座標）。畫不出來時回 None。"""
    if spec.mode == "content":
        base = content_bbox(page)
        if base is None:
            return None                     # 空白頁沒有內容可以貼齊
        m = mm_to_pt(spec.margin_mm)
        r = fitz.Rect(base.x0 - m, base.y0 - m, base.x1 + m, base.y1 + m)
        # 不可以超出頁面 —— 溢出的部分列印時會被裁掉，看起來像框線斷掉
        r = r & page.rect
    else:
        m = mm_to_pt(spec.margin_mm)
        r = fitz.Rect(page.rect.x0 + m, page.rect.y0 + m,
                      page.rect.x1 - m, page.rect.y1 - m)
    # **線寬是置中於路徑的**：畫一條 30pt 的線，有 15pt 落在路徑外。邊距小的時候
    # 那一半會超出頁面被裁掉 —— 使用者把粗細從 10 調到 30，看起來卻幾乎沒變粗
    # （實際回報過）。把路徑再往內縮半個線寬，整條線才會完整留在頁面裡。
    half = _effective_width(spec, page) / 2.0
    r = fitz.Rect(r.x0 + half, r.y0 + half, r.x1 - half, r.y1 - half)
    if r.is_empty or r.width <= 0 or r.height <= 0:
        return None                          # 邊距 + 線寬大於頁面本身
    return r


def _effective_width(spec: BorderSpec, page) -> float:
    """實際會用的線寬（pt）。

    除了設定值本身的上限，再依**這一頁的短邊**夾一次 —— 名片大小的頁面配
    72pt 的框會整個被框吃掉，剩不下內容。上限取短邊的六分之一。
    """
    short = min(page.rect.width, page.rect.height)
    return max(0.1, min(spec.width_pt, short / 6.0))


def draw_border(page, spec: BorderSpec, *, page_no: int, total: int) -> bool:
    """在一頁上畫框線。回傳有沒有真的畫（跳過的頁回 False）。

    `page_no` 是 1 起算的頁碼。
    """
    if spec.skip_first and page_no == 1:
        return False
    if spec.pages is not None and page_no not in spec.pages:
        return False

    rect = target_rect(page, spec)
    if rect is None:
        return False

    deroto = page.derotation_matrix
    width = _effective_width(spec, page)
    color = hex_to_rgb01(spec.color)
    dashes = _dashes_for(spec, width)
    opacity = max(0.0, min(1.0, spec.opacity))

    def _stroke(vr: fitz.Rect, *, width: float, col, op: float,
                dash: Optional[str]) -> None:
        if vr.is_empty or width <= 0 or op <= 0:
            return
        cr = (vr * deroto).normalize()
        shape = page.new_shape()
        rad = _radius_frac(spec, cr)
        if rad:
            shape.draw_rect(cr, radius=rad)
        else:
            shape.draw_rect(cr)
        shape.finish(color=col, width=width, dashes=dash,
                     stroke_opacity=op, fill=None,
                     lineCap=1 if spec.style == "dotted" else 0,
                     lineJoin=1)
        shape.commit()

    # 陰影先畫（要在框線底下）。用「往外擴 + 位移」的幾圈半透明筆畫模擬柔邊 ——
    # 向量 PDF 沒有真的模糊，填色又會蓋住內容，所以只能用筆畫疊。
    if spec.shadow:
        scol = hex_to_rgb01(spec.shadow_color)
        dx = mm_to_pt(spec.shadow_offset_mm)
        blur = mm_to_pt(max(0.0, spec.shadow_blur_mm))
        steps = 4
        for i in range(steps):
            grow = blur * (i + 1) / steps
            op = spec.shadow_opacity * (1.0 - i / steps) * opacity
            _stroke(fitz.Rect(rect.x0 - grow + dx, rect.y0 - grow + dx,
                              rect.x1 + grow + dx, rect.y1 + grow + dx),
                    width=width, col=scol, op=op, dash=None)

    _stroke(rect, width=width, col=color, op=opacity, dash=dashes)

    if spec.double:
        # 兩條線之間要留的是**空白**間距，所以還要加上兩條線各自的半個線寬，
        # 否則線一粗就會黏在一起看起來像一條。
        gap = mm_to_pt(max(0.1, spec.double_gap_mm)) + width
        inner = fitz.Rect(rect.x0 + gap, rect.y0 + gap,
                          rect.x1 - gap, rect.y1 - gap)
        iw = spec.double_inner_width_pt or width
        _stroke(inner, width=iw, col=color, op=opacity, dash=dashes)
    return True


def apply(doc, spec: BorderSpec) -> int:
    """對整份文件畫框，回傳實際畫了幾頁。"""
    n = 0
    total = doc.page_count
    for i in range(total):
        if draw_border(doc[i], spec, page_no=i + 1, total=total):
            n += 1
    return n
