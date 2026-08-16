"""Font discovery for PDF editor.

Scans the host OS's font directories and returns a curated list of usable
fonts, prioritizing Taiwan-relevant traditional CJK fonts and including
common open-source options (Noto TC, Source Han TC, cwTeX, TW-Kai/Sung, etc).

Output is a list of dicts:
    {
        "id": "system:/path/to/font.ttf"  # stable opaque id
        "family": "PingFang TC",         # display name
        "label": "蘋方-繁 (PingFang TC)",
        "variant": "Regular",
        "category": "taiwan" | "free-cjk" | "cjk" | "latin" | "pymupdf",
        "cjk": "traditional" | "simplified" | None,
        "style": "sans" | "serif" | "script" | "mono" | "other",
        "path": "/absolute/path",
        "idx": 0   # TTC sub-font index when applicable
    }

PyMuPDF built-ins are exposed with id="pymupdf:<name>" and no path.
"""
from __future__ import annotations

import logging
import os
import platform
import re
import threading
from functools import lru_cache
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# Taiwan-relevant font filename patterns + display metadata.
# First match wins. Patterns are case-insensitive substrings of filename.
_HINTS = [
    # (substring, family, style, cjk, category, label)
    ("pingfang",         "PingFang TC",              "sans",   "traditional", "taiwan",   "蘋方-繁 (PingFang TC)"),
    ("heititc",          "Heiti TC",                 "sans",   "traditional", "taiwan",   "黑體-繁 (Heiti TC)"),
    ("stheitimedium",    "STHeiti Medium",           "sans",   "traditional", "taiwan",   "STHeiti 中"),
    ("stheitilight",     "STHeiti Light",            "sans",   "traditional", "taiwan",   "STHeiti 細"),
    ("lihei pro",        "LiHei Pro",                "sans",   "traditional", "taiwan",   "儷黑 Pro"),
    ("lihei",            "LiHei",                    "sans",   "traditional", "taiwan",   "儷黑"),
    ("lisong",           "LiSong Pro",               "serif",  "traditional", "taiwan",   "儷宋 Pro"),
    ("applegothic",      "Apple LiGothic",           "sans",   "traditional", "taiwan",   "Apple LiGothic"),
    ("applelisung",      "Apple LiSung",             "serif",  "traditional", "taiwan",   "Apple LiSung"),
    ("biaukai",          "BiauKai",                  "script", "traditional", "taiwan",   "標楷體 (BiauKai)"),
    ("dfkaishu",         "DFKaiShu",                 "script", "traditional", "taiwan",   "華康楷書 (DFKaiShu)"),
    ("msjh",             "Microsoft JhengHei",       "sans",   "traditional", "taiwan",   "微軟正黑體"),
    ("jhenghei",         "Microsoft JhengHei",       "sans",   "traditional", "taiwan",   "微軟正黑體"),
    ("mingliu",          "MingLiU",                  "serif",  "traditional", "taiwan",   "細明體 (MingLiU)"),
    ("pmingliu",         "PMingLiU",                 "serif",  "traditional", "taiwan",   "新細明體 (PMingLiU)"),
    # FOSS CJK
    ("notosanstc",       "Noto Sans TC",             "sans",   "traditional", "free-cjk", "Noto Sans TC"),
    ("notoseriftc",      "Noto Serif TC",            "serif",  "traditional", "free-cjk", "Noto Serif TC"),
    ("notosanscjktc",    "Noto Sans CJK TC",         "sans",   "traditional", "free-cjk", "Noto Sans CJK TC"),
    ("notoserifcjktc",   "Noto Serif CJK TC",        "serif",  "traditional", "free-cjk", "Noto Serif CJK TC"),
    # Pan-CJK 多語 TTC（Debian / Ubuntu apt 裝的就是這個檔，沒 TC 後綴 —
    # 內含 TC/SC/JP/KR 4 個 face，預設取 face 0 已涵蓋 CJK Unified glyph）
    ("notosanscjk",      "Noto Sans CJK",            "sans",   "traditional", "free-cjk", "Noto Sans CJK"),
    ("notoserifcjk",     "Noto Serif CJK",           "serif",  "traditional", "free-cjk", "Noto Serif CJK"),
    ("sourcehansans",    "Source Han Sans TC",       "sans",   "traditional", "free-cjk", "思源黑體 (Source Han Sans)"),
    ("sourcehansanstc",  "Source Han Sans TC",       "sans",   "traditional", "free-cjk", "思源黑體-繁"),
    ("sourcehanserif",   "Source Han Serif TC",      "serif",  "traditional", "free-cjk", "思源宋體 (Source Han Serif)"),
    ("sourcehanseriftc", "Source Han Serif TC",      "serif",  "traditional", "free-cjk", "思源宋體-繁"),
    ("tw-kai",           "TW Kai",                   "script", "traditional", "free-cjk", "TW Kai 楷書"),
    ("tw-sung",          "TW Sung",                  "serif",  "traditional", "free-cjk", "TW Sung 宋體"),
    ("cwtexyen",         "cwTeX Yen",                "sans",   "traditional", "free-cjk", "cwTeX 圓體"),
    ("cwtexming",        "cwTeX Ming",               "serif",  "traditional", "free-cjk", "cwTeX 明體"),
    ("cwtexkai",         "cwTeX Kai",                "script", "traditional", "free-cjk", "cwTeX 楷體"),
    ("cwtexfangsong",    "cwTeX Fang Song",          "script", "traditional", "free-cjk", "cwTeX 仿宋"),
    ("cwtexheib",        "cwTeX HeiBold",            "sans",   "traditional", "free-cjk", "cwTeX 粗黑"),
    ("genyomin",         "GenYoMin TW",              "serif",  "traditional", "free-cjk", "源雲明體"),
    ("gensen",           "GenSenRounded TW",         "sans",   "traditional", "free-cjk", "源流圓體"),
    ("jason-handwriting","Jason Handwriting",        "script", "traditional", "free-cjk", "Jason 手寫體"),
    # Also some simplified + common CJK
    ("notosanscjksc",    "Noto Sans CJK SC",         "sans",   "simplified",  "cjk",      "Noto Sans CJK SC"),
    ("notoserifcjksc",   "Noto Serif CJK SC",        "serif",  "simplified",  "cjk",      "Noto Serif CJK SC"),
    # Latin free
    ("dejavusans",       "DejaVu Sans",              "sans",   None,          "latin",    "DejaVu Sans"),
    ("dejavuserif",      "DejaVu Serif",             "serif",  None,          "latin",    "DejaVu Serif"),
    ("liberationsans",   "Liberation Sans",          "sans",   None,          "latin",    "Liberation Sans"),
    ("liberationserif",  "Liberation Serif",         "serif",  None,          "latin",    "Liberation Serif"),
]


