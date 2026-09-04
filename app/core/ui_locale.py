"""介面語言：目前只有繁體中文，i18n 之後這裡會長出真正的語系選擇。

**先有這一層的用意**：工具的「只在某些語言出現」這件事要有**唯一一份清單**
（`ToolMetadata.locales`），側欄、搜尋、首頁磁磚、文件數字都從這裡讀。
這個專案吃過太多次「同一份清單寫在兩個地方就會漂」的虧。
"""
from __future__ import annotations

from typing import Iterable

#: 目前唯一的介面語言。i18n 做起來之後，這裡會改成「依使用者偏好 / Accept-Language」。
DEFAULT_LOCALE = "zh-Hant"


def current_locale() -> str:
    """目前的介面語言。**現在恆為繁體中文** —— 尚未提供切換。"""
    return DEFAULT_LOCALE


def tool_visible(locales: Iterable[str] | None, locale: str | None = None) -> bool:
    """這支工具要不要列在側欄 / 搜尋裡。

    `locales` 空的（多數工具）代表所有語言都列。有值時只在列出的語言底下出現。
    **這是「列不列」，不是「能不能用」** —— 路由與 API 一律不受影響。
    """
    if not locales:
        return True
    return (locale or current_locale()) in tuple(locales)


#: 常用的語系組合 —— 寫在工具的 `locales` 上，讓「為什麼限這幾種語言」一眼看得出來。
#:
#: `TAIWAN_ONLY`：靠**台灣特有的資料或格式**才成立（統編資料庫、電子發票 QR、
#: 身分證與台灣地址的式子、中文欄位標籤字典）。換成簡體中文環境也不成立。
#:
#: `CHINESE`：靠的是**華人文書慣例**（印章、騎縫章），跟字體無關 ——
#: 之後若支援簡體中文，這些工具照樣留著。
TAIWAN_ONLY: tuple[str, ...] = ("zh-Hant",)
CHINESE: tuple[str, ...] = ("zh-Hant", "zh-Hans")


#: 目前支援的介面語言。加語言時只動這裡與語系檔。
SUPPORTED: tuple[str, ...] = ("zh-Hant", "en")
#: 記住選擇的 cookie。**不放網址前綴**（`/en/...`）—— 全站樣板都用絕對路徑，
#: 加前綴會踩到反向代理那幾個雷（必須掛在根路徑）。
COOKIE_NAME = "jtdt_locale"
COOKIE_MAX_AGE = 365 * 24 * 3600


def normalise(tag: str | None) -> str | None:
    """把瀏覽器送來的語言標籤對到我們支援的語言；對不上回 None。

    `zh-TW` / `zh-Hant-TW` / `zh` 都當成繁體中文；`zh-CN` / `zh-Hans` **不算**
    （目前沒有簡體中文，硬對過去會讓簡中使用者看到繁中卻以為系統支援簡中）。
    """
    if not tag:
        return None
    t = tag.strip().lower().replace("_", "-")
    if t.startswith("zh"):
        if "hans" in t or t.split("-")[-1] in ("cn", "sg"):
            return None
        return "zh-Hant"
    if t.split("-")[0] == "en":
        return "en"
    return None


def from_accept_language(header: str | None) -> str | None:
    """解析 `Accept-Language`，照 q 值由高到低挑第一個支援的語言。"""
    if not header:
        return None
    items = []
    for part in header.split(","):
        bits = part.split(";")
        tag = bits[0].strip()
        q = 1.0
        for b in bits[1:]:
            b = b.strip()
            if b.startswith("q="):
                try:
                    q = float(b[2:])
                except ValueError:
                    q = 0.0
        if tag:
            items.append((q, tag))
    for _q, tag in sorted(items, key=lambda x: -x[0]):
        got = normalise(tag)
        if got:
            return got
    return None


def resolve(request) -> str:
    """這個請求要用哪個介面語言：**明確選過的 cookie > 瀏覽器偏好 > 繁體中文**。

    沒有帳號設定也要能用 —— 單機模式（未啟用認證）根本沒有「使用者」這回事，
    所以先做 cookie；之後要加「跟著帳號走」再疊上去即可。
    """
    # **不可以假設拿到的是完整的 Request** —— 側欄那條路徑在測試與部分內部呼叫
    # 裡會收到簡化的假物件（甚至 None）。語言只是顯示偏好，取不到就回繁中，
    # 絕不可以因此讓整個側欄炸掉。
    cookies = getattr(request, "cookies", None) or {}
    chosen = normalise(cookies.get(COOKIE_NAME))
    if chosen:
        return chosen
    headers = getattr(request, "headers", None) or {}
    try:
        accept = headers.get("accept-language")
    except AttributeError:
        accept = None
    return from_accept_language(accept) or DEFAULT_LOCALE
