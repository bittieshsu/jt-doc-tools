"""OCR 完成訊息要講出「實際用了哪個引擎、有沒有退回」—— 網頁與 API 兩條路都要。

原本只有網頁那條有。API 送出的作業在「我的作業」上只寫「插入 12 字」，
而那其實是 EasyOCR 模型下載失敗、退回 Tesseract 辨識的結果 —— 看起來像
這份掃描件本來就沒什麼字（Win10 實機踩到）。
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

from app.tools.pdf_ocr import router as _unused  # noqa: F401  (確認載得進來)
import importlib

R = importlib.import_module("app.tools.pdf_ocr.router")
SRC = Path(R.__file__).read_text(encoding="utf-8")


def test_the_note_names_the_fallback():
    note = R._ocr_engine_note({
        "ocr_engine_pages": {"tesseract": 1},
        "ocr_chosen_engine": "easyocr",
        "ocr_engine_total_s": 3.2,
    })
    assert "退回" in note and "Tesseract" in note and "EasyOCR" in note


def test_no_fallback_is_not_reported_as_one():
    note = R._ocr_engine_note({
        "ocr_engine_pages": {"easyocr": 2},
        "ocr_chosen_engine": "easyocr",
        "ocr_engine_total_s": 5,
    })
    assert "退回" not in note and "EasyOCR" in note


def test_every_completion_message_carries_the_note():
    """每一個設定「完成 — 處理 …」的地方都要接上那一段。"""
    tree = ast.parse(SRC)
    msgs = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Attribute)
                and node.targets[0].attr == "message"):
            text = ast.unparse(node.value)
            if "完成 — 處理" in text:
                msgs.append(text)
    assert len(msgs) >= 2, f"應該有網頁與 API 兩條路，只找到 {len(msgs)} 個"
    for m in msgs:
        assert "_ocr_engine_note" in m or "extra" in m, f"這個完成訊息沒講用了哪個引擎：{m[:80]}"


def test_tool_registration_is_logged_once(caplog):
    """作業完成通知每次都會呼叫 discover_tools() —— 不可以每次灌 50 行記錄。"""
    from app import tool_registry as tr
    tr._LOGGED_REGISTERED.clear()
    with caplog.at_level(logging.INFO, logger="app.tool_registry"):
        n = len(tr.discover_tools())
        tr.discover_tools()
        tr.discover_tools()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Registered tool:")]
    assert n > 10
    assert len(lines) == n, f"呼叫三次記了 {len(lines)} 行（工具 {n} 支），應該每支只記一次"
