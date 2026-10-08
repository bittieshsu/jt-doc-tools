"""公文撰擬 —— 把白話需求寫成「簽」或「函」，或依來文與辦理方向擬「簽辦意見」。

**格式由程式排、內容由模型寫、事實由程式驗**（全部在 `app/core/official_doc.py`）：
段名、項次、結語、陳核由程式組，模型只回內容；金額、日期、法規、條號、文號、
「已核准」這類狀態拿去跟使用者給的內容比，找不到依據的標出來。
這支工具只管「把它接成一支工具」—— 背景作業、頁面、匯出。
"""
from pathlib import Path

from ..base import ToolMetadata, ToolModule
from .router import router

metadata = ToolMetadata(
    id="official-doc",
    name="公文撰擬",
    description="把白話需求寫成「簽」或「函」，或依來文與你的辦理方向擬「簽辦意見」；"
                "格式照公文慣例排好，金額、日期與辦理方向照你寫的，沒提供的標成待補。",
    icon="official-doc",
    category="內容處理",
    # 只靠 LLM：停用時側欄 / 首頁反灰，管理員另外勾「停用時一併隱藏」才整個不列出
    requires_setup="llm",
    # 試用中（使用者 2026-10-07 指示標 Beta）：側欄、首頁、工具頁標題都會顯示
    beta=True,
    # **不限介面語言**（2026-10-07 使用者回報「切換為非中文版時不可用」）。
    # 第一版標成只給繁中（`TAIWAN_ONLY`），理由是「產出的是臺灣公文格式」—— 那個判斷錯了：
    # 反灰的判準是「換成別的介面語言後放進去會無聲失敗」，而這支工具換了介面語言照樣寫得出
    # 正確的中文公文（外籍同仁、習慣英文介面的承辦人都會用到）。產出一律是繁體中文的臺灣公文
    # 格式，英日文介面在工具說明裡講清楚。同用印 / 騎縫章 2026-09-05 被解除的那一次。
)

tool = ToolModule(
    metadata=metadata,
    router=router,
    templates_dir=Path(__file__).resolve().parent / "templates",
)
