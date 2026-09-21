"""Convert Office documents (.docx/.doc/.xlsx/.xls/.odt/.ods/.pptx…) to PDF.

Delegates to a headless LibreOffice (or its drop-in fork OxOffice, which
ships on many Mac setups). We search a few common install paths and the
``PATH``; if none is found, :func:`convert_to_pdf` raises so the caller can
surface a clear error.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional

from .zip_guard import ZipBombError

logger = logging.getLogger(__name__)


OFFICE_EXTENSIONS = {
    ".doc", ".docx", ".odt", ".rtf",
    ".xls", ".xlsx", ".ods",
    ".ppt", ".pptx", ".odp",
    ".txt", ".csv",
}


def is_office_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in OFFICE_EXTENSIONS


def find_soffice() -> Optional[str]:
    """Locate a headless office binary; returns an executable path or None.

    Order: user-customisable paths from :mod:`conv_settings` (custom first,
    then built-ins in the user's saved order, including Windows defaults),
    then a final ``PATH`` fallback via ``shutil.which``.
    """
    from .conv_settings import conv_settings
    for p in conv_settings.get_executable_paths():
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return (
        shutil.which("soffice")
        or shutil.which("libreoffice")
        or shutil.which("soffice.exe")
        or shutil.which("libreoffice.exe")
    )


def detect_engine() -> str:
    """Return a human-readable engine label: 'OxOffice', 'LibreOffice',
    or '(未安裝)'. Decides by path — anything containing 'oxoffice' (any
    case) is OxOffice, otherwise LibreOffice. Cheap path-string check
    (no subprocess) — safe to call from request handlers."""
    p = find_soffice()
    if not p:
        return "(未安裝)"
    return "OxOffice" if "oxoffice" in p.lower() else "LibreOffice"


# 限制同時執行的 office 轉檔數量。
#
# 歷史上這裡是一把 `threading.Lock()`（等於同時只准一個）。真正的理由**不是**
# profile 衝突 —— 每次呼叫早就用獨立的臨時 profile 目錄（`-env:UserInstallation`）
# —— 而是 **macOS 上兩個 osascript→soffice 會在 Aqua / WindowServer 啟動時競爭**。
# Linux / Windows 沒有這個問題，卻一起被鎖成單工：正式機是 Linux，兩個人同時轉
# 檔，第二個就得乾等前一個跑完（大檔可能十幾分鐘）。
#
# 改成可調整上限的號誌：macOS 由 concurrency_settings 強制夾成 1，其餘平台由管理
# 員設定，且**上限依實際可用記憶體推算**（一個 soffice 可吃數百 MB，開太多直接
# OOM）。
class _ResizableSemaphore:
    """可在執行期改變上限的號誌。

    直接換掉 `threading.Semaphore` 物件會有問題：正在跑的工作握著舊物件，釋放時
    放回的是舊號誌，新號誌的計數就永遠對不上。所以自己用 Condition + 計數實作，
    改上限只是改一個數字，正在執行的不受影響（縮小時不會中斷手上的工作，只是
    暫時超出上限，等它們跑完自然收斂）。
    """

    def __init__(self, limit: int = 1) -> None:
        self._cond = threading.Condition()
        self._limit = max(1, int(limit))
        self._in_use = 0

    def set_limit(self, limit: int) -> int:
        with self._cond:
            self._limit = max(1, int(limit))
            self._cond.notify_all()
            return self._limit

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def in_use(self) -> int:
        return self._in_use

    def __enter__(self):
        with self._cond:
            while self._in_use >= self._limit:
                self._cond.wait()
            self._in_use += 1
        return self

    def __exit__(self, *exc) -> None:
        with self._cond:
            self._in_use -= 1
            self._cond.notify()
        return None


_soffice_lock = _ResizableSemaphore(1)


def _track(proc):
    """把剛啟動的 soffice 掛到目前的作業底下（供管理區顯示資源用量）。

    真正吃記憶體的是 soffice 子行程，不是我們的執行緒；不登記的話管理區只能顯示
    一個跟實際無關的數字。job_manager 不在時（例如單獨跑轉檔的測試）就是 no-op。
    """
    try:
        from .job_manager import job_manager
        job_manager.register_subprocess(proc.pid)
    except Exception:  # noqa: BLE001 — 統計失敗絕不影響轉檔
        pass
    # 限制它可以用幾顆核心（預設留一顆給網頁）。要在這裡做而不是 preexec_fn ——
    # preexec_fn 在 fork 之後、exec 之前跑，那時還讀不到最新設定；而且管理員改
    # 設定後應該立刻對「下一個」轉檔生效，這裡每次啟動都重新算就自然做到了。
    try:
        from . import cpu_limit
        cpu_limit.apply_to_pid(proc.pid)
    except Exception:  # noqa: BLE001 — 限不到 CPU 只是慢，不能讓轉檔失敗
        pass
    return proc


def _untrack(proc):
    try:
        from .job_manager import job_manager
        job_manager.unregister_subprocess(proc.pid)
    except Exception:  # noqa: BLE001
        pass


def set_office_concurrency(n: int) -> int:
    """設定同時可執行的 office 轉檔數（由 concurrency_settings 呼叫）。"""
    return _soffice_lock.set_limit(n)


def office_concurrency() -> dict:
    return {"limit": _soffice_lock.limit, "in_use": _soffice_lock.in_use}


def _lower_priority():
    """把 soffice 子行程降到背景優先權（給 Popen 的 preexec_fn 用）。

    這是「降權」；另一半是「限制可用核心數」，見 `cpu_limit.apply_to_pid`
    （在 `_track` 內對已啟動的 pid 套用）。兩者搭配才擋得住多個 soffice
    同時把所有核心吃滿。

    **這是「轉檔不可影響網頁操作」的第一道措施。** soffice 轉大檔會吃滿一顆核心，
    在核心數不多、或機器本身已有其他負載的情況下，網頁請求就會排在它後面 ——
    2026-07-30 正式機實測過：閒置時輪詢每 2 秒一次，轉檔中最長 226 秒才回應一次
    （那台機器什麼都沒跑時 load average 就已經 7/6 核）。

    降優先權不會讓轉檔變慢多少（CPU 有空時它照樣全速跑），但 OS 排程器會讓
    互動式的網頁請求優先 —— 這正是我們要的取捨：轉檔慢幾秒沒人在意，網頁卡住
    十秒沒人能忍。
    """
    try:
        os.nice(10)
    except Exception:  # noqa: BLE001 — 沒權限調整就照常跑
        pass


def _kill_tree(proc) -> None:
    """把 soffice **整棵行程樹**殺掉，不是只殺我們拿到的那個 PID。

    `proc.kill()` 殺的是 `/opt/oxoffice/program/soffice`（shell 包裝腳本），
    而真正在解析檔案的 `soffice.bin` 是它的子行程 —— **會活下來變成孤兒**。
    這段程式原本的註解寫著「force-kill so it doesn't leave a zombie soffice」，
    **但它沒有做到**（正式機 2026-09-18 實測：逾時後 4 分鐘那支還在跑）。

    收尾要保證不丟例外：**這是錯誤處理路徑**，在這裡再炸一次的話，
    使用者看到的會是堆疊而不是「轉檔逾時」。
    """
    from . import proc_tree
    proc_tree.kill_tree(proc.pid)
    try:
        proc.kill()
    except Exception:                                    # noqa: BLE001
        pass
    try:
        proc.communicate(timeout=5)
    except Exception:                                    # noqa: BLE001
        pass


def _build_soffice_cmd(soffice: str, args: list[str]) -> tuple[list, dict]:
    """Build subprocess.Popen kwargs for cross-platform soffice invocation.

    Returns (argv, popen_kwargs). popen_kwargs may include `creationflags`
    (Windows) or wrap the cmd with osascript (macOS).
    """
    import sys as _sys
    import os as _os
    import shlex as _shlex
    kwargs: dict = {}
    if not _sys.platform.startswith("win"):
        # **自己一個 session** —— `soffice` 是 shell 包裝腳本，真正在算的是它
        # fork 出來的 `soffice.bin`。逾時時只 kill 我們拿到的那個 PID，
        # **子行程會活下來變成孤兒**（PPID=1）而且繼續空轉。
        # 正式機實測到過：逾時 4 分鐘之後那支 soffice.bin 還在 R 狀態燒 CPU，
        # 讓下一次轉檔更慢 —— **一次逾時會把後面每一次都拖下水**。
        kwargs["start_new_session"] = True
    if _sys.platform == "darwin":
        # macOS: 直接 fork+exec soffice 會 SIGABRT (拿不到 WindowServer)，
        # `open -W -a` 又會被當 GUI app 啟動而忽略 --headless。改用 osascript
        # 的 `do shell script` — 它在 user 的 Aqua context 跑，spawn 出來的
        # shell 子行程能繼承 GUI session 連線。
        quoted = " ".join(_shlex.quote(x) for x in [soffice] + args)
        escaped = quoted.replace("\\", "\\\\").replace('"', '\\"')
        return ["osascript", "-e", f'do shell script "{escaped}"'], kwargs
    if _sys.platform.startswith("win"):
        # Windows: 在 Service (Session 0) 跑時，soffice 預設會嘗試 attach console
        # → 卡住。CREATE_NO_WINDOW 強制 detached console。
        # 另外一些 LocalSystem service env 缺 TEMP/TMP → 給乾淨的 env 帶 .venv
        # 的 PATH 與我們可寫的 TEMP，避免 soffice 跑去寫系統路徑被擋。
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            # 背景優先權（同 _lower_priority 的用意，Windows 沒有 nice）
            | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
        )
        # 繼承 env 但確保 TEMP 是 writable（service-isolated session 的
        # %TEMP% 預設是 C:\Windows\Temp，理論上 writable，但保險起見明確設）
        env = dict(_os.environ)
        env.setdefault("TEMP", env.get("TMP") or _os.environ.get("TEMP", ""))
        env.setdefault("TMP", env["TEMP"])
        kwargs["env"] = env
    if not _sys.platform.startswith("win"):
        # macOS 走 osascript 包一層，nice 對它同樣有效（會傳遞給子 shell）
        kwargs["preexec_fn"] = _lower_priority
    return [soffice] + args, kwargs


def _profile_uri(profile_path: Path, *, paper_default: bool = False) -> str:
    """Build a valid `file://` URI for the soffice -env:UserInstallation arg.

    Bug fix (issue #5, v1.5.1): on Windows we used to build
    `file://C:\\Users\\...\\profile` by string concat. That's a malformed URI
    (Windows file URIs need three slashes + forward slashes:
    `file:///C:/Users/.../profile`). soffice silently fell back to the
    LocalSystem default profile → first-time setup hung in Session 0
    → all conversions timeout at 60s.

    Path.as_uri() does the right thing on all platforms.
    """
    _harden_profile(profile_path, paper_default=paper_default)
    return profile_path.resolve().as_uri()


#: 丟進拋棄式設定檔的設定。**停用巨集執行** ＋ **預設紙張 A4**。
#:
#: ⚠⚠ **這份設定原本從來沒有生效過**（v1.15.87 實測）：每一支轉檔函式都帶著
#: `--safe-mode`，而那個旗標會在啟動時**把使用者設定檔重設掉** ——
#: 我們剛剛種下去的 `registrymodifications.xcu` 跑完之後
#: **`DisableMacrosExecution` 已經不在裡面了**。
#: 舊註解寫「`--safe-mode` 跟巨集無關」，那句話本身沒錯，錯在它**跟我們種的
#: 設定有關**：safe-mode 會把整份洗掉。
#:
#: 判準是**行為**不是檔案在不在**：種 `ooSetupSystemLocale=zh-TW` 之後，
#: 帶 safe-mode 出來仍是 Letter、不帶才變 A4 —— 那就是「有沒有被讀進去」的證據。
#:
#: 拿掉 `--safe-mode` 之後用三份真實檔案（docx / xlsx）逐項比對過
#: **頁數、頁面尺寸、字數、逐頁算圖的像素雜湊完全一致**。
#: 它原本要防的兩件事本來就已經有別的東西擋著：使用者自訂設定 ——
#: 每次呼叫都是**全新的拋棄式 profile**，根本沒有自訂；當機復原提示 ——
#: `--norestore`。
#:
#: * `DisableMacrosExecution` = true —— 最強的一道，直接關掉巨集執行。
#: * `MacroSecurityLevel` = 3（最高）—— 萬一上面那項在某個版本被忽略時的後備。
#: * `ooSetupSystemLocale` = zh-TW —— **決定「沒有宣告頁面尺寸的來源」用什麼紙**。
#:   HTML / 純文字 / CSV 都不帶頁面尺寸，LibreOffice 就用系統語系推出來的紙張，
#:   而這台（以及多數伺服器）是 `en_US` → **Letter**。台灣印的是 A4，
#:   短 18mm、寬 6mm，照 A4 邊界排的內容會跑掉。
#:   **`LANG` / `LC_PAPER` 環境變數沒有用**（實測：`LANG=zh_TW.UTF-8` 只換掉
#:   長度單位、紙張仍是 Letter；而且多數機器根本沒有產生 zh_TW 這個 locale）。
#:   **來源自己宣告了尺寸就一律照它的**（實測 Letter 的 .odt 仍出 Letter），
#:   所以這一條只影響「本來就沒有答案」的那些輸入。
_HARDENED_PROFILE_XCU = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry"
           xmlns:xs="http://www.w3.org/2001/XMLSchema">
 <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
  <prop oor:name="DisableMacrosExecution" oor:op="fuse">
   <value>true</value>
  </prop>
 </item>
 <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
  <prop oor:name="MacroSecurityLevel" oor:op="fuse">
   <value>3</value>
  </prop>
 </item>
</oor:items>
"""

#: 只在**來源本來就沒有宣告頁面尺寸**時才追加的那一項（見 `_NO_PAGE_SIZE_SUFFIXES`）。
#:
#: **不要無條件套**：它同時是「系統語系」，會改變 CJK 的字型 fallback。
#: 實測一份真實廠商表 docx，套上去之後前導空白改用別的字型量寬度，
#: 整段標籤**左移約 10pt**（墨點數完全相同、字形也一樣，純粹是空白的寬度）。
#: 那個位移對表單那一家工具是不可接受的風險，而它們的來源本來就寫著頁面尺寸、
#: 根本不需要這一項。
_PAPER_XCU_ITEM = """ <item oor:path="/org.openoffice.Setup/L10N">
  <prop oor:name="ooSetupSystemLocale" oor:op="fuse">
   <value>zh-TW</value>
  </prop>
 </item>
"""

#: 這些來源**不帶頁面尺寸**，LibreOffice 只能用系統語系推一個出來
#: （伺服器多半是 `en_US` → Letter）。台灣印的是 A4。
_NO_PAGE_SIZE_SUFFIXES = frozenset({".html", ".htm", ".txt", ".csv", ".md"})


def _needs_paper_default(src: Path) -> bool:
    """來源自己沒宣告頁面尺寸時，才需要我們替它決定紙張。

    `.docx` / `.odt` / `.xlsx` / `.pptx` 自己就帶著尺寸，**一律照它的**
    （實測宣告 Letter 的 .odt 轉出來仍是 Letter）——
    我們不可以把別人文件的紙張改掉。
    """
    return src.suffix.lower() in _NO_PAGE_SIZE_SUFFIXES


def _harden_profile(profile_path: Path, *, paper_default: bool = False) -> None:
    """在拋棄式設定檔裡預先寫入安全設定（停用巨集）。

    **寫不進去也不可以讓轉檔失敗** —— 那會把一個「加強防護」變成
    「整批轉檔壞掉」。寫不成時記一筆 warning 就好：巨集安全性仍有
    LibreOffice 自己的預設值當底。
    """
    try:
        user_dir = profile_path / "user"
        user_dir.mkdir(parents=True, exist_ok=True)
        target = user_dir / "registrymodifications.xcu"
        if not target.exists():
            xcu = _HARDENED_PROFILE_XCU
            if paper_default:
                xcu = xcu.replace("</oor:items>", _PAPER_XCU_ITEM + "</oor:items>")
            target.write_text(xcu, encoding="utf-8")
    except OSError as e:
        logger.warning("無法寫入 soffice 安全設定（巨集停用）：%s", e)


async def convert_to_pdf_async(*args, **kwargs) -> None:
    """`convert_to_pdf` 的非同步版本 —— **端點一律用這支**。

    soffice 轉檔動輒數秒到數分鐘，而且中間還有一道號誌在排隊。在 async 端點裡
    直接呼叫會把事件迴圈卡住，全站跟著不回應。
    """
    import asyncio
    return await asyncio.to_thread(convert_to_pdf, *args, **kwargs)


async def convert_to_docx_async(*args, **kwargs):
    """`convert_to_docx` 的非同步版本 —— 端點一律用這支。"""
    import asyncio
    return await asyncio.to_thread(convert_to_docx, *args, **kwargs)


async def convert_to_odt_async(*args, **kwargs):
    """`convert_to_odt` 的非同步版本 —— 端點一律用這支。"""
    import asyncio
    return await asyncio.to_thread(convert_to_odt, *args, **kwargs)


class OfficeUnavailableError(RuntimeError):
    """**這台機器沒有裝 Office 引擎** —— 跟使用者送什麼檔案無關。

    這是**部署層面**的問題，不是使用者的錯，所以要回 **503**（服務暫時無法
    提供）而不是 500。500 會讓人以為服務整個掛了而一直重試，監控端也全是
    假警報 —— 這個專案在毀損 PDF 上早就記過同一條。

    2026-09-06 CI 抓到：runner 上沒有 LibreOffice，Markdown 轉辦公文件回 **500**。
    """


class OfficeSourceError(RuntimeError):
    """來源檔本身就讀不出來（毀損 / 被截斷 / 不是它宣稱的格式）。

    **和「轉檔失敗」要分開**：soffice 遇到這種檔案會**回傳 0 卻不產出任何檔案**，
    我們原本只能丟一句「轉檔成功但找不到輸出 .txt」——自相矛盾又幫不上忙
    （2026-09-06 使用者上傳一份**被截斷的 docx** 時踩到）。
    在送進 soffice **之前**就判斷得出來，訊息也才講得清楚。
    """


#: zip 為容器的格式（OOXML 與 ODF）。
_ZIP_OOXML = {".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm"}
_ZIP_ODF = {".odt", ".ods", ".odp", ".odg", ".odf", ".ott", ".ots", ".otp"}
#: 舊的二進位格式，容器是 OLE2 複合文件。
_OLE2 = {".doc", ".xls", ".ppt"}
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"



def ensure_readable(src: Path) -> None:
    """在丟給 soffice 之前先確認這份檔案的**容器**是完好的。

    只看容器結構，不解析內容 —— 便宜（毫秒級）而且判準明確。
    擋得到三種東西：

    1. **被截斷 / 毀損的 zip**（docx / xlsx / odt …）—— 沒有中央目錄的 zip
       就像被撕掉目錄的書，soffice 打得開檔案卻讀不出內容。
    2. **改了副檔名的假檔** —— 內容根本不是那個容器。
    3. **zip 炸彈** —— 解開後大得離譜或壓縮比異常的檔案。

    不認得的副檔名一律放行（純文字 / csv / rtf 之類沒有容器可驗）。
    """
    ext = src.suffix.lower()
    if ext in _OLE2:
        try:
            head = src.open("rb").read(8)
        except OSError as e:
            raise OfficeSourceError(f"檔案讀不到：{e}") from e
        if head != _OLE2_MAGIC:
            raise OfficeSourceError(
                "這份檔案的內容不是舊版 Office 格式（可能已毀損，或只是副檔名被改過）。")
        return
    if ext not in _ZIP_OOXML and ext not in _ZIP_ODF:
        return

    import zipfile
    if not zipfile.is_zipfile(src):
        # **ODF 允許未壓縮的 flat XML**（`.fodt` 是明示的副檔名，但副檔名寫
        # `.odt` 而內容是 flat XML 的檔案 LibreOffice 也讀得進去）。
        # 「不是 zip」因此**不等於**壞檔 —— 看起來像 XML 就放行，
        # 真正壞掉的檔案會在下一關（soffice 沒產出檔案）被抓到。
        try:
            head = src.open("rb").read(512).lstrip()
        except OSError as e:
            raise OfficeSourceError(f"檔案讀不到：{e}") from e
        if head.startswith(b"<?xml") or b"office:document" in head:
            return
    try:
        with zipfile.ZipFile(src) as z:
            names = set(z.namelist())
            if ext in _ZIP_OOXML and "[Content_Types].xml" not in names:
                raise OfficeSourceError(
                    "這份檔案缺少 Office 文件必要的內部結構（可能已毀損，"
                    "或只是副檔名被改過）。")
            if ext in _ZIP_ODF and "mimetype" not in names and "content.xml" not in names:
                raise OfficeSourceError(
                    "這份檔案缺少 ODF 文件必要的內部結構（可能已毀損，"
                    "或只是副檔名被改過）。")
            # zip 炸彈的判斷**全站只有一份**（`zip_guard`）—— 寫在各處一定會漂
            try:
                from .zip_guard import check as _zip_check
                _zip_check(z)
            except ZipBombError as e:
                raise OfficeSourceError(str(e)) from e
    except zipfile.BadZipFile as e:
        # 被截斷的檔案最常見：有局部檔頭、**沒有中央目錄**。
        raise OfficeSourceError(
            "這份檔案不完整或已毀損（找不到壓縮檔的目錄結構），Office 引擎讀不出來。"
            "常見原因是下載或複製時被中斷 —— 請重新取得檔案，"
            "或用 Word / LibreOffice 開啟後另存新檔再試一次。") from e
    except OSError as e:
        raise OfficeSourceError(f"檔案讀不到：{e}") from e



def _require_output(produced: Path, *, rc, stdout: bytes, stderr: bytes,
                    what: str, missing_hint: str = "") -> Path:
    """**先看產出、再看回傳碼** —— soffice 的離開碼不可靠。

    它可能一邊印無關的警告（`javaldx` 找不到 Java）一邊正常轉完，也可能在
    收尾階段才被中止。真正的判準是「有沒有拿到一份可用的檔案」；反過來先看
    回傳碼的話，**會把已經轉好的檔案白白丟掉，還告訴使用者「轉檔失敗」**，
    而訊息內容是那個無關的 Java 警告 —— 使用者會跑去裝 Java。

    這條規則 v1.14.46 就寫在 `convert_with_filter` 的註解裡，但**只有那一支
    照做**。v1.15.34 用一支「產出檔照寫、離開碼回 1」的假 soffice 實測：
    另外六支全部丟例外，只有 `convert_with_filter` 成功 —— 所以判準收成這一支，
    六支都改走它。

    拿不到可用產出時才丟 `RuntimeError`，而且**訊息要指向對的方向**：
    空檔案（來源毀損）、被訊號中止（記憶體 / 併行太多）、其他（附上 soffice
    自己說的話）三種分開講。
    """
    if produced.exists() and produced.stat().st_size > 0:
        return produced
    out = ((stderr or b"").decode("utf-8", "replace")
           + (stdout or b"").decode("utf-8", "replace")).strip()
    if produced.exists():
        raise RuntimeError(
            f"{what} 產生的是空檔案（{produced.name}）。來源檔可能已毀損，"
            "或這個輸出格式不支援來源的內容。")
    if rc is not None and (rc < 0 or rc >= 128):
        sig = -rc if rc < 0 else rc - 128
        raise RuntimeError(
            f"{what} 被訊號 {sig} 中止。多半是記憶體不足，或同時有太多轉檔在跑"
            " —— 跟來源檔、目標格式都無關，等一下再試。")
    msg = f"{what} 失敗"
    if missing_hint:
        msg += f"。{missing_hint}"
    if out:
        msg += f"：{out[:500]}"
    raise RuntimeError(msg)


def convert_to_pdf(src: Path, dst_pdf: Path, timeout: float = 60.0,
                   input_filter: Optional[str] = None) -> None:
    """Run soffice headless to convert ``src`` into ``dst_pdf``.

    Uses a *fresh* per-call user-profile directory (``-env:UserInstallation``)
    inside the same tempdir as the output. This serves two purposes:

    1. Avoids touching the user's real LibreOffice/OxOffice profile (otherwise
       opening the GUI while/after we've run headless leaves it locked/empty).
    2. Discards any crash/recovery state between calls — a *shared* profile
       accumulates "文件復原" prompts on macOS that block subsequent headless
       runs forever, even with --headless --norestore.

    Concurrency: serialised via a process-wide lock (see _soffice_lock).
    Multiple simultaneous calls queue up rather than interleave (one soffice
    process per host at a time keeps things predictable).

    input_filter: 顯式指定輸入篩選器名。**HTML 輸入一定要用
    ``"HTML (StarWriter)"``**，否則 soffice 走 **Writer/Web** 排版 ——
    那是「網頁檢視」模式，分頁很糟。實測一份 43 KB 的 Markdown：
    Web 排版出來 **36 頁、其中 3 頁只有 25~53 個字**（表格被切成一列一頁），
    改用 Writer 之後 **25 頁、沒有任何空頁**，每頁字數中位數 687 → 1029。

    這條規則 `convert_to_odt` / `convert_to_docx` 早就有了（v1.11.36 為了
    ODT 的 mimetype 加的），**只有這一支漏掉** —— 同一個家族要一次掃完。
    """
    soffice = find_soffice()
    if not soffice:
        raise OfficeUnavailableError(
            "找不到 LibreOffice / OxOffice。請安裝其中一個，或先自行轉成 PDF 上傳。"
        )
    # HTML 輸入自動走 Writer 篩選器（見 docstring：Web 排版的分頁很糟）
    if input_filter is None and src.suffix.lower() in (".html", ".htm"):
        input_filter = "HTML (StarWriter)"
    # 環境沒問題之後才驗**來源檔的容器** —— 毀損 / 截斷的檔案 soffice
    # 會回傳 0 卻不產檔，訊息只能寫「轉檔成功但找不到輸出」，幫不上使用者。
    ensure_readable(src)

    with tempfile.TemporaryDirectory() as td:
        # Fresh per-call profile dir. A *shared* profile accumulates crash/recovery
        # state across calls — on macOS that pops the "文件復原" dialog and blocks
        # the headless run forever. Throwing the profile away each call avoids the
        # entire problem (cost is ~200ms first-run init, acceptable).
        profile_path = Path(td) / "profile"
        soffice_args = [
            f"-env:UserInstallation={_profile_uri(profile_path, paper_default=_needs_paper_default(src))}",
            "--headless",
            "--norestore",
            "--nologo",
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            "--convert-to", "pdf",
            "--outdir", td,
        ]
        if input_filter:
            soffice_args += ["--infilter=" + input_filter]
        soffice_args += [str(src)]
        cmd, popen_kwargs = _build_soffice_cmd(soffice, soffice_args)
        # Serialise: at most one soffice at a time. Even though each call now
        # has its own profile, two concurrent osascript→soffice on macOS still
        # race on the WindowServer/Aqua bootstrap. Cheap to lock; ~no overhead
        # in the common single-user case.
        with _soffice_lock:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                **popen_kwargs,
            )
            _track(proc)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Hung parsing the file — force-kill so it doesn't leave a
                # zombie soffice holding the profile lock.
                _kill_tree(proc)
                raise RuntimeError(
                    f"office 轉 PDF 卡住（超過 {int(timeout)} 秒）。這份檔案可能已毀損或"
                    f"含有 LibreOffice/OxOffice 無法解析的內容。請用 Word/Pages 另存"
                    f"一份乾淨的版本再試，或直接請對方提供 PDF 版。"
                )
            rc = proc.returncode
        produced = _require_output(
            Path(td) / (src.stem + ".pdf"), rc=rc, stdout=stdout, stderr=stderr,
            what="office 轉 PDF")
        dst_pdf.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), str(dst_pdf))


