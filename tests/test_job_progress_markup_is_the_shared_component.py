"""載入 `job_progress.js` 的樣板**必須**放共用元件，不可以只放一個空的 `<div>`。

`JobProgress` 是靠 root 裡面的 `.job-reset` / `.job-bar-inner` / `.job-status`
接線的。只放 `<div id="xJob"></div>` 的話：

    this.resetBtn = root.querySelector('.job-reset');   // → null
    this.resetBtn.addEventListener(...)                 // → TypeError

**整段行內腳本停在那裡**，後面所有 `addEventListener` 都不會執行 ——
症狀是「按了完全沒反應」，而且**沒有任何畫面上的錯誤**。

踩過兩次：
* v1.15.36「文件擺正」——上傳區與作業區各一次。
* **v1.15.98「會議錄音轉逐字稿」** —— 使用者在正式機按「開始轉逐字稿」沒反應；
  伺服器記錄裡 `POST /upload` 有、`POST /start` **一次都沒有**。

每頁在真瀏覽器開一次那一關看不到這一個，因為那支工具沒設定好時
只渲染「請先去設定」的空殼（已另外補種子）。所以這裡再加一道**靜態**的。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 元件自己一定有的標記（改名的話這支要先紅，不要變成空迴圈）
_MARKERS = ("job-bar-inner", "job-status", "job-reset")


def _templates_using_job_progress() -> list[pathlib.Path]:
    out = []
    for p in ROOT.rglob("*.html"):
        if "/github/" in p.as_posix() or "/node_modules/" in p.as_posix():
            continue
        src = p.read_text(encoding="utf-8", errors="ignore")
        if "job_progress.js" in src or "new JobProgress(" in src:
            out.append(p)
    return sorted(out)


def test_the_component_still_has_the_hooks_we_check_for():
    """**先證明判準對得上元件本身** —— 元件改名之後這支要先紅。"""
    src = (ROOT / "app" / "web" / "templates" / "components" / "job_progress.html"
           ).read_text(encoding="utf-8")
    missing = [m for m in _MARKERS if m not in src]
    assert not missing, f"共用元件裡找不到 {missing} —— 判準要跟著改"


def test_the_scan_actually_finds_those_templates():
    """**先證明掃得到東西**（掃 0 個檔跟掃過都乾淨長得一樣）。"""
    found = _templates_using_job_progress()
    assert len(found) >= 10, f"只找到 {len(found)} 份用 JobProgress 的樣板，判準壞了"


@pytest.mark.parametrize("path", _templates_using_job_progress(), ids=lambda p: p.name)
def test_every_job_progress_root_is_the_shared_component(path: pathlib.Path):
    src = path.read_text(encoding="utf-8")
    # 註解裡會引用那個錯誤寫法當反例（use vs mention）
    body = re.sub(r"\{#.*?#\}", "", src, flags=re.S)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    if "new JobProgress(" not in body:
        pytest.skip("只載入了腳本、沒有建立元件")

    has_include = "components/job_progress.html" in body
    has_markup = all(m in body for m in _MARKERS)
    assert has_include or has_markup, (
        f"{path.relative_to(ROOT)} 建了 `JobProgress` 卻沒有共用元件的標記。"
        "只放一個空的 <div> 的話，`null.addEventListener` 會丟 TypeError，"
        "整段行內腳本停住 —— 後面的按鈕全部接不上，而畫面上沒有任何錯誤。"
        "請用 {% with progress_id='…' %}{% include \"components/job_progress.html\" %}{% endwith %}"
    )
