"""模板用到的前端元件，那一頁必須自己載進來。

## 由來（v1.15.36，使用者回報）

「掃描修正」點選檔案之後**什麼都沒發生**，拖曳也沒反應。根因是那支模板
`{% block scripts %}` 裡**一行 `<script src>` 都沒有** —— 而
`file_upload.js` / `job_progress.js` **不是全站載入的**（`base.html` 只無條件
載七支共用的），每支工具自己載。

於是 `new FileUpload(...)` 在行內腳本的第一行就丟 `ReferenceError`，
**整段腳本停在那裡**：上傳沒接線、選項面板不會出現、作業進度也不會動。

**為什麼畫面上看起來是好的**：上傳區是 `<label for=...>` 包 `<input type=file>`，
**純 HTML 就點得開檔案選擇器** —— 使用者以為功能在，選完檔案卻石沉大海，
而且畫面上沒有任何錯誤訊息（例外只進主控台）。

既有的 `test_template_js_syntax.py` 只跑 `node --check`（語法），
**這種漏載語法完全合法**，所以它看不到。

## 判準

只釘**硬相依**：`new X(...)` 這種少了就直接丟例外的用法。
`window.attachWorkspaceSave && window.attachWorkspaceSave(...)` 這種
**做過特徵偵測**的不算（`workspace_picker.js` 由 `base.html` 依工作區是否
啟用決定載不載，那是刻意的）——第一版沒分這兩種，一口氣誤報 17 支。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "app"
BASE = APP / "web" / "templates" / "base.html"

#: 全域名稱 → 定義它的檔案。少一個就是少一條防護，加新的共用元件時要補。
_GLOBALS: dict[str, str] = {
    "FileUpload": "file_upload.js",
    "JobProgress": "job_progress.js",
    "JtSelect": "custom_select.js",
    "DragPositionEditor": "drag_position_editor.js",
    "StampPlacementEditor": "stamp_placement_editor.js",
    "StampDateOverlay": "stamp_date_overlay.js",
}

_SRC = re.compile(r'<script src="/static/js/([a-z_]+\.js)"')


def _always_loaded() -> set[str]:
    """`base.html` 裡**不在 `{% if %}` 條件內**的那幾支。"""
    out = set()
    for line in BASE.read_text(encoding="utf-8").splitlines():
        m = _SRC.search(line)
        if m and "{% if" not in line:
            out.add(m.group(1))
    return out


def _pages() -> list[pathlib.Path]:
    # `components/` 是片段，載入由包含它的頁面負責，單獨判斷會誤報
    return sorted(p for p in APP.rglob("*.html") if "components" not in p.parts)


ALWAYS = _always_loaded()
PAGES = _pages()


def test_the_scan_reaches_real_pages_and_knows_the_baseline():
    """**檢查自己要有牙齒**：掃 0 個檔跟掃過都乾淨，在 pytest 輸出裡一模一樣。"""
    assert len(PAGES) > 50, f"只掃到 {len(PAGES)} 個模板，比對基準本身就不對"
    assert "toast.js" in ALWAYS, "base.html 的無條件載入清單解析錯了"
    assert "file_upload.js" not in ALWAYS, (
        "file_upload.js 變成全站載入了？那這份檢查的前提要重寫")


@pytest.mark.parametrize(
    "page", PAGES, ids=lambda p: p.relative_to(APP).as_posix())
def test_a_page_loads_what_its_inline_script_constructs(page: pathlib.Path):
    text = page.read_text(encoding="utf-8")
    srcs = set(_SRC.findall(text))
    missing = []
    for name, js in _GLOBALS.items():
        if js in ALWAYS or js in srcs:
            continue
        if re.search(rf"\bnew\s+{name}\s*\(", text):
            missing.append(f"{name} → /static/js/{js}")
    assert not missing, (
        f"{page.relative_to(APP).as_posix()} 的行內腳本 new 了這些元件，"
        f"但那一頁沒有載進來：{missing}\n"
        "少了就是行內腳本第一行丟 ReferenceError、**整段不執行** —— "
        "而畫面上看起來完全正常（上傳區是 label+input，點得開檔案選擇器），"
        "只有選完檔案之後什麼都不會發生。")