def convert_to_odg(src: Path, dst_odg: Path, timeout: float = 120.0) -> None:
    """Run soffice headless to import ``src`` (a PDF) into a Draw drawing ``.odg``.

    soffice 的 PDF 匯入濾鏡屬 Draw 模組（libpdfimportlo）—— 匯入後是繪圖文件，
    每段文字變成有絕對座標的文字方塊、圖片保留、框線變向量形狀，版面幾乎 1:1。
    pdf-to-office 的 draw 引擎用它當第一步（再重組成合法 Writer .odt）。

    Same lock / profile / safety pattern as convert_to_pdf — see that function's
    docstring. 需要 LibreOffice-draw 或 OxOffice 全套（install.sh 兩條 office 路徑
    都含 Draw）。
    """
    soffice = find_soffice()
    if not soffice:
        raise OfficeUnavailableError(
            "找不到 LibreOffice / OxOffice。請安裝其中一個，或先自行轉成 PDF 上傳。"
        )
    # 環境沒問題之後才驗**來源檔的容器** —— 毀損 / 截斷的檔案 soffice
    # 會回傳 0 卻不產檔，訊息只能寫「轉檔成功但找不到輸出」，幫不上使用者。
    ensure_readable(src)
    with tempfile.TemporaryDirectory() as td:
        profile_path = Path(td) / "profile"
        soffice_args = [
            f"-env:UserInstallation={_profile_uri(profile_path)}",
            "--headless",
            "--norestore",
            "--nologo",
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            "--convert-to", "odg",
            "--outdir", td,
            str(src),
        ]
        cmd, popen_kwargs = _build_soffice_cmd(soffice, soffice_args)
        with _soffice_lock:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                **popen_kwargs,
            )
            _track(proc)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                raise RuntimeError(
                    f"PDF 匯入 Draw 卡住（超過 {int(timeout)} 秒）。這份 PDF 可能已毀損"
                    f"或含 LibreOffice/OxOffice 無法解析的內容。"
                )
            rc = proc.returncode
        produced = _require_output(
            Path(td) / (src.stem + ".odg"), rc=rc, stdout=stdout, stderr=stderr,
            what="PDF 匯入 Draw", missing_hint="可能缺 LibreOffice-draw 模組")
        dst_odg.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), str(dst_odg))


