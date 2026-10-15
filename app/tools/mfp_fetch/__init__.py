"""事務機掃描取件（Beta；原名「事務機取件」，使用者 2026-10-14 改名，tool id 不變）。

新增事務機、連線測試與型號辨識、從事務機掃描（eSCL）進工作區、FUJIFILM 機內信箱取件（推測實作、實驗功能）。
規格書與決策在 docs-share/mfp-fetch/（不公開）。
**實機實測通過之前一律標 Beta**（使用者 2026-10-13）。
"""
from pathlib import Path

from ..base import ToolMetadata, ToolModule
from .router import router

metadata = ToolMetadata(
    id="mfp-fetch",
    name="事務機掃描取件",
    description="從事務機掃描，或取回機內信箱裡的文件，存進工作區後接續 OCR、轉檔與 AI 處理。",
    icon="printer",
    category="內容處理",
    beta=True,
)

tool = ToolModule(
    metadata=metadata,
    router=router,
    templates_dir=Path(__file__).resolve().parent / "templates",
)
