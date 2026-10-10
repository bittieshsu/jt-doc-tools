"""同一個模組裡不可以有兩個同名的頂層函式或類別。

後定義的那個會**安靜地蓋掉**前一個，不會有任何錯誤：2026-10-08 公文撰擬加匯出選項時，新寫的
`_page_extras(body, case_id)` 蓋掉了頁面本身用的 `_page_extras(user_id)`，整個頁面打不開 ——
只有真的開瀏覽器的那條測試看得到。順手掃出 `permissions.list_roles_for_subject` 也定義了兩次
（前一個從來沒被用到，已拿掉）。
"""
from __future__ import annotations

import ast
import collections
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCAN = ("app", "tools")


def _files():
    out = []
    for d in SCAN:
        out += sorted((ROOT / d).rglob("*.py"))
    return out


def test_the_scan_reaches_the_code():
    assert len(_files()) > 300


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_duplicate_top_level_names(path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        pytest.skip("不是這支檢查管的")
    seen = collections.Counter(
        n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    dup = sorted(k for k, v in seen.items() if v > 1)
    assert not dup, f"同名的頂層定義（後面的會蓋掉前面的）：{dup}"


def _is_constant(name: str) -> bool:
    return name.lstrip("_").isupper()


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_module_constant_is_assigned_twice(path):
    """全大寫的模組常數（`_ARTICLE_RE = re.compile(...)` 這種）也一樣：2026-10-09 公文撰擬的參考資料
    要抽條號，新加的 `_ARTICLE_RE` 蓋掉了檢查草稿條號用的同名正規式，
    事實檢查一跑就 `IndexError: no such group`（每一份有條號的草稿都產生不出來）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        pytest.skip("不是這支檢查管的")
    seen: collections.Counter = collections.Counter()
    for n in tree.body:
        targets = n.targets if isinstance(n, ast.Assign) else (
            [n.target] if isinstance(n, ast.AnnAssign) and n.value is not None else [])
        for tg in targets:
            if isinstance(tg, ast.Name) and _is_constant(tg.id):
                seen[tg.id] += 1
    dup = sorted(k for k, v in seen.items() if v > 1)
    assert not dup, f"同一個模組常數設了兩次（後面的會蓋掉前面的）：{dup}"
