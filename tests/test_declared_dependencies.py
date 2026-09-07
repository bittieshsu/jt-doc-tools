"""`app/` 直接 import 的第三方套件，**一定要宣告成相依**。

由來（v1.15.15，客戶回報）：`defusedxml` 從 v1.15.8 起被四個模組直接
import，卻從來沒有寫進 `pyproject.toml` / `requirements.txt`。它只是碰巧
存在於開發機與 uv 解析出來的環境裡 —— **在沒裝到的機器上，那四支工具會在
啟動時被工具註冊表安靜跳過**（記一行 ERROR 進日誌，服務照常起來、
healthz 照樣 200），使用者只會發現「工具不見了」而沒有任何線索。

CLAUDE.md 早就記過同一個家族：`fonttools` 原本只是 pdf2docx 的傳遞相依，
上游換掉就會**無聲**退回。傳遞相依不是相依宣告。
"""
from __future__ import annotations

import ast
import re
import sys
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

#: 匯入名稱 → 發行套件名稱。`packages_distributions()` 對已安裝的套件回答得出
#: 來，這份表是它答不出來時的後備（例如命名空間套件）。
_FALLBACK = {
    "onelogin": "python3-saml",
    "fitz": "pymupdf",
}

#: 這些是本專案自己的東西，不是第三方。
_OURS = {"app", "tools", "tests", "scripts"}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared() -> set[str]:
    """`pyproject.toml` 的**執行期**相依。

    只看 `project.dependencies` —— 開發用的（pytest / bandit）不該出現在
    `requirements.txt`，`app/` 也不該 import 它們。
    """
    import tomllib
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data.get("project", {}).get("dependencies", []) or []
    out = set()
    for spec in deps:
        m = re.match(r"^([A-Za-z0-9._-]+)", spec.strip())
        if m:
            out.add(_norm(m.group(1)))
    return out


def _requirements() -> set[str]:
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    out = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)", line)
        if m:
            out.add(_norm(m.group(1)))
    return out


def _imported_roots() -> dict[str, list[str]]:
    """`app/` 底下 import 到的頂層模組名 → 哪些檔案 import 了它。"""
    found: dict[str, list[str]] = {}
    for py in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:                     # 不是我們要管的事
            continue
        rel = py.relative_to(ROOT).as_posix()   # Windows 給反斜線
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:                  # 相對匯入＝我們自己的模組
                    continue
                names = [node.module or ""]
            else:
                continue
            for name in names:
                root = name.split(".", 1)[0]
                if not root or root in _OURS:
                    continue
                if root in sys.stdlib_module_names:
                    continue
                found.setdefault(root, []).append(rel)
    return found


def _distribution_for(root: str) -> str | None:
    dists = packages_distributions().get(root)
    if dists:
        return _norm(sorted(dists)[0])
    if root in _FALLBACK:
        return _norm(_FALLBACK[root])
    return None


def test_every_third_party_import_is_declared():
    """對應不到發行套件時**要紅，不可以 skip**。

    `pytest.skip` 寫在迴圈裡會讓整條測試在第一個對應不到的模組就中止，
    後面一個都沒檢查 —— 而「全部 skip」在 pytest 輸出裡跟「全部通過」
    長得一模一樣。更糟的是：對應不到最常見的原因就是**那個套件沒裝**，
    也就是這條守門要抓的那件事本身。
    """
    declared = _declared()
    missing: list[str] = []
    for root, users in sorted(_imported_roots().items()):
        dist = _distribution_for(root)
        where = f"（{users[0]} 等 {len(users)} 處）"
        if dist is None:
            if _norm(root) in declared:
                continue                     # 宣告了、只是這台沒裝
            missing.append(f"{root} → 對應不到任何已安裝的發行套件{where}")
            continue
        if dist not in declared:
            missing.append(f"{root} → {dist}{where}")
    assert not missing, (
        "這些套件是**直接 import 的**，但 pyproject.toml 沒有宣告 —— "
        "只要提供它的上游換掉，那些模組就會在使用者的機器上安靜地載不進來：\n  "
        + "\n  ".join(missing))


def test_pyproject_and_requirements_agree():
    """`requirements.txt` 是沒有 uv 的機器走的路 —— 漏一個就是同一個病。"""
    reqs = _requirements()
    skip = {"jt-doc-tools"}
    missing = sorted(d for d in _declared() - reqs - skip
                     if d not in {"hatchling", "setuptools", "wheel"})
    assert not missing, f"pyproject 有但 requirements.txt 沒有：{missing}"


def test_the_scan_actually_reaches_the_app_package():
    """空迴圈的 `assert not missing` 永遠成立。"""
    roots = _imported_roots()
    assert len(roots) >= 20, f"只掃到 {len(roots)} 個第三方模組，掃描壞了"
    assert "fastapi" in roots and "defusedxml" in roots