def convert_to_docx(src: Path, dst_docx: Path, timeout: float = 60.0,
                     input_filter: Optional[str] = None) -> None:
    """Run soffice headless to convert ``src`` (e.g. legacy .doc) into modern .docx.

    Same lock / profile / safety pattern as convert_to_pdf — see that function's
    docstring for rationale on the per-call profile + global lock.

    input_filter: 顯式指定輸入篩選器(同 convert_to_odt 說明)。
    """
    soffice = find_soffice()
    if not soffice:
        raise OfficeUnavailableError(
            "找不到 LibreOffice / OxOffice。請先安裝其中一個，或自行在 Word 內另存為 .docx 後上傳。"
        )
    # 環境沒問題之後才驗**來源檔的容器** —— 毀損 / 截斷的檔案 soffice
    # 會回傳 0 卻不產檔，訊息只能寫「轉檔成功但找不到輸出」，幫不上使用者。
    ensure_readable(src)

    if input_filter is None and src.suffix.lower() in (".html", ".htm"):
        input_filter = "HTML (StarWriter)"

    with tempfile.TemporaryDirectory() as td:
        profile_path = Path(td) / "profile"
        soffice_args = [
            f"-env:UserInstallation={_profile_uri(profile_path, paper_default=_needs_paper_default(src))}",
            "--headless", "--norestore", "--nologo",
            "--nolockcheck", "--nodefault", "--nofirststartwizard",
        ]
        if input_filter:
            soffice_args += ["--infilter=" + input_filter]
        soffice_args += [
            "--convert-to", "docx:MS Word 2007 XML",
            "--outdir", td,
            str(src),
        ]
        cmd, popen_kwargs = _build_soffice_cmd(soffice, soffice_args)
        with _soffice_lock:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     **popen_kwargs)
            _track(proc)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                raise RuntimeError(
                    f"office 轉 .docx 卡住（超過 {int(timeout)} 秒）。檔案可能已毀損或含 LibreOffice 無法解析的內容。"
                )
            rc = proc.returncode
        produced = _require_output(
            Path(td) / (src.stem + ".docx"), rc=rc, stdout=stdout, stderr=stderr,
            what="office 轉 .docx")
        dst_docx.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), str(dst_docx))


