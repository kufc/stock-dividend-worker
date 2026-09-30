; AI 媒體分類器 - Windows 安裝程式（NSIS）。由 installer/build_installer.py 產生 setup.exe。
;
; 安全設計
;  - 每位使用者安裝（不需要系統管理員權限），裝在 %LOCALAPPDATA%\Programs\AI Media Sorter
;  - 安裝資料夾一定會落在名為「AI Media Sorter」的資料夾裡（就算選了 C:\ 或「文件」，也只會建在它底下的子資料夾）
;  - 解除安裝只刪除安裝程式放進去的兩個資料夾（python、app）與少數檔案，最後用「非遞迴」方式移除安裝資料夾，
;    所以不會刪到使用者自己放在裡面的其他東西
;  - 使用者的設定、整理紀錄、AI 模型預設全部保留，要在解除安裝時各自勾選才會移除
;  - 只寫入 HKCU（目前使用者），並且完整移除；不註冊服務、不開機自動啟動
Target amd64-unicode
Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "x64.nsh"
!include "nsDialogs.nsh"

!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!ifndef STAGE
  !error "請用 installer/build_installer.py 建置（需要 /DSTAGE=暫存資料夾）"
!endif
!ifndef OUTFILE
  !define OUTFILE "AI-Media-Sorter-Setup-${VERSION}.exe"
!endif
!ifndef PUBLISHER
  !define PUBLISHER "AI Media Sorter"
!endif

!define APPNAME "AI 媒體分類器"
!define APPDIR "AI Media Sorter"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\AIMediaSorter"
!define APP_KEY "Software\AIMediaSorter"
!define LICENSE_FILE "使用說明與免責聲明_Usage-and-Disclaimer.txt"

!ifdef SIGN_CMD
  !finalize '${SIGN_CMD}'
  !uninstfinalize '${SIGN_CMD}'
!endif

Name "${APPNAME}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\${APPDIR}"
InstallDirRegKey HKCU "${APP_KEY}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
ManifestDPIAware true
BrandingText "${APPNAME} ${VERSION}"
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "FileDescription" "${APPNAME} Setup"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "CompanyName" "${PUBLISHER}"
VIAddVersionKey "LegalCopyright" "${PUBLISHER}"

!define MUI_ICON "app.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "wizard.bmp"
!define MUI_UNWELCOMEFINISHPAGE_BITMAP "wizard.bmp"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_RIGHT
!define MUI_HEADERIMAGE_BITMAP "header.bmp"
!define MUI_HEADERIMAGE_UNBITMAP "header.bmp"
!define MUI_UNICON "app.ico"
!define MUI_ABORTWARNING
!define MUI_UNABORTWARNING
!define MUI_LICENSEPAGE_CHECKBOX
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchApp

Var DataDir
Var SkipDeps
Var UnDlg
Var UnCbSettings
Var UnCbLogs
Var UnCbModel
Var UnRmSettings
Var UnRmLogs
Var UnRmModel
Var UnActive

!insertmacro GetParameters
!insertmacro GetOptions
!insertmacro GetFileName
!insertmacro GetSize
!insertmacro un.GetParameters
!insertmacro un.GetOptions

; ---------------------------------------------------------------- 頁面
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${STAGE}\license.txt"
!insertmacro MUI_PAGE_COMPONENTS
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE DirLeave
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_PAGE_CUSTOMFUNCTION_SHOW FinishShow
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
UninstPage custom un.DataPageCreate un.DataPageLeave
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "TradChinese"
!insertmacro MUI_LANGUAGE "English"

