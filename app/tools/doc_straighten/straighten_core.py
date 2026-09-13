"""文件拉正的核心：裁邊、拉正、去除不勻底色（選用：透視校正、二值化）。

**完全不用 AI、不用 GPU** —— 傳統影像處理，單執行緒 CPU 實測
200 dpi 0.83 秒/頁、300 dpi 1.4 秒/頁。

管線順序**不可以改**（規劃階段實測踩過）：

    去不勻底色 → 裁出紙張 → 估歪斜角 → 旋轉 →（選用）二值化

* **先估角再裁邊 → 角度會估成 0.00°**：四周的黑邊主導 `minAreaRect`，
  回傳的是整張圖的軸對齊矩形。
* **角度對了還可能轉錯方向**（負號加兩次），所以 `straighten_page()` 一定會
  「修正後再估一次」當自我檢查，把殘留角一起回報。

### ⚠ 「清晰化」做過頭會把文件弄壞

同一份 200 dpi 合成掃描件（歪 2.3°、黑邊、雜訊、漸層陰影），用
tesseract `chi_tra+eng` 量 OCR 相似度：

| 做法 | OCR 相似度 |
|---|---:|
| 沒修 | 0.767 |
| **只裁邊＋拉正（保持灰階）** | **0.775**（＝乾淨原稿） |
| ＋去底色 | 0.775 |
| ＋Otsu 全域二值 | 0.775 |
| ＋局部二值 blk41 | 0.600 |
| ＋局部二值 blk61 | **0.108** |

幾何修正是穩賺的；**二值化是負的**（中文細筆畫會被吃掉），而且
**看起來最乾淨的那張正是最爛的那張**。所以二值化是選項、**預設關閉**，
介面要寫明它的用途是縮檔案大小、不是提高辨識率。
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


def normalize_illum(g: np.ndarray) -> np.ndarray:
    """背景估計相除 —— 壓掉壓書造成的漸層陰影。後面每一步都依賴這個。"""
    bg = cv2.morphologyEx(g, cv2.MORPH_CLOSE,
                          cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (41, 41)))
    return cv2.divide(g, bg, scale=255)


def crop_page(g: np.ndarray) -> np.ndarray:
    """裁掉掃描機蓋板的暗邊。找不到（或找到的東西太小）就整張退回，不敢裁。"""
    bw = cv2.threshold(normalize_illum(g), 200, 255, cv2.THRESH_BINARY)[1]
    bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, np.ones((15, 15), np.uint8))
    cnts, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return g
    x, y, w, h = cv2.boundingRect(max(cnts, key=cv2.contourArea))
    return g if w * h < g.size * 0.2 else g[y:y + h, x:x + w]


def deskew_angle(g: np.ndarray, limit: float = 6.0, step: float = 0.1) -> float:
    """投影剖面法：回傳「要轉多少度才會正」。

    轉到正確角度時每一列的黑點數變化最劇烈 → 相鄰列差平方和最大。
    比 minAreaRect 穩（不受黑邊與雜點影響）。
    """
    bw = cv2.threshold(normalize_illum(g), 0, 255,
                       cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    s = cv2.resize(bw, None, fx=0.35, fy=0.35, interpolation=cv2.INTER_AREA)
    h, w = s.shape
    best, best_a = -1.0, 0.0
    for a in np.arange(-limit, limit + 1e-9, step):
        M = cv2.getRotationMatrix2D((w / 2, h / 2), float(a), 1.0)
        prof = cv2.warpAffine(s, M, (w, h), flags=cv2.INTER_NEAREST,
                              borderValue=0).sum(1, dtype=np.float64)
        v = float(((prof[1:] - prof[:-1]) ** 2).sum())
        if v > best:
            best, best_a = v, float(a)
    return best_a


def rotate(img: np.ndarray, a: float) -> np.ndarray:
    h, w = img.shape[:2]
    return cv2.warpAffine(img, cv2.getRotationMatrix2D((w / 2, h / 2), a, 1.0),
                          (w, h), flags=cv2.INTER_CUBIC, borderValue=255)


def _paper_mask(g: np.ndarray, rgb=None):
    """紙張的遮罩（四分之一尺寸）。回 `(縮圖, 遮罩)`。

    **只用亮度分割會在陰影處把紙切掉一半。** 實測使用者的手機照片
    `IMG_2905`：紙有一角落在陰影裡，Otsu 把那一塊判成桌面 —— 遮罩只蓋到
    32% 的畫面，而紙實際佔 40%，**那一角上面有字**。

    所以再加一層**色度**：紙是中性色（Lab 的 a/b 接近 128），木頭桌面 /
    橘色桌墊 / 綠色滑鼠墊偏離很多，**而且影子不改變色相**（只改亮度）。
    兩張遮罩各自做完形態學再取聯集：Otsu 負責一般情況，色度負責陰影。

    實測（IMG_2905）：32% → **40%**，紙上的墨水從 0.747 變成 **1.000**。
    """
    s = cv2.resize(g, None, fx=0.25, fy=0.25)
    b = cv2.GaussianBlur(s, (9, 9), 0)
    _t, m = cv2.threshold(b, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    if rgb is not None and getattr(rgb, "ndim", 2) == 3:
        c = cv2.resize(rgb, (s.shape[1], s.shape[0]), interpolation=cv2.INTER_AREA)
        lab = cv2.cvtColor(cv2.GaussianBlur(c, (9, 9), 0), cv2.COLOR_RGB2LAB)
        L, A, B = cv2.split(lab)
        chroma = np.hypot(A.astype(np.float32) - 128.0, B.astype(np.float32) - 128.0)
        # 中性色 ＋ 夠亮。**亮度門檻要相對於「確定是紙」那一塊**
        # （Otsu 選出來的亮區），不可以用整張圖的百分位 ——
        # 陰影面積一大，百分位自己就被拉上去，陰影裡的紙反而被排除
        # （實測：陰影裡的紙 L=118，而第 25 百分位是 122，整條判準失效）。
        # 係數掃過 0.30 / 0.40 / 0.45 / 0.55 / 0.65：**0.45 以下三個樣本
        # 全部滿分，0.55 起崩**（純度掉到 0.74 / 0.88）。取 0.45 留安全邊界，
        # 對「深色但中性」的桌面仍然擋得住（它會暗於紙的 45%）。
        ref = float(np.median(L[m > 0])) if (m > 0).any() else 255.0
        mc = ((chroma < 12) & (L > 0.45 * ref)).astype(np.uint8) * 255
        mc = cv2.morphologyEx(mc, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
        mc = cv2.morphologyEx(mc, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
        m = cv2.bitwise_or(m, mc)
    return s, m


def _quad_candidates(c: np.ndarray):
    """幾個候選四邊形（四分之一尺寸的座標）。

    **不要只產生一種。** 教科書的 `approxPolyDP` 在真實照片上常常回 5、6 個
    點（角落有陰影與圓角），而最小外接矩形永遠回得出四個點卻會多框一條桌面。
    OSS 的文件掃描 app 也是這個路數：**多產幾個候選，再用一個判準挑**。
    """
    hull = cv2.convexHull(c)
    out = []
    peri = cv2.arcLength(hull, True)
    for pct in (x / 1000.0 for x in range(5, 121)):
        ap = cv2.approxPolyDP(hull, pct * peri, True)
        if len(ap) == 4:
            out.append(ap.reshape(4, 2).astype(np.float32))
            break
    out.append(cv2.boxPoints(cv2.minAreaRect(hull)).astype(np.float32))
    return out


def _quad_score(s: np.ndarray, mask: np.ndarray, quad: np.ndarray):
    """回 `(墨水涵蓋率, 純度)` —— 兩個都是 0~1，都算在四分之一尺寸上。

    * **墨水涵蓋率**：紙上的字有多少被框進去。**切到字是不可原諒的失敗**，
      所以這個是硬條件。量的時候紙要先**內縮**幾個像素 —— 不然紙緣的陰影會
      被當成字，讓「剛好切在紙邊」的候選看起來像切到內容（實測 IMG_2904
      因此被低估 7%）。
    * **純度**：框裡面有多少真的是紙。低就是框到桌面 —— 那就是使用者看到的
      黑邊（實測現行做法在 IMG_2905 只有 0.925）。
    """
    inner = cv2.erode(mask, np.ones((7, 7), np.uint8))
    paper = inner > 0
    if not paper.any():
        return 0.0, 0.0
    thr = np.percentile(s[paper], 12)      # 紙上最暗的一成二 ≈ 筆跡
    ink = paper & (s <= thr)
    poly = np.zeros(mask.shape, np.uint8)
    cv2.fillPoly(poly, [quad.astype(np.int32)], 255)
    inside = poly > 0
    full = mask > 0
    return ((ink & inside).sum() / max(1, int(ink.sum())),
            (full & inside).sum() / max(1, int(inside.sum())))


def find_page_quad(g: np.ndarray, rgb=None):
    """手機翻拍：找紙張的四邊形。回 None 表示沒把握 → 走純拉正那條路。

    ## 做法（v1.15.39 用真實照片重寫第二次）

    1. **遮罩**：Otsu ∪ 色度（見 `_paper_mask`）。只用 Otsu 的話，陰影裡的
       半張紙會被判成桌面。
    2. **候選**：`approxPolyDP` 掃 epsilon 找四個點，加上最小外接矩形。
    3. **評分**：先要求**墨水涵蓋率不可以比最好的那個差 1% 以上**（切到字是
       不可原諒的），在這個前提下挑**純度最高**的。

    ### 為什麼不是單一演算法

    | 做法 | IMG_2904 | IMG_2905 |
    |---|---|---|
    | 舊：Otsu ＋ 最小外接矩形 | ink 0.738 / 純度 0.909 | ink 0.747 / 純度 0.925 |
    | 新：Otsu∪色度 ＋ 評分挑選 | ink 0.737 / **純度 1.000** | **ink 1.000 / 純度 1.000** |

    純度 0.925 的意思是**框進去的東西有 7.5% 是桌面** —— 使用者截圖回報的
    就是那一圈黑邊（2026-09-13）。

    ### 踩過的兩個坑（留著，不要再走一次）

    * **先去陰影再分割是錯的**：`normalize_illum()` 會把桌面也一起提亮，
      亮區從 21% 爆到 58~83%，四個角直接跑到畫面邊界。
    * **殘留角不能當判準**：把四邊形縮進紙內會切掉內容，而**殘留角仍然是
      -0.05°**，看起來完美。殘留角量的是「裁出來那塊正不正」，
      **不是「有沒有抓對紙」**。所以這裡的判準是墨水涵蓋率與純度。
    """
    s, m = _paper_mask(g, rgb)
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    frac = cv2.contourArea(c) / float(s.shape[0] * s.shape[1])
    # 太小 = 不是紙（可能是反光）；太大 = 整張都是紙（掃描件），不需要透視校正
    if frac < 0.05 or frac > 0.95:
        return None
    # 評分只看「紙那一塊」—— 畫面上別的亮物（鍵盤、白牆）不算
    only = np.zeros_like(m)
    cv2.drawContours(only, [c], -1, 255, cv2.FILLED)
    best = None
    for q in _quad_candidates(c):
        full = (q * 4).astype(np.float32)
        if not quad_is_sane(full, g.shape):
            continue
        ink, pur = _quad_score(s, only, q)
        if best is None or ink > best[0] + 1e-9:
            best = (ink, pur, full)
    if best is None:
        return None
    top_ink = best[0]
    chosen = best
    for q in _quad_candidates(c):
        full = (q * 4).astype(np.float32)
        if not quad_is_sane(full, g.shape):
            continue
        ink, pur = _quad_score(s, only, q)
        # 墨水不可以比最好的差 1% 以上；在這個前提下挑純度最高的
        if ink >= top_ink - 0.01 and pur > chosen[1]:
            chosen = (ink, pur, full)
    return chosen[2]


def order_quad(q: np.ndarray) -> np.ndarray:
    """把四個角排成 左上→右上→右下→左下。

    **手動模式必須在伺服器端跑這一步** —— 使用者可以把左上拖到右下去，
    順序亂掉會產生鏡像或轉 180° 的結果。
    """
    su, d = q.sum(1), np.diff(q, axis=1).ravel()
    return np.float32([q[np.argmin(su)], q[np.argmin(d)],
                       q[np.argmax(su)], q[np.argmax(d)]])


#: 四個內角的極差上限（度）。紙是矩形，拍歪之後仍然接近 90°；
#: 框到桌面的那種歪四邊形極差會衝到 40° 以上。
#: 這條判準借自 `andrewdcampbell/OpenCV-Document-Scanner`（640★）的
#: `angle_range`，並用今天的真實失敗案例驗過：抓對的 9°、框到桌面的 44°、
#: 整個畫面的 44°。
_MAX_ANGLE_RANGE = 40.0


def quad_angle_range(q: np.ndarray) -> float:
    """四個內角的極差（度）。"""
    o = order_quad(q)
    ang = []
    for i in range(4):
        a, b, c = o[(i - 1) % 4], o[i], o[(i + 1) % 4]
        v1, v2 = a - b, c - b
        n = float(np.linalg.norm(v1) * np.linalg.norm(v2))
        if n < 1e-6:
            return 360.0
        cosv = float(np.dot(v1, v2) / n)
        ang.append(np.degrees(np.arccos(max(-1.0, min(1.0, cosv)))))
    return float(max(ang) - min(ang))


def quad_is_sane(q: np.ndarray, shape) -> bool:
    """擋自交（蝴蝶結）、面積過小、角點跑到圖外 —— warpPerspective 不會報錯。"""
    h, w = shape[:2]
    if (q < -2).any() or (q[:, 0] > w + 2).any() or (q[:, 1] > h + 2).any():
        return False
    o = order_quad(q)
    if cv2.contourArea(o) < w * h * 0.05:
        return False
    # 凸性：自交的四邊形不會是凸的
    if not bool(cv2.isContourConvex(o.astype(np.int32))):
        return False
    # 形狀要像一張紙：四個內角不可以差太多
    return quad_angle_range(o) <= _MAX_ANGLE_RANGE


def warp_quad(g: np.ndarray, quad: np.ndarray) -> np.ndarray:
    o = order_quad(quad)
    W = int(max(np.linalg.norm(o[2] - o[3]), np.linalg.norm(o[1] - o[0])))
    H = int(max(np.linalg.norm(o[1] - o[2]), np.linalg.norm(o[0] - o[3])))
    dst = np.float32([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]])
    return cv2.warpPerspective(g, cv2.getPerspectiveTransform(o, dst), (W, H),
                               flags=cv2.INTER_CUBIC)


def binarize(g: np.ndarray, dpi: int = 200) -> np.ndarray:
    """**預設不要用。** 只為縮檔案大小，不是為了提高辨識率（會吃掉中文細筆畫）。"""
    blk = max(15, int(dpi / 200 * 41) | 1)
    return cv2.adaptiveThreshold(normalize_illum(g), 255,
                                 cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY, blk, 12)


def straighten(g: np.ndarray, quad=None, do_binarize: bool = False, dpi: int = 200):
    """quad 給了就走透視校正（手動模式 / 自動抓到四角），否則只裁邊 + 拉正。"""
    if quad is not None and quad_is_sane(np.float32(quad), g.shape):
        base = warp_quad(g, np.float32(quad))
    else:
        base = crop_page(g)
    out = rotate(base, deskew_angle(base))
    return binarize(out, dpi) if do_binarize else out


#: 一頁的處理結果 —— 回報給使用者的數字都在這裡。
@dataclass
class PageResult:
    page: int
    angle: float            # 轉了幾度（負 = 逆時針）
    residual: float         # **修正後再估一次**的殘留角，接近 0 才算成功
    quad_found: bool        # 有沒有抓到紙張的四個角（手機翻拍才會有）
    width: int
    height: int
    ms: int
    #: 這一頁**原樣保留**（已經是正的、而且有文字層）——
    #: 處理它只會把文字變成圖片，見 `_should_skip`。
    skipped: bool = False
    #: 使用者指定的整頁轉向（0 / 90 / 180 / 270），自動流程一律 0。
    rotate_deg: int = 0



def _rotate_quad(q: "np.ndarray", deg: int, shape) -> "np.ndarray":
    """把四個角的座標跟著整頁轉向一起轉（`shape` 是**轉之前**的 (h, w)）。"""
    h, w = shape[:2]
    if deg == 90:      # 順時針：(x, y) -> (h-1-y, x)
        return np.float32([[h - 1 - y, x] for x, y in q])
    if deg == 180:
        return np.float32([[w - 1 - x, h - 1 - y] for x, y in q])
    if deg == 270:     # 逆時針：(x, y) -> (y, w-1-x)
        return np.float32([[y, w - 1 - x] for x, y in q])
    return q


def straighten_page(gray: "np.ndarray", *, quad=None, do_binarize: bool = False,
                    dpi: int = 200, page_no: int = 1,
                    rotate_deg: int = 0) -> tuple["np.ndarray", PageResult]:
    """處理一頁。`quad` 給了就走透視校正（自動抓到的四角，或使用者拉的）。

    `rotate_deg`（0 / 90 / 180 / 270）是**使用者自己指定的整頁轉向**，
    在裁邊與估歪斜**之前**先轉 —— 自動估角只看 ±6°，掃反了或掃成橫的
    它救不了，那是方向問題不是歪斜問題。轉完之後照樣跑自動拉正，
    所以「轉 90° 再微調 1.2°」是一次做完的。

    **一定會回報 `residual`**（修正後再估一次的殘留角）—— 轉錯方向時角度
    看起來「有動」，只有殘留角會現形。
    """
    t0 = time.time()
    rot = int(rotate_deg) % 360
    q = np.float32(quad) if quad is not None else None
    if rot:
        if rot not in (90, 180, 270):
            raise ValueError(f"rotate_deg 只接受 0 / 90 / 180 / 270（收到 {rotate_deg}）")
        code = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
                270: cv2.ROTATE_90_COUNTERCLOCKWISE}[rot]
        # **四個角要跟著一起轉。** 不管是自動抓的還是使用者拉的，都是在
        # **轉之前**那張圖上的座標 —— 只轉影像不轉座標的話，透視校正會抓到
        # 完全不相干的區域。開發時實測：轉 90° 之後 `quad_is_sane()` 把它擋掉
        # （因為長寬對調了），所以**看起來沒事**，其實是「使用者拉的四個角被
        # 安靜地丟掉」—— 比算錯更難發現。
        if q is not None:
            q = _rotate_quad(q, rot, gray.shape)
        gray = cv2.rotate(gray, code)
    if q is not None and not quad_is_sane(q, gray.shape):
        q = None
    if q is not None:
        base = warp_quad(gray, q)
    else:
        base = crop_page(gray)
    ang = deskew_angle(base)
    out = rotate(base, ang)
    if do_binarize:
        out = binarize(out, dpi)
    resid = deskew_angle(out, limit=2.0, step=0.05)
    return out, PageResult(page=page_no, angle=round(ang, 2),
                           residual=round(resid, 2), quad_found=q is not None,
                           rotate_deg=rot,
                           width=out.shape[1], height=out.shape[0],
                           ms=int((time.time() - t0) * 1000))


#: 「已經夠正了」的門檻。
#:
#: **0.5 是量出來的，不是猜的**：完全沒歪的原生 PDF 頁面，估計器仍會回報
#: 一點角度（文字越稀疏越吵）——
#:
#:   30 行 −0.10°｜15 行 −0.10°｜5 行 −0.10°｜**只有 1 行 −0.40°**
#:
#: 門檻低於那個雜訊就會把好頁面也重新算圖（文字變圖片）。0.5° 在 A4 上
#: 是整頁約 2.5 mm 的落差，看不出來。
_SKIP_ANGLE = 0.5
#: 有多少字才算「有文字層」。掃描件的 OCR 文字層也算 —— 那種頁面**照樣要處理**
#: （歪的就是歪的），所以這個判斷要跟角度一起看，不可以只看有沒有文字。
#:
#: **30 這個數字的依據**（拿真實樣本量過）：原生 PDF 的內文頁動輒 700~3,600 字，
#: 而圖片型投影片的頁面是 0~17 字（文字本來就在圖裡）。門檻落在 30 的時候，
#: 一份 20 頁的圖片型簡報有 18 頁被處理，**總共失去 40 個字**（頁碼與短標題，
#: 平均 2.2 字/頁）—— 那些頁面本來就沒有可保的文字層。
#:
#: 那次量到「文字保住 65%」看起來很嚴重，其實是小分母造成的錯覺
#: （原檔全部只有 113 字）。**看比例之前先看絕對值。**
#:
#: 影像佔比**不能**當判準：原生簡報的滿版背景圖也是 100% 覆蓋，跟掃描件
#: 分不開（實測過）。
_TEXT_CHARS = 30


def _should_skip(page, gray) -> bool:
    """這一頁該不該原樣保留？

    **為什麼需要這個判斷**：原生 PDF（文字是向量的）丟進來，我們會把整頁
    算成圖再貼回去 —— 文字從此**選不到、搜尋不到、複製不到**，而畫面上
    看起來一模一樣。使用者不會發現，直到有人要搜尋那份文件。

    判準是**兩個條件同時成立**：
      ①這一頁有文字層（抽得到字）
      ②而且已經夠正了（歪斜 < 0.3°）

    只看①不行：掃描件被 OCR 過之後也有文字層，但它該處理（歪的就是歪的）。
    只看②不行：一份已經很正的掃描件，裁邊與去底色仍然有價值。
    """
    try:
        text = page.get_text().strip()
    except Exception:  # noqa: BLE001
        return False
    if len(text) < _TEXT_CHARS:
        return False
    # **一定要先裁邊再估角**（跟主管線同一個順序，理由見模組開頭）——
    # 我第一版直接對整張圖估，四周的黑邊主導了投影剖面，一份歪 3° 的掃描件
    # 被估成 −0.1° → 判成「已經是正的」而跳過。**這是模組說明裡就寫著的
    # 陷阱，我照樣踩了一次。**
    #
    # 另外要用完整的角度範圍（`limit` 用預設的 6°）：`limit=2.0` 看不到 3°
    # 的歪斜；`step=0.05` 在文字稀疏的頁面上比 0.1 更吵（實測 −0.45 vs −0.10）。
    return abs(deskew_angle(crop_page(gray))) < _SKIP_ANGLE


def straighten_pdf(src: Path, dst: Path, *, dpi: int = 200,
                   do_binarize: bool = False, detect_quad: bool = True,
                   overrides: dict | None = None,
                   progress=None, cancelled=None) -> list[PageResult]:
    """整份 PDF：一頁進、一頁出，**不把整份留在記憶體裡**。

    50 頁 300 dpi 全讀進記憶體約 2 GB —— 會撞上作業佇列的記憶體准入，
    小機器上更可能把服務吃垮。所以逐頁算、逐頁寫。
    """
    import fitz
    results: list[PageResult] = []
    src_doc = fitz.open(str(src))
    out_doc = fitz.open()
    try:
        total = src_doc.page_count
        for i in range(total):
            if cancelled is not None and cancelled():
                raise Cancelled()
            if progress is not None:
                progress(i, total)
            pix = src_doc[i].get_pixmap(dpi=dpi, alpha=False)
            arr = np.frombuffer(pix.samples, np.uint8).reshape(
                pix.height, pix.width, pix.n)
            gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY) if pix.n >= 3 \
                else arr[:, :, 0]
            # **已經是正的原生 PDF 頁面原樣保留** —— 處理它只會把向量文字
            # 變成圖片（見 `_should_skip`）。
            if _should_skip(src_doc[i], gray):
                out_doc.insert_pdf(src_doc, from_page=i, to_page=i)
                results.append(PageResult(
                    page=i + 1, angle=0.0, residual=0.0, quad_found=False,
                    width=int(rect_w := src_doc[i].rect.width),
                    height=int(src_doc[i].rect.height),
                    ms=0, skipped=True))
                del pix, arr, gray
                continue
            # 逐頁覆寫：使用者在畫面上拉過四個角、或指定過轉向的那幾頁。
            # **鍵是 1-based 頁碼**（畫面上看到的那個數字），不是索引。
            ov = (overrides or {}).get(i + 1) or {}
            rot = int(ov.get("rotate", 0) or 0)
            if "quad" in ov and ov["quad"]:
                # 使用者拉的點是**正規化座標**（0~1），這裡換成像素。
                # 送像素的話，預覽圖換個尺寸就全錯了。
                h, w = gray.shape[:2]
                quad = np.float32([[x * w, y * h] for x, y in ov["quad"]])
            else:
                # **彩色一起送進去**：陰影裡的紙只有靠色度才救得回來
                # （`_paper_mask`）。只給灰階時 IMG_2905 的純度只有 0.784，
                # 也就是框進去的東西有兩成是桌面。
                quad = find_page_quad(
                    gray, arr if pix.n >= 3 else None) if detect_quad else None
            fixed, res = straighten_page(gray, quad=quad, do_binarize=do_binarize,
                                         dpi=dpi, page_no=i + 1, rotate_deg=rot)
            # **編碼要看內容**：灰階掃描件用 JPEG（實測 2.7 MB → 1.0 MB，
            # 50 頁差 85 MB）；二值化過的用 PNG（只有黑白，實測 42 KB，
            # 換成 JPEG 反而會多出壓縮雜點）。
            if do_binarize:
                blob = cv2.imencode(".png", fixed)[1].tobytes()
            else:
                blob = cv2.imencode(".jpg", fixed,
                                    [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()
            # 頁面尺寸照原頁（點數）—— 不可以用像素當點數，那會變成巨大的頁面
            rect = src_doc[i].rect
            # **轉 90° / 270° 之後長寬要對調**，否則直的內容會被塞進橫的頁面
            # 而被壓扁（畫面上看起來「有轉，但比例不對」）。
            pw, ph = rect.width, rect.height
            if rot in (90, 270):
                pw, ph = ph, pw
            page = out_doc.new_page(width=pw, height=ph)
            page.insert_image(page.rect, stream=blob)
            results.append(res)
            del pix, arr, gray, fixed, blob     # 不讓上一頁撐到下一頁
        if progress is not None:
            progress(total, total)
        out_doc.save(str(dst), garbage=3, deflate=True)
    finally:
        out_doc.close()
        src_doc.close()
    return results


class Cancelled(Exception):
    """使用者按了停止 —— 呼叫端要把產出丟掉。"""


def report(path: str, do_binarize: bool = False) -> None:
    g = cv2.imread(path, 0)
    if g is None:
        raise SystemExit(f"讀不到影像：{path}")
    t0 = time.time()
    q = find_page_quad(g)
    out = straighten(g, q, do_binarize)
    dt = time.time() - t0
    # 自我檢查：修正後再估一次，殘留要接近 0（轉反方向時這裡會現形）
    resid = deskew_angle(out, limit=2.0, step=0.05)
    print(f"{path}: {g.shape[1]}x{g.shape[0]} → {out.shape[1]}x{out.shape[0]}"
          f"｜四邊形 {'有' if q is not None else '無（走純拉正）'}"
          f"｜殘留 {resid:+.2f}°｜{dt * 1000:.0f} ms")
    cv2.imwrite(path.rsplit(".", 1)[0] + "_fixed.png", out)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    for a in args:
        report(a, "--binarize" in sys.argv)
