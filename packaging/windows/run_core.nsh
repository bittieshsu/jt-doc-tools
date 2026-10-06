; =====================================================================
;  run_core.nsh -- 執行安裝核心，並把它的進度顯示在安裝視窗上
;  （installer.nsi 引入；需要 LogicLib）
; =====================================================================
; ---- 執行安裝核心，同時把它的狀態顯示在安裝視窗上 ------------------------
; 原本用 `nsExec::Exec` 執行（等它跑完、不收輸出），於是十幾二十分鐘裡安裝視窗
; 只有兩行固定的字。**不可以改回 `nsExec::ExecToLog`**（會讓安裝程式在最後一步
; 當掉，上游 bug #1323，見 installer.nsi 的 DoInstall 那段說明）。
;
; 現在改成在背景啟動 PowerShell（`CreateProcessW`，不開主控台視窗），每半秒：
; 等它一下 → 讀一次狀態檔 → 內容有變就顯示。狀態檔由 install_core.ps1 以
; **UTF-16LE** 寫、這裡用 **`FileReadUTF16LE`** 讀 —— 整條路都是 Unicode，
; 不經過系統的 ANSI 字碼頁，所以中文與日文不會變亂碼（之前的亂碼就是卡在那一關）。
;
; 輸入：$R5 = 完整的命令列。輸出：$1 = 離開碼。
; 檢查：tests/test_installer_progress_status.py
Function RunInstallCore
  StrCpy $R9 ""                          ; 上一次顯示過的狀態
  Delete "$PLUGINSDIR\status.txt"
  System::Alloc 68                       ; STARTUPINFOW（32 位元 68 bytes；Alloc 會清零）
  Pop $R6
  System::Call '*$R6(i 68)'
  System::Alloc 16                       ; PROCESS_INFORMATION
  Pop $R7
  ; 0x08000000 = CREATE_NO_WINDOW
  System::Call 'kernel32::CreateProcessW(p 0, w R5, p 0, p 0, i 0, i 0x08000000, p 0, p 0, p R6, p R7) i .R4'
  ${If} $R4 == 0
    ; 啟動不起來就照原本的方式跑（看不到進度，但安裝照樣完成）
    System::Free $R6
    System::Free $R7
    nsExec::Exec $R5
    Pop $1
    Return
  ${EndIf}
  System::Call '*$R7(p .R2, p .R3, i, i)'
  ${Do}
    ; 258 = WAIT_TIMEOUT（還在跑）
    System::Call 'kernel32::WaitForSingleObject(p R2, i 500) i .R4'
    Call ShowInstallStatus
  ${LoopWhile} $R4 = 258
  System::Call 'kernel32::GetExitCodeProcess(p R2, *i .R4)'
  StrCpy $1 $R4
  System::Call 'kernel32::CloseHandle(p R2)'
  System::Call 'kernel32::CloseHandle(p R3)'
  System::Free $R6
  System::Free $R7
FunctionEnd

; 讀狀態檔，跟上一次不同才顯示。第一個字元是種類：
; S＝換到下一步（清單多一行）、P＝同一步的進度（只更新清單上方那一行，不洗版）。
Function ShowInstallStatus
  ClearErrors
  FileOpen $R1 "$PLUGINSDIR\status.txt" r
  ${If} ${Errors}
    Return
  ${EndIf}
  FileReadUTF16LE $R1 $R0
  FileClose $R1
  ${If} $R0 == ""
    Return
  ${EndIf}
  ${If} $R0 S== $R9
    Return
  ${EndIf}
  StrCpy $R9 $R0
  StrCpy $R8 $R0 1
  StrCpy $R0 $R0 "" 1
  ${If} $R8 == "P"
    SetDetailsPrint textonly
  ${EndIf}
  DetailPrint "$R0"
  !ifdef STATUS_ECHO_FILE
    ; 只有測試編譯（makensis -DSTATUS_ECHO_FILE=…）才有：把畫面上顯示的每一行也寫進檔案，
    ; 讓沒有桌面的實機測試（SSH、無介面安裝）也看得到畫面上會出現什麼。
    FileOpen $R1 "${STATUS_ECHO_FILE}" a
    FileSeek $R1 0 END
    FileWriteUTF16LE $R1 "$R8$R0$\r$\n"
    FileClose $R1
  !endif
  SetDetailsPrint both
FunctionEnd
