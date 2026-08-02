from pathlib import Path
from ..base import ToolMetadata, ToolModule
from .router import router

metadata = ToolMetadata(
    id="pdf-bookmark", name="書籤與目錄",
    description="替 PDF 加書籤與目錄頁；多檔合併時自動以檔名建立第一層書籤，"
                "也可自動偵測標題或貼上目錄清單（標案、年報最有感）。",
    icon="list", category="檔案編輯",
)
tool = ToolModule(metadata=metadata, router=router,
                  templates_dir=Path(__file__).resolve().parent / "templates")
