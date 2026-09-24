# =====================================================================
#  install_core.ps1  --  jt-doc-tools Windows GUI installer core logic
# ---------------------------------------------------------------------
#  This is a STANDALONE, NON-INTERACTIVE installer core invoked by the
#  NSIS GUI installer (installer.nsi). It is intentionally SEPARATE from
#  the command-line install.ps1 (curl|iex one-liner) so that the two
#  install paths evolve independently and changes here can never destabilise
#  the existing one-liner flow.
#
#  Differences vs install.ps1:
#    * Parameter driven (NSIS passes InstallDir / component switches)
#    * No interactive prompts (no Read-Host, no "Press Enter") -- NSIS runs
#      it headless and captures stdout into the wizard details pane.
#    * Optional components (OCR / Office / Service / Firewall) are gated by
#      switches so the user's component-page choices are honoured.
#    * Non-zero exit code on fatal error so NSIS can detect failure.
#
#  Exit codes:
#    0  success
#    10 admin privileges missing
#    11 no internet
#    12 unsupported arch (32-bit)
#    20 uv install failed
#    21 source fetch failed
#    22 python env setup failed
#    23 winsw install failed
#    24 service start failed
# =====================================================================

param(
    [string]$InstallDir    = (Join-Path ${env:ProgramFiles} 'jt-doc-tools'),
    [string]$DataDir       = (Join-Path (Join-Path ${env:ProgramData} 'jt-doc-tools') 'Data'),
    [string]$BindHost      = '127.0.0.1',
    [int]   $Port          = 8765,
    [switch]$InstallOcr,        # PyTorch/EasyOCR VC++ redist + tesseract chi_tra
    [switch]$InstallOffice,     # OxOffice / LibreOffice document conversion
    [switch]$InstallService,    # register WinSW Windows service (autostart)
    [switch]$InstallFirewall,   # allow LAN access (binds 0.0.0.0 + firewall rule)
    [string]$RepoUrl       = 'https://github.com/jasoncheng7115/jt-doc-tools',
    [string]$RepoBranch    = 'main'
)

# Continue on native-command stderr; we judge native failures by $LASTEXITCODE
# and cmdlet failures via try/catch -- same rationale as install.ps1.
$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'

# Allow env override for pre-release testing against a local file:// mirror.
if ($env:JTDT_REPO_URL)    { $RepoUrl    = $env:JTDT_REPO_URL }
if ($env:JTDT_REPO_BRANCH) { $RepoBranch = $env:JTDT_REPO_BRANCH }

$ServiceName = 'jt-doc-tools'
$ProgData    = ${env:ProgramData}
$LogDir      = Join-Path (Join-Path $ProgData 'jt-doc-tools') 'Logs'
$BinDir      = Join-Path $InstallDir 'bin'
$NssmExe     = Join-Path $BinDir 'nssm.exe'        # legacy migration detection
$WinswExe    = Join-Path $BinDir 'jtdt-svc.exe'    # must match XML basename
$WinswXml    = Join-Path $BinDir 'jtdt-svc.xml'
$UvExe       = Join-Path $BinDir 'uv.exe'
$CliShim     = Join-Path $InstallDir 'jtdt.cmd'

# --- logging (stdout for NSIS details pane + a log file) -------------
$null = New-Item -ItemType Directory -Force -Path $LogDir -ErrorAction SilentlyContinue
$InstallLog = Join-Path $LogDir 'installer.log'
function _w($pfx, $m, $col) {
    $line = "$pfx $m"
    Write-Host $line -ForegroundColor $col
    try { Add-Content -Path $InstallLog -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue } catch {}
}
function Log  ($m) { _w '==>'  $m 'Cyan'   }
function Ok   ($m) { _w '[OK]' $m 'Green'  }
function Warn ($m) { _w '[!] ' $m 'Yellow' }
function Die  ($m, $code) {
    _w '[X] ' $m 'Red'
    exit $code
}

