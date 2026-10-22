"""企業 logo / 識別 — admin 可上傳一張企業 logo 取代預設 sidebar logo、
favicon、首頁 logo。

實作上只存一張原始上傳檔（`data/branding/logo.png`，PNG / JPEG 都正規化成
PNG），各位置（sidebar、favicon、landing）都讀同一張。SVG 不支援，因為
favicon 與 PDF 內嵌情境都需要 raster 後備。

`/branding/logo` 是公開 endpoint（**不**需登入），這樣 base.html 在登入頁
也讀得到企業 logo（避免登入頁顯示預設 logo）。
"""
from __future__ import annotations

import io
import os
import threading
from pathlib import Path
from typing import Optional

from PIL import Image

from ..config import settings


# 上傳上限：5 MB（一般企業 logo 不會超過 1 MB；給 5 MB 餘裕）
MAX_LOGO_BYTES = 5 * 1024 * 1024
# 上傳的原始圖會 resize 到最長邊不超過這個 px，避免使用者上傳 4K 圖
# 拖累每個頁面載入速度。256 px 對 sidebar / favicon 都綽綽有餘。
MAX_LOGO_DIMENSION = 256
# 允許的 MIME / 檔名 extension（白名單）
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def _logo_path() -> Path:
    return settings.branding_dir / "logo.png"


def has_custom_logo() -> bool:
    p = _logo_path()
    return p.exists() and p.stat().st_size > 0


def get_custom_logo_path() -> Optional[Path]:
    """Return the on-disk path to the custom logo, or None if not set."""
    p = _logo_path()
    return p if p.exists() and p.stat().st_size > 0 else None


def save_logo(data: bytes, original_filename: str = "") -> None:
    """Validate + normalize uploaded image to PNG, save to branding_dir.

    Raises ValueError on invalid input — caller maps to HTTP 400.
    """
    if not data:
        raise ValueError("上傳檔為空")
    if len(data) > MAX_LOGO_BYTES:
        raise ValueError(
            f"檔案過大（{len(data)/1024/1024:.1f} MB > 上限 "
            f"{MAX_LOGO_BYTES/1024/1024:.0f} MB）")
    ext = Path(original_filename or "").suffix.lower()
    if ext and ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"不支援的副檔名：{ext}（僅支援 PNG / JPG / WEBP）")
    # Decode + validate via PIL — also rejects malformed images and any
    # non-image payload that slipped through extension check.
    from io import BytesIO
    try:
        with Image.open(BytesIO(data)) as im:
            im.load()
            # Always convert to RGBA so PNG output preserves transparency
            # if the source had it (logo.png with alpha is the common case).
            im = im.convert("RGBA")
            # Down-scale large uploads — keep aspect ratio.
            if max(im.width, im.height) > MAX_LOGO_DIMENSION:
                im.thumbnail((MAX_LOGO_DIMENSION, MAX_LOGO_DIMENSION),
                             Image.Resampling.LANCZOS)
            settings.branding_dir.mkdir(parents=True, exist_ok=True)
            im.save(_logo_path(), "PNG", optimize=True)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"無法解析圖片：{e}")


def reset_logo() -> bool:
    """Remove custom logo, falling back to bundled default. Returns True
    if a custom logo was actually present and removed."""
    p = _logo_path()
    _icon_cache_path().unlink(missing_ok=True)
    if p.exists():
        p.unlink()
        return True
    return False


def custom_logo_url() -> str:
    """URL used in templates. Returns the public endpoint when a custom
    logo exists, else empty string (templates fall back to the bundled
    default in app/static/images/)."""
    p = get_custom_logo_path()
    # 網址帶版本：換了新 Logo 網址就跟著變，瀏覽器（尤其 favicon 的快取）才會重抓
    return f"/branding/logo?v={p.stat().st_mtime_ns}" if p else ""


# ----- favicon（使用者 2026-10-22：換 Logo 時 favicon 也要跟著變） -----
#: 正方形圖示的邊長（apple-touch-icon 用 180；瀏覽器頁籤縮到 16 / 32 也清楚）
ICON_SIZE = 180
#: `/favicon.ico` 裡放的尺寸
FAVICON_SIZES = ((16, 16), (32, 32), (48, 48))
_ICO_CACHE: dict[tuple, bytes] = {}
_ICON_LOCK = threading.Lock()


def _icon_cache_path() -> Path:
    return settings.branding_dir / "icon.png"