_FONT_DIRS: list[Path] = []



# --------------------------------------------------------------- TTC 子字型 --
#
# `.ttc` 是**字型集合**：一個檔案裡包好幾套字。Linux 上常見的
# `NotoSansCJK-Regular.ttc` 就有 10 套（JP / KR / SC / TC / HK × 一般 + 等寬），
# 而 **index 0 是 JP**。
#
# 這件事的殺傷力在於它完全看不出來：字都印得出來、也不會缺字，只是「直、骨、過、
# 者、銀、電、話、統、編…」這些字寫成日文寫法。實測台灣商務表單常用的 55 個字裡
# 有 36 個不一樣 —— 也就是幾乎每個欄位名都中招。
#
# CLAUDE.md 在 v1.11.40 就記過這個雷（用印的限用章），但當時只修了那一處；
# 系統字型掃描這裡一直硬寫 idx=0。

#: 依目標語系挑子字型的偏好順序（比對字型家族名稱裡的語系標記）。
_TTC_SCRIPT_PREFS: dict[str, tuple[str, ...]] = {
    "traditional": ("TC", "TW", "HK", "MO"),
    "simplified": ("SC", "CN"),
    "japanese": ("JP",),
    "korean": ("KR",),
}


def _ttc_subfont_names(path: Path) -> list[str]:
    """回 `.ttc` 內各子字型的家族名稱（依序）。不是 ttc / 讀不到就回空 list。"""
    if path.suffix.lower() != ".ttc":
        return []
    try:
        from fontTools.ttLib import TTCollection
    except Exception:  # noqa: BLE001 — 沒有 fontTools 就維持原本行為
        logger.debug("fontTools 不可用，無法挑選 .ttc 子字型")
        return []
    try:
        with TTCollection(str(path), lazy=True) as coll:
            out = []
            for f in coll.fonts:
                nm = f["name"]
                out.append(nm.getDebugName(16) or nm.getDebugName(1) or "")
            return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("讀取 %s 的子字型清單失敗：%s", path, exc)
        return []