# --- winget：**輸出一律導進記錄檔，不要流進安裝畫面** ----------------
# winget 送出的是 **UTF-8**，而 NSIS 的 `nsExec::ExecToLog` 是用**系統的
# ANSI 字碼頁**去解（繁中 Windows 是 CP950）—— 於是安裝畫面上出現一整片
# 亂碼（2026-09-15 客戶回報，Win11 25H2 的截圖）。
#
# **修法不是去轉編碼**：那一片內容本來就是 winget 的授權條款與進度動畫，
# 對使用者沒有意義，能讀也只是雜訊。改成全部寫進 `installer.log`
# （要查的時候還在），畫面上只留我們自己的一行英文狀態。
#
# 回傳 winget 的離開碼。
function Invoke-Winget([string]$PackageId, [string]$What) {
    $out = Join-Path $env:TEMP ("jtdt-winget-{0}.log" -f [guid]::NewGuid())
    $err = "$out.err"
    try {
        $args = "install --id $PackageId -e --silent " +
                "--accept-package-agreements --accept-source-agreements"
        $proc = Start-Process winget -ArgumentList $args -Wait -PassThru -NoNewWindow `
                    -RedirectStandardOutput $out -RedirectStandardError $err `
                    -ErrorAction SilentlyContinue
        foreach ($f in @($out, $err)) {
            if (Test-Path $f) {
                try {
                    # winget 寫的是 UTF-8 —— 讀的時候就要照 UTF-8 讀，
                    # 不然存進記錄檔的也是亂碼。
                    $txt = [System.IO.File]::ReadAllText($f, [System.Text.Encoding]::UTF8)
                    if ($txt.Trim()) {
                        Add-Content -Path $InstallLog -Encoding UTF8 `
                            -Value ("--- winget $PackageId ($(Split-Path $f -Leaf)) ---`r`n$txt")
                    }
                } catch {}
            }
        }
        return $(if ($proc) { $proc.ExitCode } else { 1 })
    } finally {
        foreach ($f in @($out, $err)) {
            Remove-Item $f -Force -ErrorAction SilentlyContinue
        }
    }
}

# --- preflight ------------------------------------------------------
$ident = [Security.Principal.WindowsIdentity]::GetCurrent()
$prin  = New-Object Security.Principal.WindowsPrincipal($ident)
if (-not $prin.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Die 'Administrator privileges required.' 10
}

if (-not [Environment]::Is64BitOperatingSystem) { Die '32-bit Windows is not supported.' 12 }
# Report the real machine arch. NOTE: this script may be launched by the 32-bit
# NSIS installer, so prefer PROCESSOR_ARCHITEW6432 (the 64-bit arch seen from a
# WOW64 process) and fall back to PROCESSOR_ARCHITECTURE. Covers both AMD64 (x64)
# and ARM64. uv selects the matching managed Python automatically.
$Arch = $env:PROCESSOR_ARCHITEW6432
if (-not $Arch) { $Arch = $env:PROCESSOR_ARCHITECTURE }
if (-not $Arch) { $Arch = 'x86_64' }
$IsArm64 = ($Arch -match 'ARM64')

# Real 64-bit "Program Files" -- $env:ProgramFiles resolves to the x86 path when
# this script is spawned by the 32-bit NSIS installer, so use ProgramW6432 which
# always points to the native 64-bit Program Files (x64 and ARM64 alike).
$PF64 = $env:ProgramW6432
if (-not $PF64) { $PF64 = $env:ProgramFiles }
$PFx86 = ${env:ProgramFiles(x86)}

function Test-Internet {
    foreach ($h in @('github.com', 'cdn.jsdelivr.net', 'astral.sh')) {
        try {
            $r = Invoke-WebRequest -Uri "https://$h" -Method Head -UseBasicParsing -TimeoutSec 8 -ErrorAction Stop
            if ($r.StatusCode -ge 200) { return $true }
        } catch {}
    }
    return $false
}
Log 'Checking network ...'
if (-not (Test-Internet)) {
    Die 'Cannot reach the internet (github.com / cdn.jsdelivr.net / astral.sh). Check VPN / firewall / DNS and retry.' 11
}
Ok 'Network reachable'

Log "Jason Tools Document Toolbox - GUI installer core"
Log "Platform:  Windows ($Arch)"
Log "Program:   $InstallDir"
Log "Data:      $DataDir"
Log "Bind:      $BindHost`:$Port"
Log ("Components: OCR={0} Office={1} Service={2} Firewall={3}" -f `
        [bool]$InstallOcr, [bool]$InstallOffice, [bool]$InstallService, [bool]$InstallFirewall)

# =====================================================================
#  Office (optional component)
# =====================================================================
function Test-Office {
    # Use $PF64 (real 64-bit Program Files) -- $env:ProgramFiles would be the x86
    # path here because NSIS spawns this script as a WOW64 child.
    $paths = @(
        "$PF64\OxOffice\program\soffice.exe",
        "$PF64\LibreOffice\program\soffice.exe",
        "$PFx86\OxOffice\program\soffice.exe",
        "$PFx86\LibreOffice\program\soffice.exe"
    )
    foreach ($p in $paths) { if ($p -and (Test-Path $p)) { return $true } }
    if (Get-Command soffice.exe -ErrorAction SilentlyContinue) { return $true }
    return $false
}
# OxOffice 的 MSI 約 400 MB，而且放在 GitHub 的發行檔案上 —— 有些網路到那裡很慢，
# 單一連線一路下載要四十幾分鐘。慢網路上 Invoke-WebRequest 可能交回一份**不完整**的檔案
# 而不報錯，msiexec 對它回 1625「系統原則禁止這項安裝」（事件記錄 1008：「物件無法被信任」）
# —— 看起來像權限問題，其實是檔案壞了（Win10 實機踩到）。
# 所以下載完要驗：大小要等於 GitHub 回報的大小、簽章不可以是「內容被改過」，
# 不合就重下，最多三次。檢查：tests/test_oxoffice_msi_selection.py
function Save-VerifiedMsi($url, $size, $dst) {
    for ($i = 1; $i -le 3; $i++) {
        Remove-Item $dst -Force -ErrorAction SilentlyContinue
        Log ("Downloading {0} ({1:N0} MB, attempt {2}/3)" -f $url, ($size / 1MB), $i)
        try {
            Invoke-WebRequest -Uri $url -OutFile $dst -UseBasicParsing -ErrorAction Stop
        } catch {
            Warn "Download failed: $_"
            continue
        }
        $got = 0
        if (Test-Path $dst) { $got = (Get-Item $dst).Length }
        if ($size -and $got -ne $size) {
            Warn "Downloaded $got of $size bytes (incomplete), retrying"
            continue
        }
        $sig = Get-AuthenticodeSignature $dst
        if ($sig.Status -eq 'HashMismatch') {
            Warn 'Downloaded MSI is damaged (signature hash mismatch), retrying'
            continue
        }
        return $true
    }
    Remove-Item $dst -Force -ErrorAction SilentlyContinue
    return $false
}

function Install-OxOffice {
    Log 'Trying OxOffice from GitHub release ...'
    try {
        $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/OSSII/OxOffice/releases/latest' -Headers @{ 'User-Agent' = 'jt-doc-tools-installer' }
        # 只收 64 位元的 MSI。OxOffice 的檔名是 `OxOffice_x86_64-11.0.5.msi` 與
        # `OxOffice_x86-11.0.5.msi`（32 位元）。原本比對 `win|Windows|x64`，
        # 兩個都比不到（`x86_64` 裡沒有連續的 `x64`），於是一律退到 winget 裝
        # LibreOffice —— OxOffice 從來沒有被自動裝上過。
        # 檢查：tests/test_oxoffice_msi_selection.py
        $asset = $rel.assets | Where-Object { $_.name -match '\.msi$' -and $_.name -match '(x86_64|x64|amd64|win64)' } | Select-Object -First 1
        if (-not $asset) { Warn 'No Windows MSI asset found for OxOffice'; return $false }
        $tmp = Join-Path $env:TEMP "oxoffice-install.msi"
        if (-not (Save-VerifiedMsi $asset.browser_download_url $asset.size $tmp)) {
            Warn 'OxOffice download failed three times'; return $false
        }
        Log 'Installing OxOffice (silent) ...'
        $msiLog = Join-Path $LogDir 'oxoffice-msi.log'
        $proc = Start-Process msiexec.exe -ArgumentList "/i `"$tmp`" /qn /norestart /l*v `"$msiLog`"" -Wait -PassThru
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        # 3010 = 裝好了、建議重新開機。soffice 不重開也能用 —— 原本把它當成失敗，
        # 裝好的 OxOffice 被判成失敗、又多裝一套 LibreOffice。
        if ($proc.ExitCode -eq 3010) { Ok 'OxOffice installed (Windows suggests a restart; not needed for conversion)' }
        elseif ($proc.ExitCode -ne 0) { Warn "OxOffice MSI exit code $($proc.ExitCode) (details: $msiLog)"; return $false }
        return Test-Office
    } catch { Warn "OxOffice install failed: $_"; return $false }
}
function Install-LibreOffice {
    Log 'Falling back to LibreOffice via winget ...'
    try {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            $code = Invoke-Winget 'TheDocumentFoundation.LibreOffice' 'LibreOffice'
            $proc = [pscustomobject]@{ ExitCode = $code }
            if ($proc.ExitCode -eq 0) { return Test-Office }
        }
        return $false
    } catch { Warn "LibreOffice install failed: $_"; return $false }
}
function Ensure-Office {
    if (Test-Office) { Ok 'Office engine detected'; return }
    Log 'No OxOffice / LibreOffice detected'
    if (Install-OxOffice)    { Ok 'OxOffice installed';    return }
    if (Install-LibreOffice) { Ok 'LibreOffice installed'; return }
    # Non-fatal: document conversion tools degrade, the rest works.
    Warn 'Office engine could not be installed automatically.'
    Warn '  Install later from https://github.com/OSSII/OxOffice/releases and re-run.'
}

# =====================================================================
#  Tesseract OCR + chi_tra (optional, part of OCR component)
# =====================================================================
function Find-TesseractExe {
    $cmd = Get-Command tesseract -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Path }
    foreach ($c in @(
        "C:\Program Files\Tesseract-OCR\tesseract.exe",
        "C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "$env:LOCALAPPDATA\Programs\Tesseract-OCR\tesseract.exe")) {
        if (Test-Path $c) { return $c }
    }
    return ''
}
function Test-Tesseract {
    $exe = Find-TesseractExe
    if (-not $exe) { return $false }
    $langs = & $exe --list-langs 2>&1 | Out-String
    return ($langs -match 'chi_tra')
}
function Add-TesseractToPath {
    $exe = Find-TesseractExe
    if (-not $exe) { return }
    $dir = Split-Path -Parent $exe
    $cur = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $parts = $cur -split ';' | Where-Object { $_ -ne '' }
    if ($parts -contains $dir) { return }
    try {
        [Environment]::SetEnvironmentVariable('Path', (($parts + $dir) -join ';'), 'Machine')
        $env:Path = "$env:Path;$dir"
        Ok "Tesseract added to system PATH ($dir)"
    } catch { Warn "Could not modify system PATH: $_" }
}
function Ensure-TesseractChiTra {
    $exe = Find-TesseractExe
    if (-not $exe) { return }
    $langs = & $exe --list-langs 2>&1 | Out-String
    if ($langs -match 'chi_tra') { return }
    $tessdataDir = Join-Path (Split-Path -Parent $exe) 'tessdata'
    if (-not (Test-Path $tessdataDir)) { Warn "tessdata dir not found, skip chi_tra"; return }
    $url = 'https://github.com/tesseract-ocr/tessdata_fast/raw/main/chi_tra.traineddata'
    $dst = Join-Path $tessdataDir 'chi_tra.traineddata'
    Log 'Downloading chi_tra.traineddata (~2.4 MB) for Chinese OCR ...'
    try {
        Invoke-WebRequest -Uri $url -OutFile $dst -UseBasicParsing -TimeoutSec 60 -ErrorAction Stop
        if ((Test-Path $dst) -and (Get-Item $dst).Length -gt 1000000) {
            Ok "chi_tra.traineddata downloaded ($([math]::Round((Get-Item $dst).Length/1MB,1)) MB)"
        } else {
            Remove-Item $dst -Force -ErrorAction SilentlyContinue
            Warn 'chi_tra download incomplete, removed'
        }
    } catch { Warn "chi_tra download failed: $_" }
}
function Install-Tesseract {
    $exe = Find-TesseractExe
    if ($exe) {
        Add-TesseractToPath; Ensure-TesseractChiTra
        if (Test-Tesseract) { Ok 'tesseract + chi_tra already installed'; return }
    }
    Log 'Installing tesseract OCR (optional) ...'
    try {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            $code = Invoke-Winget 'UB-Mannheim.TesseractOCR' 'Tesseract'
            $proc = [pscustomobject]@{ ExitCode = $code }
            if ($proc.ExitCode -eq 0) {
                Add-TesseractToPath; Ensure-TesseractChiTra
                if (Test-Tesseract) { Ok 'tesseract installed via winget'; return }
            }
        }
        Warn 'tesseract auto-install failed - OCR text recovery limited (EasyOCR still works)'
    } catch { Warn "tesseract install error: $_ (continuing)" }
}

# =====================================================================
#  Visual C++ Redistributable (PyTorch dep, part of OCR component)
# =====================================================================
# System32 裡**實際的** VC++ 執行階段版本（三個檔案裡最舊的那個）。**不可以只看登錄檔**：
# OxOffice 11.0.5 的 MSI 內含 14.29 的執行階段、安裝模式是 REINSTALLMODE=dmus（版本「不同」
# 就覆蓋，連舊版也蓋上去）—— 裝完 System32 的檔案變成 14.29，登錄檔卻還寫 14.44。
# PyTorch 的 c10.dll 初始化失敗（WinError 1114），EasyOCR 整個不能用（Win10 實機踩到）。
# 這時 vc_redist /install 回 0 卻什麼都不做（它認為已經裝了），要用 /repair 才會把檔案換回來。
# 檢查：tests/test_vc_runtime_repair_after_downgrade.py
function Get-VCRuntimeFileVersion {
    $dir = Join-Path $env:SystemRoot 'System32'
    if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
        $dir = Join-Path $env:SystemRoot 'Sysnative'
    }
    $worst = $null
    foreach ($n in 'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll') {
        $f = Join-Path $dir $n
        $v = [version]'0.0'
        if (Test-Path $f) {
            $pv = (Get-Item $f).VersionInfo.ProductVersion
            if ($pv -match '^(\d+\.\d+(\.\d+){0,2})') { try { $v = [version]$Matches[1] } catch {} }
        }
        if ($null -eq $worst -or $v -lt $worst) { $worst = $v }
    }
    return $worst
}
function Ensure-VCRedist {
    Log 'Checking Visual C++ Redistributable (PyTorch dep) ...'
    $current = ''
    foreach ($k in @('HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64',
                     'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\X64')) {
        if (Test-Path $k) {
            try { $v = (Get-ItemProperty $k).Version; if ($v) { $current = $v; break } } catch {}
        }
    }
    $min = [version]'14.40'
    $regOk = $false
    if ($current -match '^v?(\d+)\.(\d+)') { $regOk = ([version]"$($Matches[1]).$($Matches[2])") -ge $min }
    $files = Get-VCRuntimeFileVersion
    if ($regOk -and $files -ge $min) { Ok "Visual C++ Redistributable already current ($current; System32 $files)"; return }
    if ($regOk) { Warn "System32 runtime files are $files although $current is registered (replaced by another installer); repairing" }
    elseif ($current) { Log "Visual C++ Redistributable is old ($current); upgrading" }
    else { Log 'Visual C++ Redistributable not found; installing' }
    $vc = Join-Path $env:TEMP 'jtdt-vc_redist.x64.exe'
    if (Test-Path $vc) { Remove-Item $vc -Force -ErrorAction SilentlyContinue }
    try {
        Log 'Downloading Microsoft Visual C++ Redistributable (~25 MB) ...'
        Invoke-WebRequest -Uri 'https://aka.ms/vs/17/release/vc_redist.x64.exe' -OutFile $vc -UseBasicParsing -TimeoutSec 60 -ErrorAction Stop
        $action = if ($regOk) { '/repair' } else { '/install' }
        $proc = Start-Process -FilePath $vc -ArgumentList $action,'/quiet','/norestart' -Wait -PassThru -ErrorAction Stop
        $code = $proc.ExitCode
        if ($action -eq '/install' -and (Get-VCRuntimeFileVersion) -lt $min) {
            $proc = Start-Process -FilePath $vc -ArgumentList '/repair','/quiet','/norestart' -Wait -PassThru -ErrorAction Stop
            $code = $proc.ExitCode
        }
        $after = Get-VCRuntimeFileVersion
        # 3010 = done, restart suggested; new processes load the new DLLs without a restart
        if (($code -eq 0 -or $code -eq 3010) -and $after -ge $min) { Ok "Visual C++ Redistributable ready (System32 $after, exit $code)" }
        # 同一次開機裡 vc_redist 已經回過 3010，它就不肯再做任何事（記錄寫 0x8007015e「要先重新開機」），
        # 卻照樣回 3010 —— 只看離開碼會以為修好了
        elseif ($code -eq 3010) { Warn "System32 runtime is still $($after): restart Windows, then run 'jtdt update' to repair it (until then OCR uses tesseract)" }
        else { Warn "vc_redist exit $code, System32 runtime still $after - EasyOCR may fall back to tesseract" }
    } catch { Warn "vc_redist download/install failed: $_ (OCR falls back to tesseract)" }
}

# =====================================================================
#  git / uv / source / winsw / python  (always required)
# =====================================================================
function Install-Git {
    if (Get-Command git -ErrorAction SilentlyContinue) { Ok 'git already installed'; return }
    Log 'git not found; trying winget (required for jtdt update) ...'
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Warn 'winget not available; jtdt update needs git installed manually later'; return
    }
    try {
        $code = Invoke-Winget 'Git.Git' 'git'
        $proc = [pscustomobject]@{ ExitCode = $code }
        if ($proc.ExitCode -eq 0) {
            $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
            if (Get-Command git -ErrorAction SilentlyContinue) { Ok 'git installed via winget'; return }
        }
        Warn 'git winget install finished but git still not found'
    } catch { Warn "git install via winget failed: $_" }
}

function Install-Uv {
    if (Test-Path $UvExe) { Ok 'uv already present'; return }
    Log 'Downloading uv ...'
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    $env:UV_INSTALL_DIR = $BinDir
    $env:UV_NO_MODIFY_PATH = '1'
    Invoke-Expression (Invoke-RestMethod -Uri 'https://astral.sh/uv/install.ps1')
    if (-not (Test-Path $UvExe)) { Die 'uv install failed' 20 }
    Ok "uv installed at $UvExe"
}

$WinswBundledSha256 = 'b5066b7bbdfba1293e5d15cda3caaea88fbeab35bd5b38c41c913d492aadfc4f'
$WinswReleaseUrl    = 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW.NET461.exe'
function Install-Winsw {
    if (Test-Path $WinswExe) { Ok "WinSW already present"; return }
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    $bundled = Join-Path $InstallDir 'packaging\windows\winsw.exe'
    if (Test-Path $bundled) {
        $h = (Get-FileHash -Path $bundled -Algorithm SHA256).Hash.ToLower()
        if ($h -eq $WinswBundledSha256) {
            Copy-Item -Path $bundled -Destination $WinswExe -Force
            Ok 'WinSW installed (bundled, SHA256 verified)'; return
        }
        Warn "Bundled winsw.exe SHA256 mismatch; falling back to network download"
    }
    Log "Downloading WinSW from $WinswReleaseUrl ..."
    for ($i = 0; $i -lt 3; $i++) {
        try {
            Invoke-WebRequest -Uri $WinswReleaseUrl -OutFile $WinswExe -UseBasicParsing -TimeoutSec 20 -ErrorAction Stop
            $h = (Get-FileHash -Path $WinswExe -Algorithm SHA256).Hash.ToLower()
            if ($h -ne $WinswBundledSha256) { Remove-Item $WinswExe -Force -ErrorAction SilentlyContinue; throw 'SHA256 mismatch' }
            Ok 'WinSW downloaded and SHA256 verified'; return
        } catch { Warn "  attempt $($i+1) failed: $($_.Exception.Message)"; Start-Sleep -Seconds 3 }
    }
    Die 'WinSW install failed (network + bundled both unavailable).' 23
}

function Stop-RunningService {
    # **重寫檔案之前一定要先停服務。**
    #
    # 原本只有「不是 git repo」那條分支停 —— 而**升級既有安裝走的是 git 那條**，
    # 服務一直跑著，於是後面的 `uv venv --clear` 刪不掉被 python.exe 佔住的
    # `.venv`，整個安裝停在 `[X] uv venv failed`。v1.15.36 在真的 Windows 上
    # 實測才看到：**這是每一個「裝到既有安裝上」的客戶都會踩的路徑**。
    $svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq 'Running') {
        Log 'Stopping running service before refreshing files ...'
        Stop-Service $ServiceName -Force -ErrorAction SilentlyContinue
        # WinSW 停下來之後 python.exe 還要一點時間放掉檔案握把 ——
        # 沒有這段等待，uv 照樣會撞到「檔案使用中」。
        for ($i = 0; $i -lt 15; $i++) {
            Start-Sleep -Seconds 1
            $s = Get-Service $ServiceName -ErrorAction SilentlyContinue
            if (-not $s -or $s.Status -ne 'Running') { break }
        }
        Start-Sleep -Seconds 2
    }
}

