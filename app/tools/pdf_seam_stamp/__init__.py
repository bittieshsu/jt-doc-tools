from pathlib import Path
from ..base import ToolMetadata, ToolModule
from .router import router
metadata = ToolMetadata(
    id="pdf-seam-stamp", name="騎縫章",
    description="一個印章切成數片蓋在連續頁面上，抽換或掉頁一眼看得出來。",
    icon="seam-stamp", category="填單用印",
    # **不限語言**（2026-09-05 使用者指示）。跨頁蓋章防抽換不是華人專有，
    # 合約與標案在哪裡都有這個需求。
)
tool = ToolModule(metadata=metadata, router=router,
                  templates_dir=Path(__file__).resolve().parent / "templates")