@lru_cache(maxsize=64)
def _ttc_index_for(path_str: str, mtime: float, cjk: Optional[str]) -> int:
    """這個 `.ttc` 要用第幾套子字型（依語系）。

    `mtime` 只是拿來讓快取在檔案換掉時失效，函式本身不用它。
    挑不出來就回 0（維持原本行為，不要因為挑不到就整個壞掉）。
    """
    names = _ttc_subfont_names(Path(path_str))
    if not names:
        return 0
    prefs = _TTC_SCRIPT_PREFS.get(cjk or "", ())
    for tag in prefs:
        for i, nm in enumerate(names):
            # 比對獨立的語系標記，避免 "TC" 命中 "Mono TC" 以外的無關字串。
            # 同時排除等寬（Mono）—— 正文用等寬會很怪。
            toks = re.split(r"[\s\-_]+", nm.upper())
            if tag in toks and "MONO" not in toks:
                return i
    return 0



#: 子集化時一定保留的字元。使用者的文字之外，排版過程還可能插入這些
#: （省略號、換行後的標點、數字），少一個就會變成看不見的缺字方框。
_SUBSET_ALWAYS = (
    "0123456789"
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    " .,:;-_/()[]#&@%+*=<>?!'\"" 
    "。，、：；！？（）「」『』〈〉《》－—…‧　"
)


def subset_font(path, idx: int, text: str) -> Optional[bytes]:
    """把字型縮成「只含這些字」的一份，回位元組。

    為什麼要做：PyMuPDF 只要用到外部字型就會把**整支檔案**嵌進 PDF。中文字型
    天生很大（Noto CJK 繁中那一套 15.7 MB），所以一張只填 30 個中文字的表單會
    變成 13 MB —— 而這種檔案的用途就是寄出去，會撞到郵件附件上限。
    子集化之後實測 15.7 MB → 27 KB（601 倍），產出的 PDF 13,387 KB → 23 KB。

    **失敗一律回 None，由呼叫端退回整支字型** —— 寧可檔案大，也不要缺字。
    缺字在畫面上是看不見的方框，使用者不會發現，收件方才會。
    """
    if not text:
        return None
    # **依「字元集合」快取，不是依原字串**：頁碼每一頁的文字都不同
    # （第 1 頁 / 第 2 頁…），但數字本來就都在 `_SUBSET_ALWAYS` 裡，所以每一頁
    # 需要的**字元集合其實一模一樣**。不這樣做的話每頁都要重跑一次子集化 ——
    # 實測 20 頁要 19 秒，200 頁的文件會卡三分鐘。
    charset = "".join(sorted(set(text) | set(_SUBSET_ALWAYS)))
    return _subset_cached(str(path), _mtime(path), int(idx or 0), charset)


