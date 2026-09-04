"""文件去識別化 (De-identification) — detect and redact/mask sensitive data
in PDF and Office documents."""
from pathlib import Path

from ..base import ToolMetadata, ToolModule
from .router import router
from app.core.ui_locale import TAIWAN_ONLY

metadata = ToolMetadata(
    id="doc-deident",
    name="文件去識別化",
    description="偵測文件中的敏感資料（身分證 / 手機 / Email / 統編 …），一鍵編修或資料遮罩。",
    icon="shield",
    category="資安處理",
    # 式子是台灣的身分證 / 統編 / 手機 / 地址
    locales=TAIWAN_ONLY,
)

tool = ToolModule(
    metadata=metadata,
    router=router,
    templates_dir=Path(__file__).resolve().parent / "templates",
)
