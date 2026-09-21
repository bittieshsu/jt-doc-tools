"""首頁那句話的工具數**要用算的**（使用者 2026-09-18 要求）。

> 「XX 請填入工具數，每次更新版本如果工具數有變，這 XX 也要變」

寫死的話加工具時這個數字會漂掉，而**畫面上完全看不出來** ——
這個專案在「文件數字」上踩過很多次（工具總數、背景作業數、LLM 卡片數）。
"""
from __future__ import annotations

import json
import re

import pytest


def test_the_home_page_says_how_many_tools_there_are(client, auth_off):
    from app.tool_registry import discover_tools
    n = len(discover_tools())
    html = client.get("/").text
    m = re.search(r"整合式 PDF / Office 文件處理平台\s*(\d+)\s*支工具", html)
    assert m, "首頁沒有顯示工具數"
    assert int(m.group(1)) == n, \
        f"首頁寫 {m.group(1)} 支，註冊表實際 {n} 支"


def test_the_number_is_not_hard_coded_in_the_template():
    """判準：樣板裡不可以出現那個數字的字面值。

    只驗「數字對不對」是不夠的 —— 今天寫死一個剛好正確的數字也會過，
    然後下一次加工具就漂掉。
    """
    import pathlib
    src = pathlib.Path("app/web/templates/home.html").read_text(encoding="utf-8")
    line = next(ln for ln in src.splitlines() if "整合式 PDF / Office" in ln)
    assert "tool_count" in line, "工具數要從伺服器算好傳進來"
    assert not re.search(r"平台\s*\d+\s*支工具", line), "數字被寫死在樣板裡"


def test_the_count_is_the_whole_registry_not_what_this_user_can_see():
    """**是產品的規模，不是這位使用者看得到的幾支。**

    用「這位使用者有權限的工具數」的話，同一句話對不同人會顯示不同數字 ——
    那讀起來像產品縮水了。
    """
    import inspect
    from app.web import router as web_router
    src = inspect.getsource(web_router.build_router)
    assert '"tool_count": len(list(tools))' in src, \
        "工具數要取整個註冊表，不是過濾後的 tools_ctx"
