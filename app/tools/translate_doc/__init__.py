"""逐句翻譯工具 — 接 admin 設定好的 LLM server，左原文右譯文逐句並排。

定位：附加功能（依賴 admin → LLM 設定啟用）。LLM 沒啟用時頁面顯示提示，
不擋其他工具運作。

支援來源：直接貼文字 / 上傳 PDF / DOCX / 純文字。輸出 = JSON 對照表，
UI 並排顯示，每句可重新請 LLM 重譯。
"""
from pathlib import Path

from ..base import ToolMetadata, ToolModule
from .router import router

metadata = ToolMetadata(
    id="translate-doc",
    name="逐句翻譯",
    description="接地端 LLM 逐句翻譯，左原文右譯文並排。可上傳文字或辦公文件。",
    icon="globe",
    category="內容處理",
    # 只靠 LLM：停用時側欄 / 首頁反灰，管理員另外勾「停用時一併隱藏」才整個不列出
    requires_setup="llm",
)

tool = ToolModule(
    metadata=metadata,
    router=router,
    templates_dir=Path(__file__).resolve().parent / "templates",
)