@lru_cache(maxsize=8)
def _subset_cached(path_str: str, mtime: float, idx: int,
                   charset: str) -> Optional[bytes]:
    """實際做子集化（子集只有幾十 KB，多放幾份不心疼）。"""
    try:
        import io

        from fontTools import subset as _subset
        # fontTools.subset 在 INFO 會逐個表印一行（幾十行）—— 每做一次子集化
        # 就把伺服器日誌灌一次，真正的訊息會被淹掉。
        logging.getLogger("fontTools").setLevel(logging.WARNING)
        full = _full_font_bytes(path_str, mtime, idx)
        if not full:
            return None
        opts = _subset.Options()
        opts.layout_features = ["*"]      # 保留排版特性（標點壓縮等）
        opts.notdef_outline = True
        opts.recalc_bounds = False
        # **一定要保留原本的字形編號**。Noto CJK 這類是 CID-keyed CFF，
        # MuPDF 用**原始 glyph id** 取字形；子集化預設會重新編號，一對不上
        # 就什麼都畫不出來 —— 而且**文字層是好的**（搜尋、複製、抽取都正常），
        # 只有畫面空白，所以極難察覺（v1.14.19 就是這樣把整個產品的中文
        # 寫進 PDF 之後變成看不見，只驗了檔案變小沒有重新算圖）。
        # 代價是字型檔大一些（實測 5 KB → 764 KB），但相對整支 16 MB
        # 仍然小 20 倍以上，而且這是唯一畫得出來的做法。
        opts.retain_gids = True
        font = _subset.load_font(io.BytesIO(full), opts)
        sub = _subset.Subsetter(options=opts)
        sub.populate(text=charset)
        sub.subset(font)
        out = io.BytesIO()
        _subset.save_font(font, out, opts)
        data = out.getvalue()
        # **保險**：子集化之後每一個要畫的字都必須還在。缺了就整份放棄，
        # 用原本的字型 —— 檔案大總比印出方框好。
        #
        # **只能要求「原本畫得出來的字」還在**。第一版拿 charset 直接比，
        # 於是使用者從網頁或 Word 貼上帶著零寬空格（U+200B）或 BOM（U+FEFF）
        # 的公司名時，那些字元**任何中文字型都沒有**，卻被算成「子集化弄丟了」
        # → 整份退回完整字型 → 產出的 PDF 從 188 KB 變成 13,386 KB，
        # 正好回到 v1.14.19 修好前的數字，等於子集化在這種輸入下完全失效。
        # 而且退回是白費的：完整字型一樣畫不出那些字元。
        if not _covers(data, charset, base_font=io.BytesIO(full)):
            logger.warning("字型子集化後有字不見了，退回完整字型")
            return None
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("字型子集化失敗（退回完整字型）：%s", exc)
        return None


def _covers(font_bytes: bytes, text: str, base_font=None) -> bool:
    """這份字型是不是每一個字都**畫得出來**。

    分兩關，**兩關都要過**：

    1. `cmap` 有沒有這個字碼 —— 缺了就是缺字。
    2. **真的算一次圖，確認紙上有墨水** —— 這一關是 v1.14.19 慘案的教訓：
       當時只驗第 1 關，而子集化重新編號 glyph 之後 cmap 仍然完好、
       文字層也完好（搜尋、複製、抽取都正常），**只有畫面是空白的**。
       檔案還變小了，看起來一切都對 —— 產品的中文就這樣整個變成隱形。
       字型的正確與否只有渲染器說了算，所以這裡就問渲染器。
    """
    try:
        import io

        from fontTools.ttLib import TTFont
        f = TTFont(io.BytesIO(font_bytes), lazy=True)
        cmap = f.getBestCmap()
        # 原本就沒有的字碼不算「弄丟」—— 詳見呼叫端的說明。
        base_cmap = None
        if base_font is not None:
            try:
                base_font.seek(0)
                base_cmap = TTFont(base_font, lazy=True).getBestCmap()
            except Exception:  # noqa: BLE001 — 讀不到就退回舊行為（比較嚴格）
                base_cmap = None
        missing = {ch for ch in text
                   if ch.strip() and ord(ch) not in cmap
                   and (base_cmap is None or ord(ch) in base_cmap)}
        if missing:
            logger.warning("字型子集化後缺少 %d 個字：%s",
                           len(missing), "".join(sorted(missing))[:20])
            return False
    except Exception:  # noqa: BLE001 — 驗不了就當作不安全
        return False
    return _renders_ink(font_bytes, text)


