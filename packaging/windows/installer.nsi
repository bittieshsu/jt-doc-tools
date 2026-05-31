; =====================================================================
;  installer.nsi  --  Jason Tools Document Toolbox (jt-doc-tools)
;                     Windows GUI installer (NSIS / MUI2)
; ---------------------------------------------------------------------
;  Thin bootstrapper: ships only the PowerShell core scripts + LICENSE +
;  icon. At runtime install_core.ps1 downloads Python (uv-managed),
;  clones the repo, sets up the venv and registers the WinSW service.
;
;  This file is NEW and self-contained. It does NOT modify or depend on
;  the existing command-line install.ps1 -- the two install paths are
;  intentionally isolated so the GUI installer can never destabilise the
;  curl|iex one-liner.
;
;  Build (on Linux/macOS with NSIS installed):
;    makensis -DVERSION=1.11.68 installer.nsi
;  CI builds this on an Ubuntu runner (see release-windows-installer.yml).
; =====================================================================

Unicode true

!ifndef VERSION
  !define VERSION "0.0.0"
!endif

!define APPNAME      "Jason Tools Document Toolbox"
!define SHORTNAME    "jt-doc-tools"
!define PUBLISHER    "Jason Cheng"
!define WEBSITE      "https://jasoncheng7115.github.io/jt-doc-tools/"
!define REPOURL      "https://github.com/jasoncheng7115/jt-doc-tools"
!define ARP_KEY      "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SHORTNAME}"

Name "${APPNAME} ${VERSION}"
OutFile "jt-doc-tools-${VERSION}-setup.exe"
InstallDir "$PROGRAMFILES64\${SHORTNAME}"
RequestExecutionLevel admin    ; system-level install (matches install.ps1)
SetCompressor /SOLID lzma
ShowInstDetails show
ShowUninstDetails show

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "Sections.nsh"
!include "x64.nsh"

; ---- branding -------------------------------------------------------
!define MUI_ICON   "assets\jtdt.ico"
!define MUI_UNICON "assets\jtdt.ico"
!define MUI_ABORTWARNING

; ---- install pages --------------------------------------------------
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "..\..\LICENSE"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES

; finish page: offer to open the web UI
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "$(FINISH_OPEN)"
!define MUI_FINISHPAGE_RUN_FUNCTION "OpenWebUI"
!define MUI_FINISHPAGE_LINK "$(FINISH_LINK)"
!define MUI_FINISHPAGE_LINK_LOCATION "${WEBSITE}"
!insertmacro MUI_PAGE_FINISH

; ---- uninstall pages ------------------------------------------------
!define MUI_UNCONFIRMPAGE_TEXT_TOP "$(UNINST_TOP)"
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

; ---- languages (Traditional Chinese first, then English) ------------
!insertmacro MUI_LANGUAGE "TradChinese"
!insertmacro MUI_LANGUAGE "English"

; ---- localized strings ----------------------------------------------
LangString FINISH_OPEN  ${LANG_TRADCHINESE} "開啟 jt-doc-tools 網頁介面"
LangString FINISH_OPEN  ${LANG_ENGLISH}     "Open the jt-doc-tools web interface"
LangString FINISH_LINK  ${LANG_TRADCHINESE} "前往 jt-doc-tools 介紹網站"
LangString FINISH_LINK  ${LANG_ENGLISH}     "Visit the jt-doc-tools website"
LangString UNINST_TOP   ${LANG_TRADCHINESE} "這會移除 ${APPNAME}。使用者資料（銀行帳號、簽名、歷史記錄）預設保留，可在下一步選擇是否一併刪除。"
LangString UNINST_TOP   ${LANG_ENGLISH}     "This will remove ${APPNAME}. User data (bank accounts, signatures, history) is kept by default; you can choose to delete it next."

LangString DESC_Core    ${LANG_TRADCHINESE} "核心程式與 Python 執行環境（必要）。"
LangString DESC_Core    ${LANG_ENGLISH}     "Core program and Python runtime (required)."
LangString DESC_Ocr     ${LANG_TRADCHINESE} "OCR 文字辨識引擎（PyTorch + EasyOCR + 中文訓練檔，約 700MB）。"
LangString DESC_Ocr     ${LANG_ENGLISH}     "OCR engine (PyTorch + EasyOCR + Chinese data, ~700MB)."
LangString DESC_Office  ${LANG_TRADCHINESE} "Office 文件轉檔引擎（OxOffice，約 600MB）。"
LangString DESC_Office  ${LANG_ENGLISH}     "Office conversion engine (OxOffice, ~600MB)."
LangString DESC_Svc     ${LANG_TRADCHINESE} "註冊 Windows 服務，開機自動啟動。"
LangString DESC_Svc     ${LANG_ENGLISH}     "Register a Windows service that starts automatically at boot."
LangString DESC_Fw      ${LANG_TRADCHINESE} "允許區域網路其他電腦連入（防火牆例外；服務改綁 0.0.0.0）。不需要請取消。"
LangString DESC_Fw      ${LANG_ENGLISH}     "Allow other LAN machines to connect (firewall rule; binds 0.0.0.0). Uncheck if not needed."