def convert_to_pptx(src: Path, dst_pptx: Path, timeout: float = 120.0) -> None:
    """把 Impress 檔（.odp）轉成 PowerPoint .pptx。

    與 convert_to_docx 同一套 lock / 獨立 profile / 逾時處理（理由見 convert_to_pdf
    的說明）。**需要 office 套件的 Impress 模組**（oxoffice-impress /
    libreoffice-impress）；缺模組時 soffice 會回一句誤導的「source file could not
    be loaded」，因此這裡把訊息換成可行動的說明。
    """
    soffice = find_soffice()
    if not soffice:
        raise OfficeUnavailableError(
            "找不到 LibreOffice / OxOffice。請先安裝其中一個再轉簡報檔。"
        )
    # 環境沒問題之後才驗**來源檔的容器** —— 毀損 / 截斷的檔案 soffice
    # 會回傳 0 卻不產檔，訊息只能寫「轉檔成功但找不到輸出」，幫不上使用者。
    ensure_readable(src)

    with tempfile.TemporaryDirectory() as td:
        profile_path = Path(td) / "profile"
        soffice_args = [
            f"-env:UserInstallation={_profile_uri(profile_path)}",
            "--headless", "--norestore", "--nologo",
            "--nolockcheck", "--nodefault", "--nofirststartwizard",
            "--convert-to", "pptx:Impress Office Open XML",
            "--outdir", td,
            str(src),
        ]
        cmd, popen_kwargs = _build_soffice_cmd(soffice, soffice_args)
        with _soffice_lock:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, **popen_kwargs)
            _track(proc)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                raise RuntimeError(
                    f"office 轉 .pptx 卡住（超過 {int(timeout)} 秒）。"
                    "簡報物件過多時會發生,可改輸出 .odp。"
                )
            rc = proc.returncode
        produced = _require_output(
            Path(td) / (src.stem + ".pptx"), rc=rc, stdout=stdout, stderr=stderr,
            what="office 轉 .pptx",
            missing_hint="多半是 office 套件缺少 Impress 模組"
                         "（請安裝 oxoffice-impress 或 libreoffice-impress）")
        dst_pptx.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), str(dst_pptx))