def custom_icon_path() -> Optional[Path]:
    """企業 Logo 的正方形版本（給 favicon 用）。企業 Logo 多半是橫的，直接當 favicon 會被壓扁或縮到看不清楚：
    先裁掉四周全透明的邊，再置中補成正方形（透明底）。Logo 換了才重做（比修改時間）。"""
    src = get_custom_logo_path()
    if not src:
        return None
    dst = _icon_cache_path()
    with _ICON_LOCK:
        try:
            if dst.stat().st_mtime_ns >= src.stat().st_mtime_ns:
                return dst
        except FileNotFoundError:
            pass
        with Image.open(src) as im:
            im = im.convert("RGBA")
            box = im.getchannel("A").getbbox()
            if box:
                im = im.crop(box)
            side = max(im.size)
            canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
            if side != ICON_SIZE:
                canvas = canvas.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".tmp")
        canvas.save(tmp, "PNG")
        os.replace(tmp, dst)
    return dst


def icon_url() -> str:
    """頁面 `<link rel="icon">` 用的網址：企業 Logo 的正方形版（帶版本），否則選的內建 Logo（本來就是正方形）。"""
    p = get_custom_logo_path()
    if p:
        return f"/branding/icon.png?v={p.stat().st_mtime_ns}"
    return builtin_logo_url(spot="icon")


def favicon_ico() -> bytes:
    """`/favicon.ico` 的內容：跟著目前的 Logo（企業 Logo 或選的內建 Logo）。
    瀏覽器在沒有 `<link rel="icon">` 的網址（PDF 預覽、下載、API）與書籤會自己要這個路徑。"""
    src = custom_icon_path()
    if src is None:
        src = Path(__file__).resolve().parent.parent.parent / builtin_logo_url(spot="icon").lstrip("/")
    key = (str(src), src.stat().st_mtime_ns)
    hit = _ICO_CACHE.get(key)
    if hit is not None:
        return hit
    with Image.open(src) as im:
        buf = io.BytesIO()
        im.convert("RGBA").save(buf, format="ICO", sizes=list(FAVICON_SIZES))
    data = buf.getvalue()
    _ICO_CACHE.clear()
    _ICO_CACHE[key] = data
    return data


# ----- 站台名稱 customization (v1.4.68 起) -----
# 客戶可改「Jason Tools 文件工具箱」→「<某某公司> 文件工具箱」之類自家品牌。
# 改完之後出現在 sidebar 上方、瀏覽器頁籤 title、首頁 hero、login 頁。
# Apache 2.0 license 允許 white-label，這只是內建讓不會改 source 的客戶也能改。
_MAX_SITE_NAME_LEN = 60


def _site_name_path() -> Path:
    return settings.branding_dir / "site_name.txt"


def get_site_name(default: str = "") -> str:
    """Return custom site name if set, else `default` (typically settings.app_name).
    讀檔每次 request，不快取 — 改完立即生效，不用 restart。檔案不存在或
    空白回 default。"""
    p = _site_name_path()
    try:
        if p.exists():
            txt = p.read_text(encoding="utf-8").strip()
            if txt:
                return txt[:_MAX_SITE_NAME_LEN]
    except Exception:
        pass
    return default


def set_site_name(name: str) -> None:
    """Save custom site name. Empty / whitespace → reset to default
    (delete the file). Raises ValueError on too-long input."""
    name = (name or "").strip()
    if not name:
        # Reset path
        try:
            _site_name_path().unlink(missing_ok=True)
        except Exception:
            pass
        return
    if len(name) > _MAX_SITE_NAME_LEN:
        raise ValueError(f"站台名稱不得超過 {_MAX_SITE_NAME_LEN} 字元（目前 {len(name)} 字元）")
    settings.branding_dir.mkdir(parents=True, exist_ok=True)
    _site_name_path().write_text(name, encoding="utf-8")


def has_custom_site_name() -> bool:
    return _site_name_path().exists() and _site_name_path().stat().st_size > 0


# ----- 內建 Logo 與左側選單配色（v1.16.78 起）-----
# 存在 `data/branding/appearance.json`（整個 branding 目錄本來就在設定備份裡）。
# 每一頁都會讀（base.html 的 Logo 與 `<html data-sb-theme>`），所以照檔案修改時間快取。

#: 內建 Logo：沒有上傳企業 Logo 時用哪一個。
#: `jt` 是 Jason Tools Logo（原本的預設，側欄依背景深淺換深色版 / 淺色版）；
#: `doc` 是介紹網站左上角那個文件圖示（自帶底色，深淺背景都用同一張）。
BUILTIN_LOGOS = ("jt", "doc")
DEFAULT_BUILTIN_LOGO = "jt"
_BUILTIN_LOGO_NAMES = {"jt": "Jason Tools Logo", "doc": "文件圖示（介紹網站）"}
_JT_ON_DARK = "/static/images/logo-on-dark.png"
_JT_ON_LIGHT = "/static/images/logo-on-light.png"
_DOC_LOGO = "/static/images/logo-doc.png"