def _is_cjk(ch: str) -> bool:
    """是不是漢字（含擴充區與相容區）。標點與注音不算 —— 要的是真的字形。"""
    o = ord(ch)
    return (0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF
            or 0xF900 <= o <= 0xFAFF or 0x20000 <= o <= 0x2FA1F)


def _ink_sample(text: str) -> str:
    """挑幾個字給算圖保險用 —— **優先中文**。

    抽成獨立函式是為了讓測試驗得到真的取樣邏輯：寫在 `_renders_ink` 裡面時，
    測試只能在 spy 裡重算一次同樣的邏輯，那是在測自己寫的複本 —— 實測把
    正式碼改回錯的版本，那種測試照樣全綠。
    """
    cjk = [ch for ch in text if _is_cjk(ch)]
    return "".join(cjk[:4]) or "".join(ch for ch in text if ch.strip())[:4]


def _renders_ink(font_bytes: bytes, text: str) -> bool:
    """把幾個字畫出來，看紙上是不是真的有東西。

    只取樣幾個字 —— 目的是抓「整份字型對不到字形」這種全有全無的毀損，
    不是逐字校對（逐字算圖太慢，而這類毀損從來不會只壞一個字）。

    **一定要優先取中文字**。這道保險是為了抓 CID-keyed CFF 的字形錯位
    （v1.14.19 慘案）而加的，而那種錯位**只影響 CJK，拉丁字母照畫**。
    第一版寫成「取前幾個非空白字元」，但傳進來的是 `sorted()` 過的字元集合、
    而 `_SUBSET_ALWAYS` 塞滿 ASCII → 取到的永遠是 `!"#%` 這幾個標點。
    實測把 `retain_gids` 拿掉：中文墨水 0（正是那個故障），ASCII 墨水 427，
    這道保險照樣回 True 把壞掉的字型放行 —— 它從來沒有畫過一個中文字。
    """
    sample = _ink_sample(text)
    if not sample:
        return True
    try:
        import fitz
        doc = fitz.open()
        page = doc.new_page(width=260, height=120)
        page.insert_font(fontname="jtprobe", fontbuffer=font_bytes)
        page.insert_text((20, 70), sample, fontname="jtprobe", fontsize=28)
        pix = page.get_pixmap(dpi=72, alpha=False)
        data = pix.samples
        ink = sum(1 for i in range(0, len(data), 3) if data[i] < 200)
        doc.close()
        if ink < 20:
            logger.warning("字型子集化後畫不出字形（渲染出來是空白）")
            return False
        return True
    except Exception as exc:  # noqa: BLE001 — 驗不了就當作不安全
        logger.warning("字型子集化後無法驗證渲染：%s", exc)
        return False


def _mtime(path) -> float:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def _full_font_bytes(path_str: str, mtime: float, idx: int) -> Optional[bytes]:
    """整支字型的位元組（`.ttc` 取指定子字型；其他直接讀檔）。"""
    if idx:
        return _extract_subfont(path_str, mtime, idx)
    try:
        return Path(path_str).read_bytes()
    except OSError:
        return None


def embeddable_font(path, idx: int = 0, text: Optional[str] = None):
    """回 `(fontfile, fontbuffer)` —— 給 PyMuPDF 用，兩者只會有一個有值。

    給了 `text` 就**只嵌那些字**（見 `subset_font`）—— 中文字型整支十幾 MB，
    不縮的話一張填了幾十個字的表單就變成 13 MB。

    **PyMuPDF 的公開 API 完全沒有 ttc 索引參數**（`Font()` / `insert_font()` /
    `insert_text()` 都沒有），`fontfile` 一律用第 0 套。所以要用別套就只能自己把
    子字型抽成位元組再用 `fontbuffer` 傳進去。

    `idx == 0` 時回 `(路徑, None)`，走原本最省的路徑（不抽、不佔記憶體）。
    抽取失敗時也回路徑 —— 寧可字形不對，也不要整個印不出來。
    """
    path = Path(path)
    if text:
        sub = subset_font(path, idx, text)
        if sub:
            return (None, sub)
        # 子集化失敗 → 往下走原本的路（整支嵌入），不要因此印不出字
    if not idx:
        return (str(path), None)
    buf = _extract_subfont(str(path), path.stat().st_mtime if path.exists() else 0,
                           int(idx))
    return (None, buf) if buf else (str(path), None)


