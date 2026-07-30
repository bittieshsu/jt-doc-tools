"""Convert Office documents (.docx/.doc/.xlsx/.xls/.odt/.ods/.pptx…) to PDF.

Delegates to a headless LibreOffice (or its drop-in fork OxOffice, which
ships on many Mac setups). We search a few common install paths and the
``PATH``; if none is found, :func:`convert_to_pdf` raises so the caller can
surface a clear error.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional


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


def _build_soffice_cmd(soffice: str, args: list[str]) -> tuple[list, dict]:
    """Build subprocess.Popen kwargs for cross-platform soffice invocation.

    Returns (argv, popen_kwargs). popen_kwargs may include `creationflags`
    (Windows) or wrap the cmd with osascript (macOS).
    """
    import sys as _sys
    import os as _os
    import shlex as _shlex
    kwargs: dict = {}
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


def _profile_uri(profile_path: Path) -> str:
    """Build a valid `file://` URI for the soffice -env:UserInstallation arg.

    Bug fix (issue #5, v1.5.1): on Windows we used to build
    `file://C:\\Users\\...\\profile` by string concat. That's a malformed URI
    (Windows file URIs need three slashes + forward slashes:
    `file:///C:/Users/.../profile`). soffice silently fell back to the
    LocalSystem default profile → first-time setup hung in Session 0
    → all conversions timeout at 60s.

    Path.as_uri() does the right thing on all platforms.
    """
    return profile_path.resolve().as_uri()


def convert_to_pdf(src: Path, dst_pdf: Path, timeout: float = 60.0) -> None:
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
    """
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError(
            "找不到 LibreOffice / OxOffice。請安裝其中一個，或先自行轉成 PDF 上傳。"
        )

    with tempfile.TemporaryDirectory() as td:
        # Fresh per-call profile dir. A *shared* profile accumulates crash/recovery
        # state across calls — on macOS that pops the "文件復原" dialog and blocks
        # the headless run forever. Throwing the profile away each call avoids the
        # entire problem (cost is ~200ms first-run init, acceptable).
        profile_path = Path(td) / "profile"
        soffice_args = [
            f"-env:UserInstallation={_profile_uri(profile_path)}",
            "--safe-mode",       # skip user customisations + recovery prompt
            "--headless",
            "--norestore",
            "--nologo",
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            "--convert-to", "pdf",
            "--outdir", td,
            str(src),
        ]
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
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                raise RuntimeError(
                    f"office 轉 PDF 卡住（超過 {int(timeout)} 秒）。這份檔案可能已毀損或"
                    f"含有 LibreOffice/OxOffice 無法解析的內容。請用 Word/Pages 另存"
                    f"一份乾淨的版本再試，或直接請對方提供 PDF 版。"
                )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"office 轉 PDF 失敗：{stderr.decode('utf-8', 'replace') or stdout.decode('utf-8', 'replace')}"
                )
        produced = Path(td) / (src.stem + ".pdf")
        if not produced.exists():
            raise RuntimeError("轉檔成功但找不到輸出檔")
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
        raise RuntimeError(
            "找不到 LibreOffice / OxOffice。請安裝其中一個，或先自行轉成 PDF 上傳。"
        )
    with tempfile.TemporaryDirectory() as td:
        profile_path = Path(td) / "profile"
        soffice_args = [
            f"-env:UserInstallation={_profile_uri(profile_path)}",
            "--safe-mode",
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
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                raise RuntimeError(
                    f"PDF 匯入 Draw 卡住（超過 {int(timeout)} 秒）。這份 PDF 可能已毀損"
                    f"或含 LibreOffice/OxOffice 無法解析的內容。"
                )
            if proc.returncode != 0:
                raise RuntimeError(
                    "PDF 匯入 Draw 失敗（可能缺 LibreOffice-draw 模組）："
                    + (stderr.decode("utf-8", "replace") or stdout.decode("utf-8", "replace"))
                )
        produced = Path(td) / (src.stem + ".odg")
        if not produced.exists():
            raise RuntimeError("PDF 匯入成功但找不到輸出的 .odg")
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
        raise RuntimeError(
            "找不到 LibreOffice / OxOffice。請先安裝其中一個，或自行在 Word 內另存為 .docx 後上傳。"
        )

    if input_filter is None and src.suffix.lower() in (".html", ".htm"):
        input_filter = "HTML (StarWriter)"

    with tempfile.TemporaryDirectory() as td:
        profile_path = Path(td) / "profile"
        soffice_args = [
            f"-env:UserInstallation={_profile_uri(profile_path)}",
            "--safe-mode", "--headless", "--norestore", "--nologo",
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
                proc.kill()
                try: proc.communicate(timeout=5)
                except Exception: pass
                raise RuntimeError(
                    f"office 轉 .docx 卡住（超過 {int(timeout)} 秒）。檔案可能已毀損或含 LibreOffice 無法解析的內容。"
                )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"office 轉 .docx 失敗：{stderr.decode('utf-8', 'replace') or stdout.decode('utf-8', 'replace')}"
                )
        produced = Path(td) / (src.stem + ".docx")
        if not produced.exists():
            raise RuntimeError("轉檔成功但找不到輸出 .docx")
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
        raise RuntimeError(
            "找不到 LibreOffice / OxOffice。請先安裝其中一個再轉簡報檔。"
        )

    with tempfile.TemporaryDirectory() as td:
        profile_path = Path(td) / "profile"
        soffice_args = [
            f"-env:UserInstallation={_profile_uri(profile_path)}",
            "--safe-mode", "--headless", "--norestore", "--nologo",
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
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                raise RuntimeError(
                    f"office 轉 .pptx 卡住（超過 {int(timeout)} 秒）。"
                    "簡報物件過多時會發生,可改輸出 .odp。"
                )
            if proc.returncode != 0:
                err = (stderr.decode("utf-8", "replace")
                       or stdout.decode("utf-8", "replace"))
                raise RuntimeError(f"office 轉 .pptx 失敗：{err}")
        produced = Path(td) / (src.stem + ".pptx")
        if not produced.exists():
            raise RuntimeError(
                "轉檔成功但找不到輸出 .pptx。多半是 office 套件缺少 Impress 模組"
                "（請安裝 oxoffice-impress 或 libreoffice-impress）。"
            )
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
        raise RuntimeError(
            "找不到 LibreOffice / OxOffice。請先安裝其中一個才能輸出 .odt 格式。"
        )

    # HTML 輸入自動指定 Writer 篩選器，避免 Web filter 輸出 text-web mimetype
    if input_filter is None and src.suffix.lower() in (".html", ".htm"):
        input_filter = "HTML (StarWriter)"

    with tempfile.TemporaryDirectory() as td:
        profile_path = Path(td) / "profile"
        soffice_args = [
            f"-env:UserInstallation={_profile_uri(profile_path)}",
            "--safe-mode", "--headless", "--norestore", "--nologo",
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
                proc.kill()
                try: proc.communicate(timeout=5)
                except Exception: pass
                raise RuntimeError(
                    f"office 轉 .odt 卡住（超過 {int(timeout)} 秒）。"
                )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"office 轉 .odt 失敗：{stderr.decode('utf-8', 'replace') or stdout.decode('utf-8', 'replace')}"
                )
        produced = Path(td) / (src.stem + ".odt")
        if not produced.exists():
            raise RuntimeError("轉檔成功但找不到輸出 .odt")
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
        raise RuntimeError(
            "找不到 LibreOffice / OxOffice — Office / ODF 檔案需先轉成 TXT 才能翻譯。"
        )
    with tempfile.TemporaryDirectory() as td:
        profile_path = Path(td) / "profile"
        soffice_args = [
            f"-env:UserInstallation={_profile_uri(profile_path)}",
            "--safe-mode",
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
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                raise RuntimeError(
                    f"office 轉文字卡住（超過 {int(timeout)} 秒）。"
                    "這份檔案可能已毀損或含有 LibreOffice/OxOffice 無法解析的內容。"
                )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"office 轉文字失敗：{stderr.decode('utf-8', 'replace') or stdout.decode('utf-8', 'replace')}"
                )
        produced = Path(td) / (src.stem + ".txt")
        if not produced.exists():
            raise RuntimeError("轉檔成功但找不到輸出 .txt")
        # soffice writes UTF-8 (BOM-stripped); be tolerant of encoding hiccups.
        try:
            return produced.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            return produced.read_bytes().decode("utf-8", errors="replace")