#: 左側選單配色：(代號, 種類, 背景深淺, 名稱, 一句說明)。顏色本身只寫在 `static/css/platform.css`
#: 的 `[data-sb-theme="…"]` 區塊（一份）；測試會比對兩邊的代號與深淺一致，並量文字對比。
SIDEBAR_THEMES = (
    ("indigo", "gradient", "dark", "靛紫（預設）", "原本的配色，靛紫漸層"),
    ("ocean", "gradient", "dark", "海洋藍", "藍色漸層，沉穩專業"),
    ("forest", "gradient", "dark", "森林綠", "深綠漸層，柔和護眼"),
    ("wine", "gradient", "dark", "酒紅", "酒紅漸層，醒目有個性"),
    ("copper", "gradient", "dark", "暖橘", "橘褐漸層，溫暖有活力"),
    ("graphite", "gradient", "dark", "石墨黑", "灰黑漸層，低調俐落"),
    ("navy", "solid", "dark", "海軍藍", "深藍純色，正式穩重"),
    ("teal", "solid", "dark", "墨綠", "墨綠純色，清爽沉靜"),
    ("charcoal", "solid", "dark", "炭黑", "近黑純色，文字對比最高"),
    ("silver", "solid", "light", "淺灰", "淺灰底深色字，明亮簡潔"),
    ("mist", "solid", "light", "淡藍", "淡藍底深藍字，清新柔和"),
    ("sand", "solid", "light", "米白", "米白底深棕字，溫潤舒適"),
)
DEFAULT_SIDEBAR_THEME = "indigo"
_THEME_IDS = tuple(t[0] for t in SIDEBAR_THEMES)
_THEME_TONE = {t[0]: t[2] for t in SIDEBAR_THEMES}

#: 首頁 Logo 的光暈（v1.17.0，使用者 2026-10-19）：(代號, 名稱, 光暈的 RGB；None＝不要光暈)。
#: 顏色只寫在這裡；首頁與設定頁的 CSS 都由這份產生（值是固定常數，不是使用者輸入）。
HERO_GLOWS = (
    ("indigo", "靛紫（預設）", (99, 102, 241)),
    ("blue", "海洋藍", (59, 130, 246)),
    ("teal", "青綠", (20, 184, 166)),
    ("green", "森林綠", (34, 197, 94)),
    ("amber", "暖橘", (245, 158, 11)),
    ("rose", "玫瑰紅", (244, 63, 94)),
    ("slate", "石墨灰", (100, 116, 139)),
    ("none", "不要光暈", None),
)
#: 首頁標題文字的漸層（三個色點）。預設是原本的靛紫；其他的最淺那一點對首頁底色也有 3:1（大字）。
HERO_TITLES = (
    ("indigo", "靛紫（預設）", ("#7c6df0", "#5b4bd9", "#4c3ad0")),
    ("blue", "海洋藍", ("#2563eb", "#1d4ed8", "#1e40af")),
    ("teal", "青綠", ("#0d9488", "#0f766e", "#115e59")),
    ("green", "森林綠", ("#15803d", "#166534", "#14532d")),
    ("amber", "暖橘", ("#ea580c", "#c2410c", "#9a3412")),
    ("rose", "玫瑰紅", ("#e11d48", "#be123c", "#9f1239")),
    ("slate", "石墨灰", ("#475569", "#334155", "#1e293b")),
)
DEFAULT_HERO_GLOW = "indigo"
DEFAULT_HERO_TITLE = "indigo"
_GLOW = {g[0]: g[2] for g in HERO_GLOWS}
_TITLE = {t[0]: t[2] for t in HERO_TITLES}

_appearance_cache: tuple[object, dict] = (None, {})


def _appearance_path() -> Path:
    return settings.branding_dir / "appearance.json"


def get_appearance() -> dict:
    """{"sidebar_theme", "builtin_logo", "hero_glow", "hero_title"}；檔案沒有、壞掉或代號不認得都回預設。"""
    global _appearance_cache
    p = _appearance_path()
    # 快取的鍵用修改時間（奈秒）＋大小＋inode：設定備份匯入是原地覆寫（inode 不變），
    # 修改時間解析度粗的檔案系統上光靠時間可能分不出來，再加大小多一層。
    try:
        st = p.stat()
        key = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        key = None
    if key and _appearance_cache[0] == key:
        return dict(_appearance_cache[1])
    data: dict = {}
    if key:
        try:
            import json
            raw = json.loads(p.read_text(encoding="utf-8"))
            data = raw if isinstance(raw, dict) else {}
        except Exception:
            data = {}
    out = {
        "sidebar_theme": data.get("sidebar_theme") if data.get("sidebar_theme") in _THEME_IDS
        else DEFAULT_SIDEBAR_THEME,
        "builtin_logo": data.get("builtin_logo") if data.get("builtin_logo") in BUILTIN_LOGOS
        else DEFAULT_BUILTIN_LOGO,
        "hero_glow": data.get("hero_glow") if data.get("hero_glow") in _GLOW else DEFAULT_HERO_GLOW,
        "hero_title": data.get("hero_title") if data.get("hero_title") in _TITLE else DEFAULT_HERO_TITLE,
    }
    _appearance_cache = (key, out)
    return dict(out)


