from pathlib import Path

from ..base import ToolMetadata, ToolModule
from .router import router
metadata = ToolMetadata(
    id="pdf-stamp",
    name="用印與簽名",
    description="上傳 PDF，套用印章 / 簽名 / Logo 圖片並下載；支援批次處理。",
    icon="stamp",
    category="填單用印",
    # **不限語言**（2026-09-05 使用者指示）。蓋章 / 簽名不是華人專有：
    # 英文環境一樣會蓋公司章、貼簽名圖、加 logo。原本限成中文是我判斷錯了。
)

tool = ToolModule(
    metadata=metadata,
    router=router,
    templates_dir=Path(__file__).resolve().parent / "templates",
    assets_used=["stamp"],
)