; ---------------------------------------------------------------- 文字
LangString SEC_MAIN ${LANG_TRADCHINESE} "AI 媒體分類器（必要）"
LangString SEC_MAIN ${LANG_ENGLISH} "AI Media Sorter (required)"
LangString SEC_DESKTOP ${LANG_TRADCHINESE} "桌面捷徑"
LangString SEC_DESKTOP ${LANG_ENGLISH} "Desktop shortcut"
LangString DESC_MAIN ${LANG_TRADCHINESE} "程式本體、內建的 Python，以及安裝時下載的 PyTorch 與 AI 模型（需要網路，約數 GB）。"
LangString DESC_MAIN ${LANG_ENGLISH} "The program, its bundled Python, and PyTorch plus the AI model downloaded during setup (needs internet, several GB)."
LangString DESC_DESKTOP ${LANG_TRADCHINESE} "在桌面建立捷徑。"
LangString DESC_DESKTOP ${LANG_ENGLISH} "Create a shortcut on the desktop."
LangString MSG_NEED64 ${LANG_TRADCHINESE} "這個程式需要 64 位元的 Windows。"
LangString MSG_NEED64 ${LANG_ENGLISH} "This program requires 64-bit Windows."
LangString MSG_RUNNING ${LANG_TRADCHINESE} "AI 媒體分類器正在執行中。請先關閉程式視窗，再按「重試」。"
LangString MSG_RUNNING ${LANG_ENGLISH} "AI Media Sorter is running. Close its window, then click Retry."
LangString MSG_DEPS ${LANG_TRADCHINESE} "正在下載並安裝 PyTorch 與 AI 模型（需要網路，可能要幾分鐘到幾十分鐘）。請看另一個黑色視窗的進度，不要關閉它⋯"
LangString MSG_DEPS ${LANG_ENGLISH} "Downloading and installing PyTorch and the AI model (needs internet, may take minutes to tens of minutes). Watch the black window for progress and do not close it..."
LangString MSG_DEPS_SKIP ${LANG_TRADCHINESE} "略過 AI 元件的下載（/SKIPDEPS）。"
LangString MSG_DEPS_SKIP ${LANG_ENGLISH} "Skipping the AI components download (/SKIPDEPS)."
LangString MSG_DEPS_FAIL ${LANG_TRADCHINESE} "AI 元件沒有安裝完成（詳細原因在剛才的黑色視窗裡）。$\r$\n$\r$\n按「重試」再試一次；按「取消」先完成安裝，之後可以從開始功能表執行「修復」補裝（在那之前程式還不能使用）。"
LangString MSG_DEPS_FAIL ${LANG_ENGLISH} "The AI components were not installed completely (details were in the black window).$\r$\n$\r$\nClick Retry to try again, or Cancel to finish setup now and complete it later with the Repair entry in the Start menu (the program cannot be used until then)."
LangString MSG_DEPS_LATER ${LANG_TRADCHINESE} "AI 元件尚未安裝完成；請從開始功能表執行「修復」補裝。"
LangString MSG_DEPS_LATER ${LANG_ENGLISH} "The AI components are not installed yet; run Repair from the Start menu to complete."
LangString LNK_APP ${LANG_TRADCHINESE} "${APPNAME}"
LangString LNK_APP ${LANG_ENGLISH} "AI Media Sorter"
LangString LNK_REPAIR ${LANG_TRADCHINESE} "修復（重新下載 AI 元件）"
LangString LNK_REPAIR ${LANG_ENGLISH} "Repair (re-download AI components)"
LangString LNK_HELP ${LANG_TRADCHINESE} "使用說明與免責聲明"
LangString LNK_HELP ${LANG_ENGLISH} "User Guide and Disclaimer"
LangString LNK_UNINST ${LANG_TRADCHINESE} "解除安裝"
LangString LNK_UNINST ${LANG_ENGLISH} "Uninstall"
LangString UN_BADDIR ${LANG_TRADCHINESE} "在這個資料夾找不到 AI 媒體分類器的安裝檔案，為了安全不會刪除任何東西。"
LangString UN_BADDIR ${LANG_ENGLISH} "AI Media Sorter's files were not found in this folder, so nothing will be deleted."
LangString UN_INTRO ${LANG_TRADCHINESE} "程式檔案會被移除。你的照片、影片與整理輸出的資料夾都不會被動到。$\r$\n以下是你的個人資料，預設保留；要一起移除請勾選："
LangString UN_INTRO ${LANG_ENGLISH} "The program files will be removed. Your photos, videos and output folders are never touched.$\r$\nThe items below are your personal data and are kept by default; tick them to remove them too:"
LangString UN_CB_SETTINGS ${LANG_TRADCHINESE} "我的設定與分類清單"
LangString UN_CB_SETTINGS ${LANG_ENGLISH} "My settings and category list"
LangString UN_CB_LOGS ${LANG_TRADCHINESE} "整理紀錄與錯誤紀錄（移除後，就無法再用本程式移除複本或處理原檔）"
LangString UN_CB_LOGS ${LANG_ENGLISH} "Organize records and error logs (without them the program can no longer remove copies or handle originals)"
LangString UN_CB_MODEL ${LANG_TRADCHINESE} "AI 模型檔案（Hugging Face 快取；可能跟其他程式共用，下次使用要重新下載）"
LangString UN_CB_MODEL ${LANG_ENGLISH} "AI model files (Hugging Face cache; may be shared with other programs and must be downloaded again next time)"
LangString UN_ACTIVE_PRE ${LANG_TRADCHINESE} "注意：目前有 "
LangString UN_ACTIVE_PRE ${LANG_ENGLISH} "Warning: there are "
LangString UN_ACTIVE_POST ${LANG_TRADCHINESE} " 份整理紀錄還沒處理完；勾選「整理紀錄」後就無法再用本程式移除那些複本或原檔（檔案本身不受影響）。"
LangString UN_ACTIVE_POST ${LANG_ENGLISH} " organize records that are not finished; if you remove the records, this program can no longer remove those copies or originals (the files themselves are not affected)."
LangString FIN_NODEPS ${LANG_TRADCHINESE} "AI 元件（PyTorch 與 AI 模型）還沒有安裝完成，程式暫時無法使用。$\r$\n$\r$\n請確認網路連線後，從開始功能表執行「AI 媒體分類器 → 修復（重新下載 AI 元件）」。"
LangString FIN_NODEPS ${LANG_ENGLISH} "The AI components (PyTorch and the AI model) are not installed yet, so the program cannot be used for now.$\r$\n$\r$\nCheck your internet connection, then run Start menu > AI 媒體分類器 > Repair."
LangString UN_LEFT ${LANG_TRADCHINESE} "有些檔案無法移除（可能被其他程式使用中）。請關閉相關程式後手動刪除這個資料夾："
LangString UN_LEFT ${LANG_ENGLISH} "Some files could not be removed (they may be in use). Close other programs and delete this folder manually:"


