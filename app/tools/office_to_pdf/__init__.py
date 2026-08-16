from pathlib import Path
from ..base import ToolMetadata, ToolModule
from .router import router

metadata = ToolMetadata(
    id="office-to-pdf", name="辦公文件轉 PDF",
    description="把辦公文件批次轉成 PDF。",
    icon="page", category="格式轉換",
)
tool = ToolModule(metadata=metadata, router=router,
                  templates_dir=Path(__file__).resolve().parent / "templates")
