from pathlib import Path

from ..base import ToolMetadata, ToolModule
from .router import router

metadata = ToolMetadata(
    id="office-convert",
    name="辦公文件格式互轉",
    description="同一類文件之間互轉格式：文書檔、試算表、簡報各自互換，"
                "部分格式還可以指定版本。",
    icon="swap",
    category="格式轉換",
)

tool = ToolModule(
    metadata=metadata,
    router=router,
    templates_dir=Path(__file__).resolve().parent / "templates",
)
