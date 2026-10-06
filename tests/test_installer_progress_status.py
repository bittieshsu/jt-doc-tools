"""Windows 安裝程式要看得到「現在在做什麼」，而且不可以是亂碼（v1.16.55）。

## 由來

安裝核心（install_core.ps1）要跑 10～30 分鐘，但安裝視窗從頭到尾只有兩行固定的字 ——
不可以用 `nsExec::ExecToLog` 把輸出串進畫面（上游 bug #1323 會讓安裝程式在最後一步
當掉），而以前串進畫面的輸出又會被 NSIS 用系統 ANSI 字碼頁解成亂碼。

現在的做法：install_core.ps1 把「目前這一步」寫進一個**狀態檔**（UTF-16LE、不加 BOM），
installer.nsi 在背景啟動它、每半秒用 `FileReadUTF16LE` 讀一次、有變才顯示。
整條路都是 Unicode，不經過 ANSI 字碼頁。

## 判準

* 每一個用到的狀態都有三種語言的文字，而且三種語言的 `{0}` `{1}` 個數一樣
  （少一個的話 PowerShell 的 `-f` 會丟例外，那一行就不顯示）。
* 寫的一邊是 UTF-16LE 無 BOM、讀的一邊是 `FileReadUTF16LE` —— 兩邊任何一邊換掉都會亂碼。
* `File.Replace` 的第三個參數不可以是 `$null`（PowerShell 會換成空字串、每次都失敗，
  畫面停在第一步 —— 2026-10-06 在 Windows 實機上抓到）。
* 安裝程式把狀態檔路徑與介面語言傳給核心；語言代碼涵蓋安裝程式宣告的每一種語言。
* 背景啟動時要等它結束、拿到離開碼（`$1`，失敗的判斷靠它）；啟動不起來要退回原本的方式。

實機驗證（中文 / 日文顯示、下載進度、離開碼）見 TEST_PLAN §0.3。
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.nsis_source import code_text, declared_languages  # noqa: E402
from tools.repo_paths import public_root  # noqa: E402

PKG = public_root(ROOT) / "packaging" / "windows"
CORE = PKG / "install_core.ps1"
NSI = PKG / "installer.nsi"
RUN = PKG / "run_core.nsh"

#: 安裝程式宣告的語言（`declared_languages` 一律大寫）→ (NSIS 的語言代碼, 狀態表裡的鍵)。
#: 英文是預設值（switch 的 default）。
LANG_MAP = {"TRADCHINESE": ("1028", "zh"), "JAPANESE": ("1041", "ja"), "ENGLISH": (None, "en")}


def _core() -> str:
    lines = CORE.read_text(encoding="utf-8-sig").splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))


def _function(text: str, name: str) -> str:
    m = re.search(rf"^function {re.escape(name)}\b.*?^}}", text, re.M | re.S)
    assert m, f"找不到 function {name}"
    return m.group(0)


def _status_table() -> dict[str, dict[str, str]]:
    text = _core()
    m = re.search(r"^\$StatusText = @\{\n(.*?)^\}", text, re.M | re.S)
    assert m, "找不到 $StatusText 狀態文字表"
    table: dict[str, dict[str, str]] = {}
    # 每一筆的結尾是「引號 + 右大括號」—— 只找第一個 `}` 的話會停在 `{0}` 裡面
    for key, body in re.findall(r"'([a-z_]+)'\s*=\s*@\{(.*?')\s*\}", m.group(1), re.S):
        table[key] = dict(re.findall(r"\b(zh|en|ja)\s*=\s*'([^']*)'", body))
    return table


def _used_keys() -> set[str]:
    text = _core()
    used = set(re.findall(r"\bSet-(?:Status|Progress)\s+'([a-z_]+)'", text))
    used |= set(re.findall(r"\bKey\s*=\s*'([a-z_]+)'", text))
    used |= set(re.findall(r"\b_status_text\s+'([a-z_]+)'", text))
    return used


def test_the_scan_sees_the_status_calls():
    """先證明掃得到東西：狀態表與呼叫點都要找得到一定數量（實際 21 個鍵、20 個以上的呼叫）。"""
    assert len(_status_table()) >= 15, "狀態文字表幾乎是空的，掃描器大概壞了"
    assert len(_used_keys()) >= 15, "找不到 Set-Status / Set-Progress 的呼叫，掃描器大概壞了"


def test_every_used_status_has_text_in_every_language():
    table = _status_table()
    missing = sorted(_used_keys() - set(table))
    assert not missing, f"這幾個狀態沒有文字（畫面上會什麼都不顯示）：{missing}"
    langs = {LANG_MAP[n][1] for n in declared_languages(NSI.read_text(encoding="utf-8-sig"))}
    bad = {k: sorted(langs - {l for l, v in t.items() if v.strip()})
           for k, t in table.items() if langs - {l for l, v in t.items() if v.strip()}}
    assert not bad, f"這幾個狀態少了某些語言的文字：{bad}"


def test_placeholders_match_across_languages():
    """`{0}` `{1}` 的個數三種語言要一樣 —— 譯文少一個不會在中文介面出事，只在那個語言出事。"""
    bad = {}
    for key, t in _status_table().items():
        sets = {lang: tuple(sorted(set(re.findall(r"\{(\d+)\}", s)))) for lang, s in t.items()}
        if len(set(sets.values())) > 1:
            bad[key] = sets
    assert not bad, f"這幾個狀態的參數個數各語言不同：{bad}"


def test_every_installer_language_has_a_status_language():
    """安裝程式加了第四種語言卻沒加進狀態表，畫面會安靜地退回英文。"""
    text = _core()
    switch = re.search(r"^\$UiLangKey = switch \(\$UiLang\) \{(.*)\}\s*$", text, re.M)
    assert switch, "找不到把語言代碼換成狀態表鍵的那一行"
    langs = declared_languages(NSI.read_text(encoding="utf-8-sig"))
    unknown = [n for n in langs if n not in LANG_MAP]
    assert not unknown, f"安裝程式宣告了狀態表不認得的語言：{unknown}（要加進 $StatusText 與這張對照）"
    for name in langs:
        lcid, key = LANG_MAP[name]
        if lcid:
            assert re.search(rf"'{lcid}'\s*\{{\s*'{key}'\s*\}}", switch.group(1)), (
                f"{name}（{lcid}）沒有對到 '{key}'")
    assert re.search(r"default\s*\{\s*'en'\s*\}", switch.group(1)), "對不上的語言要退回英文"


def test_the_status_file_is_unicode_on_both_ends():
    """寫：UTF-16LE 不加 BOM。讀：`FileReadUTF16LE`。**任何一邊換掉就是亂碼。**

    BOM 也不可以有 —— `FileReadUTF16LE` 不會把它吃掉，畫面第一個字會是一個怪字元，
    而且「跟上一次一樣就不顯示」的比對也會失效。
    """
    core = _core()
    assert re.search(r"New-Object System\.Text\.UnicodeEncoding\(\$false, \$false\)", core), (
        "狀態檔要用 UTF-16LE、不加 BOM 寫")
    body = _function(core, "_status")
    assert "WriteAllText(" in body and "$Utf16NoBom" in body, "寫狀態檔沒有用那個編碼"
    run = code_text(RUN.read_text(encoding="utf-8-sig"))
    assert "FileReadUTF16LE" in run, "讀狀態檔要用 FileReadUTF16LE"
    assert not re.search(r"^\s*FileRead\s", run, re.M), (
        "用 FileRead 讀狀態檔＝用系統 ANSI 字碼頁解，中文與日文會變亂碼")


def test_replace_does_not_pass_null():
    body = _function(_core(), "_status")
    m = re.search(r"\[System\.IO\.File\]::Replace\(([^)]*)\)", body)
    assert m, "找不到換上新狀態檔的那一行"
    args = [a.strip() for a in m.group(1).split(",")]
    assert args[-1] == "[NullString]::Value", (
        f"File.Replace 的第三個參數是 {args[-1]} —— `$null` 會被換成空字串，"
        "第二次起每次都失敗，畫面停在第一步")


def test_writing_the_status_never_breaks_the_install():
    """狀態檔只是顯示用：沒指定（手動執行）就不寫，寫不進去也不可以讓安裝失敗。"""
    body = _function(_core(), "_status")
    assert re.search(r"if \(-not \$StatusFile\) \{ return \}", body), "手動執行（沒有狀態檔）時要跳過"
    assert "catch" in body, "寫狀態檔失敗要接住，不可以讓安裝失敗"


def test_failure_is_reported_on_screen_too():
    die = _function(_core(), "Die")
    assert "Set-Status 'failed'" in die, "安裝失敗時畫面上要講出來（不然停在最後一步看起來像卡住）"


def test_the_installer_passes_the_status_file_and_language():
    nsi = code_text(NSI.read_text(encoding="utf-8-sig"))
    cmd = re.search(r"^\s*StrCpy \$R5 '(.*install_core\.ps1.*)'\s*$", nsi, re.M)
    assert cmd, "找不到組安裝核心命令列的那一行"
    assert '-StatusFile "$PLUGINSDIR\\status.txt"' in cmd.group(1), "沒有把狀態檔路徑傳給核心"
    assert "-UiLang $LANGUAGE" in cmd.group(1), "沒有把介面語言傳給核心（畫面會是英文）"
    run = code_text(RUN.read_text(encoding="utf-8-sig"))
    assert run.count('"$PLUGINSDIR\\status.txt"') >= 2, "讀的檔案跟傳給核心的不是同一個"
    assert re.search(r'^!include "run_core\.nsh"', nsi, re.M), "installer.nsi 沒有引入 run_core.nsh"
    i = nsi.index("Call RunInstallCore")
    assert re.search(r"\$\{If\} \$1 != 0", nsi[i:i + 400]), "執行完沒有檢查離開碼"


def test_the_core_runs_in_the_background_and_its_exit_code_is_kept():
    run = code_text(RUN.read_text(encoding="utf-8-sig"))
    body = run[run.index("Function RunInstallCore"):run.index("FunctionEnd")]
    assert re.search(r"kernel32::CreateProcessW\(p 0, w R5,.*i 0x08000000", body), (
        "要用 CreateProcessW 在背景啟動（0x08000000 = 不開主控台視窗）")
    loop = re.search(r"\$\{Do\}(.*?)\$\{LoopWhile\} \$R4 = 258", body, re.S)
    assert loop, "要一直等到它結束（258 = WAIT_TIMEOUT）"
    assert "WaitForSingleObject" in loop.group(1) and "Call ShowInstallStatus" in loop.group(1), (
        "等待的迴圈裡要讀狀態檔")
    assert loop.group(1).index("WaitForSingleObject") < loop.group(1).index("Call ShowInstallStatus"), (
        "要先等再讀 —— 結束之後還要再讀最後一次，才看得到「已完成 / 失敗」")
    after = body[loop.end():]
    assert "GetExitCodeProcess" in after and re.search(r"StrCpy \$1 \$R4", after), (
        "離開碼要放進 $1（安裝程式用它判斷成功或失敗）")
    fallback = re.search(r"\$\{If\} \$R4 == 0(.*?)Return", body, re.S)
    assert fallback and "nsExec::Exec $R5" in fallback.group(1) and "Pop $1" in fallback.group(1), (
        "背景啟動不起來時要退回 nsExec::Exec（看不到進度，但安裝照樣完成）")


def test_progress_lines_update_in_place():
    """下載進度每秒一筆 —— 全部加進清單會洗版。`P` 開頭的只更新清單上方那一行。"""
    run = code_text(RUN.read_text(encoding="utf-8-sig"))
    body = run[run.index("Function ShowInstallStatus"):]
    m = re.search(r'\$\{If\} \$R8 == "P"\s*SetDetailsPrint textonly\s*\$\{EndIf\}\s*DetailPrint "\$R0"', body)
    assert m, "進度行要用 SetDetailsPrint textonly 顯示"
    assert re.search(r"SetDetailsPrint both\s*FunctionEnd", body), "顯示完要切回 both，不然之後的步驟也進不了清單"


def test_the_echo_hook_only_exists_in_test_builds():
    """把畫面上的字另外寫進檔案，只在 `-DSTATUS_ECHO_FILE=` 的測試編譯才有。"""
    run = code_text(RUN.read_text(encoding="utf-8-sig"))
    m = re.search(r"!ifdef STATUS_ECHO_FILE(.*?)!endif", run, re.S)
    assert m and "FileWriteUTF16LE" in m.group(1), "測試用的回寫要包在 !ifdef 裡"
    rest = run[:m.start()] + run[m.end():]
    assert "STATUS_ECHO_FILE" not in rest, "STATUS_ECHO_FILE 不可以出現在 !ifdef 之外的程式碼"


def test_the_download_shows_its_progress():
    core = _core()
    msi = _function(core, "Save-VerifiedMsi")
    assert "Save-UrlWithProgress" in msi, "OxOffice（約 400 MB）的下載沒有顯示進度"
    dl = _function(core, "Save-UrlWithProgress")
    assert "Set-Progress 'office_dl'" in dl, "下載迴圈裡沒有回報進度"
    assert re.search(r"\$tick -ne \$lastTick", dl), "進度每秒更新一次就好（每讀一塊就寫檔會拖慢下載）"


@pytest.mark.parametrize("text,want", [
    ("   Downloading torch (226.3MiB)\n   Downloading numpy (12.1MiB)\n Downloaded numpy\n",
     ("python_dl", "torch")),
    ("   Downloading numpy (12.1MiB)\n Downloaded numpy\n", ("python_unpack", None)),
    ("Resolved 125 packages in 2ms\n", (None, None)),
    # 升級時套件都在快取裡：沒有任何 Downloading，但「Prepared」之後就是安裝階段
    ("Resolved 125 packages in 2ms\nPrepared 96 packages in 9.48s\n", ("python_unpack", None)),
])
def test_the_uv_parser_shape(text, want):
    """uv 的下載訊息在不是終端機時照樣會印（uv 0.11 實測）：`Downloading 名稱 (大小)` 與
    ` Downloaded 名稱`。這裡用 Python 照同一個規則跑一次，釘住規則本身的意思 ——
    PowerShell 那一份在 Windows 實機上另外跑過（TEST_PLAN §0.3）。"""
    body = _function(_core(), "Get-UvDownloadState")
    assert r"'^\s*Downloading (\S+) \(([^)]+)\)'" in body
    assert r"'^\s*Downloaded (\S+)\s*$'" in body
    assert r"'^\s*Prepared \d+ package'" in body
    order, size, done, prepared = [], {}, set(), False
    for line in text.splitlines():
        m = re.match(r"^\s*Downloading (\S+) \(([^)]+)\)", line)
        if m:
            if m.group(1) not in size:
                order.append(m.group(1))
            size[m.group(1)] = m.group(2)
            continue
        m = re.match(r"^\s*Downloaded (\S+)\s*$", line)
        if m:
            done.add(m.group(1))
            continue
        if re.match(r"^\s*Prepared \d+ package", line):
            prepared = True
    pending = [n for n in order if n not in done]
    got = (("python_dl", pending[-1]) if pending
           else (("python_unpack", None) if (done or prepared) else (None, None)))
    assert got == want
