<#
.SYNOPSIS
    Register the Atlas web surface and worker as Windows services under NSSM,
    with restart-on-failure, rotating logs, and delayed auto-start.

.DESCRIPTION
    Two services:

      AtlasWeb     python -m scripts.run_web --host 127.0.0.1 --port 8000
      AtlasWorker  python -m scripts.run_worker

    Both run from the repo root so core/config.py finds .env. Each restarts
    5 seconds after any exit, logs to logs\<name>.service.log with 10 MB
    rotation, and starts automatically (delayed) at boot. Postgres and Ollama
    still come from `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d`, which Docker Desktop restarts on
    login when "Start Docker Desktop when you sign in" is on; until it is up
    the services crash and NSSM retries, which is the intended behaviour.

    Run as your own account (-RunAsCurrentUser) rather than LocalSystem when
    the worker must reach Docker Desktop (core/backup.py shells out to
    `docker exec` for pg_dump). LocalSystem cannot see a per-user Docker
    Desktop engine.

    AtlasWorker is skipped with a warning until scripts\run_worker.py exists
    (it lands with the r1/notify-and-approvals branch).

    Requires NSSM on PATH:   winget install NSSM.NSSM
    Run from an elevated PowerShell at the repo root:

        powershell -ExecutionPolicy Bypass -File scripts\install_windows_service.ps1 -RunAsCurrentUser
        powershell -ExecutionPolicy Bypass -File scripts\install_windows_service.ps1 -Uninstall

    Afterwards:  nssm status AtlasWeb | nssm restart AtlasWorker | nssm edit AtlasWeb
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$Python = "",
    [string]$Nssm = "nssm",
    [int]$WebPort = 8000,
    [switch]$RunAsCurrentUser,
    [switch]$NoStart,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this from an elevated (Administrator) PowerShell."
    }
}

function Invoke-Nssm {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    $out = & $Nssm @Args 2>&1
    if ($LASTEXITCODE -ne 0) { throw "nssm $($Args -join ' ') failed: $out" }
    return $out
}

function Test-ServiceExists([string]$name) {
    return [bool](Get-Service -Name $name -ErrorAction SilentlyContinue)
}

function Remove-AtlasService([string]$name) {
    if (-not (Test-ServiceExists $name)) {
        Write-Host "[service] $name not installed"
        return
    }
    & $Nssm stop $name 2>&1 | Out-Null
    Invoke-Nssm remove $name confirm | Out-Null
    Write-Host "[service] removed $name"
}

function Install-AtlasService {
    param(
        [string]$Name,
        [string]$DisplayName,
        [string]$Description,
        [string[]]$Arguments,
        [string]$LogFile,
        [pscredential]$Credential
    )
    if (Test-ServiceExists $Name) {
        Write-Host "[service] $Name exists; updating its settings"
        & $Nssm stop $Name 2>&1 | Out-Null
    } else {
        Invoke-Nssm install $Name $Python | Out-Null
        Write-Host "[service] installed $Name"
    }

    Invoke-Nssm set $Name Application $Python | Out-Null
    Invoke-Nssm set $Name AppParameters ($Arguments -join " ") | Out-Null
    Invoke-Nssm set $Name AppDirectory $RepoRoot | Out-Null
    Invoke-Nssm set $Name DisplayName $DisplayName | Out-Null
    Invoke-Nssm set $Name Description $Description | Out-Null
    Invoke-Nssm set $Name Start SERVICE_DELAYED_AUTO_START | Out-Null
    Invoke-Nssm set $Name AppEnvironmentExtra "PYTHONUNBUFFERED=1" "PYTHONIOENCODING=utf-8" | Out-Null

    # Restart on any exit, 5 s after a crash; if it crashes again within
    # 10 s NSSM backs off instead of spinning.
    Invoke-Nssm set $Name AppExit Default Restart | Out-Null
    Invoke-Nssm set $Name AppRestartDelay 5000 | Out-Null
    Invoke-Nssm set $Name AppThrottle 10000 | Out-Null

    # Graceful stop: Ctrl+C first (run_web / run_worker handle SIGINT and
    # drain), then escalate. 20 s is enough for an in-flight agent turn.
    Invoke-Nssm set $Name AppStopMethodSkip 0 | Out-Null
    Invoke-Nssm set $Name AppStopMethodConsole 20000 | Out-Null
    Invoke-Nssm set $Name AppStopMethodWindow 5000 | Out-Null
    Invoke-Nssm set $Name AppStopMethodThreads 5000 | Out-Null

    # Logs: both streams to one file, rotated at service start past 10 MB.
    Invoke-Nssm set $Name AppStdout $LogFile | Out-Null
    Invoke-Nssm set $Name AppStderr $LogFile | Out-Null
    Invoke-Nssm set $Name AppRotateFiles 1 | Out-Null
    # Rotate at service start only. Online rotation makes NSSM feed output
    # through pipes, and with stdout and stderr in one file it never drained
    # stderr: Python blocked mid-log-write and the service froze (2026-10-08).
    Invoke-Nssm set $Name AppRotateOnline 0 | Out-Null
    Invoke-Nssm set $Name AppRotateBytes 10485760 | Out-Null

    if ($Credential) {
        $user = $Credential.UserName
        if ($user -notmatch "\\") { $user = ".\$user" }
        $plain = $Credential.GetNetworkCredential().Password
        Invoke-Nssm set $Name ObjectName $user $plain | Out-Null
        Write-Host "[service] $Name runs as $user"
    }
}

