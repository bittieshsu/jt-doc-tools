"""同一份結果只放一顆下載鈕（v1.16.14）。

共用的進度列（`components/job_progress.html`）作業完成時會自己出現「下載 .xlsx」
「存至工作區」「處理新檔案」那一排。有些工具又在結果區自己放了一顆下載鈕，
而兩顆拿到的是**同一個檔、同一個檔名** —— 文件翻譯與 PDF 壓縮就是這樣，
使用者看到兩顆會以為是兩種東西（2026-09-23 使用者截圖問「是不是完全一樣的功能」）。

已經自己處理好的工具有兩種做法，這條檢查兩種都認：
* JS 裡 `new JobProgress(..., { showDownload: false })`（會議摘要、轉逐字稿）
* CSS 把該頁那個進度列的 `.job-download` 或整排 `.job-actions` 藏起來
  （PDF 轉文書檔、PDF 轉簡報檔、頁面加框）

**判準只看樣板裡寫死的按鈕**（`<a …>` 裡有下載圖示）。文件翻譯在預覽停下來的地方
用 JS 另建一顆「下載翻譯後的檔案（共 N 頁）」—— 那是 v1.15.23 刻意加的
（客戶把「預覽只有前 6 頁」讀成「只翻到第 6 頁」），只在頁數超過預覽時出現，不在這條的範圍。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = sorted((ROOT / "app" / "tools").glob("*/templates/*.html"))

# 寫死在樣板裡、帶下載圖示的連結按鈕
_OWN_DOWNLOAD = re.compile(r"<a\b[^>]*>\s*\{\{\s*icon\(\s*['\"]download['\"]")


def _strip_comments(html: str) -> str:
    return re.sub(r"\{#.*?#\}|<!--.*?-->", "", html, flags=re.S)


def _markup(html: str) -> str:
    """去掉 `<script>` 與 `<style>` 的內容 —— 只看畫在頁面上的標記。"""
    return re.sub(r"<(script|style)\b[^>]*>.*?</\1\b[^>]*>", "", html, flags=re.S | re.I)


def _shared_download_hidden(html: str) -> bool:
    if re.search(r"showDownload\s*:\s*false", html):
        return True
    for rule in re.finditer(r"([^{}]+)\{([^}]*)\}", html):
        sel, body = rule.group(1), rule.group(2)
        if re.search(r"\.job-(download|actions)\b", sel) and re.search(r"display\s*:\s*none", body):
            return True
    return False


def _uses_shared_progress(html: str) -> bool:
    return "new JobProgress" in html and "components/job_progress.html" in html


def _offenders() -> list[str]:
    bad = []
    for p in TEMPLATES:
        html = _strip_comments(p.read_text(encoding="utf-8"))
        if not _uses_shared_progress(html) or _shared_download_hidden(html):
            continue
        if _OWN_DOWNLOAD.search(_markup(html)):
            bad.append(p.relative_to(ROOT).as_posix())
    return bad


def test_no_tool_shows_two_download_buttons_for_one_result():
    bad = _offenders()
    assert not bad, (
        "這些工具的進度列會出現下載鈕，結果區又自己放了一顆 —— 兩顆拿到的是同一個檔。"
        "拿掉其中一顆，或把進度列那顆藏起來（showDownload: false）：" + ", ".join(bad))


def test_the_scan_reaches_the_tools_that_use_the_shared_progress_bar():
    """掃 0 支跟「都合格」在輸出裡長得一樣 —— 先證明真的掃到用共用進度列的工具。"""
    users = [p for p in TEMPLATES
             if _uses_shared_progress(_strip_comments(p.read_text(encoding="utf-8")))]
    assert len(users) >= 15, f"只掃到 {len(users)} 支用共用進度列的工具 —— 判準大概錯了"


@pytest.mark.parametrize("html,hidden", [
    ("new JobProgress($('x'), { showDownload: false })", True),
    ("#p2oJob .job-download, #p2oJob .job-reset { display: none !important; }", True),
    ("#bdJob .job-actions { display: none !important; }", True),
    ("#x .job-download { color: red; }", False),
    ("new JobProgress($('x'), {})", False),
])
def test_hidden_detection(html, hidden):
    """反向對照：只改顏色不算藏起來，否則任何一條碰到 .job-download 的樣式都會讓工具被放過。"""
    assert _shared_download_hidden(html) is hidden


def test_a_button_created_by_script_is_not_counted():
    """預覽停下來那顆是 JS 建的（`document.createElement('a')`），不可以被當成樣板裡的第二顆。"""
    html = ('<script>const a = document.createElement("a"); a.innerHTML = '
            '"{{ icon(\'download\') }}";</script>')
    assert not _OWN_DOWNLOAD.search(_markup(html))
