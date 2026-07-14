"""乘車證明整理：拉台鐵 / 高鐵乘車（購票）證明 PDF，自動抽出日期 / 交通工具 /
來源-目的 / 費用等欄位，累積成清單，可多格式匯出（CSV / XLSX / ODS / JSON /
XML / TXT / MD）。做法與「電子發票處理」一致：解析後存 per-user buffer，桌面表格
呈現，欄位可自訂顯示 / 順序，一鍵匯出報帳。"""
from pathlib import Path

from ..base import ToolMetadata, ToolModule
from .router import router

metadata = ToolMetadata(
    id="transit-proof",
    name="乘車證明整理",
    description="拉台鐵 / 高鐵乘車證明 PDF，自動整理日期 / 交通工具 / 起訖 / 費用成表格，可批次匯出報帳。",
    icon="car",
    category="內容處理",
)

tool = ToolModule(
    metadata=metadata,
    router=router,
    templates_dir=Path(__file__).resolve().parent / "templates",
)
