"""缺中文字型時，**一般使用者**在工具頁上看得到提示（v1.14.47）。

測試清單：

  偵測層（`app/core/font_health.py`）
    1. 繪製時挑得到字型            → ok=True，帶回字型檔名
    2. 黑體沒有、明體有            → ok=True（不為了字形差異嚇使用者）
    3. 兩種都挑不到                → ok=False，且安裝指令**取自 sys_deps**
                                     （不可在 font_health 裡另抄一份）
    4. 偵測本身炸掉                → Jinja global 回 ok=True（寧可安靜，不可誤報）

  版面層
    5. 會把中文寫進 PDF 的工具     → 模板都要 include 提示元件（**自動列舉**，
                                     不寫死清單，否則下一支新工具照樣會漏）
    6. 字型齊全                    → 頁面上完全沒有這塊
    7. 字型缺少 + 管理員 / 單機模式 → 給得出安裝路徑（相依套件檢查 + 指令）
    8. 字型缺少 + 一般使用者        → 改成「請聯絡管理員」，不給進不去的管理連結
    9. `.cjk-warn` 樣式必須在 platform.css 裡（自己發明的類別名沒補樣式，
       畫面會是一段沒有樣式的文字攤在那裡）
"""
from __future__ import annotations

import ast
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as app_main

_ROOT = Path(__file__).resolve().parent.parent
_TOOLS = _ROOT / "app" / "tools"
_INCLUDE = 'components/cjk_font_notice.html'
#: 一經匯入就代表「這支工具會自己把中文畫進 PDF」的模組。
#: `pdf_text_overlay` 是核心的填字引擎（表單填寫走它），也算。
_CJK_MODULES = {"font_catalog", "pdf_text_overlay"}


