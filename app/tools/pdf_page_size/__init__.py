from pathlib import Path
from ..base import ToolMetadata, ToolModule
from .router import router

metadata = ToolMetadata(
    id="pdf-page-size", name="頁面尺寸統一",
    description="把混合尺寸的頁面統一成同一種紙張（A4 / A3 / 自訂）；"
                "可選縮放留白、置中不縮放或裁切，直橫混排也能一併處理。",
    icon="aspect-ratio", category="檔案編輯",
)
tool = ToolModule(metadata=metadata, router=router,
                  templates_dir=Path(__file__).resolve().parent / "templates")
