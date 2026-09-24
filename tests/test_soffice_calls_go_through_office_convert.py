"""所有 soffice 轉檔都要走 `app/core/office_convert.py`；Windows 上不可以執行
`soffice --version`。

**2026-09-24 在 Windows 實機（安裝程式全新安裝）上抓到的一整族：**

| 地方 | 自己寫的 | 在 Windows 服務裡的後果 |
|---|---|---|
| PDF 轉文書檔的結果預覽 | `file://{profile_dir}` | 不合法的網址 → 卡 180 秒 → 預覽空白（一頁 PDF 要 3 分鐘） |
| jtdt-reform 輸出 docx | 同上 | 卡到逾時才失敗 |
| 擷取文字的 ODT 輸出 | 沒指定設定檔 | 用系統帳號的預設設定檔，初次設定卡住 |
| 相依檢查 / 轉檔設定頁查版本 | `soffice --version` | 不會自己結束，每次都留下一支 soffice.bin |

`file://` + `C:\\…` 在 Linux 上剛好不會錯（路徑是 `/` 開頭，湊成 `file:///…`），
所以開發機與 CI 上**永遠是綠的**。issue #5（v1.5.1）修過一模一樣的網址問題，
當時只修了 office_convert 裡的那幾支 —— 另外三處自己呼叫 soffice 的一直沒改到。

判準：`--convert-to` 與 `UserInstallation` 這兩個字串**只准出現在 office_convert**
（AST 看真的字串常數，不看說明與註解）。
"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_ALLOWED = {"app/core/office_convert.py"}


def _code_strings(path: pathlib.Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.body and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)}
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            out.append((n.lineno, n.value))
    return out


def _app_files() -> list[pathlib.Path]:
    files = sorted((ROOT / "app").rglob("*.py"))
    assert len(files) > 200, f"只掃到 {len(files)} 支 —— 範圍不對，這條等於沒檢查"
    return files


def test_nobody_builds_their_own_soffice_command():
    bad = []
    for p in _app_files():
        rel = p.relative_to(ROOT).as_posix()
        if rel in _ALLOWED:
            continue
        for ln, s in _code_strings(p):
            if "--convert-to" in s or "UserInstallation" in s:
                bad.append(f"{rel}:{ln}  {s[:60]!r}")
    assert not bad, (
        "有地方自己組 soffice 指令 —— 一律改用 office_convert 的 convert_to_*()。\n"
        "自己組的 `file://{路徑}` 在 Windows 上不是合法網址（服務會卡到逾時），"
        "逾時也只殺得到外層的 soffice.exe：\n  " + "\n  ".join(bad))


def test_the_scan_really_sees_office_convert():
    """反向對照：掃描器要真的認得出那兩個字串 —— 不然上面那條永遠是綠的。"""
    hits = [s for _, s in _code_strings(ROOT / "app/core/office_convert.py")
            if "--convert-to" in s or "UserInstallation" in s]
    assert len(hits) >= 5, f"在 office_convert 只認出 {len(hits)} 處，掃描器壞了"


def test_version_probes_use_the_shared_helper():
    """兩支查版本的都要委派給 `soffice_version`（不可以自己跑 `--version`）。"""
    for rel, func in (("app/core/conv_settings.py", "_probe_version"),
                      ("app/core/sys_deps.py", "_probe_office")):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == func)
        calls = {getattr(c.func, "id", getattr(c.func, "attr", ""))
                 for c in ast.walk(fn) if isinstance(c, ast.Call)}
        assert "soffice_version" in calls, f"{rel}:{func} 沒有委派給 soffice_version"
        consts = [n.value for n in ast.walk(fn)
                  if isinstance(n, ast.Constant) and n.value == "--version"]
        assert not consts, f"{rel}:{func} 還在自己跑 --version"


def test_windows_never_launches_soffice_to_read_its_version(monkeypatch):
    """Windows 上查版本**一個行程都不可以起**，讀執行檔的版本資源就好。"""
    from app.core import office_convert as oc

    def _boom(*a, **k):
        raise AssertionError("Windows 上不可以為了查版本啟動 soffice")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(oc, "_win_product_version", lambda b: "11.0.5.1")
    assert oc.soffice_version(r"C:\Program Files\OxOffice\program\soffice.exe") \
        == "OxOffice 11.0.5.1"
    monkeypatch.setattr(oc, "_win_product_version", lambda b: "26.2.3.2")
    assert oc.soffice_version(r"C:\Program Files\LibreOffice\program\soffice.exe") \
        == "LibreOffice 26.2.3.2"
    monkeypatch.setattr(oc, "_win_product_version", lambda b: "")
    assert oc.soffice_version(r"C:\x\soffice.exe") == ""


@pytest.mark.skipif(sys.platform.startswith("win"), reason="只在非 Windows 跑 --version")
def test_other_platforms_still_read_the_version():
    from app.core import office_convert as oc
    b = oc.find_soffice()
    if not b:
        pytest.skip("這台沒有 soffice")
    v = oc.soffice_version(b)
    assert v and ("Office" in v), f"取不到版本：{v!r}"
    assert not any(len(w) >= 20 for w in v.split()), f"建置雜湊沒有去掉：{v!r}"
