"""以 root 寫資料目錄的 CLI 指令，收尾**一定要把擁有者改回去**。

`sudo jtdt …` 跑起來是 root。只要它在資料目錄裡**建出一個新檔案**
（audit.sqlite、`ocr_settings.json`、WAL、備份目錄…），那個檔案就是
root:root —— 服務是用 `jtdt` 帳號跑的，接下來會拿到

    sqlite3.OperationalError: attempt to write a readonly database

而且**是在使用者做完救援動作之後才壞的**：`jtdt reset-password` 正是
「被鎖在門外」時的那條路，救完卻登不進去是最糟的失敗方式。

v1.4.2 已經為 `jtdt auth disable` 踩過一次（設定檔變成 root:root 600，
畫面上顯示預設值），當時加了 `_chown_data_files_back()`。這條守門是把
那個保險釘死在**每一支會寫資料目錄的指令**上。
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

cli = importlib.import_module("app.cli")
SRC = Path(cli.__file__).read_text(encoding="utf-8")
TREE = ast.parse(SRC)

#: 會以 root 寫入資料目錄的指令。新增這類指令時要一起加進來 ——
#: 這份清單是刻意手寫的：「有沒有寫到資料目錄」沒辦法只靠靜態掃描判斷
#: （寫入常常藏在被呼叫的模組裡，例如 `tessdata_manager` 寫
#: `ocr_settings.json`）。
MUST_CHOWN = {
    "svc_update",
    "svc_reset_password",
    "svc_audit_user_create",
    "_run_auth_helper",          # auth disable / show / set-local / db backup / restore
}

#: 這幾支的寫入點分散在好幾個 return 之後，統一在派送層用 `finally` 收尾。
CHOWN_AT_DISPATCH = {
    "svc_ocr_lang_install", "svc_ocr_lang_remove",
    "svc_ocr_lang_switch", "svc_ocr_lang_quality",
}

_GUARD = "_chown_data_files_back"


def _func(name: str) -> ast.FunctionDef:
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"`app/cli.py` 裡找不到 {name}（改名了？）")


def _calls(node: ast.AST) -> set[str]:
    """真的**呼叫**到的名字 —— 用 AST 的 Call 節點，不是字串比對。

    註解裡提到 `_chown_data_files_back` 跟真的呼叫它，在字串比對眼中
    一模一樣（這個專案被自己寫的註解騙過一次）。
    """
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                out.add(n.func.id)
            elif isinstance(n.func, ast.Attribute):
                out.add(n.func.attr)
    return out


@pytest.mark.parametrize("name", sorted(MUST_CHOWN))
def test_root_commands_hand_the_data_dir_back(name: str):
    called = _calls(_func(name))
    assert _GUARD in called or "_run_auth_helper" in called, (
        f"`{name}` 會以 root 寫資料目錄，卻沒有呼叫 {_GUARD}()。"
        "少了它，服務帳號之後會拿到 attempt to write a readonly database。")


def test_the_ocr_commands_are_covered_at_the_dispatch_layer():
    """它們有好幾個 return 點，逐個補一定會漏 —— 統一在派送層 `finally`。"""
    main = _func("main")
    for node in ast.walk(main):
        if not isinstance(node, ast.Try) or not node.finalbody:
            continue
        if _GUARD not in _calls(ast.Module(body=node.finalbody, type_ignores=[])):
            continue
        inner = _calls(ast.Module(body=node.body, type_ignores=[]))
        if CHOWN_AT_DISPATCH <= inner:
            return
    raise AssertionError(
        f"派送 {sorted(CHOWN_AT_DISPATCH)} 的地方沒有用 finally 收尾呼叫 {_GUARD}()")


def test_the_guard_itself_is_safe_to_call_anywhere():
    """它會在每一支指令收尾時被呼叫，所以自己不可以炸掉或誤動。"""
    fn = _func("_chown_data_files_back")
    src = ast.get_source_segment(SRC, fn) or ""
    assert "if not owner" in src, "取不到擁有者時要直接返回"
    assert "if uid == 0" in src, "資料目錄本來就是 root 的話不必動"
    assert "except" in src, "chown 失敗不可以讓指令整個失敗"


def test_windows_has_no_ownership_to_restore():
    assert "_is_windows()" in (ast.get_source_segment(SRC, _func("_data_dir_owner")) or "")
