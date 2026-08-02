"""騎縫章 —— 把一個印章切成數片，分別蓋在連續幾頁上。

## 為什麼要有這個

合約、標案的實務作法：整疊文件蓋一個跨頁的章，**任何一頁被抽換或掉頁都看得出來**
（那一片對不起來）。這是紙本世界防抽換最直接的手段，數位化之後仍然被要求。

## 兩種模式的幾何不同

* **側邊騎縫（side）**：紙張**疊起來**、稍微扇開，章蓋在整疊的側邊。
  數位化之後每一頁各拿一片，**位置一律貼齊同一邊**；印出來疊好扇開時拼成完整的章。
* **對開跨頁（spread）**：紙張**並排攤開**（裝訂成冊的樣子），章橫跨接縫。
  每一頁拿到的是「章落在它身上的那一段」—— 第一頁在右緣、最後一頁在左緣。

兩者的切片方式一樣，**差別只在每一片放在頁面的哪個位置**。

## 一個容易做錯的地方：旋轉必須在切片之前

章要歪一點才像手工蓋的。但**先切片再各自旋轉**，每片會繞自己的中心轉，接縫立刻
對不起來 —— 那是這種工具最明顯的破綻。正確順序是**先把完整的章旋轉好，再切**。
這裡的 `_rotated_stamp()` 就是為了強制這個順序而存在。
"""
from __future__ import annotations

import io
import math
import random
from dataclasses import dataclass, field
from typing import Optional

import fitz
from PIL import Image

#: 一片最少要有多寬（mm）。頁數多時每片會很細，細到看不出是什麼就失去意義了 ——
#: 低於這個寬度就自動縮小每組頁數（並回報）。
MIN_SLICE_MM = 3.0

#: 亂數角度的上限。歪太多會蓋到內文，而且看起來不像蓋章像貼紙。
MAX_JITTER_ANGLE = 8.0


@dataclass
class SeamSpec:
    """騎縫章怎麼蓋。長度單位一律 mm、角度一律度。"""
    mode: str = "side"              # side（側邊騎縫）/ spread（對開跨頁）
    group: int = 2                  # 一個章跨幾頁；0 = 整份文件一個章
    edge: str = "right"             # side 模式貼哪一邊：right / left
    size_mm: float = 40.0           # 章的邊長（正方形章）
    offset_mm: float = 3.0          # 離頁緣多遠
    pos_mm: float = 0.0             # 縱向位置：0 = 置中，正值往下
    angle_deg: float = 0.0          # 固定角度
    opacity: float = 1.0

    jitter_pos: bool = False        # 每組縱向位置亂數
    jitter_pos_mm: float = 12.0
    jitter_angle: bool = False      # 每組角度亂數
    jitter_angle_deg: float = 4.0
    seed: int = 0                   # 0 = 隨機產生一個（會回報，以便重現）

    def normalized(self, page_count: int) -> "SeamSpec":
        """把值夾在合理範圍。**在伺服器端做** —— 前端的 min/max 只是提示。"""
        g = int(self.group or 0)
        if g <= 0 or g > page_count:
            g = page_count          # 0 或超過總頁數 = 整份一個章
        return SeamSpec(
            mode="spread" if self.mode == "spread" else "side",
            group=max(2, g) if page_count >= 2 else 1,
            edge="left" if self.edge == "left" else "right",
            size_mm=max(8.0, min(120.0, float(self.size_mm))),
            offset_mm=max(0.0, min(60.0, float(self.offset_mm))),
            pos_mm=max(-200.0, min(200.0, float(self.pos_mm))),
            angle_deg=max(-45.0, min(45.0, float(self.angle_deg))),
            opacity=max(0.05, min(1.0, float(self.opacity))),
            jitter_pos=bool(self.jitter_pos),
            jitter_pos_mm=max(0.0, min(60.0, float(self.jitter_pos_mm))),
            jitter_angle=bool(self.jitter_angle),
            jitter_angle_deg=max(0.0, min(MAX_JITTER_ANGLE,
                                          float(self.jitter_angle_deg))),
            seed=int(self.seed or 0),
        )


