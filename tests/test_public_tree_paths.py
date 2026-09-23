"""測試不可以寫死 `github/` 這一層（2026-09-13，CI 在 main 上紅了才抓到）。

開發樹的公開檔在 `github/` 底下，**標準 clone 下來就在根目錄** ——
寫死 `github/OPS.md` 的測試在 clone 上永遠 `FileNotFoundError`，
而開發機上一直是綠的。

這條 CLAUDE.md 記過好幾次（v1.15.7 的外部評估一次紅了 70 支），
但**沒有檢查**，所以 v1.15.24 新寫的 `test_ops_iis_prereq_order.py` 又踩了
—— 而且是**推上 GitHub 之後 CI 才告訴我們**。

判準走 AST：找 `Path("github/...")`、`ROOT / "github"` 這種寫法。
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 這些檔案裡的 `github/` 是**說明文字**或**刻意比對開發樹**，不是路徑解析。
#: 每一條都要寫理由。
_ALLOWED = {
    # 這支測試自己在講那個字串
    "test_public_tree_paths.py",
    # 這支的用途就是驗「開發樹與公開樹兩種結構都跑得起來」
    "test_repo_paths.py",
}


def _offenders() -> list[str]:
    """只抓「字面值**直接被當成路徑用**」的形狀。

    有些測試把 `"github/install.sh"` 當**邏輯名稱**放在對照表裡，之後再用
    helper 解析成開發樹或公開樹的實際位置（`test_heic_support` /
    `test_dependency_declaration_sop` 就是這樣）—— 那是正確的寫法，
    不可以誤報。所以判準是這兩種：

        Path("github/xxx") / open("github/xxx")   ← 直接構成路徑
        ROOT / "github" / "xxx"                   ← 用 `/` 接起來
    """
    bad = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        if path.name in _ALLOWED:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            # ① Path("github/…") / open("github/…")
            if isinstance(node, ast.Call):
                fname = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
                if fname in ("Path", "open", "read_text", "read_bytes"):
                    for arg in node.args[:1]:
                        if isinstance(arg, ast.Constant) and \
                                isinstance(arg.value, str) and \
                                arg.value.startswith("github/"):
                            bad.append(f"{path.name}:{node.lineno} "
                                       f"{fname}({arg.value!r})")
            # ② x / "github" / …
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                for side in (node.left, node.right):
                    if isinstance(side, ast.Constant) and side.value == "github":
                        bad.append(f'{path.name}:{node.lineno} … / "github"')
    return bad


def test_no_test_hard_codes_the_github_layer():
    bad = _offenders()
    assert not bad, (
        "這些測試寫死了 `github/` 那一層 —— 在標準 clone 上會 "
        "FileNotFoundError（開發機上看不出來）：\n  " + "\n  ".join(bad)
        + "\n請改用 `tools.repo_paths.public_root()`。")


def test_the_resolver_exists_and_picks_by_file_presence():
    """`public_root()` 的判準必須是**檔案在不在**，不是資料夾名字。"""
    from tools.repo_paths import public_root
    root = public_root(ROOT)
    assert (root / "README.md").exists(), root
    assert (root / "OPS.md").exists(), root