; =====================================================================
;  Sections  (all optional sections default to selected = 全勾)
; =====================================================================
Section "核心程式 / Core (required)" SecCore
  SectionIn RO
SectionEnd

Section "OCR 文字辨識引擎 / OCR engine" SecOcr
SectionEnd

Section "Office 轉檔引擎 / Office engine" SecOffice
SectionEnd

Section "Windows 服務 / Service (autostart)" SecSvc
SectionEnd

Section "區域網路存取 / LAN access" SecFw
SectionEnd

; Hidden section that performs the actual install once component choices
; are known. The '-' prefix hides it from the components list.
Section "-DoInstall"
  SetDetailsPrint both
  ; System-level install: shortcuts go to the All Users start menu, not the
  ; current (elevating) user's. Without this, $SMPROGRAMS = the running user's
  ; profile and the shortcut is invisible to everyone else on the machine.
  SetShellVarContext all

  ; Force 64-bit registry/FS views (we are an x64-only product).
  ${If} ${RunningX64}
    SetRegView 64
  ${Else}
    MessageBox MB_ICONSTOP "32-bit Windows is not supported."
    Abort
  ${EndIf}

  ; Extract the install core to the (temp) plugins dir.
  ; NOTE: uninstall_core.ps1 is deliberately extracted AFTER install_core runs.
  ; install_core's Fetch-Code wipes every non-bin file in $INSTDIR before the
  ; git clone, so anything written here first would be deleted. We drop the
  ; uninstall core in afterwards so the installer is self-contained and does not
  ; depend on the public repo shipping these scripts.
  SetOutPath "$PLUGINSDIR"
  File "install_core.ps1"

  ; ---- build the PowerShell switch string from component selections ----
  StrCpy $0 ""
  ${If} ${SectionIsSelected} ${SecOcr}
    StrCpy $0 "$0 -InstallOcr"
  ${EndIf}
  ${If} ${SectionIsSelected} ${SecOffice}
    StrCpy $0 "$0 -InstallOffice"
  ${EndIf}
  ${If} ${SectionIsSelected} ${SecSvc}
    StrCpy $0 "$0 -InstallService"
  ${EndIf}
  ${If} ${SectionIsSelected} ${SecFw}
    StrCpy $0 "$0 -InstallFirewall"
  ${EndIf}

  ; Prefer the 64-bit PowerShell via Sysnative. The NSIS installer is always a
  ; 32-bit process, so plain powershell.exe is the WOW64 build whose registry /
  ; Program Files views are redirected -- that broke Office/VC-redist detection.
  ; Sysnative resolves to the native System32 from a 32-bit process (x64 + ARM64).
  StrCpy $2 "powershell.exe"
  IfFileExists "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe" 0 +2
    StrCpy $2 "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe"

  DetailPrint "Running installer core (downloads Python + source, please wait) ..."
  nsExec::ExecToLog '"$2" -NoProfile -ExecutionPolicy Bypass -File "$PLUGINSDIR\install_core.ps1" -InstallDir "$INSTDIR"$0'
  Pop $1
  ${If} $1 != 0
    MessageBox MB_ICONSTOP "安裝失敗 (install_core.ps1 exit code $1)。$\r$\n請查看 $\"%ProgramData%\${SHORTNAME}\Logs\installer.log$\" 以取得詳情。"
    Abort
  ${EndIf}

  ; ---- drop the uninstall core in now (after Fetch-Code's wipe) ----------
  SetOutPath "$INSTDIR\packaging\windows"
  File "uninstall_core.ps1"

  ; ---- write uninstaller + Add/Remove-Programs registry entry ----------
  WriteUninstaller "$INSTDIR\uninstall.exe"

  WriteRegStr   HKLM "${ARP_KEY}" "DisplayName"     "${APPNAME}"
  WriteRegStr   HKLM "${ARP_KEY}" "DisplayVersion"  "${VERSION}"
  WriteRegStr   HKLM "${ARP_KEY}" "Publisher"       "${PUBLISHER}"
  WriteRegStr   HKLM "${ARP_KEY}" "URLInfoAbout"    "${WEBSITE}"
  WriteRegStr   HKLM "${ARP_KEY}" "HelpLink"        "${REPOURL}"
  WriteRegStr   HKLM "${ARP_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr   HKLM "${ARP_KEY}" "DisplayIcon"     "$INSTDIR\uninstall.exe"
  WriteRegStr   HKLM "${ARP_KEY}" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
  ; Silent uninstall runs in-place (_?=) -- NSIS's auto-relaunch-to-temp does NOT
  ; forward /S, so in-place is the reliable way to run the section silently. The
  ; section schedules a detached cleanup to remove the locked uninstall.exe + dir.
  WriteRegStr   HKLM "${ARP_KEY}" "QuietUninstallString" "$\"$INSTDIR\uninstall.exe$\" /S _?=$INSTDIR"
  WriteRegDWORD HKLM "${ARP_KEY}" "NoModify" 1
  WriteRegDWORD HKLM "${ARP_KEY}" "NoRepair" 1

  ; Start menu shortcut (browser link to the local UI).
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortcut  "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "http://127.0.0.1:8765/" "" "$INSTDIR\packaging\windows\assets\jtdt.ico"
  CreateShortcut  "$SMPROGRAMS\${APPNAME}\解除安裝 Uninstall.lnk" "$INSTDIR\uninstall.exe"
SectionEnd

; ---- component descriptions ----------------------------------------
!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecCore}   "$(DESC_Core)"
  !insertmacro MUI_DESCRIPTION_TEXT ${SecOcr}    "$(DESC_Ocr)"
  !insertmacro MUI_DESCRIPTION_TEXT ${SecOffice} "$(DESC_Office)"
  !insertmacro MUI_DESCRIPTION_TEXT ${SecSvc}    "$(DESC_Svc)"
  !insertmacro MUI_DESCRIPTION_TEXT ${SecFw}     "$(DESC_Fw)"
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Function OpenWebUI
  ExecShell "open" "http://127.0.0.1:8765/"
