"""會議摘要 —— 逐字稿進來，**能指回原文的**摘要、決議、待辦、風險與章節出去。

跟「字數統計」的 LLM 摘要分工：那支是對一份文件生三五句話；這支是把一場會議
拆成「開完會之後，沒參加的人需要知道的事」，而且**每一條都要指得回是誰在
第幾段講的** —— 因為會議記錄會被拿去當依據，一條沒有出處的決議比沒有那條更糟。

輸入是**逐字稿檔案**（WebVTT / SRT / JSON / 純文字 / Word / ODF）。
錄音轉逐字稿是另一件事，由語音服務負責。
"""
from pathlib import Path

from ..base import ToolMetadata, ToolModule
from .router import router

metadata = ToolMetadata(
    id="meeting-summary",
    name="會議摘要",
    description="把會議逐字稿整理成摘要、決議、待辦、風險與章節，"
                "每一條都指得回原文的第幾段、誰講的。",
    icon="clipboard-list",
    category="內容處理",
)

tool = ToolModule(
    metadata=metadata,
    router=router,
    templates_dir=Path(__file__).resolve().parent / "templates",
)
