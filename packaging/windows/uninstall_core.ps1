# =====================================================================
#  uninstall_core.ps1  --  jt-doc-tools GUI uninstaller core logic
# ---------------------------------------------------------------------
#  Invoked by the NSIS uninstaller. Stops/removes the service, firewall
#  rule and PATH entry, and (optionally) purges user data. The NSIS
#  uninstaller itself handles deleting program files (RMDir) and the
#  Add/Remove-Programs registry key, so this script does NOT touch those.
#
#  STANDALONE -- shares no code with app/cli.py's `jtdt uninstall`; the
#  GUI uninstall path is fully self-contained for stability isolation.
#
#  Exit code is always 0 (best-effort cleanup; NSIS continues regardless).
# =====================================================================

param(
    [string]$InstallDir = (Join-Path ${env:ProgramFiles} 'jt-doc-tools'),
    [switch]$PurgeData
)

$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'

$ServiceName = 'jt-doc-tools'
$ProgData    = ${env:ProgramData}
$DataRoot    = Join-Path $ProgData 'jt-doc-tools'   # holds Data\ and Logs\
$LogDir      = Join-Path $DataRoot 'Logs'
$BinDir      = Join-Path $InstallDir 'bin'
$WinswExe    = Join-Path $BinDir 'jtdt-svc.exe'

# Log to ProgramData (survives InstallDir removal, and the keep-data uninstall).
$null = New-Item -ItemType Directory -Force -Path $LogDir -ErrorAction SilentlyContinue
$UninstLog = Join-Path $LogDir 'uninstaller.log'
function _w($pfx, $m, $col) {
    $line = "$pfx $m"
    Write-Host $line -ForegroundColor $col
    try { Add-Content -Path $UninstLog -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue } catch {}
}
function Log  ($m) { _w '==>'  $m 'Cyan'   }
function Ok   ($m) { _w '[OK]' $m 'Green'  }
function Warn ($m) { _w '[!] ' $m 'Yellow' }

Log "uninstall_core start (InstallDir=$InstallDir PurgeData=$([bool]$PurgeData))"

# 1) Stop the service and wait until it is really STOPPED, otherwise the
#    python child keeps .venv files locked and RMDir fails.
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    Log 'Stopping service ...'
    & sc.exe stop $ServiceName 2>&1 | Out-Null
    for ($i = 0; $i -lt 30; $i++) {
        $s = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if (-not $s -or $s.Status -eq 'Stopped') { break }
        Start-Sleep -Seconds 1
    }
    # 2) Uninstall the WinSW service registration.
    if (Test-Path $WinswExe) {
        Log 'Uninstalling WinSW service ...'
        & $WinswExe uninstall 2>&1 | Out-Null
        Start-Sleep -Seconds 1
    }
    & sc.exe delete $ServiceName 2>&1 | Out-Null
    Ok 'Service removed'
} else {
    Log 'Service not present (already removed)'
}

# 3) Make sure no orphan python is still holding port 8765 (WinSW stop can
#    leave a child behind -- the .154 deploy hazard documented in CLAUDE.md).
try {
    $conns = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        try { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue } catch {}
    }
} catch {
    # Get-NetTCPConnection not on very old hosts; fall back to netstat parse.
    $lines = netstat -ano | Select-String ':8765\s' | Select-String 'LISTENING'
    foreach ($ln in $lines) {
        $procId = ($ln.ToString() -split '\s+')[-1]
        if ($procId -match '^\d+$') { try { Stop-Process -Id ([int]$procId) -Force -ErrorAction SilentlyContinue } catch {} }
    }
}

# 4) Remove firewall rule (no-op if absent).
& netsh advfirewall firewall delete rule name="jt-doc-tools" 2>&1 | Out-Null
Ok 'Firewall rule removed (if present)'

# 5) Remove InstallDir from the system PATH.
try {
    $sysPath = [Environment]::GetEnvironmentVariable('Path','Machine')
    if ($sysPath) {
        $parts = $sysPath -split ';' | Where-Object { $_ -ne '' -and $_ -ne $InstallDir }
        [Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), 'Machine')
        Ok 'Removed from system PATH'
    }
} catch { Warn "Could not modify system PATH: $_" }

# 6) Optionally purge user data (banks, signatures, history, audit).
if ($PurgeData) {
    if (Test-Path $DataRoot) {
        Log "Purging user data: $DataRoot ..."
        Remove-Item $DataRoot -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path $DataRoot) { Warn 'Some data files were locked and could not be removed' }
        else { Ok 'User data purged' }
    }
} else {
    Log "User data kept at $DataRoot (re-install reuses it)"
}

Ok 'Uninstall core finished'
exit 0