@lru_cache(maxsize=2)
def _extract_subfont(path_str: str, mtime: float, idx: int) -> Optional[bytes]:
    """把 `.ttc` 的第 idx 套抽成單一字型的位元組。

    **快取上限刻意只有 2**：實測抽一份 Noto CJK 子字型是 **15.7 MB**（第一次
    約 850 ms，之後 0.15 ms）。同時真的會用到的字型很少（多半就是黑體正常體），
    放太多只是白佔記憶體 —— 這台機器還要跑 OCR 與轉檔。
    """
    try:
        import io

        from fontTools.ttLib import TTCollection
        with TTCollection(str(path_str)) as coll:
            if idx >= len(coll.fonts):
                return None
            b = io.BytesIO()
            coll.fonts[idx].save(b)
            return b.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning("抽取 %s 第 %d 套子字型失敗：%s", path_str, idx, exc)
        return None


def _detect_font_dirs() -> list[Path]:
    global _FONT_DIRS
    if _FONT_DIRS:
        return _FONT_DIRS
    dirs: list[Path] = []
    sysname = platform.system()
    if sysname == "Darwin":  # macOS
        dirs = [
            Path("/System/Library/Fonts"),
            Path("/System/Library/Fonts/Supplemental"),
            Path("/Library/Fonts"),
            Path.home() / "Library" / "Fonts",
        ]
    elif sysname == "Windows":
        dirs = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
                Path.home() / "AppData/Local/Microsoft/Windows/Fonts"]
    else:  # Linux / other
        dirs = [
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            Path.home() / ".fonts",
            Path.home() / ".local/share/fonts",
        ]
    _FONT_DIRS = [d for d in dirs if d.exists()]
    return _FONT_DIRS


def custom_fonts_dir() -> Path:
    """User-uploaded fonts live here; they are scanned alongside system
    fonts and get category='custom'."""
    from ..config import settings
    p = settings.fonts_dir
    p.mkdir(parents=True, exist_ok=True)
    return p


def _match_hint(filename: str) -> Optional[tuple]:
    fn = filename.lower().replace(" ", "").replace("-", "").replace("_", "")
    # Sort by longest pattern first to prefer more specific matches
    for pattern, family, style, cjk, category, label in sorted(
        _HINTS, key=lambda h: -len(h[0])
    ):
        if pattern.replace("-", "").replace("_", "") in fn:
            return (family, style, cjk, category, label)
    return None


_CACHE_LOCK = threading.Lock()
_CACHE: Optional[list[dict]] = None


# ---------- Hidden fonts persistence ----------
# Admin can hide certain detected fonts so they don't appear in tool font
# pickers. Hidden state lives in data/font_settings.json:
#     { "hidden": ["pymupdf:default", "system:/path/...", ...] }
def _settings_path() -> Path:
    from ..config import settings as _s
    return _s.data_dir / "font_settings.json"


def get_hidden_ids() -> set[str]:
    import json as _json
    p = _settings_path()
    if not p.exists():
        return set()
    try:
        return set(_json.loads(p.read_text(encoding="utf-8")).get("hidden", []))
    except Exception:
        return set()


def set_hidden_ids(ids: list[str]) -> None:
    import json as _json
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps({"hidden": sorted(set(ids))}, indent=2),
                 encoding="utf-8")


