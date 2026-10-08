"""無頭瀏覽器測試的共用設定：找瀏覽器、挑一個**它讀得到**的素材目錄。

## 為什麼要共用

這段邏輯原本在五支 e2e 測試裡各寫一份。第五份（`doc-diff` 的頁面模式）
只比對路徑字串、沒有讀檔案內容，於是**判斷不出 snap** ——
素材被放到 `/tmp`，`DOM.setFileInputFiles` 照樣「成功」、畫面上的檔名也
顯示得出來，只有真的送出時才失敗，而測試看到的是「結果沒出來」然後
**skip 掉**。整支等於沒跑，而 pytest 輸出裡看起來一切正常。

## snap 的陷阱（CLAUDE.md 慣例第⑲條）

Ubuntu 的 `/usr/bin/chromium-browser` **是一支 shell 包裝腳本**（不是符號連結），
真正跑的是 snap 版。snap 版**讀不到 `/opt`，而且它看到的 `/tmp` 是它自己的**
—— 所以要判斷 snap 只能**讀那支檔案的內容**，看路徑看不出來。
"""
from __future__ import annotations

import os
import shutil
import tempfile

#: 依序試這幾個位置。
BROWSERS = ("/usr/bin/chromium-browser", "/usr/bin/chromium",
            "/snap/bin/chromium", "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable")


def browser() -> str | None:
    """回瀏覽器的執行檔路徑，找不到就 `None`。"""
    for b in BROWSERS:
        if os.path.exists(b):
            return b
    return shutil.which("chromium") or shutil.which("google-chrome")


def is_snap(path: str | None = None) -> bool:
    """那支瀏覽器是不是 snap 版。

    **一定要讀檔案內容** —— `/usr/bin/chromium-browser` 是一支包裝腳本，
    路徑上看不出任何 snap 的痕跡。
    """
    b = path or browser() or ""
    if not b:
        return False
    if "snap" in b:
        return True
    try:
        return "snap" in open(b, "rb").read(400).decode("utf-8", "ignore")
    except OSError:
        return False


def uploadable_dir(prefix: str = "jtdt-test") -> str:
    """挑一個**瀏覽器讀得到**的目錄放測試素材。

    放錯地方的症狀很誤導：畫面上檔名顯示得出來，只有真的送出時才
    `net::ERR_FILE_NOT_FOUND`，看起來像我們的上傳程式壞掉。
    """
    if is_snap():
        d = os.path.expanduser(f"~/snap/chromium/common/{prefix}")
    else:
        d = tempfile.mkdtemp(prefix=f"{prefix}-")
    os.makedirs(d, exist_ok=True)
    return d


_RUNS: dict[str, bool] = {}


def browser_runs(path: str | None = None, timeout: float = 45.0) -> bool:
    """那支瀏覽器**在這台真的跑得起來**嗎（開一頁空白、印得出 DOM）。

    找得到執行檔不代表跑得起來：GitHub 的 Ubuntu 機器上 `/usr/bin/chromium-browser`
    是 snap 的空殼，snap 沒有在跑 —— 一叫就卡住（2026-10-08 CI 上等滿 120 秒變紅，
    其他用瀏覽器的測試在那種情況是 skip）。結果依路徑快取。
    """
    import subprocess
    b = path or browser()
    if not b:
        return False
    if b not in _RUNS:
        try:
            r = subprocess.run([b, "--headless", "--disable-gpu", "--no-sandbox",
                                "--dump-dom", "about:blank"],
                               capture_output=True, text=True, timeout=timeout)
            _RUNS[b] = r.returncode == 0 and "<html" in (r.stdout or "").lower()
        except (OSError, subprocess.SubprocessError):
            _RUNS[b] = False
    return _RUNS[b]