; ---------------------------------------------------------------- 共用函式
Function NormalizeInstDir
  ; 去掉結尾的 \，並確保最後一層資料夾叫「AI Media Sorter」：不會直接裝進使用者選的資料夾（例如 C:\ 或「文件」）
  StrCpy $0 $INSTDIR 1 -1
  ${If} $0 == "\"
    StrCpy $INSTDIR $INSTDIR -1
  ${EndIf}
  ${GetFileName} $INSTDIR $0
  ${If} $0 != "${APPDIR}"
    StrCpy $INSTDIR "$INSTDIR\${APPDIR}"
  ${EndIf}
FunctionEnd

Function DirLeave
  Call NormalizeInstDir
FunctionEnd

Function SetPyEnv
  ; 讓內建的 Python 不論目前的工作資料夾在哪裡，都找得到程式（不能把工作資料夾設在要被刪除的資料夾裡）
  System::Call 'Kernel32::SetEnvironmentVariable(t "PYTHONPATH", t "$INSTDIR\app") i.r0'
  System::Call 'Kernel32::SetEnvironmentVariable(t "PYTHONUTF8", t "1") i.r0'
FunctionEnd

Function un.SetPyEnv
  System::Call 'Kernel32::SetEnvironmentVariable(t "PYTHONPATH", t "$INSTDIR\app") i.r0'
  System::Call 'Kernel32::SetEnvironmentVariable(t "PYTHONUTF8", t "1") i.r0'
FunctionEnd

