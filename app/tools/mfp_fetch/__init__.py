"""事務機取件（Beta）：查看事務機個人資料夾中的掃描文件，匯入工作區後接續處理。

目前階段（尚未實機驗證）只做得到新增事務機、連線測試與型號辨識；登入信箱、列出文件與
取件要等實機驗證後才開放。規格書與決策在 docs-share/mfp-fetch/（不公開）。
**實機實測通過之前一律標 Beta**（使用者 2026-10-13）。
"""
from pathlib import Path

from ..base import ToolMetadata, ToolModule
from .router import router

metadata = ToolMetadata(
    id="mfp-fetch",
    name="事務機取件",
    description="查看事務機個人資料夾中的掃描文件，匯入工作區後接續 OCR、轉檔與 AI 處理。",
    icon="printer",
    category="內容處理",
    beta=True,
)

tool = ToolModule(
    metadata=metadata,
    router=router,
    templates_dir=Path(__file__).resolve().parent / "templates",
)
