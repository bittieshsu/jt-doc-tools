"""Markdown → PDF / DOCX / ODT: render markdown with theme CSS and convert
to printable / editable office formats via soffice headless."""
from pathlib import Path

from ..base import ToolMetadata, ToolModule
from .router import router

metadata = ToolMetadata(
    id="markdown-to-doc",
    name="Markdown 轉辦公文件",
    description="貼上或拖入 Markdown，套用主題後輸出 PDF 或文書檔（.docx / .odt），含所有頁面預覽。",
    icon="file-text",
    category="格式轉換",
)

tool = ToolModule(
    metadata=metadata,
    router=router,
    templates_dir=Path(__file__).resolve().parent / "templates",
)