Function CheckNotRunning
  ; 升級或修復時，程式若還開著，檔案會被鎖住；用程式自己的鎖檔判斷
  ${IfNot} ${FileExists} "$INSTDIR\python\python.exe"
    Return
  ${EndIf}
  ${IfNot} ${FileExists} "$INSTDIR\app\media_sorter\uninstaller.py"
    Return
  ${EndIf}
  Call SetPyEnv
  retry:
  nsExec::ExecToStack '"$INSTDIR\python\python.exe" -m media_sorter.uninstaller --check-running --data-dir "$DataDir"'
  Pop $0
  Pop $1
  ${If} $0 == 42 ; 42＝程式正在執行
    ${If} ${Silent}
      SetErrorLevel 5
      Abort
    ${EndIf}
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "$(MSG_RUNNING)" IDRETRY retry
    Abort
  ${EndIf}
FunctionEnd

Function FinishShow
  ${IfNot} ${FileExists} "$INSTDIR\app\install-ok.txt"
    SendMessage $mui.FinishPage.Run ${BM_SETCHECK} ${BST_UNCHECKED} 0
    ShowWindow $mui.FinishPage.Run ${SW_HIDE}
    SendMessage $mui.FinishPage.Text ${WM_SETTEXT} 0 "STR:$(FIN_NODEPS)"
  ${EndIf}
FunctionEnd

Function LaunchApp
  ${IfNot} ${FileExists} "$INSTDIR\app\install-ok.txt"
    Return
  ${EndIf}
  SetOutPath "$INSTDIR\app"
  Exec '"$INSTDIR\python\pythonw.exe" -m media_sorter'
FunctionEnd

; ---------------------------------------------------------------- 安裝
Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "$(MSG_NEED64)"
    Abort
  ${EndIf}
  StrCpy $DataDir "$LOCALAPPDATA\${APPDIR}"
  StrCpy $SkipDeps "0"
  ${GetParameters} $R0
  ${GetOptions} $R0 "/SKIPDEPS" $R1
  ${IfNot} ${Errors}
    StrCpy $SkipDeps "1"
  ${EndIf}
  ClearErrors
  Call NormalizeInstDir
FunctionEnd

Section "$(SEC_MAIN)" SecMain
  SectionIn RO
  AddSize 7340032 ; 安裝時下載的 PyTorch（CUDA 版約 3~4 GB）與 AI 模型（1.5~5 GB），約 7 GB
  Call CheckNotRunning
  SetOutPath "$INSTDIR"
  ; 內建的 Python 執行環境（含 tkinter）；程式本體；解除安裝程式
  File /r "${STAGE}\python"
  RMDir /r "$INSTDIR\app\media_sorter" ; 只清掉舊版的程式碼；使用者資料不在這裡
  SetOutPath "$INSTDIR\app"
  File /r "${STAGE}\app\*.*"
  SetOutPath "$INSTDIR"
  File "app.ico"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; 「設定 → 應用程式」裡的項目（只寫目前使用者）
  WriteRegStr HKCU "${APP_KEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\app.ico"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1

  ; 開始功能表
  SetOutPath "$INSTDIR\app"
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\$(LNK_APP).lnk" "$INSTDIR\python\pythonw.exe" "-m media_sorter" "$INSTDIR\app.ico" 0
  CreateShortCut "$SMPROGRAMS\${APPNAME}\$(LNK_REPAIR).lnk" "$INSTDIR\app\setup-deps.cmd" "pause" "$INSTDIR\app.ico" 0
  CreateShortCut "$SMPROGRAMS\${APPNAME}\$(LNK_HELP).lnk" "$INSTDIR\app\${LICENSE_FILE}"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\$(LNK_UNINST).lnk" "$INSTDIR\uninstall.exe"

  ; 下載並安裝 PyTorch 與 AI 模型（沿用程式內的安裝邏輯：依顯示卡選 CUDA 版本）
  ${If} $SkipDeps == "1"
    DetailPrint "$(MSG_DEPS_SKIP)"
  ${Else}
    deps:
    DetailPrint "$(MSG_DEPS)"
    ${If} ${Silent}
      nsExec::ExecToLog '"$SYSDIR\cmd.exe" /c ""$INSTDIR\app\setup-deps.cmd" nopause"'
      Pop $0
    ${Else}
      ExecWait '"$SYSDIR\cmd.exe" /c ""$INSTDIR\app\setup-deps.cmd""' $0
    ${EndIf}
    ${If} $0 != 0
      ${If} ${Silent}
        SetErrorLevel 2
        DetailPrint "$(MSG_DEPS_LATER)"
      ${Else}
        MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "$(MSG_DEPS_FAIL)" IDRETRY deps
        DetailPrint "$(MSG_DEPS_LATER)"
      ${EndIf}
    ${EndIf}
  ${EndIf}

  ; 「設定 → 應用程式」顯示的大小
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" $0
SectionEnd