def set_appearance(*, sidebar_theme: Optional[str] = None,
                   builtin_logo: Optional[str] = None, hero_glow: Optional[str] = None,
                   hero_title: Optional[str] = None) -> dict:
    """改其中幾項；代號不認得丟 ValueError（呼叫端回 400，一項都不改）。回改完之後的設定。"""
    if hero_glow is not None and hero_glow not in _GLOW:
        raise ValueError("unknown hero glow")
    if hero_title is not None and hero_title not in _TITLE:
        raise ValueError("unknown hero title colour")
    cur = get_appearance()
    if hero_glow is not None:
        cur["hero_glow"] = hero_glow
    if hero_title is not None:
        cur["hero_title"] = hero_title
    if sidebar_theme is not None:
        if sidebar_theme not in _THEME_IDS:
            raise ValueError("unknown sidebar theme")
        cur["sidebar_theme"] = sidebar_theme
    if builtin_logo is not None:
        if builtin_logo not in BUILTIN_LOGOS:
            raise ValueError("unknown builtin logo")
        cur["builtin_logo"] = builtin_logo
    from . import atomic_json
    settings.branding_dir.mkdir(parents=True, exist_ok=True)
    atomic_json.write_json(_appearance_path(), cur)
    global _appearance_cache
    _appearance_cache = (None, {})
    return get_appearance()


def sidebar_theme() -> str:
    return get_appearance()["sidebar_theme"]


def sidebar_tone(theme: Optional[str] = None) -> str:
    """`dark`（白字）或 `light`（深色字）。"""
    return _THEME_TONE.get(theme or sidebar_theme(), "dark")


def theme_list() -> list[dict]:
    return [{"id": i, "kind": k, "tone": tone, "name": n, "desc": d} for i, k, tone, n, d in SIDEBAR_THEMES]


def hero_glow_list() -> list[dict]:
    return [{"id": i, "name": n, "rgb": ",".join(map(str, c)) if c else ""} for i, n, c in HERO_GLOWS]


def hero_title_list() -> list[dict]:
    return [{"id": i, "name": n, "stops": list(c)} for i, n, c in HERO_TITLES]


def hero_style(glow: Optional[str] = None, title: Optional[str] = None) -> dict:
    """首頁 hero 的顏色：`glow` 是 `"r,g,b"`（空字串＝不要光暈），`title` 是三個色點。"""
    a = get_appearance()
    gid = glow if glow in _GLOW else a["hero_glow"]
    tid = title if title in _TITLE else a["hero_title"]
    g = _GLOW[gid]
    return {"glow": ",".join(map(str, g)) if g else "", "title": list(_TITLE[tid])}


def builtin_logo_list() -> list[dict]:
    return [{"id": i, "name": _BUILTIN_LOGO_NAMES[i], "url": builtin_logo_url(i)} for i in BUILTIN_LOGOS]


def builtin_logo_url(logo: Optional[str] = None, spot: str = "page") -> str:
    """內建 Logo 的網址。`spot` 是 `sidebar`（看選單背景深淺挑版本）或其他位置（白底）。"""
    logo = logo or get_appearance()["builtin_logo"]
    if logo == "doc":
        return _DOC_LOGO
    if spot == "sidebar" and sidebar_tone() == "dark":
        return _JT_ON_DARK
    return _JT_ON_LIGHT


def logo_url(spot: str = "page") -> str:
    """各位置實際要用的 Logo：上傳過企業 Logo 就用那張，否則用選的內建 Logo。
    `icon`（favicon）用正方形版本，見 `icon_url()`。"""
    if spot == "icon":
        return icon_url()
    return custom_logo_url() or builtin_logo_url(spot=spot)


def builtin_logo_file(logo: Optional[str] = None) -> Path:
    """內建 Logo 的檔案（通知信要把圖夾進信裡；信件底色是白的）。"""
    rel = builtin_logo_url(logo, spot="page").lstrip("/")
    return Path(__file__).resolve().parent.parent.parent / rel
