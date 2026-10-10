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

## 每次啟動都要給自己的設定檔目錄（`profile_arg()`）

沒給 `--user-data-dir` 時，無頭 Chromium 自己建一個暫存設定檔（snap 版放在
`~/snap/chromium/common/chromium-headless/scoped_dir*`），**只有正常結束才會刪**。
我們的測試一律用 `terminate()` / `kill()` 收尾 —— 實測兩種都留下來，一個約 11 MB，
全套測試跑幾輪就是幾千個、幾十 GB（2026-10-10 同一台機器上的另一個專案查到 55 GB）。

所以每次啟動都用 `profile_arg()` 給一個**我們自己的**目錄：建在 `jtdt-profiles/` 底下、
名稱開頭是建立它的行程編號，行程結束時（`atexit`）刪掉自己建的；建立新目錄時順便刪掉
**建立者已經不在**的舊目錄（上次被中斷沒收到尾的）。snap 自己的 `chromium-headless`
一律不碰 —— 那裡可能有別人正在用的設定檔。
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import time

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
                                profile_arg(), "--dump-dom", "about:blank"],
                               capture_output=True, text=True, timeout=timeout)
            _RUNS[b] = r.returncode == 0 and "<html" in (r.stdout or "").lower()
        except (OSError, subprocess.SubprocessError):
            _RUNS[b] = False
    return _RUNS[b]


#: 這個行程建過的設定檔目錄（結束時刪掉）。
_MINE: list[str] = []
_ATEXIT = [False]


def profile_root() -> str:
    """放設定檔目錄的地方：snap 版要放它讀得到的位置。"""
    if is_snap():
        return os.path.expanduser("~/snap/chromium/common/jtdt-profiles")
    return os.path.join(tempfile.gettempdir(), "jtdt-profiles")


def _pid_alive(pid: int) -> bool:
    # 不用 `os.kill(pid, 0)`：Windows 上那等於結束那個行程。
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001 — 判斷不了就當它還在（寧可不刪）
        return True


def sweep_stale(root: str | None = None) -> int:
    """刪掉建立者已經不在的設定檔目錄（上一次被中斷、沒收到尾的）。"""
    root = root or profile_root()
    n = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        head = name.split("-", 1)[0]
        if not head.isdigit():
            continue                        # 不是照我們的規則建的，不動
        pid = int(head)
        if pid == os.getpid() or _pid_alive(pid):
            continue
        shutil.rmtree(os.path.join(root, name), ignore_errors=True)
        n += 1
    return n


def cleanup_profiles() -> None:
    """刪掉這個行程建過的設定檔目錄（瀏覽器要先關掉）。

    剛被 terminate、還沒完全結束的瀏覽器會在刪掉之後再寫幾個檔，把目錄長回來 ——
    刪完還在就等一下再刪，最多三次。"""
    while _MINE:
        d = _MINE.pop()
        for _ in range(3):
            shutil.rmtree(d, ignore_errors=True)
            if not os.path.exists(d):
                break
            time.sleep(0.5)


def profile_dir() -> str:
    """建一個這次啟動專用的設定檔目錄。"""
    root = profile_root()
    os.makedirs(root, exist_ok=True)
    if not _ATEXIT[0]:
        _ATEXIT[0] = True
        sweep_stale(root)
        atexit.register(cleanup_profiles)
    d = tempfile.mkdtemp(prefix=f"{os.getpid()}-", dir=root)
    _MINE.append(d)
    return d


def profile_arg() -> str:
    """`--user-data-dir=<這次啟動專用的目錄>`，放進啟動瀏覽器的參數裡。"""
    return f"--user-data-dir={profile_dir()}"