# Resolved here, not as a param default: Windows PowerShell 5.1 can leave
# $PSScriptRoot empty while it evaluates param() defaults.
if (-not $RepoRoot) {
    $here = $PSScriptRoot
    if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Definition }
    $RepoRoot = (Resolve-Path (Join-Path $here "..")).Path
}

Assert-Admin

if (-not (Get-Command $Nssm -ErrorAction SilentlyContinue)) {
    throw "nssm not found on PATH. Install it with:  winget install NSSM.NSSM"
}

if ($Uninstall) {
    Remove-AtlasService "AtlasWorker"
    Remove-AtlasService "AtlasWeb"
    exit 0
}

if (-not $Python) { $Python = Join-Path $RepoRoot ".venv\Scripts\python.exe" }
if (-not (Test-Path $Python)) { throw "python not found at $Python (pass -Python or create .venv first)" }
if (-not (Test-Path (Join-Path $RepoRoot ".env"))) { throw "no .env at $RepoRoot; copy .env.example and fill it in first" }

$logDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$cred = $null
if ($RunAsCurrentUser) {
    $cred = Get-Credential -UserName $env:USERNAME -Message "Password for $env:USERNAME (stored by the Service Control Manager, never written by this script)"
    if (-not $cred) { throw "credential prompt cancelled" }
}

Install-AtlasService -Name "AtlasWeb" `
    -DisplayName "Atlas web surface" `
    -Description "Atlas dashboard, PWA and API on 127.0.0.1:$WebPort. Exposed remotely only through the Cloudflare Tunnel." `
    -Arguments @("-m", "scripts.run_web", "--host", "127.0.0.1", "--port", "$WebPort", "--log-file", "logs\web.log") `
    -LogFile (Join-Path $logDir "web.service.log") `
    -Credential $cred

$workerEntry = Join-Path $RepoRoot "scripts\run_worker.py"
$workerInstalled = $false
if (Test-Path $workerEntry) {
    Install-AtlasService -Name "AtlasWorker" `
        -DisplayName "Atlas worker" `
        -Description "Atlas scheduler: executive loop, briefs, hygiene, backups. Exactly one instance must run." `
        -Arguments @("-m", "scripts.run_worker", "--log-file", "logs\worker.log") `
        -LogFile (Join-Path $logDir "worker.service.log") `
        -Credential $cred
    $workerInstalled = $true
} else {
    Write-Warning "[service] scripts\run_worker.py not found; AtlasWorker skipped. Re-run after the r1/notify-and-approvals branch is merged."
}

if (-not $NoStart) {
    Invoke-Nssm start AtlasWeb | Out-Null
    Write-Host "[service] AtlasWeb started"
    if ($workerInstalled) {
        Invoke-Nssm start AtlasWorker | Out-Null
        Write-Host "[service] AtlasWorker started"
    }
}

Write-Host ""
Write-Host "[service] status:   nssm status AtlasWeb; nssm status AtlasWorker"
Write-Host "[service] logs:     $logDir\web.service.log, $logDir\worker.service.log"
Write-Host "[service] remember: never also run 'docker compose --profile app up'; two workers means two schedulers."
