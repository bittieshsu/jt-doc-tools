from pathlib import Path
from ..base import ToolMetadata, ToolModule
from .router import router

metadata = ToolMetadata(
    id="pdf-seam-stamp", name="騎縫章",
    description="一個印章切成數片蓋在連續頁面上，抽換或掉頁一眼看得出來。",
    icon="stamp", category="填單用印",
)
tool = ToolModule(metadata=metadata, router=router,
                  templates_dir=Path(__file__).resolve().parent / "templates")
