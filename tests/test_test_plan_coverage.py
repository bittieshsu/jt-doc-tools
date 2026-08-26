"""測試計畫本身的守門：計畫沒涵蓋到的東西要紅燈。

發版門檻是照 `TEST_PLAN.md` 跑的。**計畫漏了什麼，那塊就等於沒驗過** ——
而且是無聲的：報告看起來仍然全綠。

歷史教訓（2026-08-16 稽核）：必跑指令引用了一個**不存在的測試檔**，照抄下去
直接 file not found，整條資安檢查等於沒跑；24 個工具只有 API 抽測沒有功能
驗收；三個管理頁整頁零驗收。這些都是人工維護清單必然的下場。

所以這裡一律**從程式實算**（路由表、工具註冊表），不寫死期望值 —— 寫死的
期望值自己就是下一個會漂掉的東西。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLAN = ROOT / "TEST_PLAN.md"
PLAN_SEC = ROOT / "TEST_PLAN_SECURITY.md"


def _plan_text() -> str:
    return PLAN.read_text(encoding="utf-8") + "\n" + PLAN_SEC.read_text(encoding="utf-8")


def _routes():
    import app.main as app_main
    return app_main.app.routes


def test_every_tool_appears_in_the_plan():
    from app.tool_registry import discover_tools
    text = _plan_text()
    missing = sorted(t.metadata.id for t in discover_tools()
                     if t.metadata.id not in text)
    assert not missing, f"這些工具在測試計畫裡完全沒提到：{missing}"


def test_every_api_endpoint_appears_in_the_plan():
    """新增 API 卻忘了寫驗收 —— 這條會擋下來。

    比對用路徑尾段（`/api/` 之後那一截），避免因為前綴寫法不同而誤判。
    """
    text = _plan_text()
    missing = sorted({
        r.path for r in _routes()
        if "/api/" in getattr(r, "path", "")
        and r.path.split("/api/")[-1] not in text
    })
    assert not missing, (
        "這些 API 端點在測試計畫裡沒有任何驗收項：\n  "
        + "\n  ".join(missing)
        + "\n請補進 TEST_PLAN.md §4（工具 API）或 §4.6（非工具 API）。")


def test_admin_pages_appear_in_the_plan():
    """整頁零驗收的管理頁 —— 2026-08-16 稽核抓到過三個。"""
    text = _plan_text()
    pages = sorted({
        r.path for r in _routes()
        if getattr(r, "path", "").startswith("/admin/")
        and "{" not in r.path
        and "GET" in (getattr(r, "methods", None) or set())
        and "/api/" not in r.path
    })
    missing = [p for p in pages
               if p not in text and p.rsplit("/", 1)[-1] not in text]
    assert not missing, (
        "這些管理頁在測試計畫裡沒提到：\n  " + "\n  ".join(missing))


#: 只檢查「**指令**裡引用的檔案」。說明文字裡引用當反例的不算 ——
#: 掃描連說明一起掃會誤報，這個專案已經踩過兩次（migration FK 順序、
#: fail-open 形狀），這裡不再犯第三次。
_CMD_LINE = re.compile(r"^\s*(?:\$ )?(?:[A-Z_]+=\S+\s+)*"
                       r"(?:uv run |sudo )?(?:pytest|python3?|bash|sh)\b.*$",
                       re.MULTILINE)
_FILE_REF = re.compile(r"(?:tests|scripts|tools|temp|temp_pdfs)/[\w./-]+"
                       r"\.(?:py|sh|yaml|json)")


def test_commands_in_the_plan_reference_files_that_exist():
    """照著計畫貼上去的指令不可以 file not found。

    只看指令行，不看說明文字（說明裡會引用「當初寫錯的檔名」當反例）。
    """
    bad = []
    for doc in (PLAN, PLAN_SEC):
        text = doc.read_text(encoding="utf-8")
        for line in _CMD_LINE.findall(text):
            for ref in _FILE_REF.findall(line):
                if not (ROOT / ref).exists():
                    bad.append(f"{doc.name}: {ref}")
    assert not bad, ("計畫的必跑指令引用了不存在的檔案（照抄會直接失敗，"
                     "整條檢查等於沒跑）：\n  " + "\n  ".join(sorted(set(bad))))


def test_the_plan_does_not_hardcode_a_tool_count_that_can_drift():
    """計畫裡寫的工具數要跟實際一致（寫死的數字一定會漂）。"""
    from app.tool_registry import discover_tools
    actual = len(discover_tools())
    text = PLAN.read_text(encoding="utf-8")
    claims = [int(n) for n in re.findall(r"現\s*(\d+)\s*個工具", text)]
    wrong = [n for n in claims if n != actual]
    assert not wrong, f"計畫寫的工具數 {wrong} 與實際 {actual} 不符"
