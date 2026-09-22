"""會議錄音轉逐字稿 —— 音訊 / 視訊進去，**帶時間與發言者的逐字稿**出來。

**分工**（使用者 2026-09-17 拍板）：聲音相關的（辨識、發言者分離、標點校正）
在 jtlw，那些要 GPU 與音訊上下文；**摘要、決議、待辦、心智圖、翻譯全部在
這裡**。所以這支工具做完之後，結果頁有一顆「轉送會議摘要」把逐字稿交給
另一支工具 —— 兩支各自的相依很乾淨，客戶沒裝 jtlw 時「會議摘要」照樣能用。

**這支工具需要外部服務**（`requires_setup="jtlw"`）：沒在管理區設定好之前
側欄與首頁都反灰（使用者 2026-09-21 指示）。判準是「設定齊不齊」不是
「這台裝了什麼」—— jtlw 是別台機器上的服務，裝不裝在我們這裡看不出來。
"""
from pathlib import Path

from ..base import ToolMetadata, ToolModule
from .router import router

metadata = ToolMetadata(
    id="meeting-transcribe",
    name="會議錄音轉逐字稿",
    description="把會議錄音或錄影轉成帶時間與發言者的逐字稿，可直接轉送給「會議摘要」。",
    icon="play",
    category="內容處理",
    requires_setup="jtlw",
)

tool = ToolModule(
    metadata=metadata,
    router=router,
    templates_dir=Path(__file__).resolve().parent / "templates",
)
