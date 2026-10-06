"""解除安裝時，畫面上的字要是「解除安裝」，不可以是安裝的字（v1.16.55）。

## 由來（2026-10-06 實機回報）

解除安裝完成後的最後一頁寫著「即將完成安裝 … 已在電腦安裝 …」，視窗標題是「… 安裝」，
還勾著「開啟網頁介面」—— 讀的人會以為剛剛是又裝了一次。

解除安裝與安裝是**同一支 exe、共用同一組頁面**，所以頁面上的字不可以寫死成安裝的字。
另外，原本「藏起勾選框」那段寫在完成頁的 PRE 回呼裡 —— **PRE 的時候控制項還沒建立**，
`$mui.FinishPage.Run` 是空的，藏了個空，所以勾選框一直都在。

## 判準

* 完成頁藏勾選框與連結要在 **SHOW** 回呼（Windows 實機確認過：SHOW 之後勾選框的
  WS_VISIBLE 旗標是關的、勾選狀態是 0）。
* 視窗標題、進度頁標題、完成頁標題與內文都是變數，解除安裝模式要填成解除安裝的字。
* 安裝模式的視窗標題**不可以拿 `$(^SetupCaption)` 來填** —— `Caption` 指令蓋掉的正是它，
  拿它還原＝拿自己還原自己，標題變成空白（實機量到過）。
* 安裝模式的字要在**選完語言之後**才填（不然在中文 Windows 選英文，標題仍是中文）。
* 「要不要一併刪除使用者資料」不可以在 `.onInit` 問 —— NSIS 在 .onInit 之前就算好了
  對話框標題，那時標題變數還沒有值，標題列是空白（實機量到過）。
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.nsis_source import code_text, declared_languages  # noqa: E402
from tools.repo_paths import public_root  # noqa: E402

NSI = public_root(ROOT) / "packaging" / "windows" / "installer.nsi"

#: 頁面上會出現、要依模式換字的變數
PAGE_VARS = ("WIN_CAPTION", "IF_HDR", "IF_SUB", "IF_FIN_HDR", "IF_FIN_SUB", "FIN_TITLE", "FIN_TEXT")


def _code() -> str:
    return code_text(NSI.read_text(encoding="utf-8-sig"))


def _function(code: str, name: str) -> str:
    m = re.search(rf"^Function {re.escape(name)}\b(.*?)^FunctionEnd", code, re.M | re.S)
    assert m, f"找不到 Function {name}"
    return m.group(1)


def _section(code: str, name: str) -> str:
    m = re.search(rf'^Section "{re.escape(name)}"(.*?)^SectionEnd', code, re.M | re.S)
    assert m, f"找不到 Section {name}"
    return m.group(1)


def _uninstall_branch(oninit: str) -> str:
    i = oninit.index('${If} $UNMODE == "1"')
    j = oninit.index("Return", i)
    return oninit[i:j]


def test_the_finish_page_hides_its_controls_after_they_exist():
    code = _code()
    fin = code.index("!insertmacro MUI_PAGE_FINISH")
    prev = code.rindex("!insertmacro", 0, fin)
    defines = code[prev:fin]
    assert "MUI_PAGE_CUSTOMFUNCTION_PRE" not in defines, (
        "完成頁不可以用 PRE 回呼 —— 那時勾選框還沒建立，藏了個空")
    m = re.search(r"!define MUI_PAGE_CUSTOMFUNCTION_SHOW\s+(\w+)", defines)
    assert m, "完成頁要有 SHOW 回呼來藏解除安裝用不到的勾選框與連結"
    body = _function(code, m.group(1))
    assert re.search(r'\$\{If\} \$UNMODE == "1"', body), "只在解除安裝模式藏"
    for ctl in ("$mui.FinishPage.Run", "$mui.FinishPage.Link"):
        assert re.search(rf"ShowWindow {re.escape(ctl)} 0", body), f"{ctl} 沒有藏起來"
    assert re.search(r"SendMessage \$mui\.FinishPage\.Run \$\{BM_SETCHECK\} 0 0", body), (
        "勾選框要先取消勾選 —— 藏起來但還勾著的話，按完成照樣會去開網頁")


def test_page_texts_come_from_variables():
    code = _code()
    assert re.search(r'^Caption "\$WIN_CAPTION"', code, re.M), "視窗標題要是變數"
    for define, var in (("MUI_PAGE_HEADER_TEXT", "IF_HDR"), ("MUI_PAGE_HEADER_SUBTEXT", "IF_SUB"),
                        ("MUI_INSTFILESPAGE_FINISHHEADER_TEXT", "IF_FIN_HDR"),
                        ("MUI_INSTFILESPAGE_FINISHHEADER_SUBTEXT", "IF_FIN_SUB")):
        m = re.search(rf'!define {define} "\$(\w+)"\s*(?:!define [^\n]*\s*)*!insertmacro MUI_PAGE_INSTFILES',
                      code)
        assert m and m.group(1) == var, f"進度頁的 {define} 要用 ${var}"
    for define, var in (("MUI_FINISHPAGE_TITLE", "FIN_TITLE"), ("MUI_FINISHPAGE_TEXT", "FIN_TEXT")):
        assert re.search(rf'!define {define} "\${var}"', code), f"完成頁的 {define} 要用 ${var}"


def test_uninstall_mode_fills_every_page_text_with_uninstall_wording():
    code = _code()
    branch = _uninstall_branch(_function(code, ".onInit"))
    want = {"WIN_CAPTION": "$(^UninstallCaption)", "IF_HDR": "$(UN_HDR)", "IF_SUB": "$(UN_SUB)",
            "IF_FIN_HDR": "$(UN_FIN_HDR)", "IF_FIN_SUB": "$(UN_FIN_SUB)", "FIN_TITLE": "$(UN_FIN_TITLE)"}
    for var, val in want.items():
        assert re.search(rf'StrCpy \${var}\s+"{re.escape(val)}"', branch), (
            f"解除安裝模式沒有把 ${var} 換成 {val}")
    un = _section(code, "-DoUninstall")
    assert re.search(r'StrCpy \$FIN_TEXT "\$\(UN_FIN_PURGED\)"', un) and \
        re.search(r'StrCpy \$FIN_TEXT "\$\(UN_FIN_KEPT\)"', un), (
            "完成頁的內文要依有沒有刪資料講不同的話（資料留在哪裡）")


def test_install_mode_texts_are_set_after_the_language_dialog():
    oninit = _function(_code(), ".onInit")
    dlg = oninit.index("!insertmacro MUI_LANGDLL_DISPLAY")
    for var in PAGE_VARS:
        sets = [m.start() for m in re.finditer(rf"StrCpy \${var}\s", oninit)]
        branch_start = oninit.index('${If} $UNMODE == "1"')
        install_sets = [s for s in sets if s > dlg]
        assert install_sets, f"安裝模式選完語言之後沒有填 ${var}"
        early = [s for s in sets if s < branch_start and var != "WIN_CAPTION"]
        assert not early, f"${var} 在選語言之前就填了（選英文時會是中文）"


def test_the_install_caption_is_not_the_string_caption_overrides():
    code = _code()
    assert not re.search(r'StrCpy \$WIN_CAPTION\s+"\$\(\^SetupCaption\)"', code), (
        "`Caption` 指令蓋掉的就是 ^SetupCaption —— 拿它填標題，標題會是空白")
    assert re.search(r'StrCpy \$WIN_CAPTION\s+"\$\(INST_CAPTION\)"', code), "安裝模式要用自己的標題字串"
    for lang in declared_languages(code):
        assert re.search(rf'LangString INST_CAPTION\s+\$\{{LANG_{lang}\}}\s+"\$\(\^Name\) ', code), (
            f"{lang} 沒有安裝視窗的標題")


def test_the_purge_question_is_not_asked_in_oninit():
    code = _code()
    oninit = _function(code, ".onInit")
    assert "UN_ASK_PURGE" not in oninit, (
        "在 .onInit 問「要不要刪資料」，對話框的標題列是空白（標題在 .onInit 之前就算好了）")
    un = _section(code, "-DoUninstall")
    ask = un.find("$(UN_ASK_PURGE)")
    assert ask != -1, "解除安裝沒有問要不要一併刪除使用者資料"
    assert un.index("un_dir_ok:") < ask < un.index("-PurgeData"), (
        "要在確認過安裝目錄之後、組解除安裝指令之前問")
    m = re.search(r'MessageBox [^\n]*\\\s*"\$\(UN_ASK_PURGE\)"\s*\\\s*/SD IDNO', un)
    assert m, "無介面解除安裝時預設要**保留**資料（/SD IDNO）"


def test_uninstall_wording_exists_in_every_language():
    code = _code()
    for name in ("UN_HDR", "UN_SUB", "UN_FIN_HDR", "UN_FIN_SUB", "UN_FIN_TITLE",
                 "UN_FIN_KEPT", "UN_FIN_PURGED"):
        for lang in declared_languages(code):
            assert re.search(rf"LangString {name}\s+\$\{{LANG_{lang}\}}\s+\"[^\"]+\"", code), (
                f"{name} 少了 {lang}")
    for lang in declared_languages(code):
        kept = re.search(rf'LangString UN_FIN_KEPT\s+\$\{{LANG_{lang}\}}\s+"([^"]+)"', code).group(1)
        assert "$UN_DATA_DIR" in kept, f"{lang}：資料保留時要講出資料在哪裡"