Section /o "$(SEC_DESKTOP)" SecDesktop
  SetOutPath "$INSTDIR\app"
  CreateShortCut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\python\pythonw.exe" "-m media_sorter" "$INSTDIR\app.ico" 0
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} "$(DESC_MAIN)"
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} "$(DESC_DESKTOP)"
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ---------------------------------------------------------------- 解除安裝
Function un.onInit
  StrCpy $DataDir "$LOCALAPPDATA\${APPDIR}"
  StrCpy $UnRmSettings "0"
  StrCpy $UnRmLogs "0"
  StrCpy $UnRmModel "0"
  ; 只有確實是安裝程式裝出來的資料夾才會動作
  ${IfNot} ${FileExists} "$INSTDIR\app\installed.flag"
    MessageBox MB_OK|MB_ICONSTOP "$(UN_BADDIR)"
    SetErrorLevel 4
    Abort
  ${EndIf}
  ; 靜默解除安裝：預設保留所有個人資料；要移除請加 /REMOVE_SETTINGS /REMOVE_LOGS /REMOVE_MODEL
  ${un.GetParameters} $R0
  ${un.GetOptions} $R0 "/REMOVE_SETTINGS" $R1
  ${IfNot} ${Errors}
    StrCpy $UnRmSettings "1"
  ${EndIf}
  ClearErrors
  ${un.GetOptions} $R0 "/REMOVE_LOGS" $R1
  ${IfNot} ${Errors}
    StrCpy $UnRmLogs "1"
  ${EndIf}
  ClearErrors
  ${un.GetOptions} $R0 "/REMOVE_MODEL" $R1
  ${IfNot} ${Errors}
    StrCpy $UnRmModel "1"
  ${EndIf}
  ClearErrors
  ; 程式開著就不解除安裝
  ${If} ${FileExists} "$INSTDIR\python\python.exe"
    Call un.SetPyEnv
    retry:
    nsExec::ExecToStack '"$INSTDIR\python\python.exe" -m media_sorter.uninstaller --check-running --data-dir "$DataDir"'
    Pop $0
    Pop $1
    ${If} $0 == 42
    ${AndIfNot} ${Silent}
      MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "$(MSG_RUNNING)" IDRETRY retry
      Abort
    ${EndIf}
  ${EndIf}
FunctionEnd

Function un.CheckNotRunning
  ; 解除安裝真正開始前再檢查一次（靜默模式只有在這裡中止，結束代碼 5 才會帶出去）
  ${IfNot} ${FileExists} "$INSTDIR\python\python.exe"
    Return
  ${EndIf}
  Call un.SetPyEnv
  retry:
  nsExec::ExecToStack '"$INSTDIR\python\python.exe" -m media_sorter.uninstaller --check-running --data-dir "$DataDir"'
  Pop $0
  Pop $1
  ${If} $0 == 42
    ${IfNot} ${Silent}
      MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "$(MSG_RUNNING)" IDRETRY retry
    ${EndIf}
    ; 解除安裝程式用 Abort 中止時不會帶出 SetErrorLevel 的代碼，所以用 Quit（同樣不會刪任何東西）
    SetErrorLevel 5
    Quit
  ${EndIf}
FunctionEnd

