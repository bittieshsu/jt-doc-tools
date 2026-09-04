from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import APIRouter


@dataclass
class ToolMetadata:
    id: str
    name: str
    description: str
    icon: str = "🛠️"
    category: str = "PDF"
    version: str = "0.1.0"
    enabled: bool = True
    #: 這支工具只在哪些介面語言底下出現在側欄與搜尋裡。**空的＝所有語言都出現**。
    #:
    #: 有幾支工具是為**中文 / 台灣的文件與慣例**做的：表單自動填寫靠的是中文標籤
    #: 關鍵字、電子發票處理讀的是台灣的 QR 格式、去識別化的式子是台灣的身分證與
    #: 地址。把英文文件丟進去會「執行成功但什麼都沒抓到」—— **比看不到這支工具
    #: 更糟**，因為使用者會以為處理過了。所以介面切成非繁中時，這些工具不列出來。
    #:
    #: **這只影響「列不列在側欄與搜尋」**：路由照常可用（有人可能介面用英文、
    #: 手上卻正好有一份中文表單）、API 不受影響（那是給機器呼叫的）、權限矩陣
    #: 也不受影響（權限不可以因為使用者換了介面語言就改變）。
    locales: tuple[str, ...] = ()


@dataclass
class ToolModule:
    metadata: ToolMetadata
    router: APIRouter
    templates_dir: Optional[Path] = None
    assets_used: list[str] = field(default_factory=list)  # e.g. ["stamp"]