function Fetch-Code {
    Stop-RunningService
    if (Test-Path (Join-Path $InstallDir '.git')) {
        Log 'Existing git install detected, updating ...'
        Push-Location $InstallDir
        try {
            git fetch --depth=1 origin $RepoBranch
            if ($LASTEXITCODE -ne 0) { Die 'git fetch failed' 21 }
            git reset --hard "origin/$RepoBranch"
            if ($LASTEXITCODE -ne 0) { Die 'git reset failed' 21 }
        } finally { Pop-Location }
        return
    }
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Warn "$InstallDir not a git repo; cleaning non-bin files (keeping bin/) ..."
    Get-ChildItem $InstallDir -Force | Where-Object { $_.Name -ne 'bin' } |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path "$InstallDir\.venv") {
        Start-Sleep -Seconds 2
        Remove-Item "$InstallDir\.venv" -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path "$InstallDir\.venv") { Die '.venv locked; stop service and retry.' 21 }
    }
    $tmpSrc = Join-Path $env:TEMP 'jtdt-gui-src'
    if (Test-Path $tmpSrc) { Remove-Item $tmpSrc -Recurse -Force -ErrorAction SilentlyContinue }
    if (Get-Command git -ErrorAction SilentlyContinue) {
        Log "Cloning $RepoUrl ($RepoBranch) ..."
        git clone --depth=1 --branch $RepoBranch $RepoUrl $tmpSrc
        if ($LASTEXITCODE -ne 0) { Die 'git clone failed' 21 }
        Get-ChildItem $tmpSrc -Force | Copy-Item -Destination $InstallDir -Recurse -Force
        Remove-Item $tmpSrc -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Log 'git unavailable, tarball fallback ...'
        $tmp = Join-Path $env:TEMP 'jtdt-src.zip'
        $ext = Join-Path $env:TEMP 'jtdt-extract'
        foreach ($p in @($tmp,$ext)) { if (Test-Path $p) { Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue } }
        Invoke-WebRequest -Uri "$RepoUrl/archive/refs/heads/$RepoBranch.zip" -OutFile $tmp -UseBasicParsing
        Expand-Archive -Path $tmp -DestinationPath $ext -Force
        $first = Get-ChildItem $ext -Directory | Select-Object -First 1
        Copy-Item "$($first.FullName)\*" $InstallDir -Recurse -Force
        Remove-Item $tmp, $ext -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path (Join-Path $InstallDir 'pyproject.toml'))) { Die 'Source fetch failed: pyproject.toml missing' 21 }
    Ok 'Source code ready'
}

