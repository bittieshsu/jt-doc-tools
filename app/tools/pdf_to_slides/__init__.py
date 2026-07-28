"""PDF 轉簡報檔（pdf-to-slides）。

PDF → OpenDocument 簡報 (.odp) / PowerPoint (.pptx)，**版面重現**：每頁內容依原始
座標放進投影片錨定的物件，位置 / 圖片 / 框線幾乎 1:1 保留。

只有一顆引擎（jtdt-layout）—— 簡報本來就是「頁面 + 絕對定位物件」的模型，不像文書
檔要在「流動可編輯」與「版面忠實」之間取捨，因此不需要多引擎選擇。
"""
from pathlib import Path

from ..base import ToolMetadata, ToolModule
from .router import router

metadata = ToolMetadata(
    id="pdf-to-slides",
    name="PDF 轉簡報檔（Beta）",
    description="PDF 轉成 PowerPoint (.pptx) 或 OpenDocument 簡報 (.odp)。",
    icon="presentation",
    category="格式轉換",
)

tool = ToolModule(
    metadata=metadata,
    router=router,
    templates_dir=Path(__file__).resolve().parent / "templates",
)
