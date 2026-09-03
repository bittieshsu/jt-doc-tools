"""文件翻譯 —— 產出**同一種格式**的檔案，只有文字換成譯文，排版不動。

與「逐句翻譯」的分工：
  * 逐句翻譯：左原文右譯文的**對照表**，來源可以是貼上的文字或 PDF，
    產出是對照清單（txt / md / csv / docx / odt / pdf）。
  * 文件翻譯（這一支）：吃**辦公文件**，直接在檔案的 XML 上把文字換成譯文，
    產出跟來源同格式、同版面的檔案。

**不支援 PDF**：PDF 裡沒有「段落」這種東西，文字是一塊一塊定位好的碎片，
換成長度不同的譯文之後版面一定跑掉。要翻 PDF 請用「逐句翻譯」。
"""
from pathlib import Path

from ..base import ToolMetadata, ToolModule
from .router import router

metadata = ToolMetadata(
    id="doc-translate",
    name="文件翻譯",
    description="把辦公文件整份翻譯成另一種語言，產出同格式、同版面的檔案"
                "（只換文字，不重排版面）。",
    icon="translate",
    category="內容處理",
)

tool = ToolModule(
    metadata=metadata,
    router=router,
    templates_dir=Path(__file__).resolve().parent / "templates",
)