function Setup-Python {
    Log 'Setting up isolated Python environment (uv sync) ...'
    $env:UV_PYTHON_PREFERENCE = 'only-managed'
    $setupBat = Join-Path $InstallDir 'setup-python.cmd'
    if (-not (Test-Path $setupBat)) { Die "setup-python.cmd not found at $setupBat" 22 }
    # 防 LF-only 行尾：無 git 的機器走 tarball 下載原始碼時保留 repo 的 LF 行尾，
    # cmd.exe 執行 LF-only 批次檔會逐 token 噴「不是內部或外部命令」錯誤。執行前
    # 一律強制正規化成 CRLF，無論來源是 git clone 或 tarball 都保證可跑。
    try {
        $rawCmd = [System.IO.File]::ReadAllText($setupBat)
        $crlfCmd = ($rawCmd -replace "`r`n", "`n") -replace "`n", "`r`n"
        if ($crlfCmd -ne $rawCmd) {
            [System.IO.File]::WriteAllText($setupBat, $crlfCmd, (New-Object System.Text.UTF8Encoding($false)))
        }
    } catch {
        Warn "Could not normalize setup-python.cmd line endings: $_"
    }
    cmd /c "`"$setupBat`" `"$InstallDir`" 2>&1" | ForEach-Object { Write-Output $_ }
    switch ($LASTEXITCODE) {
        0 { Ok "Python environment ready: $InstallDir\.venv" }
        2 { Die 'uv venv failed' 22 }
        3 { Die 'uv sync failed' 22 }
        4 { Die 'Critical import smoke test failed - deps not installed' 22 }
        default { Die "Setup-Python failed (exit $LASTEXITCODE)" 22 }
    }
}