@dataclass
class SeamPlan:
    """算出來的結果：每一頁要蓋哪一片、蓋在哪。"""
    groups: list[list[int]] = field(default_factory=list)   # 每組的頁索引（0 起算）
    placements: list[dict] = field(default_factory=list)    # 每一片的位置
    seed: int = 0
    warnings: list[str] = field(default_factory=list)


def _mm(v: float) -> float:
    """mm → pt。"""
    return v * 72.0 / 25.4


def make_groups(page_count: int, group: int) -> list[list[int]]:
    """把頁碼切成組。**最後一組不足時就按實際頁數切** —— 補空白片會在最後一頁
    留下半個章，看起來像印壞了。"""
    if page_count <= 0:
        return []
    g = max(1, min(group, page_count))
    return [list(range(i, min(i + g, page_count)))
            for i in range(0, page_count, g)]


def _rotated_stamp(img: Image.Image, angle: float) -> Image.Image:
    """把**完整的章**旋轉好（切片之前）。

    順序很重要：先切再各自旋轉的話，每片會繞自己的中心轉，接縫對不起來。
    `expand=True` 讓旋轉後的畫布容得下整個章，不會被切角。
    """
    if not angle:
        return img
    return img.rotate(angle, resample=Image.BICUBIC, expand=True,
                      fillcolor=(0, 0, 0, 0))


def slice_stamp(img: Image.Image, n: int) -> list[Image.Image]:
    """把章縱向切成 n 片（由左至右）。

    用**累進取整**分配寬度，不是每片都 `w // n` —— 後者會在右邊留下最多 n-1 px
    的殘缺，拼起來看得出接縫。
    """
    n = max(1, n)
    w, h = img.size
    out: list[Image.Image] = []
    prev = 0
    for i in range(1, n + 1):
        cut = round(w * i / n)
        out.append(img.crop((prev, 0, max(cut, prev + 1), h)))
        prev = cut
    return out


def plan(doc: fitz.Document, spec: SeamSpec) -> SeamPlan:
    """算出每一頁要蓋哪一片、蓋在什麼位置。**不畫圖** —— 純幾何，好測。"""
    spec = spec.normalized(doc.page_count)
    groups = make_groups(doc.page_count, spec.group)
    warns: list[str] = []

    seed = spec.seed or random.randrange(1, 2**31 - 1)
    rnd = random.Random(seed)

    # 每一片太細就看不出是什麼 —— 回報但不擅自改設定（那是使用者的選擇）
    if groups and spec.size_mm / max(1, len(groups[0])) < MIN_SLICE_MM:
        warns.append(
            f"每組 {len(groups[0])} 頁時，一頁只分到約 "
            f"{spec.size_mm / len(groups[0]):.1f} mm 寬 —— 印出來會細到看不出是什麼。"
            f"建議把章放大，或減少每組頁數。")

    placements: list[dict] = []
    for g in groups:
        k = len(g)
        angle = spec.angle_deg
        if spec.jitter_angle and spec.jitter_angle_deg:
            angle += rnd.uniform(-spec.jitter_angle_deg, spec.jitter_angle_deg)
        dy = 0.0
        if spec.jitter_pos and spec.jitter_pos_mm:
            dy = rnd.uniform(-spec.jitter_pos_mm, spec.jitter_pos_mm)
        for i, pno in enumerate(g):
            placements.append({
                "page": pno, "slice": i, "slices": k,
                "angle": angle, "dy_mm": dy,
            })
    return SeamPlan(groups=groups, placements=placements, seed=seed,
                    warnings=warns)


