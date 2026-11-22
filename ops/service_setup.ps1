<#
.SYNOPSIS
    Registers launcher.py as a Windows service (AshwaX) via NSSM.

.DESCRIPTION
    One-time setup. Must be run as Administrator (service registration
    requires elevation). Installs NSSM via Chocolatey if not already
    present, then wraps "python launcher.py" as a Windows service that
    starts automatically (delayed, on boot) with no auto-restart-on-crash
    - if the stack goes down after starting, it stays down until a human
    restarts it, rather than looping unattended.

    Restart is then just:
        nssm restart AshwaX

    launcher.py itself is unchanged: NSSM's stop sequence sends Ctrl+C,
    which launcher.py already catches (KeyboardInterrupt) to gracefully
    stop its children, and launcher.py's own _kill_stale_processes()
    already sweeps any leftover python.exe on every start.

.NOTES
    Run this script once. Re-running is safe (nssm install/set calls are
    idempotent for an existing service of the same name).
#>

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

$ServiceName = "AshwaX"
$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$PythonExe   = "C:\Python313\python.exe"
$LauncherArg = "launcher.py"
$LogDir      = Join-Path $ProjectRoot "logs"

if (-not (Test-Path $PythonExe)) {
    throw "Python not found at $PythonExe - update the `$PythonExe variable in this script."
}

if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    Write-Host "Installing NSSM via Chocolatey..."
    choco install nssm -y
}

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

$existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existingService) {
    Write-Host "Service '$ServiceName' already exists (status: $($existingService.Status)) - updating config only."
} else {
    Write-Host "Installing service '$ServiceName'..."
    nssm install $ServiceName $PythonExe $LauncherArg
}

nssm set $ServiceName AppDirectory $ProjectRoot
nssm set $ServiceName AppStdout (Join-Path $LogDir "service_stdout.log")
nssm set $ServiceName AppStderr (Join-Path $LogDir "service_stderr.log")
nssm set $ServiceName AppRotateFiles 1
nssm set $ServiceName AppRotateOnline 1
nssm set $ServiceName AppRotateBytes 10485760

# Automatic (delayed) start - the service comes up on every boot, after
# other services/network are up, without needing a human to start it.
nssm set $ServiceName Start SERVICE_AUTO_START
nssm set $ServiceName DelayedAutostart 1

# No auto-restart loop on crash/exit - if the stack goes down, it stays
# down until a human looks at the logs and restarts it. Auto-restarting a
# trading engine that just crashed for an unknown reason is not a safe
# default.
nssm set $ServiceName AppExit Default Exit

Write-Host ""
Write-Host "Done. Service '$ServiceName' is registered (auto-start at boot, no auto-restart-on-crash)."
Write-Host "  Start:   nssm start $ServiceName"
Write-Host "  Stop:    nssm stop $ServiceName"
Write-Host "  Restart: nssm restart $ServiceName"
Write-Host "  Status:  nssm status $ServiceName"
Write-Host "  Logs:    $LogDir\service_stdout.log / service_stderr.log (plus the existing logs\*.log per-component files)"