function Prepare-Data {
    Log "Preparing data directory $DataDir ..."
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    New-Item -ItemType Directory -Force -Path $LogDir  | Out-Null
    $seed = Join-Path $InstallDir 'data'
    if ((Test-Path $seed) -and (-not (Get-ChildItem $DataDir -Force -ErrorAction SilentlyContinue))) {
        Copy-Item "$seed\*" $DataDir -Recurse -Force
    }
}

function Write-WinswXml {
    param([string]$BindAddr = '127.0.0.1', [int]$SvcPort = 8765)
    $py = Join-Path $InstallDir '.venv\Scripts\python.exe'
    $xml = @'
<service>
  <id>{0}</id>
  <name>Jason Tools Document Toolbox</name>
  <description>Jason Tools Document Toolbox - PDF / Office processing</description>
  <executable>{1}</executable>
  <arguments>-m app.main</arguments>
  <workingdirectory>{2}</workingdirectory>
  <log mode="roll-by-size">
    <sizeThreshold>5120</sizeThreshold>
    <keepFiles>5</keepFiles>
  </log>
  <logpath>{3}</logpath>
  <env name="JTDT_DATA_DIR" value="{4}"/>
  <env name="JTDT_HOST" value="{5}"/>
  <env name="JTDT_PORT" value="{6}"/>
  <onfailure action="restart" delay="10 sec"/>
  <onfailure action="restart" delay="20 sec"/>
  <onfailure action="restart" delay="60 sec"/>
  <resetfailure>1 hour</resetfailure>
  <startmode>Automatic</startmode>
</service>
'@ -f $ServiceName, $py, $InstallDir, $LogDir, $DataDir, $BindAddr, $SvcPort
    Set-Content -Path $WinswXml -Value $xml -Encoding UTF8
}

