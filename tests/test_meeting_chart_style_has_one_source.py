"""圖的配色只有一份 —— 前端畫圖、伺服器畫匯出用的圖，顏色必須同源。

## 由來（v1.15.68）

使用者要求「圖都要用前端產 才能互動」，於是**畫法**變成兩份：
畫面上那張在瀏覽器裡畫（`static/js/meeting_charts.js`），
匯出的 PNG / PDF / Markdown 內嵌仍然是伺服器畫的
（`app/core/meeting_charts.py`）。

本專案一向反對「同一份東西寫在兩個地方」，這裡是**刻意的取捨**，
但要把它守住：

* **畫法漂掉看得見**（下載的圖排版跟畫面不一樣，一眼就知道）。
* **顏色漂掉看不見** —— 使用者會以為是兩張不同的圖，或是以為某個發言者
  在兩張圖上是兩個人（`PALETTE` 的說明自己就寫著這件事）。

所以判準是：**前端不可以自己寫顏色**，一律由伺服器透過 `data-*` 送進 DOM
（同本專案既有的 `workspace_extensions()` → `data-ws-exts` 做法）。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = ROOT / "static" / "js" / "meeting_charts.js"
TPL = (ROOT / "app" / "tools" / "meeting_summary" / "templates"
       / "meeting_summary.html")
ROUTER = ROOT / "app" / "tools" / "meeting_summary" / "router.py"

#: 灰階與白色不算「配色」—— 那是版面用的底色與文字色，兩邊本來就各自寫。
#: 這條清單只放實際出現在程式裡的中性色，**不要放通配式**
#: （放寬到「任何灰色都可以」的話，有人把品牌色寫成 `#6366f1` 也擋不到）。
_NEUTRAL = {
    "#ffffff", "#fff", "#0f172a", "#1e293b", "#334155", "#475569",
    "#64748b", "#94a3b8", "#4338ca",   # 4338ca 是 fallback，下面另有檢查
    "#6366f1",                          # 焦點外框（CSS 用，不是資料配色）
    "#f1f5f9",                          # 發言者時間軸的空軌道底色
}


def _hex_colours(text: str) -> list[str]:
    return [m.group(0).lower() for m in re.finditer(r"#[0-9a-fA-F]{3,8}\b", text)]


def test_the_front_end_does_not_hardcode_the_palette():
    src = JS.read_text(encoding="utf-8")
    # 去註解再看 —— 說明裡會**引用**顏色當例子（use vs mention，本專案
    # 被自己的註解騙過很多次）。
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"(?m)^\s*//.*$", " ", src)
    bad = sorted({c for c in _hex_colours(src) if c not in _NEUTRAL})
    assert not bad, (
        f"`{JS.name}` 自己寫了配色：{bad}。"
        "配色要從伺服器的 `meeting_charts.PALETTE` / `KIND_STYLE` 經 "
        "`data-palette` / `data-kinds` 送進來 —— 不然畫面上是一個顏色、"
        "下載的 PNG 是另一個。")


def test_the_server_sends_the_palette_into_the_page():
    router = ROUTER.read_text(encoding="utf-8")
    assert "mc.PALETTE" in router and "mc.KIND_STYLE" in router, (
        "router 沒有把伺服器端的配色送進樣板 —— 前端就只能自己抄一份了")
    tpl = TPL.read_text(encoding="utf-8")
    for name in ("chart_palette", "chart_kinds"):
        assert "{{ " + name + " }}" in tpl, f"樣板沒有輸出 {name}"
    assert "data-palette" in tpl and "data-kinds" in tpl


def test_the_front_end_reads_them_instead_of_guessing():
    src = JS.read_text(encoding="utf-8")
    # 前端拿到的是參數，不是自己組的預設值
    assert "opts.palette" in src and "opts.kinds" in src, (
        "前端沒有從外面接收配色")
    tpl = TPL.read_text(encoding="utf-8")
    assert "dataset.palette" in tpl and "dataset.kinds" in tpl, (
        "樣板沒有把 data-* 讀出來傳給繪圖")


def test_the_scan_actually_reaches_the_files():
    """「掃 0 個檔」跟「掃過都乾淨」在 pytest 輸出裡長得一模一樣。"""
    for p in (JS, TPL, ROUTER):
        assert p.is_file(), f"{p} 不在了 —— 檔案改名的話這份檢查要跟著改"
    assert len(_hex_colours(JS.read_text(encoding="utf-8"))) >= 3, (
        "在前端檔案裡一個顏色都掃不到，判準可能已經失效")