def list_fonts(include_hidden: bool = False) -> list[dict]:
    """Return the catalog. Scans + caches on first call.

    By default hidden fonts (admin's choice in font management page) are
    filtered out — tools / pickers use this default. Pass include_hidden=True
    in admin UI to display them with a `hidden` flag for re-show.
    """
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is not None:
            # Apply hidden filter on the cached scan result so admin can
            # toggle visibility without forcing a rescan.
            hidden = get_hidden_ids()
            if include_hidden:
                return [{**f, "hidden": f["id"] in hidden} for f in _CACHE]
            return [f for f in _CACHE if f["id"] not in hidden]
        out: list[dict] = []
        # PyMuPDF built-ins — always present (can be picked even if no
        # system font matches).
        out.extend([
            {"id": "pymupdf:default",   "family": "自動 (中文用繁體宋體 + Helvetica)",
             "label": "自動（中文繁體 + Helvetica）", "variant": "",
             "category": "pymupdf", "cjk": None, "style": "sans"},
            {"id": "pymupdf:sans",      "family": "PyMuPDF Sans",
             "label": "PyMuPDF 內建 Sans（繁中黑體 + Helvetica）", "variant": "",
             "category": "pymupdf", "cjk": None, "style": "sans"},
            {"id": "pymupdf:serif",     "family": "PyMuPDF Serif",
             "label": "PyMuPDF 內建 Serif（繁中宋體 + Times）", "variant": "",
             "category": "pymupdf", "cjk": None, "style": "serif"},
            {"id": "pymupdf:simplified","family": "PyMuPDF 簡體",
             "label": "PyMuPDF 簡體（SimSun）", "variant": "",
             "category": "pymupdf", "cjk": "simplified", "style": "serif"},
            {"id": "pymupdf:helv",      "family": "Helvetica",
             "label": "Helvetica（僅英數）", "variant": "",
             "category": "pymupdf", "cjk": None, "style": "sans"},
            {"id": "pymupdf:tiro",      "family": "Times",
             "label": "Times（僅英數）", "variant": "",
             "category": "pymupdf", "cjk": None, "style": "serif"},
            {"id": "pymupdf:cour",      "family": "Courier",
             "label": "Courier（等寬、僅英數）", "variant": "",
             "category": "pymupdf", "cjk": None, "style": "mono"},
        ])
        # System scan
        seen_paths: set[Path] = set()
        for d in _detect_font_dirs():
            try:
                for p in d.rglob("*"):
                    if not p.is_file():
                        continue
                    if p.suffix.lower() not in (".ttf", ".otf", ".ttc"):
                        continue
                    if p in seen_paths:
                        continue
                    seen_paths.add(p)
                    hint = _match_hint(p.name)
                    if not hint:
                        continue
                    family, style, cjk, category, label = hint
                    variant = _variant_from_name(p.name)
                    # `.ttc` 要挑對子字型 —— 這裡原本硬寫 0，而 Noto CJK 的
                    # 第 0 套是**日文**，等於全站中文都用日文字形。
                    try:
                        sub_idx = _ttc_index_for(str(p), p.stat().st_mtime, cjk)
                    except Exception:  # noqa: BLE001 — 挑不到就維持 0
                        sub_idx = 0
                    out.append({
                        "id": f"system:{p}",
                        "family": family,
                        "label": f"{label}" + (f" {variant}" if variant else ""),
                        "variant": variant,
                        "category": category,
                        "cjk": cjk,
                        "style": style,
                        "path": str(p),
                        "idx": sub_idx,
                    })
            except Exception:
                continue

        # Custom (user-uploaded) fonts — show every file (no hint filter)
        # with category='custom' so organisation fonts show up even if they
        # don't match our Taiwan/FOSS patterns.
        try:
            cdir = custom_fonts_dir()
            for p in sorted(cdir.rglob("*")):
                if not p.is_file():
                    continue
                if p.suffix.lower() not in (".ttf", ".otf", ".ttc"):
                    continue
                stem = p.stem
                variant = _variant_from_name(p.name)
                out.append({
                    "id": f"custom:{p.name}",
                    "family": stem,
                    "label": f"{stem}" + (f" {variant}" if variant else ""),
                    "variant": variant,
                    "category": "custom",
                    "cjk": None,
                    "style": "sans",
                    "path": str(p),
                    # 管理員上傳的 .ttc 不知道是給哪個語系用的 —— 本產品面向台灣，
                    # 預設挑繁中那一套（挑不到就回 0，等同原本行為）。
                    "idx": _ttc_index_for(str(p), p.stat().st_mtime, "traditional"),
                })
        except Exception:
            pass

        # Category sort: custom first, taiwan, free-cjk, cjk, latin, pymupdf
        order = {"custom": -1, "taiwan": 0, "free-cjk": 1,
                 "cjk": 2, "latin": 3, "pymupdf": 9}
        out.sort(key=lambda f: (order.get(f["category"], 8), f["label"]))
        _CACHE = out
        hidden = get_hidden_ids()
        if include_hidden:
            return [{**f, "hidden": f["id"] in hidden} for f in out]
        return [f for f in out if f["id"] not in hidden]


