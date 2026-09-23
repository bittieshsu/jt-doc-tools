"""程式碼裡不可以用到**從來沒定義過**的名稱（v1.16.11）。

一次查出七處，其中四處是真的會壞：

| 名稱 | 從哪一版 | 後果 |
|---|---|---|
| `cli.py` 的 `_safe_fetch` | v1.15.11 | `jtdt update` 的健康檢查**三個平台一律失敗**（服務其實是好的） |
| `main.py` 的 `html_mod` | v1.14.16 | 瀏覽器遇到 401 / 403 / 404 的友善錯誤頁變成 **500** |
| `admin/router.py` 的 `log` / `logger` | v1.15.13 / v1.14.51 | 該回 400 的變成 500 |
| `tessdata_manager.py` 的 `log` | v1.7.5 | 失敗時丟例外而不是回 False |

**共同點是 NameError 被 `except Exception` 吞掉或變成 500** —— 沒有任何測試
紅，因為每一處都在「出錯時才走到」的分支上。同一個檔案（`admin/router.py`）
v1.14.31 就記過一次一模一樣的病（`settings` 沒 import，整組設定備份是死的），
**當時沒有加檢查**，於是又發生四次。

判準是自己寫的 AST 範圍分析，**不依賴 pyflakes**（CI 與正式機都沒裝，裝不到
就 skip 的檢查等於沒有）。刻意保守：

* 型別註記一律不看 —— 有 `from __future__ import annotations` 時是字串，
  區域變數的註記本來就不會被求值。
* 模組裡有 `from x import *` 就整支跳過（看不出它帶進了什麼）。
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__package__", "__spec__",
                   "__loader__", "__builtins__", "__path__", "__annotations__",
                   "__dict__", "__class__", "__version__"}
_BUILTINS = set(dir(builtins)) | _MODULE_DUNDERS


def _targets(node: ast.AST) -> set[str]:
    """一個賦值目標綁定了哪些名稱。"""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, ast.Starred) and isinstance(n.value, ast.Name):
            out.add(n.value.id)
    return out


def _bound_in(body_nodes) -> set[str]:
    """一個範圍（模組 / 函式 / 類別）**自己**綁定的名稱 —— 不走進巢狀的函式與類別。"""
    out: set[str] = set()
    stack = list(body_nodes)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
            # 裝飾器與預設值是在外層求值的，但它們只會「用」不會「綁」
            continue
        if isinstance(n, ast.Lambda):
            continue
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, (ast.Assign,)):
            for t in n.targets:
                out |= _targets(t)
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
            out |= _targets(n.target)
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            out |= _targets(n.target)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                if it.optional_vars is not None:
                    out |= _targets(it.optional_vars)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.NamedExpr):
            out |= _targets(n.target)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out |= set(n.names)
        elif isinstance(n, ast.Delete):
            for t in n.targets:
                out |= _targets(t)
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.MatchMapping) and n.rest:
            out.add(n.rest)
        stack.extend(ast.iter_child_nodes(n))
    return out


def _globals_declared(tree: ast.Module) -> set[str]:
    """函式裡 `global x` 之後賦值的 x 也是模組層的名字。"""
    return {name for n in ast.walk(tree) if isinstance(n, ast.Global) for name in n.names}


def _strip_annotations(tree: ast.AST) -> None:
    for n in ast.walk(tree):
        if isinstance(n, ast.arg):
            n.annotation = None
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n.returns = None
        elif isinstance(n, ast.AnnAssign):
            n.annotation = ast.Constant(value=None)


def undefined_names(src: str) -> list[tuple[int, str]]:
    tree = ast.parse(src)
    if any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
           for n in ast.walk(tree)):
        return []
    _strip_annotations(tree)
    module_scope = _bound_in(tree.body) | _globals_declared(tree) | _BUILTINS
    bad: list[tuple[int, str]] = []

    def visit(node: ast.AST, visible: set[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                # 裝飾器與參數預設值在**外層**求值
                if not isinstance(child, ast.Lambda):
                    for d in child.decorator_list:
                        visit_expr(d, visible)
                for d in child.args.defaults + [x for x in child.args.kw_defaults if x]:
                    visit_expr(d, visible)
                a = child.args
                params = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
                if a.vararg:
                    params.add(a.vararg.arg)
                if a.kwarg:
                    params.add(a.kwarg.arg)
                body = child.body if isinstance(child.body, list) else [child.body]
                inner = visible | params | _bound_in(body)
                for stmt in body:
                    visit_expr(stmt, inner) if isinstance(child, ast.Lambda) else None
                if not isinstance(child, ast.Lambda):
                    wrapper = ast.Module(body=child.body, type_ignores=[])
                    visit(wrapper, inner)
            elif isinstance(child, ast.ClassDef):
                for d in child.decorator_list + child.bases + [k.value for k in child.keywords]:
                    visit_expr(d, visible)
                # 類別本體看得到自己綁的名字；但類別裡的**方法**看不到（Python 的規則）
                class_scope = visible | _bound_in(child.body)
                for stmt in child.body:
                    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        visit(ast.Module(body=[stmt], type_ignores=[]), visible)
                    else:
                        visit(ast.Module(body=[stmt], type_ignores=[]), class_scope)
            elif isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                inner = visible | {t for g in child.generators for t in _targets(g.target)}
                visit(child, inner)
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                if child.id not in visible:
                    bad.append((child.lineno, child.id))
            else:
                visit(child, visible)

    def visit_expr(expr: ast.AST, visible: set[str]) -> None:
        visit(ast.Module(body=[ast.Expr(value=expr)] if isinstance(expr, ast.expr)
                         else [expr], type_ignores=[]), visible)

    visit(tree, module_scope)
    return bad


def _sources() -> list[Path]:
    out = []
    for top in ("app", "tools"):
        out += [p for p in (ROOT / top).rglob("*.py") if "__pycache__" not in p.parts]
    return sorted(out)


SOURCES = _sources()


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_undefined_names(path):
    bad = undefined_names(path.read_text(encoding="utf-8"))
    assert not bad, (
        f"{path.relative_to(ROOT).as_posix()} 用到從來沒定義過的名稱："
        + ", ".join(f"第 {ln} 行 {name}" for ln, name in bad)
        + "\n（多半是 import 漏了、或被插進字串裡 —— 在 except 裡它會變成無聲的失敗）")


def test_the_scan_reaches_the_code():
    """掃 0 支跟「都乾淨」在輸出裡長得一樣。"""
    rel = {p.relative_to(ROOT).as_posix() for p in SOURCES}
    assert len(SOURCES) >= 200, len(SOURCES)
    for must in ("app/cli.py", "app/main.py", "app/admin/router.py"):
        assert must in rel


@pytest.mark.parametrize("src,name", [
    # 真正發生過的形狀：import 在一段產生出來的腳本字串裡
    ('S = """\nfrom app.core import safe_fetch as _safe_fetch\n"""\n'
     "def f():\n    try:\n        _safe_fetch.urlopen('x')\n    except Exception:\n        pass\n",
     "_safe_fetch"),
    ("def f():\n    return html_mod.escape('x')\n", "html_mod"),
    ("class A:\n    x = 1\n    def m(self):\n        return x\n", "x"),       # 方法看不到類別本體
    ("def f():\n    return [y for y in range(3)] + [y]\n", "y"),            # 推導式不外洩
])
def test_the_checker_catches_them(src, name):
    assert name in {n for _, n in undefined_names(src)}


@pytest.mark.parametrize("src", [
    "import os\ndef f():\n    return os.sep\n",
    "def f(a, *b, c=1, **d):\n    return a, b, c, d\n",
    "def f():\n    global G\n    G = 1\ndef g():\n    return G\n",
    "def f():\n    x = 1\n    def g():\n        return x\n    return g\n",
    "def f(p: 'Undefined') -> 'AlsoUndefined':\n    v: Nope = 1\n    return v\n",
    "try:\n    import foo\nexcept ImportError:\n    foo = None\nprint(foo)\n",
    "print(__file__, __name__)\n",
    "def f():\n    return [a + b for a in range(2) for b in range(a)]\n",
    "class A:\n    x = 1\n    y = x + 1\n",
    "def f():\n    return later()\ndef later():\n    return 1\n",
    "def f():\n    if (n := 3) > 1:\n        return n\n",
])
def test_the_checker_does_not_cry_wolf(src):
    assert undefined_names(src) == []
