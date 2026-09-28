"""PDF → Image: convert each PDF page to PNG / WebP / JPEG (by DPI or a fixed width); single image or a ZIP."""
from pathlib import Path

from ..base import ToolMetadata, ToolModule
from .router import router

metadata = ToolMetadata(
    id="pdf-to-image",
    name="辦公文件轉圖片",
    description="PDF 或辦公文件每頁轉成 PNG / WebP / JPEG，可以指定寬度；多頁自動打包 ZIP。",
    icon="image",
    category="格式轉換",
)

tool = ToolModule(
    metadata=metadata,
    router=router,
    templates_dir=Path(__file__).resolve().parent / "templates",
)