def convert_to_odt(src: Path, dst_odt: Path, timeout: float = 60.0,
                    input_filter: Optional[str] = None) -> None:
    """Run soffice headless to convert ``src`` (e.g. .docx) into .odt (writer8).

    Same lock / profile / safety pattern as convert_to_pdf — see that function's
    docstring. 給 pdf-to-office 工具把 pdf2docx 出來的 docx 再轉成 odt 用。

    input_filter: 顯式指定輸入篩選器名(如 ``"HTML (StarWriter)"``)。HTML 輸入
    若不指定 → soffice 預設用 Web filter,結果 ODT 內 mimetype 變
    ``text-web`` 而非 ``text``,使用者開檔會看到 HTML 內容而非正常 ODT。
    """
    soffice = find_soffice()
    if not soffice:
        raise OfficeUnavailableError(
            "找不到 LibreOffice / OxOffice。請先安裝其中一個才能輸出 .odt 格式。"
        )
    # 環境沒問題之後才驗**來源檔的容器** —— 毀損 / 截斷的檔案 soffice
    # 會回傳 0 卻不產檔，訊息只能寫「轉檔成功但找不到輸出」，幫不上使用者。
    ensure_readable(src)

    # HTML 輸入自動指定 Writer 篩選器，避免 Web filter 輸出 text-web mimetype
    if input_filter is None and src.suffix.lower() in (".html", ".htm"):
        input_filter = "HTML (StarWriter)"

    with tempfile.TemporaryDirectory() as td:
        profile_path = Path(td) / "profile"
        soffice_args = [
            f"-env:UserInstallation={_profile_uri(profile_path, paper_default=_needs_paper_default(src))}",
            "--headless", "--norestore", "--nologo",
            "--nolockcheck", "--nodefault", "--nofirststartwizard",
        ]
        if input_filter:
            soffice_args += ["--infilter=" + input_filter]
        soffice_args += [
            "--convert-to", "odt:writer8",
            "--outdir", td,
            str(src),
        ]
        cmd, popen_kwargs = _build_soffice_cmd(soffice, soffice_args)
        with _soffice_lock:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     **popen_kwargs)
            _track(proc)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                raise RuntimeError(
                    f"office 轉 .odt 卡住（超過 {int(timeout)} 秒）。"
                )
            rc = proc.returncode
        produced = _require_output(
            Path(td) / (src.stem + ".odt"), rc=rc, stdout=stdout, stderr=stderr,
            what="office 轉 .odt")
        dst_odt.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), str(dst_odt))


