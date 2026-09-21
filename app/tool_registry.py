from __future__ import annotations

import importlib
import pkgutil
from typing import Iterable

from .logging_setup import get_logger
from .tools.base import ToolModule

logger = get_logger(__name__)

#: 啟動時載不進來的工具：套件名 -> 原因（例外型別＋訊息）。
#:
#: **為什麼要記**：載入失敗原本只留一行 ERROR 然後跳過 —— 服務照常啟動、
#: `/healthz` 照樣 200，使用者只發現「工具不見了」。少一個直接 import 的套件
#: （`defusedxml` 那次）就會這樣，而管理員沒有任何地方看得到。
#:
#: **只增不減**：之後有人補裝套件，這個行程也不會把工具掛上去（要重啟），
#: 所以這裡記的是「**這個行程實際上少了哪些工具**」，不是「現在還缺不缺」。
_LOAD_FAILURES: dict[str, str] = {}


def load_failures() -> dict[str, str]:
    """這個行程有哪些工具沒載進來（套件名 -> 原因）。"""
    return dict(_LOAD_FAILURES)


def discover_tools() -> list[ToolModule]:
    """Import every subpackage of app.tools and collect their exported `tool` attribute."""
    import app.tools as tools_pkg

    found: list[ToolModule] = []
    for mod_info in pkgutil.iter_modules(tools_pkg.__path__):
        if not mod_info.ispkg:
            continue
        name = mod_info.name
        try:
            mod = importlib.import_module(f"app.tools.{name}")
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed loading tool %s: %s", name, e)
            _LOAD_FAILURES[name] = f"{type(e).__name__}: {e}"
            continue
        tool = getattr(mod, "tool", None)
        if not isinstance(tool, ToolModule):
            logger.warning("Tool package %s does not expose a ToolModule as `tool`", name)
            _LOAD_FAILURES[name] = "套件沒有匯出 ToolModule"
            continue
        if not tool.metadata.enabled:
            logger.info("Tool %s is disabled", tool.metadata.id)
            continue
        found.append(tool)
        logger.info("Registered tool: %s (%s)", tool.metadata.id, tool.metadata.name)
    return found


def mount_tools(app, tools: Iterable[ToolModule]) -> None:
    for tool in tools:
        prefix = f"/tools/{tool.metadata.id}"
        app.include_router(tool.router, prefix=prefix, tags=[tool.metadata.id])