Function un.DataPageCreate
  nsDialogs::Create 1018
  Pop $UnDlg
  ${If} $UnDlg == error
    Abort
  ${EndIf}
  ${NSD_CreateLabel} 0 0 100% 30u "$(UN_INTRO)"
  Pop $0
  ${NSD_CreateCheckbox} 8u 34u -8u 12u "$(UN_CB_SETTINGS)"
  Pop $UnCbSettings
  ${NSD_CreateCheckbox} 8u 50u -8u 20u "$(UN_CB_LOGS)"
  Pop $UnCbLogs
  ${NSD_AddStyle} $UnCbLogs ${BS_MULTILINE}
  ${NSD_CreateCheckbox} 8u 74u -8u 20u "$(UN_CB_MODEL)"
  Pop $UnCbModel
  ${NSD_AddStyle} $UnCbModel ${BS_MULTILINE}
  StrCpy $UnActive "0"
  ${If} ${FileExists} "$INSTDIR\python\python.exe"
    nsExec::ExecToStack '"$INSTDIR\python\python.exe" -m media_sorter.uninstaller --active-logs --data-dir "$DataDir"'
    Pop $0
    Pop $1
    ${If} $0 > 0
    ${AndIf} $0 < 1000
      StrCpy $UnActive $0
    ${EndIf}
  ${EndIf}
  ${If} $UnActive != "0"
    ${NSD_CreateLabel} 0 100u 100% 30u "$(UN_ACTIVE_PRE)$UnActive$(UN_ACTIVE_POST)"
    Pop $0
  ${EndIf}
  nsDialogs::Show
FunctionEnd

Function un.DataPageLeave
  ${NSD_GetState} $UnCbSettings $UnRmSettings
  ${NSD_GetState} $UnCbLogs $UnRmLogs
  ${NSD_GetState} $UnCbModel $UnRmModel
FunctionEnd

Section "Uninstall"
  Call un.CheckNotRunning
  ; 1) 使用者資料：只處理有勾選的項目，由程式內的解除安裝邏輯（有各種安全檢查）執行
  StrCpy $R2 ""
  ${If} $UnRmSettings == "1"
    StrCpy $R2 "$R2 --remove-settings"
  ${EndIf}
  ${If} $UnRmLogs == "1"
    StrCpy $R2 "$R2 --remove-logs"
  ${EndIf}
  ${If} $UnRmModel == "1"
    StrCpy $R2 "$R2 --remove-model"
  ${EndIf}
  ${If} $R2 != ""
  ${AndIf} ${FileExists} "$INSTDIR\python\python.exe"
    nsExec::ExecToLog '"$INSTDIR\python\python.exe" -m media_sorter.uninstaller --data-only --data-dir "$DataDir" --yes$R2'
    Pop $0
  ${EndIf}

  ; 2) 捷徑（只刪自己建立的檔案）
  Delete "$DESKTOP\${APPNAME}.lnk"
  Delete "$SMPROGRAMS\${APPNAME}\$(LNK_APP).lnk"
  Delete "$SMPROGRAMS\${APPNAME}\$(LNK_REPAIR).lnk"
  Delete "$SMPROGRAMS\${APPNAME}\$(LNK_HELP).lnk"
  Delete "$SMPROGRAMS\${APPNAME}\$(LNK_UNINST).lnk"
  RMDir "$SMPROGRAMS\${APPNAME}"

  ; 3) 程式檔案：只刪安裝程式放進去的兩個資料夾與兩個檔案；安裝資料夾本身用「非遞迴」方式移除（不是空的就保留）
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\app"
  Delete "$INSTDIR\app.ico"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"

  ; 4) 登錄檔
  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "${APP_KEY}"

  ${If} ${FileExists} "$INSTDIR\python"
  ${OrIf} ${FileExists} "$INSTDIR\app"
    SetErrorLevel 3
    ${IfNot} ${Silent}
      MessageBox MB_OK|MB_ICONEXCLAMATION "$(UN_LEFT)$\r$\n$INSTDIR"
    ${EndIf}
  ${EndIf}
SectionEnd
