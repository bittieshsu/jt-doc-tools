"""前端 JS 不可以動態注入 `<style>` —— CSP 會把它整段擋掉。

## 由來（2026-08-17 使用者回報「從工作區載入按了沒用」）

`workspace_picker.js` 用 `document.createElement('style')` 注入選擇視窗的
樣式。本站的 CSP 自 v1.12.30 起 style-src 只收 `'self'` 與 nonce ——
動態注入的 `<style>` 沒有 nonce，被**整段擋掉**。

後果的形狀很陰險：

* 功能面**沒有任何例外** —— DOM 都建好了、事件都綁好了
* 只是樣式全沒套：`position:fixed` 的遮罩變成 `static` 的普通 div，
  視窗內容攤在頁面最底部，沒有遮罩、沒有置中
* 使用者看到的是「按了沒反應」或「跑出很奇怪的東西」
* 「頁面沒有 JS 錯誤」的自動檢查**完全抓不到**（CSP 違規只進 console，
  不丟例外）

修法：樣式一律放 `platform.css`（`'self'` 允許），JS 只建 DOM。
這跟 CLAUDE.md 記的「模板 inline style 要 nonce」是同一條 CSP 規則的
JS 版 —— 模板那邊有 `test_csp_nonce.py` 守著，JS 這邊一直沒有。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS_FILES = sorted((ROOT / "static" / "js").glob("*.js"))

_INJECT = re.compile(
    r"""createElement\(\s*['"]style['"]\s*\)|innerHTML\s*[+]?=\s*['"`][^'"`]*<style""",
    re.I)


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_no_dynamic_style_element(path):
    src = path.read_text(encoding="utf-8", errors="replace")
    # 去掉註解 —— 說明裡引用「原本錯誤的寫法」當反例不算（本專案守門測試
    # 的既有慣例：掃程式碼，不掃說明）
    body = re.sub(r"//[^\n]*", "", src)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    hits = _INJECT.findall(body)
    assert not hits, (
        f"{path.name} 動態注入 <style> —— CSP style-src 沒有 unsafe-inline，"
        "這段樣式會被整段擋掉，元件變成無樣式的 DOM 攤在頁面上。"
        "樣式請放 platform.css，JS 只建 DOM。")


def test_picker_styles_live_in_platform_css():
    """選擇視窗的樣式必須在 platform.css（不是散落或消失）。"""
    css = (ROOT / "static" / "css" / "platform.css").read_text(encoding="utf-8")
    for cls in (".ws-picker-backdrop", ".ws-picker-dialog", ".ws-pick-card"):
        assert cls in css, f"platform.css 缺 {cls} —— 選擇視窗會變成無樣式 div"
    # 關鍵屬性：backdrop 一定要是 fixed 覆蓋層
    m = re.search(r"\.ws-picker-backdrop\{([^}]*)\}", css)
    assert m and "position:fixed" in m.group(1).replace(" ", ""), (
        "backdrop 不是 position:fixed —— 內容會攤在頁面底部")
