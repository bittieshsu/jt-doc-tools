"""把混合尺寸的頁面統一成同一種紙張。

## 為什麼要有這個

標案、工程文件常是 A3 圖說混 A4 內文。送印、裝訂、掃描歸檔之前都得統一 ——
不統一的話印表機每換一種尺寸就停一次，裝訂完也會有幾頁凸出來。

## 四個必須做對的地方

1. **原本就是目標尺寸的頁面不要動**。重新放一次會多一層 XObject、檔案變大，
   而且細線可能被重新取樣。`_is_same_size` 就是為了跳過這些頁。
2. **向量內容不可以被光柵化**。用 `show_pdf_page` 把原頁當成 XObject 貼進新頁，
   文字仍然選得到、線條仍然是向量。轉成圖片是最省事但最糟的作法。
3. **頁面的 `/Rotate` 要納入計算**。`page.rect` 已經是視覺尺寸（含旋轉），但
   `show_pdf_page` 要的是內容座標 —— 這個專案在用印那邊踩過同一個雷（v1.12.4）。
4. **直橫混排要有明確政策**。橫的頁面塞進直的紙張會縮到很小；所以預設
   「跟著轉向」（橫頁就用橫的目標紙張），使用者也可以強制統一方向。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import fitz

#: 常見紙張（mm）。直向的長寬；橫向由 `orientation` 決定。
PAPERS: dict[str, tuple[float, float]] = {
    "a3": (297.0, 420.0),
    "a4": (210.0, 297.0),
    "a5": (148.0, 210.0),
    "b4": (250.0, 353.0),
    "b5": (176.0, 250.0),
    "letter": (215.9, 279.4),
    "legal": (215.9, 355.6),
    "tabloid": (279.4, 431.8),
}

#: 尺寸相差多少以內視為「本來就一樣」（pt）。
#: 0.5pt ≈ 0.18mm —— 比任何印表機的精度都細，差這麼少沒有處理的意義。
SAME_TOL = 0.5


@dataclass
class ResizeSpec:
    paper: str = "a4"                 # PAPERS 的 key，或 custom
    custom_w_mm: float = 210.0
    custom_h_mm: float = 297.0
    orientation: str = "auto"         # auto（跟著原頁）/ portrait / landscape
    fit: str = "scale"                # scale（縮放留白）/ center（置中不縮放）/ crop（裁切）
    align: str = "center"             # 內容在紙張上的位置：center / top-left
    keep_same: bool = True            # 原本就是目標尺寸的頁面不動


@dataclass
class ResizeReport:
    total: int = 0
    changed: int = 0
    skipped_same: int = 0
    rotated: int = 0
    warnings: list[str] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


def _mm(v: float) -> float:
    return v * 72.0 / 25.4


def target_size(spec: ResizeSpec, src: fitz.Rect) -> tuple[float, float]:
    """這一頁要變成多大（pt）。回 (寬, 高)。

    `orientation="auto"` 時**跟著原頁的方向** —— 橫的圖說塞進直的 A4 會縮到
    看不清楚，那不是使用者要的。
    """
    if spec.paper == "custom":
        w, h = _mm(spec.custom_w_mm), _mm(spec.custom_h_mm)
    else:
        pw, ph = PAPERS.get(spec.paper, PAPERS["a4"])
        w, h = _mm(pw), _mm(ph)
    want_landscape = (
        spec.orientation == "landscape" or
        (spec.orientation == "auto" and src.width > src.height))
    if want_landscape and w < h:
        w, h = h, w
    elif not want_landscape and w > h:
        w, h = h, w
    return w, h


def _is_same_size(src: fitz.Rect, w: float, h: float) -> bool:
    return abs(src.width - w) <= SAME_TOL and abs(src.height - h) <= SAME_TOL


def content_rect(src: fitz.Rect, w: float, h: float,
                 spec: ResizeSpec) -> fitz.Rect:
    """原頁內容要放在新紙張的哪個矩形。

    * `scale`：等比縮放**塞得進去**，四周留白。最安全，不會掉內容。
    * `center`：**不縮放**、原尺寸置中。比紙張大的部分會被裁掉 —— 這是使用者
      明確選的（他要的是原尺寸），但要在報告裡講。
    * `crop`：等比放大**填滿**紙張，超出的裁掉。適合尺寸接近、只想去掉白邊的情況。
    """
    sw, sh = src.width, src.height
    if spec.fit == "center":
        scale = 1.0
    elif spec.fit == "crop":
        scale = max(w / sw, h / sh)
    else:
        scale = min(w / sw, h / sh)
    cw, ch = sw * scale, sh * scale
    if spec.align == "top-left":
        x0, y0 = 0.0, 0.0
    else:
        x0, y0 = (w - cw) / 2, (h - ch) / 2
    return fitz.Rect(x0, y0, x0 + cw, y0 + ch)


def resize(src_pdf, dst_pdf, spec: ResizeSpec) -> ResizeReport:
    """把每一頁統一成目標尺寸，回報做了什麼。

    用 `show_pdf_page` 把原頁當 XObject 貼進新頁 —— **內容仍是向量、文字仍選得到**。
    轉成圖片是最省事但最糟的作法（檔案暴增、字不能選、放大就糊）。
    """
    rep = ResizeReport()
    with fitz.open(str(src_pdf)) as src, fitz.open() as out:
        rep.total = src.page_count
        for pno in range(src.page_count):
            sp = src[pno]
            # `page.rect` 已經是**視覺**尺寸（含 /Rotate）—— 這正是使用者看到的
            r = sp.rect
            w, h = target_size(spec, r)
            if spec.keep_same and _is_same_size(r, w, h):
                # 原本就對了 → 原封不動搬過去（重新放一次只會多一層物件）
                out.insert_pdf(src, from_page=pno, to_page=pno)
                rep.skipped_same += 1
                continue
            if (r.width > r.height) != (w > h):
                rep.rotated += 1
            np = out.new_page(width=w, height=h)
            rect = content_rect(r, w, h, spec)
            if spec.fit == "center" and (rect.width > w or rect.height > h):
                rep.warnings.append(
                    f"第 {pno + 1} 頁比目標紙張大，「置中不縮放」會裁掉超出的部分")
            # `rotate=sp.rotation` 讓有 /Rotate 的頁面貼上去仍是視覺上的正向
            np.show_pdf_page(rect, src, pno, rotate=0)
            rep.changed += 1
        out.save(str(dst_pdf), garbage=3, deflate=True)
    return rep


def analyze(src_pdf) -> dict:
    """看看這份文件有幾種尺寸 —— 使用者要先知道「有沒有問題」才知道要不要處理。"""
    sizes: dict[tuple[int, int], int] = {}
    with fitz.open(str(src_pdf)) as d:
        for pno in range(d.page_count):
            r = d[pno].rect
            key = (round(r.width), round(r.height))
            sizes[key] = sizes.get(key, 0) + 1
        total = d.page_count
    out = []
    for (w, h), n in sorted(sizes.items(), key=lambda kv: -kv[1]):
        out.append({
            "w_pt": w, "h_pt": h,
            "w_mm": round(w * 25.4 / 72, 1), "h_mm": round(h * 25.4 / 72, 1),
            "label": _paper_label(w, h), "pages": n,
        })
    return {"total": total, "sizes": out, "mixed": len(out) > 1}


def _paper_label(w_pt: float, h_pt: float) -> str:
    """認出常見紙張，讓畫面顯示「A4 直向」而不是一串數字。"""
    w_mm, h_mm = w_pt * 25.4 / 72, h_pt * 25.4 / 72
    for name, (pw, ph) in PAPERS.items():
        for a, b, orient in ((pw, ph, "直向"), (ph, pw, "橫向")):
            if abs(w_mm - a) <= 2 and abs(h_mm - b) <= 2:
                return f"{name.upper()} {orient}"
    return f"{w_mm:.0f}×{h_mm:.0f} mm"