def convert_to_text(src: Path, timeout: float = 60.0) -> str:
    """Run soffice headless to convert ``src`` into UTF-8 plain text.

    Equivalent to opening the file in OxOffice/LibreOffice and choosing
    "File → Save As → Text (UTF-8)" — gives the same paragraph layout
    you'd get from manually copy-pasting from the rendered document.
    Use this for translate-doc / wordcount where preserving paragraph
    structure matters more than perfect formatting.

    Returns the decoded text. Raises RuntimeError if soffice missing or
    conversion fails.
    """
    soffice = find_soffice()
    if not soffice:
        raise OfficeUnavailableError(
            "找不到 LibreOffice / OxOffice — Office / ODF 檔案需先轉成 TXT 才能翻譯。"
        )
    # 環境沒問題之後才驗**來源檔的容器** —— 毀損 / 截斷的檔案 soffice
    # 會回傳 0 卻不產檔，訊息只能寫「轉檔成功但找不到輸出」，幫不上使用者。
    ensure_readable(src)
    with tempfile.TemporaryDirectory() as td:
        profile_path = Path(td) / "profile"
        soffice_args = [
            f"-env:UserInstallation={_profile_uri(profile_path)}",
            "--headless",
            "--norestore",
            "--nologo",
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            "--convert-to", "txt:Text (encoded):UTF8",
            "--outdir", td,
            str(src),
        ]
        cmd, popen_kwargs = _build_soffice_cmd(soffice, soffice_args)
        with _soffice_lock:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                **popen_kwargs,
            )
            _track(proc)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                raise RuntimeError(
                    f"office 轉文字卡住（超過 {int(timeout)} 秒）。"
                    "這份檔案可能已毀損或含有 LibreOffice/OxOffice 無法解析的內容。"
                )
            rc = proc.returncode
        produced = _require_output(
            Path(td) / (src.stem + ".txt"), rc=rc, stdout=stdout, stderr=stderr,
            what="office 轉文字")
        # soffice writes UTF-8 (BOM-stripped); be tolerant of encoding hiccups.
        try:
            return produced.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            return produced.read_bytes().decode("utf-8", errors="replace")


