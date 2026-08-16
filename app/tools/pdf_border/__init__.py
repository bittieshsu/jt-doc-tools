from pathlib import Path
from ..base import ToolMetadata, ToolModule
from .router import router

metadata = ToolMetadata(
    id="pdf-border", name="頁面加框",
    description="每一頁加上框線；可設粗細、顏色、線型、圓角、雙線與陰影，"
                "支援 PDF 與辦公文件（簡報加外框最常用）。",
    icon="border", category="檔案編輯",
)
tool = ToolModule(metadata=metadata, router=router,
                  templates_dir=Path(__file__).resolve().parent / "templates")
