"""Windows 安裝程式：磁碟空間、「已安裝的應用程式」的大小、解除安裝留下的東西（v1.16.57）。

## 由來（2026-10-06 在 Windows 實機用 Release 上的 v1.16.55 安裝檔跑一輪看到的）

* 元件頁與資料夾頁的「所需空間」寫 **56.0 KB** —— 這支是瘦安裝程式，NSIS 只算得到
  它自己帶的兩支腳本，而實際要裝約 3 GB。磁碟不夠的機器照樣讓人按下一步。
* 「已安裝的應用程式」那一列**沒有大小** —— 從來沒有人寫 `EstimatedSize`。
* 解除安裝會先把自己複製到 %TEMP% 再跑，那一份**從來不刪**（實機上已經留了兩支），
  檔名還寫成 `jtdt-uninstall-.16.55.exe`：`$${VERSION}` 的 `$$` 是字面的 `$`，
  於是變成執行期的 `$1` 暫存器加上「.16.55」。
* 解除安裝的進度清單有一行寫死的英文（`Running uninstall core ...`）。
* 全新安裝的 `installer.log` 印 `[!] … not a git repo` —— 讀的人會以為哪裡裝壞了。
* 視窗底部寫著「Nullsoft Install System v3.09-4」。
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.nsis_source import code_text, declared_languages  # noqa: E402
from tools.repo_paths import public_root  # noqa: E402

WIN = public_root(ROOT) / "packaging" / "windows"
NSI = WIN / "installer.nsi"
RUN_CORE = WIN / "run_core.nsh"
CORE_PS1 = WIN / "install_core.ps1"

#: 2026-10-06 在 Windows 實機量到、各元件**最少**佔的空間（MB）。AddSize 不可以比這個小。
MEASURED_MB = {
    "Core": 1187,     # C:\Program Files\jt-doc-tools（程式與 Python 套件）
    "OCR": 240,       # Tesseract-OCR（還不含第一次辨識下載的模型）
    "Office": 648,    # OxOffice
}


def _code(path: pathlib.Path = NSI) -> str:
    return code_text(path.read_text(encoding="utf-8-sig"))


def _section(code: str, name: str) -> str:
    m = re.search(rf'^Section (?:/o )?"{re.escape(name)}"[^\n]*\n(.*?)^SectionEnd', code, re.M | re.S)
    assert m, f"找不到 Section {name}"
    return m.group(1)


def _function(code: str, name: str) -> str:
    m = re.search(rf"^Function {re.escape(name)}\b(.*?)^FunctionEnd", code, re.M | re.S)
    assert m, f"找不到 Function {name}"
    return m.group(1)


def _add_size_kb(section_body: str) -> int:
    m = re.search(r"^\s*AddSize\s+(\d+)\s*$", section_body, re.M)
    assert m, "這個元件沒有 AddSize —— 「所需空間」只會算到安裝程式自己帶的腳本"
    return int(m.group(1))


# --------------------------------------------------------------- 所需空間
def test_each_component_declares_at_least_what_it_really_installs():
    code = _code()
    for name, mb in MEASURED_MB.items():
        kb = _add_size_kb(_section(code, name))
        assert kb >= mb * 1024, f"{name}：AddSize {kb} KB 比實機量到的 {mb} MB 還小"


def test_the_descriptions_quote_the_same_size_as_add_size():
    """元件說明裡寫的大小要跟 AddSize 對得上 —— 說明寫 600 MB、算的是 2 GB，讀的人不知道該信哪個。"""
    code = _code()
    sizes = {"Core": "DESC_Core", "OCR": "DESC_Ocr", "Office": "DESC_Office"}
    langs = declared_languages(NSI.read_text(encoding="utf-8-sig"))
    assert len(langs) >= 3
    for section, desc in sizes.items():
        add_mb = _add_size_kb(_section(code, section)) / 1024
        lines = re.findall(rf'^LangString {desc}\s+\$\{{(LANG_\w+)\}}\s+"([^"]*)"', code, re.M)
        assert len(lines) == len(langs), f"{desc} 要有每一種語言（{len(lines)} / {len(langs)}）"
        for lang, text in lines:
            m = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB)", text)
            assert m, f"{desc}（{lang}）沒寫大小"
            said = float(m.group(1)) * (1024 if m.group(2) == "GB" else 1)
            assert abs(said - add_mb) <= add_mb * 0.12, (
                f"{desc}（{lang}）寫 {m.group(0)}，AddSize 是 {add_mb:.0f} MB")


# --------------------------------------------------------------- 已安裝的應用程式
def test_the_installer_writes_the_installed_size():
    body = _section(_code(), "-DoInstall")
    i = body.find('${GetSize} "$INSTDIR" "/S=0K"')
    j = body.find('WriteRegDWORD HKLM "${ARP_KEY}" "EstimatedSize"')
    assert i >= 0, "沒有量安裝目錄的大小（要用 /S=0K，EstimatedSize 的單位是 KB）"
    assert j > i, "EstimatedSize 要寫量出來的值"


def test_tree_size_counts_files_and_does_not_follow_links(tmp_path):
    from app import cli
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.bin").write_bytes(b"\0" * 3000)
    (tmp_path / "y.bin").write_bytes(b"\0" * 1000)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"\0" * 500_000)
    try:
        (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    except OSError:      # 沒有權限建連結的平台（Windows 一般帳號）就不驗這一半
        pass
    assert cli._tree_size_kb(tmp_path) == 4   # 4000 bytes → 無條件進位成 4 KB


class _FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_winreg(store: dict) -> types.ModuleType:
    m = types.ModuleType("winreg")
    m.HKEY_LOCAL_MACHINE = object()
    m.KEY_READ = 1
    m.KEY_SET_VALUE = 2
    m.REG_DWORD = 4

    def OpenKey(*_a, **_k):
        if store.get("missing_key"):
            raise OSError("no such key")
        return _FakeKey()

    def QueryValueEx(_k, name):
        if name not in store:
            raise OSError(name)
        return store[name], 4

    def SetValueEx(_k, name, _r, kind, value):
        store.setdefault("writes", []).append((name, kind, value))
        store[name] = value

    m.OpenKey, m.QueryValueEx, m.SetValueEx = OpenKey, QueryValueEx, SetValueEx
    return m


def test_the_service_writes_the_size_when_it_is_missing_or_stale(monkeypatch, tmp_path):
    from app import cli
    (tmp_path / "f.bin").write_bytes(b"\0" * 10 * 1024 * 1024)      # 10 MB
    monkeypatch.setattr(cli, "_is_windows", lambda: True)
    monkeypatch.setattr(cli, "_install_root", lambda: tmp_path)

    store: dict = {}
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg(store))
    cli._sync_windows_estimated_size()
    assert store.get("writes") == [("EstimatedSize", 4, 10240)], store

    store["writes"] = []
    store["EstimatedSize"] = 10200          # 差不到 1%：不重寫
    cli._sync_windows_estimated_size()
    assert store["writes"] == []

    store["EstimatedSize"] = 5000           # 舊的值：要更新
    cli._sync_windows_estimated_size()
    assert store["writes"] == [("EstimatedSize", 4, 10240)]


def test_the_size_sync_never_raises(monkeypatch):
    from app import cli
    cli._sync_windows_estimated_size()                     # 非 Windows：直接回
    monkeypatch.setattr(cli, "_is_windows", lambda: True)
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg({"missing_key": True}))
    cli._sync_windows_estimated_size()                     # 沒有那個鍵（不是安裝程式裝的）也不可以丟例外


def test_the_service_updates_the_size_off_the_startup_path():
    """`jtdt update` 不會重跑安裝程式 —— 大小要由服務自己更新；但走整個目錄要幾秒，不放在啟動當下。"""
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    startup = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "_startup")
    later = next((n for n in ast.walk(startup)
                  if isinstance(n, ast.FunctionDef) and n.name == "_later"), None)
    assert later is not None, "找不到延遲同步的 _later"

    def calls(node):
        return {getattr(c.func, "id", getattr(c.func, "attr", ""))
                for c in ast.walk(node) if isinstance(c, ast.Call)}

    assert "_sync_windows_estimated_size" in calls(later), "延遲同步沒有更新安裝大小"
    later_ids = {id(n) for n in ast.walk(later)}
    direct = {getattr(c.func, "id", getattr(c.func, "attr", ""))
              for c in ast.walk(startup) if isinstance(c, ast.Call) and id(c) not in later_ids}
    assert "_sync_windows_estimated_size" not in direct, "走整個安裝目錄要幾秒，不可以放在啟動當下"


# --------------------------------------------------------------- %TEMP% 裡的解除安裝副本
def test_the_temp_copy_name_has_the_whole_version():
    code = _code()
    assert "$${VERSION}" not in code, "`$${VERSION}` 會變成 `$1` 暫存器加版本後半段"
    assert '"$TEMP\\jtdt-uninstall-${VERSION}.exe"' in code


def test_copies_left_by_earlier_runs_are_cleared():
    oninit = _function(_code(), ".onInit")
    sweep = 'Delete "$TEMP\\jtdt-uninstall-*.exe"'
    un = oninit[oninit.index('${If} $UNMODE == "1"'):oninit.index("!insertmacro MUI_LANGDLL_DISPLAY")]
    assert sweep in un, "解除安裝開始時要先清掉以前留下的副本"
    assert un.index(sweep) < un.index("kernel32::CopyFile"), "要在複製這一份之前清（不然會把剛複製的也刪掉）"
    inst = oninit[oninit.index("!insertmacro MUI_LANGDLL_DISPLAY") - 200:]
    assert sweep in inst, "安裝時也要清（舊版解除安裝留下的）"


def test_the_temp_copy_deletes_itself_without_a_window():
    code = _code()
    oninit = _function(code, ".onInit")
    # /fromtemp 那一份才是要刪掉的（在安裝目錄裡的那一份會被 RMDir 一起刪）
    assert re.search(r'un_no_copy:\s*\$\{Else\}\s*StrCpy \$UN_FROMTEMP "1"', oninit), (
        "從 %TEMP% 執行的那一份要記下來（UN_FROMTEMP），結束時才知道要刪自己")
    body = _section(code, "-DoUninstall")
    m = re.search(r'\$\{If\} \$UN_FROMTEMP == "1"(.*?)\$\{EndIf\}', body, re.S)
    assert m, "解除安裝結束時沒有刪掉 %TEMP% 裡的自己"
    block = m.group(1)
    assert "del " in block and "$EXEPATH" in block and "Call RunDetachedHidden" in block
    assert not re.search(r"^\s*Exec\b", block, re.M), "`Exec` 會開出一個黑色主控台視窗，最多掛十分鐘"
    helper = _function(_code(RUN_CORE), "RunDetachedHidden")
    assert "CreateProcessW" in helper and "0x08000000" in helper, "要 CREATE_NO_WINDOW"
    assert "WaitForSingleObject" not in helper, "不可以等它 —— 它要等我們結束才刪得掉"


# --------------------------------------------------------------- 畫面上的字
def test_every_progress_line_follows_the_interface_language():
    for path in (NSI, RUN_CORE):
        for arg in re.findall(r"^\s*DetailPrint\s+(\S.*?)\s*$", _code(path), re.M):
            assert re.fullmatch(r'"(\$\(\w+\)|\$\w+)"', arg), (
                f"{path.name}：DetailPrint {arg} —— 進度清單的字要走 LangString（或狀態檔），不可以寫死")


def test_the_bottom_line_shows_the_product_not_the_build_tool():
    m = re.search(r'^BrandingText\s+(?:/\w+\s+)?"([^"]*)"', _code(), re.M)
    assert m, "沒有 BrandingText —— 視窗底部會寫「Nullsoft Install System v3.09-4」"
    assert "Nullsoft" not in m.group(1) and "jt-doc-tools" in m.group(1)


def test_a_fresh_install_does_not_log_a_warning():
    text = CORE_PS1.read_text(encoding="utf-8-sig")
    body = text[text.index("function Fetch-Code"):text.index("\n}", text.index("function Fetch-Code"))]
    lines = body.splitlines()
    # 只看真的會印出來的那一行（`Warn …`）—— 註解裡引用舊訊息的那句不算
    warn = [i for i, line in enumerate(lines)
            if "not a git repo" in line and line.lstrip().startswith("Warn ")]
    assert warn, "找不到那一行（改名了的話這條要跟著改）"
    for i in warn:
        above = "\n".join(lines[max(0, i - 3):i])
        assert re.search(r"if \(\$leftover\.Count -gt 0\)", above), (
            "「not a git repo」的警告要只在安裝目錄裡真的有舊東西時才印")
    assert "Fresh install" in body
