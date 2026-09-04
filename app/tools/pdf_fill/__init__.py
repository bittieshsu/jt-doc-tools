from pathlib import Path

from ..base import ToolMetadata, ToolModule
from .router import router
from app.core.ui_locale import TAIWAN_ONLY

metadata = ToolMetadata(
    id="pdf-fill",
    name="表單自動填寫",
    description="上傳廠商資料表 / 申請書（PDF 與辦公文件），自動辨識欄位後用公司基本資料填好。",
    icon="form",
    category="填單用印",
    # 欄位定位靠中文標籤關鍵字，英文表單抓不到欄位
    locales=TAIWAN_ONLY,
)

tool = ToolModule(
    metadata=metadata,
    router=router,
    templates_dir=Path(__file__).resolve().parent / "templates",
    assets_used=[],
)