function Install-Service {
    param([string]$BindAddr = '127.0.0.1', [int]$SvcPort = 8765)
    Log 'Installing Windows Service (via WinSW) ...'
    $existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($existing) {
        Log '  Existing service detected, stopping & removing ...'
        & sc.exe stop $ServiceName 2>&1 | Out-Null
        Start-Sleep -Seconds 2
        if (Test-Path $NssmExe) { & $NssmExe remove $ServiceName confirm 2>&1 | Out-Null }
        else { & sc.exe delete $ServiceName 2>&1 | Out-Null }
        Start-Sleep -Seconds 1
    }
    Write-WinswXml -BindAddr $BindAddr -SvcPort $SvcPort
    & $WinswExe install 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Die "WinSW install failed (exit $LASTEXITCODE)." 23 }
    & $WinswExe start 2>&1 | Out-Null
    Start-Sleep -Seconds 2
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $svc -or $svc.Status -ne 'Running') { Die 'WinSW start failed - service not Running.' 24 }
    if (Test-Path $NssmExe) { Remove-Item $NssmExe -Force -ErrorAction SilentlyContinue }
    Ok "Windows Service '$ServiceName' installed and started (autostart)"
}

function Install-Cli {
    Log 'Creating jtdt command ...'
    $py = Join-Path $InstallDir '.venv\Scripts\python.exe'
    Set-Content -Path $CliShim -Value "@echo off`r`n`"$py`" -m app.cli %*" -Encoding ASCII
    $sysPath = [Environment]::GetEnvironmentVariable('Path','Machine')
    if ($sysPath -notmatch [regex]::Escape($InstallDir)) {
        [Environment]::SetEnvironmentVariable('Path', "$sysPath;$InstallDir", 'Machine')
        Ok 'Added to system PATH (new terminal required)'
    }
    Ok "jtdt command: $CliShim"
}

function Install-Firewall {
    param([int]$SvcPort = 8765)
    Log "Adding firewall rule for LAN access (TCP $SvcPort) ..."
    & netsh advfirewall firewall delete rule name="jt-doc-tools" 2>&1 | Out-Null
    & netsh advfirewall firewall add rule name="jt-doc-tools" dir=in action=allow protocol=TCP localport=$SvcPort 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Ok "Firewall inbound rule added (TCP $SvcPort)" }
    else { Warn "Could not add firewall rule (exit $LASTEXITCODE)" }
}

function Health-Check {
    param([int]$SvcPort = 8765)
    Log 'Waiting for service to come up ...'
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$SvcPort/healthz" -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) { Ok "Service online: http://127.0.0.1:$SvcPort/"; return }
        } catch {}
        Start-Sleep -Seconds 1
    }
    Warn 'Health check did not pass within 30s. Run: jtdt logs'
}

# =====================================================================
#  Orchestration
# =====================================================================
# LAN access => bind 0.0.0.0 so other machines on the subnet can connect.
# Otherwise localhost-only (matches install.ps1 default).
#
# **升級時沿用既有的監聽位址與 port**（WinSW 的設定檔在 bin\，Fetch-Code 不會清掉）。
# 安裝程式不記得上一次的選擇：「區域網路存取」改成預設不勾之後，一台原本給整個
# 單位共用的機器重跑安裝程式升級，會被改成只有本機連得到（2026-09-24 在 Win11
# 實機用 v1.16.20 的安裝檔測到）；自訂過的 port 也會被改回 8765（一直都是錯的）。
# installer.nsi 會在升級時先把那個選項勾起來；這裡再保留原本的位址（例如只綁
# 在某一張網卡上）與 port。使用者在升級時**自己取消勾選**的話照他的意思改回本機。
# 檢查：tests/test_installer_silent_mode.py
$PrevHost = $null; $PrevPort = $null
if (Test-Path $WinswXml) {
    try {
        $prevXml = Get-Content $WinswXml -Raw -ErrorAction Stop
        if ($prevXml -match 'name="JTDT_HOST"\s+value="([^"]+)"') { $PrevHost = $Matches[1] }
        if ($prevXml -match 'name="JTDT_PORT"\s+value="(\d+)"') { $PrevPort = [int]$Matches[1] }
    } catch {}
}
if ($PrevPort -and -not $PSBoundParameters.ContainsKey('Port')) {
    $Port = $PrevPort
    Log "Keeping existing port $Port"
}
$Loopback = @('127.0.0.1', 'localhost', '::1')
if ($InstallFirewall) {
    $EffectiveBind = if ($PrevHost -and ($Loopback -notcontains $PrevHost)) { $PrevHost } else { '0.0.0.0' }
} else {
    $EffectiveBind = '127.0.0.1'
}
Log "Service will listen on $EffectiveBind`:$Port"

if ($InstallOffice) { Ensure-Office } else { Log 'Office component skipped (user choice)' }
if ($InstallOcr) {
    if ($IsArm64) {
        Warn 'ARM64 detected: EasyOCR (PyTorch) wheels may be unavailable on Windows ARM64;'
        Warn '  OCR will fall back to tesseract (lighter, CJK accuracy lower). tesseract still installs.'
    }
    Install-Tesseract
} else { Log 'OCR component skipped (user choice)' }
Install-Git
Install-Uv
Fetch-Code
Install-Winsw
if ($InstallOcr)    { Ensure-VCRedist }
Setup-Python
Prepare-Data
if ($InstallService) {
    Install-Service -BindAddr $EffectiveBind -SvcPort $Port
    Install-Cli
    if ($InstallFirewall) { Install-Firewall -SvcPort $Port }
    Health-Check -SvcPort $Port
} else {
    Install-Cli
    Log 'Service component skipped; start manually with: jtdt start'
}

# --- Add/Remove Programs 的版本要寫「實際裝進去的」版本 -----------------
# installer 是瘦 bootstrapper：檔名與 NSIS 的 ${VERSION} 是**打包當天**的版本，
# 程式碼卻是安裝當下從 main 抓的。不改的話「設定 → 應用程式」會顯示一個跟實際
# 完全對不上的版本（實測：檔名 1.12.82 的 installer 裝出 v1.15.6，登錄檔卻寫
# 1.12.82），使用者與客服都會被誤導。
#
# 注意：這支腳本是**打包時就嵌進 exe 的**（installer.nsi 的 `File`），不是安裝
# 時才下載 —— 所以改這裡只對**重新打包過的** installer 有效。既有安裝要靠
# `app/cli.py:_sync_windows_display_version()`，下一次 `jtdt update` 會更正。
function Sync-DisplayVersion {
    try {
        $mainPy = Join-Path $InstallDir 'app\main.py'
        if (-not (Test-Path $mainPy)) { return }
        $m = Select-String -Path $mainPy -Pattern '^VERSION\s*=\s*"([^"]+)"' |
             Select-Object -First 1
        if (-not $m) { return }
        $ver = $m.Matches[0].Groups[1].Value
        $key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\jt-doc-tools'
        if (Test-Path $key) {
            Set-ItemProperty -Path $key -Name 'DisplayVersion' -Value $ver
            Log "Add/Remove Programs version set to $ver"
        }
    } catch { Log "could not sync DisplayVersion: $($_.Exception.Message)" }
}
Sync-DisplayVersion

Ok 'Install complete!'
exit 0
