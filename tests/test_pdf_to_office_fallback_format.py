"""引擎退回別的格式時，**檔名要跟著實際內容走**，而且要講出來。

jtdt-reform 的 ODT→docx 那一步失敗時會改交 ODT（`ConvertResult.output_format`
變成 `"odt"`）。原本兩條路（網頁與 `/convert` API）都照**使用者要求的**格式命名：
ODT 的內容配上 `.docx` 檔名，Word 打開說檔案毀損，而畫面上寫「完成」。
2026-09-24 在 Windows 實機上測到（那一步在 Windows 上會卡到逾時才失敗）。
"""
from __future__ import annotations

import ast
import importlib
import pathlib
from types import SimpleNamespace

R = importlib.import_module("app.tools.pdf_to_office.router")
ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_the_extension_follows_what_was_actually_produced():
    ext, note = R._delivered_ext(SimpleNamespace(output_format="odt"), "docx")
    assert ext == ".odt"
    assert note, "退回了卻沒有講 —— 使用者會以為拿到的是 Word 檔"


def test_no_note_when_nothing_fell_back():
    """反向對照：沒有退回時不可以多一句話（不然每一次都在喊「失敗」）。"""
    for fmt in ("docx", "odt"):
        ext, note = R._delivered_ext(SimpleNamespace(output_format=fmt), fmt)
        assert ext == f".{fmt}" and note == ""


def test_both_conversion_paths_use_it():
    """網頁與 API 兩條路都要用同一支 —— 只修一條的話另一條照樣交出錯的檔名。"""
    tree = ast.parse((ROOT / "app/tools/pdf_to_office/router.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "_delivered_ext"]
    assert len(calls) >= 2, f"只有 {len(calls)} 條路用 _delivered_ext"
    helper = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_delivered_ext")
    inside = {id(n) for n in ast.walk(helper)}
    bad = [n.lineno for n in ast.walk(tree)
           if isinstance(n, ast.IfExp) and isinstance(n.body, ast.Constant)
           and n.body.value == ".odt" and id(n) not in inside]
    assert not bad, f"還有地方照要求的格式組副檔名（第 {bad} 行）"