def _imports_cjk_font_path(py: Path) -> bool:
    """這個檔案有沒有真的 import 到會畫中文的模組。

    用 `ast` 看 import 節點，不做字串比對 —— 註解或說明裡提到模組名不算
    （守門掃描連說明一起掃會誤報，本專案踩過兩次）。
    """
    try:
        tree = ast.parse(py.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")
            if _CJK_MODULES.intersection(mod):
                return True
            if any(a.name in _CJK_MODULES for a in node.names):
                return True
        elif isinstance(node, ast.Import):
            for a in node.names:
                if _CJK_MODULES.intersection(a.name.split(".")):
                    return True
    return False


def _cjk_writing_tool_dirs() -> list[Path]:
    out = []
    for pkg in sorted(p for p in _TOOLS.iterdir() if p.is_dir()):
        if pkg.name.startswith("__"):
            continue
        if any(_imports_cjk_font_path(py) for py in pkg.rglob("*.py")):
            out.append(pkg)
    return out


# ------------------------------------------------------------------ 1, 2, 3
def test_status_ok_when_a_cjk_font_is_resolvable(monkeypatch):
    from app.core import font_catalog, font_health
    monkeypatch.setattr(font_catalog, "best_cjk_path",
                        lambda style="sans", cjk="traditional":
                        (Path("/fonts/NotoSansCJK-Regular.ttc"), 3))
    st = font_health.cjk_status()
    assert st["ok"] is True
    assert st["font"] == "NotoSansCJK-Regular.ttc"


def test_status_ok_when_only_serif_is_available(monkeypatch):
    """只有明體時工具會拿明體頂替黑體 —— 字形有出入但不缺字，不該報警。"""
    from app.core import font_catalog, font_health

    def _only_serif(style="sans", cjk="traditional"):
        return (Path("/fonts/NotoSerifCJK-Regular.ttc"), 3) if style == "serif" else None

    monkeypatch.setattr(font_catalog, "best_cjk_path", _only_serif)
    assert font_health.cjk_status()["ok"] is True


def test_status_missing_pulls_install_cmd_from_sys_deps(monkeypatch):
    from app.core import font_catalog, font_health, sys_deps
    monkeypatch.setattr(font_catalog, "best_cjk_path",
                        lambda style="sans", cjk="traditional": None)
    st = font_health.cjk_status()
    assert st["ok"] is False
    assert st["font"] == ""
    # 指令必須是 sys_deps 那一份算出來的，不是這裡另外寫死的字串
    assert st["install_cmd"] == sys_deps.install_cmd_for("cjk-fonts")


def test_font_health_does_not_hardcode_install_cmd():
    """安裝指令只能有一份（`sys_deps._DEPS`）。"""
    src = (_ROOT / "app" / "core" / "font_health.py").read_text(encoding="utf-8")
    assert "install_cmd_for" in src
    assert "apt install" not in src


# ---------------------------------------------------------------------- 4
def test_jinja_global_fails_open_when_probe_raises(monkeypatch):
    """偵測壞掉時要安靜 —— 對一台其實裝好字型的機器報警，使用者會去追一個
    不存在的問題。"""
    from app.core import font_health

    def _boom(*a, **kw):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(font_health, "cjk_status", _boom)
    assert app_main._tpl_cjk_font_status()["ok"] is True


# ---------------------------------------------------------------------- 5
def test_every_cjk_writing_tool_includes_the_notice():
    """**自動列舉**：只要工具會自己把中文畫進 PDF，模板就要有這塊提示。

    寫死一份工具清單的話，下一支新工具照樣會漏 —— 而漏掉是無聲的
    （使用者只會看到一排方框，不會看到錯誤訊息）。
    """
    missing = []
    for pkg in _cjk_writing_tool_dirs():
        tpl_dir = pkg / "templates"
        html = list(tpl_dir.glob("*.html")) if tpl_dir.is_dir() else []
        if not any(_INCLUDE in p.read_text(encoding="utf-8") for p in html):
            missing.append(pkg.name)
    assert not missing, (
        "這些工具會把中文寫進 PDF，但模板沒有 include "
        f"{_INCLUDE}：{missing}")


def test_enumeration_actually_finds_tools():
    """列舉條件寫錯（例如都比對不到）時，上一條測試會空跑而永遠通過。"""
    names = {p.name for p in _cjk_writing_tool_dirs()}
    assert {"pdf_pageno", "pdf_watermark", "pdf_fill"} <= names


# ------------------------------------------------------------------- 6, 7
def _get_tool_page(client, monkeypatch, ok: bool):
    from app.core import font_health
    monkeypatch.setattr(
        font_health, "cjk_status",
        lambda cjk="traditional": {
            "ok": ok, "font": "NotoSansCJK-Regular.ttc" if ok else "",
            "install_cmd": "" if ok else "sudo apt install fonts-noto-cjk"})
    r = client.get("/tools/pdf-pageno/")
    assert r.status_code == 200
    return r.text


def test_notice_absent_when_font_available(client, monkeypatch, auth_off):
    html = _get_tool_page(client, monkeypatch, ok=True)
    assert "cjk-warn" not in html
    assert "找不到中文字型" not in html


def test_notice_shown_with_admin_path_when_font_missing(client, monkeypatch,
                                                        auth_off):
    """認證關閉 = 單機模式，使用者本人就是管理員 → 直接給安裝路徑。"""
    html = _get_tool_page(client, monkeypatch, ok=False)
    assert "cjk-warn" in html
    assert "找不到中文字型" in html
    assert "/admin/sys-deps" in html
    assert "sudo apt install fonts-noto-cjk" in html


# ---------------------------------------------------------------------- 8
def test_non_admin_is_told_to_contact_the_admin(admin_session, monkeypatch):
    """一般使用者進不去管理區 —— 給他管理連結只是把人推去撞 403。"""
    from app.core import font_health, permissions, sessions, user_manager
    monkeypatch.setattr(font_health, "cjk_status",
                        lambda cjk="traditional": {
                            "ok": False, "font": "",
                            "install_cmd": "sudo apt install fonts-noto-cjk"})
    uid = user_manager.create_local(
        username="carol", display_name="Carol",
        password="ClerkUser1234", roles=["clerk"],  # clerk 有「插入頁碼」
    )
    permissions.invalidate_cache()
    token, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
    c = TestClient(app_main.app)
    c.cookies.set(sessions.COOKIE_NAME, token)

    r = c.get("/tools/pdf-pageno/")
    assert r.status_code == 200
    assert "cjk-warn" in r.text
    assert "請聯絡管理員" in r.text
    assert "/admin/sys-deps" not in r.text
    assert "/admin/fonts" not in r.text


# ---------------------------------------------------------------------- 9
def test_notice_class_has_styles_in_platform_css():
    css = (_ROOT / "static" / "css" / "platform.css").read_text(encoding="utf-8")
    assert ".cjk-warn {" in css


def test_notice_is_not_injected_by_js():
    """樣式一律走 platform.css —— JS 動態注入的 <style> 會被 CSP 整段擋掉，
    而且不會有任何 JS 例外（看起來就只是「元件壞了」）。"""
    tpl = (_ROOT / "app" / "web" / "templates" / "components"
           / "cjk_font_notice.html").read_text(encoding="utf-8")
    assert "<style" not in tpl
