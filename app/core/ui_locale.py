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