def convert_with_filter(src: Path, dst: Path, ext: str, filter_name: str,
                        timeout: float = 180.0) -> None:
    """用**指定的濾鏡**把 ``src`` 轉成 ``dst``（給「辦公文件格式互轉」用）。

    與上面那幾支 `convert_to_*` 的差別是濾鏡由呼叫端決定 —— 目標格式是使用者
    在畫面上選的，可用清單由 :mod:`app.core.office_formats` 從 soffice 自己的
    註冊表列出來。

    ## 兩件一定要做的事

    1. **一律轉到獨立的暫存目錄**，不要就地轉。soffice 的輸出檔名是
       「來源主檔名 + 目標副檔名」，**來源與輸出落在同一個目錄且副檔名相同時，
       它會什麼都不做並回傳成功**（實測：`.pptx` 轉 `.pptx` 放同一目錄，
       檔案的 SHA-1 完全沒變、回傳碼 0）。而同副檔名互轉正是這個工具的用途
       之一（換版本、修復壞檔），踩到的機率不低。
    2. **轉完要確認產出真的存在而且不是空的**。soffice 對不認得的濾鏡名稱
       同樣是回傳 0 但不產檔 —— 沒有這一步，使用者會拿到「轉檔成功」訊息
       配上一個 0 位元組的檔案。

    Raises:
        RuntimeError: 找不到 soffice、逾時、soffice 回錯、或產出不存在 / 空檔。
    """
    soffice = find_soffice()
    if not soffice:
        raise OfficeUnavailableError(
            "找不到 LibreOffice / OxOffice。請先安裝其中一個再使用格式轉換。")

    ext = ext.lower().lstrip(".")
    with tempfile.TemporaryDirectory() as td:
        outdir = Path(td) / "out"      # 見上面第 1 點：一定要獨立目錄
        outdir.mkdir()
        profile_path = Path(td) / "profile"
        soffice_args = [
            f"-env:UserInstallation={_profile_uri(profile_path, paper_default=_needs_paper_default(src))}",
            "--headless", "--norestore", "--nologo",
            "--nolockcheck", "--nodefault", "--nofirststartwizard",
            "--convert-to", f"{ext}:{filter_name}",
            "--outdir", str(outdir),
            str(src),
        ]
        cmd, popen_kwargs = _build_soffice_cmd(soffice, soffice_args)
        with _soffice_lock:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, **popen_kwargs)
            _track(proc)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                raise RuntimeError(
                    f"轉換卡住（超過 {int(timeout)} 秒）。文件物件過多時會發生，"
                    "可改轉 ODF 格式（.odt / .ods / .odp）試試。")
            rc = proc.returncode

        # **先看產出、再看回傳碼。** soffice 的離開碼不可靠 —— 它可能一邊
        # 印無關的警告（javaldx 找不到 Java）一邊正常轉完，也可能在收尾階段
        # 才死掉。真正的判準是「有沒有拿到一份可用的檔案」；反過來先看回傳碼
        # 的話，會把已經轉好的檔案白白丟掉。
        produced = outdir / f"{src.stem}.{ext}"
        if not produced.exists():
            # soffice 偶爾會用它自己認定的副檔名（例如 Type 有多個副檔名時）
            others = [p for p in outdir.iterdir() if p.is_file()]
            if len(others) == 1:
                produced = others[0]

        if produced.exists() and produced.stat().st_size > 0:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(produced), str(dst))
            return

        # 到這裡就是真的沒有可用產出了 —— 見上面第 2 點，這一段是唯一
        # 擋得住「無聲失敗」的地方。訊息要指向對的方向，不然使用者會一直
        # 換目標格式，而問題其實在別的地方。
        out = (stdout.decode("utf-8", "replace")
               + stderr.decode("utf-8", "replace"))
        if produced.exists():          # 有檔但是空的
            raise RuntimeError(
                f"轉換產生的是空檔案（{produced.name}）。來源檔可能已毀損，"
                "或這個輸出格式不支援來源的內容。")
        if rc is not None and (rc < 0 or rc >= 128):
            # 被訊號中止（實測 137 = SIGKILL）。多半是記憶體不足，或同時
            # 有太多轉檔在跑 —— 跟來源檔、目標格式都無關，講錯方向使用者
            # 會一直換格式重試。
            raise RuntimeError(
                "轉換被系統中止（可能是記憶體不足，或同時進行的轉檔太多）。"
                "請稍後再試一次；持續發生請減少同時轉換的份數。")
        if "could not be loaded" in out:
            # 來源載不進去（檔案毀損、或缺對應模組 —— 缺 Impress 時也是這句）
            raise RuntimeError(
                "來源檔打不開。可能是檔案已毀損、副檔名與實際內容不符，"
                "或這套 office 缺少對應模組（文書檔需 Writer、"
                "試算表需 Calc、簡報需 Impress）。")
        if rc:
            raise RuntimeError(f"轉換失敗：{out.strip()[:300]}")
        # 回傳 0、來源也載進去了，卻沒有檔案 → 濾鏡不認得
        raise RuntimeError(
            f"轉換沒有產生檔案。這套 office 可能不支援「{filter_name}」"
            "這個輸出格式，請改選其他目標格式。")

    # 環境沒問題之後才驗**來源檔的容器** —— 毀損 / 截斷的檔案 soffice
    # 會回傳 0 卻不產檔，訊息只能寫「轉檔成功但找不到輸出」，幫不上使用者。
    ensure_readable(src)