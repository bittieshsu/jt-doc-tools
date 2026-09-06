"""zip 炸彈：**每一條讀使用者 zip 的路徑都要擋得住**。

## 由來

辦公文件、設定備份、資產匯入、工作區收檔、送件檢核、文件翻譯 —— 全都是 zip。
2026-09-06 加毀損檔攔截時只在辦公文件那條路加了防護，其餘幾處還敞著：
一個 40 KB 的檔案可以在伺服器上安靜地展開成幾十 GB。

判斷**全站只有一份**（`app/core/zip_guard.py`）—— 寫在各處一定會漂，
而漏掉的那一處完全看不出來。

**唯一的例外是設定備份的匯入**：它有自己更嚴格的一套（總量 2 GiB、
單檔 512 MiB、外加 zip-slip 路徑白名單），因為它會把內容寫進 `data/`。
那條路的風險不只是資源耗盡，所以**不可以為了統一而換成通用判斷**。
"""
from __future__ import annotations

import io
import zipfile

import pytest

from app.core import zip_guard


def _bomb(ratio_only: bool = False) -> bytes:
    """壓縮比高、解開後 200 MB 的 zip。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", b"\0" * (200 * 1024 * 1024))
    return buf.getvalue()


def _normal() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<document>" + "x" * 5000 + "</document>")
    return buf.getvalue()


def test_a_bomb_is_rejected():
    with pytest.raises(zip_guard.ZipBombError):
        with zipfile.ZipFile(io.BytesIO(_bomb())) as z:
            zip_guard.check(z)


def test_a_normal_document_passes():
    """**好檔案一個都不能誤擋** —— 誤擋比漏擋更糟，使用者會以為自己的檔壞了。"""
    with zipfile.ZipFile(io.BytesIO(_normal())) as z:
        zip_guard.check(z)


def test_a_small_but_highly_compressible_file_passes():
    """小檔案的高壓縮比很常見且無害（純文字的 .odt）—— 不可以只看比例。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("content.xml", b"a" * (2 * 1024 * 1024))   # 2 MB 全同字元
    with zipfile.ZipFile(buf) as z:
        zip_guard.check(z)          # 不可以丟例外


def test_office_conversion_refuses_a_bomb(tmp_path):
    from app.core import office_convert as oc
    p = tmp_path / "bomb.docx"
    p.write_bytes(_bomb())
    with pytest.raises(oc.OfficeSourceError):
        oc.ensure_readable(p)


def test_every_user_facing_zip_read_is_guarded():
    """**守門的守門**：新增一處讀使用者 zip 卻沒接防護時要紅。

    判準是原始碼形狀 —— 掃出「開啟 zip 來讀」的地方，逐一確認同一個函式裡
    有呼叫防護。已知的例外寫在 `EXEMPT` 並附理由（設定匯入有自己更嚴格的
    一套；讀我們自己產出的檔不算使用者輸入）。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    #: 檔案 → 為什麼不需要走通用防護
    EXEMPT = {
        "core/settings_export.py":
            "有自己更嚴格的一套（2 GiB / 512 MiB / zip-slip 白名單）",
        "main.py": "讀的是我們自己產出的作業結果，不是使用者輸入",
        "tools/aes_zip/router.py": "工具已停用",
        "tools/pdf_to_office/engines/doc_merge.py": "讀 soffice 產出的中間檔",
        "tools/pdf_to_office/engines/draw_engine.py": "讀 soffice 產出的中間檔",
        "tools/pdf_to_slides/engines/slides_engine.py": "讀 soffice 產出的中間檔",
        "core/zip_guard.py": "防護本身",
    }
    bad = []
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in str(p):
            continue
        rel = p.relative_to(root).as_posix()
        if rel in EXEMPT:
            continue
        src = p.read_text(encoding="utf-8")
        if "ZipFile(" not in src:
            continue
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # **用 AST 找真正的呼叫，不要用字串比對** —— 第一版檢查
            # `"zip_guard" not in body`，結果被我自己寫的**註解**騙過去
            # （註解裡就有 `zip_guard` 四個字），變異驗證時才發現守門是假的。
            # 這個坑本專案記過很多次：靜態掃描不可以連註解一起掃。
            reads = guarded = False
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                fname = getattr(sub.func, "attr", None) or getattr(sub.func, "id", None)
                if fname == "ZipFile":
                    # 寫出用的第二個參數是 "w"
                    mode = (sub.args[1].value
                            if len(sub.args) > 1 and isinstance(sub.args[1], ast.Constant)
                            else None)
                    if mode != "w":
                        reads = True
                elif fname in ("_zip_check", "check_path") or (
                        fname == "check"
                        and getattr(getattr(sub.func, "value", None), "id", "") == "zip_guard"):
                    guarded = True
            if reads and not guarded:
                bad.append(f"{rel}::{node.name}")
    assert not bad, (
        "這些地方讀了 zip 卻沒有接 zip 炸彈防護（`app/core/zip_guard.py`）：\n  "
        + "\n  ".join(bad)
        + "\n真的不需要就加進本測試的 EXEMPT 並寫明理由。")
