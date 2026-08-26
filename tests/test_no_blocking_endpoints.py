"""async 端點裡不可以直接做重活 —— 那會把整站鎖住。

2026-08-26 正式機實測：使用者在跑「文件去識別化」時，全站**兩分鐘完全不回應**。
日誌寫得很清楚：

    事件迴圈被卡住 116.4 秒（執行中 0 個作業、排隊 0 個）
    慢請求 116.9 秒：POST /tools/doc-deident/process

「執行中 0 個作業」是關鍵 —— 它根本沒走背景作業佇列，是**同步的重活直接寫在
async 端點裡**。同一支工具的公開 API 早就包在 `to_thread` 裡了，只有網頁用的
那條漏掉：同一件事兩份實作，只有一份修過。

同一份日誌裡還有 `POST /tools/pdf-editor/save` 卡 6.2 秒，同一個形狀。

這支測試**只釘住已經修好的端點**，不強求全站一次到位（其餘同形狀的端點列在
下面的待辦清單裡，修一支就從清單移到守門清單）。這樣新的 regression 會被抓到，
而既有的技術債不會讓整份測試永遠是紅的。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

#: 已經修好、不可以退步的端點：(檔案, 函式名)
GUARDED = [
    ("app/tools/doc_deident/router.py", "process"),
    ("app/tools/pdf_editor/router.py", "save"),
]

#: 還沒處理的同形狀端點（v1.14.55 盤點）。修好一支就搬到 GUARDED。
#: 留著這份清單是為了**不要假裝問題已經解決** —— 沉默的技術債會被遺忘。
KNOWN_REMAINING = 42


def _endpoint_body(path: str, func: str) -> ast.AST:
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == func:
            return node
    raise AssertionError(f"{path} 裡找不到 {func}()")


def _offloads(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr in ("to_thread", "run_in_executor")
               for n in ast.walk(node))


def _heavy_calls(node: ast.AST) -> set:
    heavy = {"apply_redactions", "get_pixmap", "insert_pdf", "subset_fonts",
             "convert_to_pdf", "convert_to_docx", "convert_to_odt", "render_page_png"}
    found = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr in heavy:
            found.add(n.attr)
    return found


@pytest.mark.parametrize("path,func", GUARDED)
def test_guarded_endpoints_offload_heavy_work(path, func):
    node = _endpoint_body(path, func)
    heavy = _heavy_calls(node)
    assert heavy, f"{func}() 裡找不到重活 —— 這條測試的前提變了，請重新確認"
    assert _offloads(node), (
        f"{path}:{func}() 直接在 async 端點裡做 {sorted(heavy)} —— "
        "會把事件迴圈鎖住，全站對所有人都不回應。"
        "請包成同步函式再 `await asyncio.to_thread(...)`。"
    )


def test_the_remaining_debt_is_not_growing():
    """其餘同形狀的端點只准變少，不准變多。

    不設成「必須是 0」是刻意的 —— 一次改 50 支端點的風險比 bug 本身高。
    但也不能就這樣算了，所以用一個會隨著新增而變紅的計數守著。
    """
    heavy = {"apply_redactions", "get_pixmap", "insert_pdf", "subset_fonts",
             "convert_to_pdf", "convert_to_docx", "convert_to_odt", "render_page_png"}
    guarded = {(p, f) for p, f in GUARDED}
    offenders = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            is_endpoint = any(
                isinstance(d, ast.Call)
                and getattr(d.func, "attr", "") in ("post", "get", "put", "delete", "api_route")
                for d in node.decorator_list)
            if not is_endpoint or (str(path), node.name) in guarded:
                continue
            if _heavy_calls(node) and not _offloads(node):
                offenders.append(f"{path}:{node.name}")

    assert len(offenders) <= KNOWN_REMAINING, (
        f"又多了直接在事件迴圈上做重活的端點（{len(offenders)} > {KNOWN_REMAINING}）：\n  "
        + "\n  ".join(sorted(offenders)[-8:])
        + "\n新端點請把重活包進 `asyncio.to_thread(...)`。")