_VARIANT_PATTERNS = [
    ("ultrabold", "UltraBold"), ("extrabold", "ExtraBold"),
    ("semibold", "SemiBold"), ("demibold", "DemiBold"),
    ("bolditalic", "Bold Italic"),
    ("bold", "Bold"), ("italic", "Italic"), ("oblique", "Oblique"),
    ("light", "Light"), ("thin", "Thin"), ("medium", "Medium"),
    ("regular", ""), ("normal", ""), ("book", ""),
]


def _variant_from_name(filename: str) -> str:
    name = Path(filename).stem.lower().replace(" ", "").replace("-", "").replace("_", "")
    for key, label in _VARIANT_PATTERNS:
        if key in name:
            return label
    return ""


def refresh_cache() -> None:
    """Force a rescan (e.g., after user adds a font file)."""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


def resolve_font_id(font_id: str) -> Optional[dict]:
    """Find font entry by id; None if not found."""
    for f in list_fonts():
        if f.get("id") == font_id:
            return f
    return None


# 偏好順序：相同分類內，越上面越優先（檔名子字串）。
# 用來給 best_cjk_path() 升級 PyMuPDF 內建（china-t/ts/s/ss 在 Linux 上會
# 渲染成厚實 sans，根本不是文件上寫的 MingLiU 宋體）。
_BEST_CJK_PREFERENCES = {
    ("traditional", "serif"): [
        "notoseriftc", "notoserifcjk", "sourcehanseriftc", "sourcehanserif",
        "lisong", "applelisung", "tw-sung", "cwtexming", "genyomin",
        "pmingliu", "mingliu",
    ],
    ("traditional", "sans"): [
        "notosanstc", "notosanscjk", "sourcehansanstc", "sourcehansans",
        "msjh", "jhenghei", "pingfang", "heititc", "stheitimedium",
        "applegothic", "lihei", "cwtexyen", "cwtexheib",
    ],
    ("simplified", "serif"): [
        "notoserifcjksc", "sourcehanserifsc",
    ],
    ("simplified", "sans"): [
        "notosanscjksc", "sourcehansanssc",
    ],
}


def best_cjk_path(style: str = "sans",
                  cjk: str = "traditional") -> Optional[tuple[Path, int]]:
    """Return (path, ttc_idx) of the best available real CJK font for the
    requested style, or None if nothing usable is installed.

    Matches against `_BEST_CJK_PREFERENCES`; only considers fonts the catalog
    has actually scanned (via list_fonts), so hidden / missing files are
    auto-skipped. `.ttc` 會回**對應語系的子字型索引**（Noto CJK 的第 0 套是
    日文，用錯整份文件的中文都會是日文字形）—— 呼叫端要把 idx 一起帶下去，
    PyMuPDF 請走 `embeddable_font()`。
    """
    prefs = _BEST_CJK_PREFERENCES.get((cjk, style)) or []
    if not prefs:
        return None
    fonts = list_fonts()
    norm_keys = []
    for entry in fonts:
        path = entry.get("path")
        if not path:
            continue
        if entry.get("style") != style:
            continue
        if entry.get("cjk") and entry["cjk"] != cjk:
            continue
        norm_name = Path(path).name.lower()\
            .replace(" ", "").replace("-", "").replace("_", "")
        norm_keys.append((norm_name, Path(path), int(entry.get("idx") or 0)))
    for pat in prefs:
        pat_norm = pat.replace(" ", "").replace("-", "").replace("_", "")
        for norm_name, p, idx in norm_keys:
            if pat_norm in norm_name and p.exists():
                return (p, idx)
    return None