FunctionEnd

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "32-bit Windows is not supported."
    Abort
  ${EndIf}
  !insertmacro MUI_LANGDLL_DISPLAY
FunctionEnd

; =====================================================================
;  Uninstaller
; =====================================================================
Var /GLOBAL UN_PURGE

Function un.onInit
  SetRegView 64
  ; Ask whether to also delete user data. /SD IDNO => silent mode
  ; (QuietUninstallString uses "/S _?=$INSTDIR") defaults to KEEPING data.
  StrCpy $UN_PURGE "0"
  MessageBox MB_YESNO|MB_ICONQUESTION \
    "是否一併刪除使用者資料（銀行帳號、簽名、歷史記錄）？$\r$\n$\r$\n選「否」會保留資料，下次重新安裝可沿用。" \
    /SD IDNO IDYES un_purge_yes IDNO un_purge_done
  un_purge_yes:
    StrCpy $UN_PURGE "1"
  un_purge_done:
FunctionEnd

Section "Uninstall"
  SetDetailsPrint both
  SetRegView 64
  SetShellVarContext all   ; match the install context for shortcut removal

  ; --- breadcrumb (diagnostic): record what the uninstaller actually does ---
  CreateDirectory "$APPDATA\${SHORTNAME}\Logs"
  FileOpen $4 "$APPDATA\${SHORTNAME}\Logs\nsis-uninstall.marker" w
  FileWrite $4 "uninstall section reached$\r$\nINSTDIR=$INSTDIR$\r$\n"

  ; Run the uninstall core (stop service, firewall, PATH, optional purge).
  StrCpy $0 ""
  ${If} $UN_PURGE == "1"
    StrCpy $0 " -PurgeData"
  ${EndIf}

  ; 64-bit PowerShell via Sysnative (same rationale as install -- native registry
  ; / service / firewall view from a 32-bit uninstaller, x64 + ARM64).
  StrCpy $2 "powershell.exe"
  IfFileExists "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe" 0 +2
    StrCpy $2 "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
  FileWrite $4 "psexe=$2$\r$\n"

  IfFileExists "$INSTDIR\packaging\windows\uninstall_core.ps1" core_found core_missing
  core_found:
    FileWrite $4 "uninstall_core.ps1 found, running ...$\r$\n"
    DetailPrint "Running uninstall core ..."
    nsExec::ExecToLog '"$2" -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\packaging\windows\uninstall_core.ps1" -InstallDir "$INSTDIR"$0'
    Pop $1
    FileWrite $4 "nsExec exit=$1$\r$\n"
    Goto skip_uncore
  core_missing:
    FileWrite $4 "uninstall_core.ps1 NOT FOUND at $INSTDIR\packaging\windows$\r$\n"
  skip_uncore:
  FileClose $4

  ; Remove program files. /REBOOTOK schedules locked files for deletion on
  ; next boot (e.g. a .venv file still briefly held).
  RMDir /r /REBOOTOK "$INSTDIR"

  ; Start menu + registry cleanup.
  Delete "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk"
  Delete "$SMPROGRAMS\${APPNAME}\解除安裝 Uninstall.lnk"
  RMDir  "$SMPROGRAMS\${APPNAME}"
  DeleteRegKey HKLM "${ARP_KEY}"

  ; When run in-place (silent _?= mode) uninstall.exe is locked and $INSTDIR
  ; survives RMDir. Schedule a detached cmd that waits ~2s for us to exit, then
  ; removes the leftover. No-op when the interactive auto-relaunch already wiped
  ; $INSTDIR. (Windows self-delete idiom, see CLAUDE.md.)
  IfFileExists "$INSTDIR\uninstall.exe" 0 +2
    Exec 'cmd /c ping -n 3 127.0.0.1 >nul & rmdir /s /q "$INSTDIR"'

  ${If} $UN_PURGE == "1"
    DetailPrint "User data purged."
  ${Else}
    DetailPrint "User data kept at %ProgramData%\${SHORTNAME}."
  ${EndIf}
SectionEnd