def slice_rect(page: fitz.Page, spec: SeamSpec, idx: int, total: int,
               dy_mm: float, stamp_ratio: float) -> fitz.Rect:
    """這一片要蓋在頁面的哪個矩形。

    `stamp_ratio` = 旋轉後整個章的 高/寬，用來決定片的高度（旋轉會讓章變高變寬，
    片的比例要跟著走，否則會被壓扁）。

    * **side**：每一片都貼齊同一邊（印出來疊好扇開才拼得起來）。
    * **spread**：並排攤開時要接得上 —— 第一頁貼右緣、最後一頁貼左緣、
      中間的頁面整頁滿版。
    """
    r = page.rect
    w_pt = _mm(spec.size_mm)
    slice_w = w_pt / max(1, total)
    h_pt = w_pt * stamp_ratio
    cy = r.height / 2 + _mm(spec.pos_mm + dy_mm)
    y0, y1 = cy - h_pt / 2, cy + h_pt / 2
    off = _mm(spec.offset_mm)

    if spec.mode == "spread":
        # 並排攤開：章橫跨整組頁面的接縫
        if total == 1:
            x0 = (r.width - slice_w) / 2
        elif idx == 0:
            x0 = r.width - slice_w - off        # 第一頁 → 貼右緣
        elif idx == total - 1:
            x0 = off                             # 最後一頁 → 貼左緣
        else:
            x0 = (r.width - slice_w) / 2        # 中間頁 → 置中
    else:
        # 側邊騎縫：一律貼齊同一邊
        x0 = (r.width - slice_w - off) if spec.edge == "right" else off
    return fitz.Rect(x0, y0, x0 + slice_w, y1)


def apply_seam(doc: fitz.Document, stamp_png: bytes, spec: SeamSpec
               ) -> SeamPlan:
    """把騎縫章蓋上去。回 `SeamPlan`（含用到的亂數種子與警告）。"""
    spec = spec.normalized(doc.page_count)
    p = plan(doc, spec)

    base = Image.open(io.BytesIO(stamp_png)).convert("RGBA")
    # 每一組的角度可能不同 → 旋轉結果要快取，不要每頁重算
    cache: dict[float, tuple[list[Image.Image], float]] = {}

    for pl in p.placements:
        angle = round(pl["angle"], 2)
        key = (angle, pl["slices"])
        if key not in cache:
            rot = _rotated_stamp(base, angle)
            ratio = rot.size[1] / max(1, rot.size[0])
            cache[key] = (slice_stamp(rot, pl["slices"]), ratio)
        slices, ratio = cache[key]
        page = doc[pl["page"]]
        rect = slice_rect(page, spec, pl["slice"], pl["slices"],
                          pl["dy_mm"], ratio)
        buf = io.BytesIO()
        slices[pl["slice"]].save(buf, format="PNG")
        # 頁面有 /Rotate 時要換回內容座標，否則會蓋到別的地方
        # （PyMuPDF 的 insert_image 不理會頁面旋轉 —— v1.12.4 踩過）
        page.insert_image(rect * page.derotation_matrix, stream=buf.getvalue(),
                          overlay=True, rotate=page.rotation,
                          keep_proportion=False)
    return p


def reconstruct(stamp_png: bytes, spec: SeamSpec, n: int) -> bytes:
    """把切片依序拼回去，給預覽用 —— 讓使用者看到「疊好之後長什麼樣」。

    只看單頁的預覽是沒有意義的：使用者看到的是一條細片，判斷不出對不對。
    """
    base = Image.open(io.BytesIO(stamp_png)).convert("RGBA")
    rot = _rotated_stamp(base, spec.angle_deg)
    parts = slice_stamp(rot, max(1, n))
    gap = 3                                     # 片與片之間留一點縫，看得出切在哪
    w = sum(p.size[0] for p in parts) + gap * (len(parts) - 1)
    out = Image.new("RGBA", (w, rot.size[1]), (255, 255, 255, 0))
    x = 0
    for p in parts:
        out.paste(p, (x, 0), p)
        x += p.size[0] + gap
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()
