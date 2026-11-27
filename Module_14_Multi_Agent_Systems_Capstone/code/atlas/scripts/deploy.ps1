<#
.SYNOPSIS
    Deploy Atlas on this laptop and give it a permanent HTTPS link for the phone.

.DESCRIPTION
    One command for the first install and for every update after it. Each run:

      1. pulls the latest master (fast-forward only; stops on local edits)
      2. installs Python dependencies into .venv (dependencies only; Atlas runs
         from this folder and is never built as a package)
      3. fills any missing generated secrets in .env (VAPID keys, ingest and
         MCP tokens); never overwrites a value you set, never prints one
      4. starts Postgres and Ollama (docker compose), waits for Postgres,
         pulls the embedding model on first run
      5. applies every migration (scripts\migrate.bat)
      6. applies the Atlas power plan so the lid can close (every run; idempotent)
      7. installs or restarts the AtlasWeb and AtlasWorker services (NSSM)
      8. checks http://127.0.0.1:8000/api/health
      9. publishes the permanent link:
           tailscale  (default) https://<laptop>.<tailnet>.ts.net, reachable
                      only from devices signed in to your Tailscale account
           cloudflare https://<your hostname>, through a Cloudflare Tunnel;
                      refuses to finish unless Cloudflare Access blocks
                      anonymous requests
           none       local only
     10. checks the link end to end and prints the phone steps

    Atlas has no login of its own; identity comes from Tailscale or Cloudflare
    Access. This script never enables Tailscale Funnel (public internet) and
    turns it off if it finds it on.

    Double-click scripts\deploy.bat, or from any PowerShell at the repo root:

        powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1
        powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Expose cloudflare -PublicHost atlas.example.com -TunnelToken <token>
        powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -SkipPull

    The script elevates itself (UAC prompt) because services and the power plan
    need Administrator.
#>
[CmdletBinding()]
param(
    [ValidateSet("tailscale", "cloudflare", "none")]
    [string]$Expose = "tailscale",
    [string]$PublicHost = "",
    [string]$TunnelToken = "",
    [int]$WebPort = 8000,
    [switch]$SkipPull,
    [switch]$SkipPowerPlan,
    [switch]$NoPause
)

# Continue, not Stop: Windows PowerShell 5.1 turns any redirected stderr line from a
# native tool (psql NOTICEs, docker, git) into a terminating error under Stop. Every
# native call below checks $LASTEXITCODE instead, and cmdlets that must fail say so.
$ErrorActionPreference = "Continue"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

# ---------------------------------------------------------------- helpers

function Write-Step([string]$text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Ok([string]$text) { Write-Host "    ok  $text" -ForegroundColor Green }
function Write-Note([string]$text) { Write-Host "    ..  $text" -ForegroundColor DarkGray }
function Write-Warn2([string]$text) { Write-Host "    !!  $text" -ForegroundColor Yellow }

function Stop-Deploy([string]$text) {
    Write-Host ""
    Write-Host "DEPLOY STOPPED: $text" -ForegroundColor Red
    if (-not $NoPause) { Read-Host "Press Enter to close" | Out-Null }
    exit 1
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Find-Exe([string]$name, [string[]]$candidates) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { return $c } }
    return $null
}

function Install-WithWinget([string]$id, [string]$label) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Stop-Deploy "$label is not installed and winget is unavailable. Install $label by hand, then re-run."
    }
    Write-Note "installing $label with winget"
    & winget install --id $id -e --silent --accept-source-agreements --accept-package-agreements | Out-Host
    Update-SessionPath
}

function Get-EnvMap([string]$path) {
    $map = @{}
    if (-not (Test-Path $path)) { return $map }
    foreach ($line in Get-Content -LiteralPath $path -Encoding UTF8) {
        if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
        $k, $v = $line -split '=', 2
        $map[$k.Trim()] = $v.Trim().Trim('"').Trim("'")
    }
    return $map
}

function Set-EnvValue([string]$path, [string]$key, [string]$value) {
    # Replace an existing blank "KEY=" line, else append. Never touches a key that has a value.
    $lines = @(Get-Content -LiteralPath $path -Encoding UTF8)
    $done = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^\s*$([regex]::Escape($key))\s*=\s*$") { $lines[$i] = "$key=$value"; $done = $true; break }
    }
    if (-not $done) { $lines += "$key=$value" }
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllLines($path, [string[]]$lines, $utf8)
}

function Update-EnvValue([string]$path, [string]$key, [string]$value) {
    # Replace KEY=anything with KEY=value (append when absent). For moving an old
    # Atlas default forward; callers check the current value first.
    $lines = @(Get-Content -LiteralPath $path -Encoding UTF8)
    $done = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^\s*$([regex]::Escape($key))\s*=") { $lines[$i] = "$key=$value"; $done = $true; break }
    }
    if (-not $done) { $lines += "$key=$value" }
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllLines($path, [string[]]$lines, $utf8)
}

function New-Token {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return ([Convert]::ToBase64String($bytes)).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Get-Health([string]$url, [int]$timeoutSec = 10) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec $timeoutSec -MaximumRedirection 0 -ErrorAction Stop
        return @{ status = [int]$r.StatusCode; body = $r.Content; type = [string]$r.Headers["Content-Type"] }
    } catch {
        $resp = $_.Exception.Response
        if ($resp) {
            $body = ""
            try { $body = (New-Object IO.StreamReader($resp.GetResponseStream())).ReadToEnd() } catch { $body = "" }
            return @{ status = [int]$resp.StatusCode; body = $body; type = [string]$resp.ContentType }
        }
        return @{ status = 0; body = $_.Exception.Message; type = "" }
    }
}

function Wait-Health([string]$url, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    $last = $null
    while ((Get-Date) -lt $deadline) {
        $last = Get-Health $url 5
        if ($last.status -eq 200) { return $last }
        Start-Sleep -Seconds 2
    }
    return $last
}

function Restart-AtlasService([string]$name, [string]$logName) {
    # Not "nssm restart": that waits on the Service Control Manager with no limit,
    # and one service that will not stop froze the deploy at step 7 with no output.
    # Stop with a 45 s limit, end the process tree if it is still up, start, and say
    # what happens at each step.
    $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
    if (-not $svc) { return "missing" }
    if ($svc.Status -ne "Stopped") {
        Write-Note "${name}: stopping (status $($svc.Status))"
        & sc.exe stop $name *> $null
        $deadline = (Get-Date).AddSeconds(45)
        do { Start-Sleep -Seconds 2; $svc.Refresh() } while ($svc.Status -ne "Stopped" -and (Get-Date) -lt $deadline)
        if ($svc.Status -ne "Stopped") {
            $wmi = Get-CimInstance Win32_Service -Filter "Name='$name'" -ErrorAction SilentlyContinue
            if ($wmi -and $wmi.ProcessId) {
                Write-Note "${name} did not stop within 45 seconds; ending it and its Python process"
                & taskkill.exe /PID $wmi.ProcessId /T /F *> $null
            }
            $deadline = (Get-Date).AddSeconds(30)
            do { Start-Sleep -Seconds 1; $svc.Refresh() } while ($svc.Status -ne "Stopped" -and (Get-Date) -lt $deadline)
        }
    }
    # Leftover Python from earlier frozen runs (not children of the service any more)
    # can hold the log file or the port, and NSSM then fails to start. End them.
    $module = if ($name -eq "AtlasWeb") { "scripts.run_web" } else { "scripts.run_worker" }
    $orphans = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape("-m $module") })
    foreach ($o in $orphans) {
        Write-Note "${name}: ending leftover Python process $($o.ProcessId)"
        & taskkill.exe /PID $o.ProcessId /T /F *> $null
    }
    if ($orphans.Count) { Start-Sleep -Seconds 2 }

    # A start sent while the service is still stopping is ignored, so wait for
    # Stopped and retry the start up to three times.
    for ($try = 1; $try -le 3; $try++) {
        $svc.Refresh()
        if ($svc.Status -eq "Running") { return "running" }
        if ($svc.Status -eq "Stopped") {
            Write-Note "${name}: starting"
            $startOut = (& sc.exe start $name 2>&1 | Out-String)
            if ($LASTEXITCODE -ne 0) {
                $reason = ($startOut -split "`r?`n" | Where-Object { $_ -match "FAILED|error|denied|logon|password" }) -join " "
                if (-not $reason) { $reason = $startOut.Trim() }
                Write-Warn2 "Windows refused to start ${name} (sc exit $LASTEXITCODE): $reason"
            }
        } else {
            Write-Note "${name}: waiting while it is $($svc.Status)"
        }
        $deadline = (Get-Date).AddSeconds(25)
        do { Start-Sleep -Seconds 1; $svc.Refresh() } while ($svc.Status -ne "Running" -and (Get-Date) -lt $deadline)
        if ($svc.Status -eq "Running") { return "running" }
    }
    $log = Join-Path $RepoRoot "logs\$logName"
    if (Test-Path $log) {
        $age = [int]((Get-Date) - (Get-Item -LiteralPath $log).LastWriteTime).TotalMinutes
        Write-Warn2 "last lines of logs\${logName} (written $age min ago):"
        Get-Content -LiteralPath $log -Tail 15 | ForEach-Object { Write-Host "        $_" }
    }
    # NSSM writes its own reasons (log file locked, logon failure, app exited at
    # once) to the Application event log, not to the service log.
    try {
        $events = Get-WinEvent -FilterHashtable @{ LogName = "Application"; ProviderName = "nssm"; StartTime = (Get-Date).AddMinutes(-10) } -MaxEvents 30 -ErrorAction Stop |
            Where-Object { $_.Message -match [regex]::Escape($name) } | Select-Object -First 4
        if ($events) {
            Write-Warn2 "what Windows recorded for ${name}:"
            foreach ($ev in $events) { Write-Host ("        " + ($ev.Message -replace "\s+", " ").Trim()) }
        }
    } catch { }
    return [string]$svc.Status
}

# ---------------------------------------------------------------- elevate

if (-not (Test-Admin)) {
    Write-Host "Atlas deploy needs Administrator for services and the power plan. Asking Windows..."
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Expose", $Expose, "-WebPort", "$WebPort")
    if ($PublicHost) { $argList += @("-PublicHost", $PublicHost) }
    if ($TunnelToken) { $argList += @("-TunnelToken", $TunnelToken) }
    if ($SkipPull) { $argList += "-SkipPull" }
    if ($SkipPowerPlan) { $argList += "-SkipPowerPlan" }
    if ($NoPause) { $argList += "-NoPause" }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argList -WorkingDirectory $RepoRoot
    exit 0
}

Write-Host "Atlas deploy  $(Get-Date -Format 'yyyy-MM-dd HH:mm')  $RepoRoot" -ForegroundColor White

if ($Expose -eq "cloudflare" -and -not $PublicHost) {
    Stop-Deploy "-Expose cloudflare needs -PublicHost, for example atlas.example.com"
}

# ---------------------------------------------------------------- 1. code

Write-Step "1/10  Code"
$git = Find-Exe "git" @("$env:ProgramFiles\Git\cmd\git.exe")
if (-not $git) { Stop-Deploy "git not found. Install Git for Windows, then re-run." }
$before = (& $git rev-parse --short HEAD).Trim()
if ($SkipPull) {
    Write-Note "skipping pull (-SkipPull); deploying $before as it is"
} else {
    $dirty = & $git status --porcelain --untracked-files=no
    if ($dirty) {
        Write-Host ($dirty -join "`n")
        Stop-Deploy "tracked files have local edits. Commit or stash them, or re-run with -SkipPull to deploy what is here."
    }
    & $git fetch --quiet origin master
    if ($LASTEXITCODE -ne 0) { Stop-Deploy "git fetch failed. Check the network and your GitHub sign-in." }
    $branch = (& $git rev-parse --abbrev-ref HEAD).Trim()
    if ($branch -ne "master") {
        Write-Note "on branch $branch; switching to master"
        & $git checkout --quiet master
        if ($LASTEXITCODE -ne 0) { Stop-Deploy "could not switch to master." }
    }
    & $git merge --ff-only --quiet origin/master
    if ($LASTEXITCODE -ne 0) { Stop-Deploy "master has local commits that are not on GitHub; fast-forward refused. Push or reset them first." }
}
$after = (& $git rev-parse --short HEAD).Trim()
if ($before -eq $after) { Write-Ok "master at $after (already current)" } else { Write-Ok "master $before -> $after" }

# ---------------------------------------------------------------- 2. python

Write-Step "2/10  Python dependencies"
$py = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    $launcher = Find-Exe "py" @()
    if (-not $launcher) { Stop-Deploy ".venv is missing and the py launcher is not installed. Install Python 3.12 from python.org, then re-run." }
    Write-Note "creating .venv with Python 3.12"
    & $launcher -3.12 -m venv (Join-Path $RepoRoot ".venv")
    if ($LASTEXITCODE -ne 0) { Stop-Deploy "could not create .venv with Python 3.12." }
}
$pyInfo = @(& $py -c "import sys; print('%d.%d' % sys.version_info[:2]); print(sys.base_prefix)")
if ($LASTEXITCODE -ne 0 -or $pyInfo.Count -lt 2) { Stop-Deploy ".venv\Scripts\python.exe does not run. Delete the .venv folder and run deploy again." }
$pyVer = $pyInfo[0].Trim()
if ([version]$pyVer -lt [version]"3.12") { Stop-Deploy ".venv runs Python $pyVer; Atlas needs 3.12 or newer. Install Python 3.12 from python.org, delete .venv, and run deploy again." }
if ($pyInfo[1] -match "WindowsApps") {
    Write-Warn2 ".venv is built on the Microsoft Store Python, which misbehaves in admin windows and as a service."
    Write-Warn2 "If a later step fails, install Python 3.12 from python.org, delete the .venv folder, and run deploy again."
}

# Dependencies only. Atlas runs from the repo folder (python -m scripts.run_web), so
# the project itself is never built or installed. That skips pip's isolated build
# subprocess, which crashes (0xC0000005) under some Windows Python setups.
New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot "logs") | Out-Null
$reqFile = Join-Path $RepoRoot "logs\requirements.deploy.txt"
& $py -c "import sys, tomllib; d = tomllib.load(open('pyproject.toml', 'rb')); open(sys.argv[1], 'w', encoding='utf-8').write(chr(10).join(d['project']['dependencies']) + chr(10))" $reqFile
if ($LASTEXITCODE -ne 0) { Stop-Deploy "could not read the dependency list from pyproject.toml." }
$stampFile = Join-Path $RepoRoot ".venv\atlas-deps.sha256"
$stamp = (Get-FileHash -Algorithm SHA256 -LiteralPath $reqFile).Hash + " py$pyVer"
$oldStamp = ""
if (Test-Path $stampFile) { $oldStamp = (Get-Content -LiteralPath $stampFile -Raw).Trim() }
if ($oldStamp -eq $stamp) {
    Write-Note "dependencies unchanged since the last deploy"
} else {
    Write-Note "installing dependencies (the first run downloads a few hundred MB)"
    & $py -m pip install --quiet --disable-pip-version-check --prefer-binary -r $reqFile
    if ($LASTEXITCODE -ne 0) { Stop-Deploy "pip install failed; read the output above." }
    Set-Content -LiteralPath $stampFile -Value $stamp -Encoding ASCII
}
$version = (& $py -c "from core.version import VERSION; print(VERSION)")
if ($LASTEXITCODE -ne 0 -or -not $version) { Stop-Deploy "Atlas code does not import with this Python; read the error above." }
$version = ([string]$version).Trim()
Write-Ok "Atlas $version ready on Python $pyVer"

# ---------------------------------------------------------------- 3. .env

Write-Step "3/10  Settings (.env)"
$envPath = Join-Path $RepoRoot ".env"
if (-not (Test-Path $envPath)) {
    Copy-Item (Join-Path $RepoRoot ".env.example") $envPath
    Stop-Deploy ".env did not exist, so it was created from .env.example. Open .env, paste ANTHROPIC_API_KEY and the other keys you have, save, and run deploy again."
}
$envMap = Get-EnvMap $envPath
if (-not $envMap["ANTHROPIC_API_KEY"] -or $envMap["ANTHROPIC_API_KEY"] -match "REPLACE_ME|placeholder") {
    Stop-Deploy "ANTHROPIC_API_KEY in .env is blank or a placeholder. Paste the real key and run deploy again."
}
if (-not $envMap["POSTGRES_PASSWORD"] -or $envMap["POSTGRES_PASSWORD"] -match "REPLACE_WITH_A_LONG_RANDOM_PASSWORD|atlas_local_dev_change_on_vps") {
    Stop-Deploy "POSTGRES_PASSWORD in .env is blank or a placeholder. Set a long random password and run deploy again."
}
$added = @()
if (-not $envMap["ATLAS_VAPID_PUBLIC_KEY"] -or -not $envMap["ATLAS_VAPID_PRIVATE_KEY"]) {
    $pair = & $py -m scripts.vapid_keys
    if ($LASTEXITCODE -ne 0) { Stop-Deploy "could not generate VAPID keys." }
    foreach ($line in $pair) {
        $k, $v = $line -split '=', 2
        Set-EnvValue $envPath $k.Trim() $v.Trim()
    }
    $added += "web push keys"
}
if (-not $envMap["ATLAS_VAPID_SUBJECT"]) {
    $mail = (& $git config user.email)
    if ($mail) { Set-EnvValue $envPath "ATLAS_VAPID_SUBJECT" "mailto:$($mail.Trim())"; $added += "push contact" }
}
if (-not $envMap["ATLAS_INGEST_TOKEN"]) { Set-EnvValue $envPath "ATLAS_INGEST_TOKEN" (New-Token); $added += "ingest token" }
if (-not $envMap["ATLAS_MCP_TOKEN"]) { Set-EnvValue $envPath "ATLAS_MCP_TOKEN" (New-Token); $added += "MCP token" }
# Old Atlas defaults that have a better replacement. Only an exact old default
# moves; anything you set yourself stays.
$moved = @()
if ($envMap["ATLAS_MODEL_SCAN"] -eq "claude-haiku-4-5-20251001") {
    Update-EnvValue $envPath "ATLAS_MODEL_SCAN" "claude-haiku-5-5"
    $moved += "triage model Haiku 4.5 -> Haiku 5.5 (a tenth of the price)"
}
foreach ($m in $moved) { Write-Ok "updated $m" }
if ($added.Count) { Write-Ok ("generated " + ($added -join ", ") + " (values stay in .env, not shown)") } else { Write-Ok ".env complete" }

# ---------------------------------------------------------------- 4. docker

Write-Step "4/10  Postgres and Ollama"
$docker = Find-Exe "docker" @("$env:ProgramFiles\Docker\Docker\resources\bin\docker.exe")
if (-not $docker) { Stop-Deploy "Docker not found. Install Docker Desktop, turn on 'Start when you sign in', then re-run." }
& $docker info *> $null
if ($LASTEXITCODE -ne 0) {
    $desktop = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
    if (Test-Path $desktop) {
        Write-Note "starting Docker Desktop"
        Start-Process $desktop
        $deadline = (Get-Date).AddMinutes(3)
        do { Start-Sleep -Seconds 5; & $docker info *> $null } while ($LASTEXITCODE -ne 0 -and (Get-Date) -lt $deadline)
    }
    if ($LASTEXITCODE -ne 0) { Stop-Deploy "Docker is not running. Start Docker Desktop and re-run." }
}
& $docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
if ($LASTEXITCODE -ne 0) { Stop-Deploy "docker compose up failed; read the output above." }
$deadline = (Get-Date).AddSeconds(120)
$pg = ""
do {
    $pg = (& $docker inspect -f "{{.State.Health.Status}}" atlas-postgres 2>$null)
    if ($pg -eq "healthy") { break }
    Start-Sleep -Seconds 3
} while ((Get-Date) -lt $deadline)
if ($pg -ne "healthy") { Stop-Deploy "Postgres did not become healthy within two minutes (status: $pg). Try: docker compose logs postgres" }
$models = (& $docker exec atlas-ollama ollama list 2>$null) -join "`n"
if ($models -notmatch "nomic-embed-text") {
    Write-Note "pulling the embedding model nomic-embed-text (one time, about 270 MB)"
    & $docker exec atlas-ollama ollama pull nomic-embed-text | Out-Host
    if ($LASTEXITCODE -ne 0) { Stop-Deploy "could not pull nomic-embed-text into atlas-ollama; memory search needs it." }
}
Write-Ok "Postgres healthy, Ollama up with nomic-embed-text"

# ---------------------------------------------------------------- 5. migrations

Write-Step "5/10  Database migrations"
$migrateOut = & cmd.exe /c "scripts\migrate.bat 2>&1"
if ($LASTEXITCODE -ne 0) {
    $migrateOut | Select-Object -Last 25 | Out-Host
    Stop-Deploy "a migration failed; the failing file is named above. Nothing after it ran."
}
$count = @(Get-ChildItem (Join-Path $RepoRoot "db\migrations") -Filter *.sql).Count
Write-Ok "$count migrations applied (each is idempotent)"
# Projects from scripts\seed_projects.py: names, kinds, priorities and objectives
# only; never next actions or touch times, which the loop owns.
& $py -m scripts.seed_projects *> $null
if ($LASTEXITCODE -eq 0) { Write-Ok "projects refreshed" } else { Write-Warn2 "project refresh failed; run python -m scripts.seed_projects to see why" }
# Applications listed in config\jobs.yaml (idempotent; the Gmail tracker keeps them current).
& $py -m scripts.jobs --seed *> $null
if ($LASTEXITCODE -ne 0) { Write-Warn2 "job tracker seed failed; run python -m scripts.jobs --seed to see why" }

# ---------------------------------------------------------------- 6. power

Write-Step "6/10  Power plan"
if ($SkipPowerPlan) {
    Write-Note "skipped (-SkipPowerPlan)"
} else {
    # Idempotent: re-applied on every deploy so fixes to the plan reach the laptop.
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $RepoRoot "scripts\powerplan.ps1")
    if ($LASTEXITCODE -ne 0) { Write-Warn2 "power plan did not apply; Atlas will stop when the laptop sleeps" } else { Write-Ok "Atlas plan active: no sleep on AC, lid close does nothing" }
}

# ---------------------------------------------------------------- 7. services

Write-Step "7/10  Services"
$nssm = Find-Exe "nssm" @("$env:ProgramFiles\nssm\win64\nssm.exe", "$env:LOCALAPPDATA\Microsoft\WinGet\Links\nssm.exe")
if (-not $nssm) { Install-WithWinget "NSSM.NSSM" "NSSM"; $nssm = Find-Exe "nssm" @("$env:LOCALAPPDATA\Microsoft\WinGet\Links\nssm.exe") }
if (-not $nssm) { Stop-Deploy "NSSM installed but not on PATH yet. Close this window and run deploy again." }

$haveWeb = [bool](Get-Service -Name "AtlasWeb" -ErrorAction SilentlyContinue)
if (-not $haveWeb) {
    # A dashboard started by the old start_atlas.bat holds the port; the service could not bind.
    $holder = Get-NetTCPConnection -LocalPort $WebPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($holder) {
        $proc = Get-Process -Id $holder.OwningProcess -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -like "python*") {
            Write-Note "stopping the old start_atlas.bat dashboard (pid $($proc.Id)) so the service can take port $WebPort"
            Stop-Process -Id $proc.Id -Force
        } elseif ($proc) {
            Stop-Deploy "port $WebPort is used by $($proc.ProcessName) (pid $($proc.Id)). Free it or pass -WebPort."
        }
    }
    Write-Note "first run: installing AtlasWeb and AtlasWorker. Windows asks for your password once; the services run as you so backups can reach Docker."
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $RepoRoot "scripts\install_windows_service.ps1") -RunAsCurrentUser -RepoRoot $RepoRoot -Nssm $nssm -WebPort $WebPort
    if ($LASTEXITCODE -ne 0) { Stop-Deploy "service install failed; read the output above." }
    Write-Ok "services installed and started"
} else {
    $svcArgs = @{
        "AtlasWeb"    = "-m scripts.run_web --host 127.0.0.1 --port $WebPort --log-file logs\web.log"
        "AtlasWorker" = "-m scripts.run_worker --log-file logs\worker.log"
    }
    foreach ($pair in @(@("AtlasWeb", "web.log"), @("AtlasWorker", "worker.log"))) {
        $svc = $pair[0]
        # Settings fixed after first install reach existing services here.
        # --log-file: Atlas writes its own log, so NSSM's handles can never block it.
        # AppRotateOnline 0: online rotation froze both services (see install_windows_service.ps1).
        if (Get-Service -Name $svc -ErrorAction SilentlyContinue) {
            & $nssm set $svc AppParameters $svcArgs[$svc] *> $null
            if ($LASTEXITCODE -ne 0) { Write-Warn2 "could not update $svc arguments with nssm (exit $LASTEXITCODE)" }
            & $nssm set $svc AppRotateOnline 0 *> $null
        }
        $state = Restart-AtlasService $svc $pair[1]
        if ($state -eq "running") { Write-Ok "$svc restarted on $after" }
        elseif ($state -eq "missing") { Write-Warn2 "$svc is not installed; run scripts\install_windows_service.ps1 -RunAsCurrentUser" }
        else { Write-Warn2 "$svc did not start (status $state); the log lines above say why" }
    }
}

# ---------------------------------------------------------------- 8. local health

Write-Step "8/10  Local health"
$localUrl = "http://127.0.0.1:$WebPort/api/health"
# web=1: wait on the web process alone; the loop's state is reported below.
$h = Wait-Health "$localUrl`?web=1" 90
if ($h -and $h.status -eq 200) {
    # One more call with deep=1: asks Anthropic whether the key can use the models (free).
    $deepH = Get-Health "$localUrl`?web=1&deep=1" 30
    if ($deepH.status -eq 200) { $h = $deepH }
}
if (-not $h -or $h.status -ne 200) {
    $detail = ""
    if ($h) { $detail = "status $($h.status) $($h.body)" }
    foreach ($name in @("web.log", "web.service.log")) {
        $webLog = Join-Path $RepoRoot "logs\$name"
        if (Test-Path $webLog) {
            Write-Warn2 "last lines of logs\${name}:"
            Get-Content -LiteralPath $webLog -Tail 25 | ForEach-Object { Write-Host "        $_" }
        }
    }
    Stop-Deploy "Atlas did not answer healthy on $localUrl within 90 seconds. $detail"
}
$health = $h.body | ConvertFrom-Json
Write-Ok "web up, Atlas $($health.version), database $($health.db), schema $($health.schema)"
$claudeProblem = ""
if ($health.anthropic) {
    if ($health.anthropic.ok) { Write-Ok "Claude: $($health.anthropic.detail)" }
    else {
        $claudeProblem = [string]$health.anthropic.detail
        Write-Host "    !!  Claude is NOT reachable: $claudeProblem" -ForegroundColor Red
        Write-Host "        The dashboard works, but the CEO and agents cannot answer until this is fixed." -ForegroundColor Red
    }
}
switch ($health.worker.state) {
    "ticking"  { Write-Ok "background loop ticked $($health.worker.age_min) min ago" }
    "starting" { Write-Ok "background loop restarted; first tick within 30 minutes" }
    default    { Write-Warn2 "background loop: $($health.worker.state). $($health.problems -join ' ')" }
}
foreach ($w in @($health.warnings)) { if ($w) { Write-Warn2 $w } }
# The outside alarm: the only thing that notices when the whole laptop is off.
if (-not $envMap["ATLAS_HEARTBEAT_URL"]) {
    Write-Warn2 "no outside alarm: if the laptop sleeps or loses power, nothing tells you. Run scripts\connect.bat to add a free healthchecks.io check (2 minutes)."
}

# Which data sources are connected and whether their tokens still work.
$connOut = & $py -m scripts.connect --check 2>&1 | Select-Object -Skip 1
foreach ($line in $connOut) {
    $t = ([string]$line).Trim()
    if ($t.StartsWith("ok")) { Write-Ok $t.Substring(3).Trim() }
    elseif ($t.StartsWith("!!")) { Write-Warn2 "$($t.Substring(3).Trim()) (fix: scripts\connect.bat)" }
    elseif ($t) { Write-Note "$($t.TrimStart('-').Trim()) (connect: scripts\connect.bat)" }
}

# First mail learning as soon as Gmail is connected; afterwards the worker
# refreshes it daily (06:40) and weekly (Sunday 05:00) on its own.
if ($envMap["ATLAS_GOOGLE_REFRESH_TOKEN"]) {
    Write-Note "learning from Gmail (people, noise, open loops); the first pass takes a minute or two"
    $learnOut = & $py -m scripts.learn_mail --if-older-than 7 2>&1 | Select-Object -Last 1
    if ($LASTEXITCODE -eq 0) { Write-Ok "$learnOut" } else { Write-Warn2 "mail learning failed; run python -m scripts.learn_mail to see why" }
}

# ---------------------------------------------------------------- 9. link

Write-Step "9/10  Permanent link ($Expose)"
$publicUrl = ""

if ($Expose -eq "tailscale") {
    $ts = Find-Exe "tailscale" @("$env:ProgramFiles\Tailscale\tailscale.exe")
    if (-not $ts) { Install-WithWinget "Tailscale.Tailscale" "Tailscale"; $ts = Find-Exe "tailscale" @("$env:ProgramFiles\Tailscale\tailscale.exe") }
    if (-not $ts) { Stop-Deploy "Tailscale installed but not found yet. Close this window and run deploy again." }

    $state = $null
    try { $state = (& $ts status --json 2>$null | Out-String) | ConvertFrom-Json } catch { $state = $null }
    if (-not $state -or $state.BackendState -ne "Running") {
        Write-Note "sign in to Tailscale: open the login link printed below (or click the Tailscale tray icon, Log in)"
        Write-Note "use the account you will also use on the iPhone; this window continues once you are signed in"
        # --unattended keeps the link up when nobody is signed in to Windows.
        & $ts up --unattended
        $deadline = (Get-Date).AddMinutes(5)
        do {
            Start-Sleep -Seconds 3
            try { $state = (& $ts status --json 2>$null | Out-String) | ConvertFrom-Json } catch { $state = $null }
        } while ((-not $state -or $state.BackendState -ne "Running") -and (Get-Date) -lt $deadline)
        if (-not $state -or $state.BackendState -ne "Running") { Stop-Deploy "Tailscale sign-in did not finish. Sign in from the tray icon and run deploy again." }
    }
    $dns = ([string]$state.Self.DNSName).TrimEnd(".")
    if (-not $dns) { Stop-Deploy "Tailscale has no MagicDNS name for this laptop. Turn on MagicDNS at login.tailscale.com/admin/dns and run deploy again." }

    # Funnel would put Atlas on the public internet with no login in front of it.
    $serveJson = (& $ts serve status --json 2>$null | Out-String)
    if ($serveJson -match '"AllowFunnel"\s*:\s*\{\s*"[^"]+"\s*:\s*true') {
        Write-Warn2 "Tailscale Funnel was on (public internet). Turning it off."
        & $ts serve reset | Out-Null
    }

    Write-Note "publishing http://127.0.0.1:$WebPort at https://$dns (tailnet only)"
    Write-Note "if Tailscale asks you to enable HTTPS certificates or Serve, open the link it prints, approve, and it continues"
    & $ts serve --bg --https=443 "http://127.0.0.1:$WebPort"
    if ($LASTEXITCODE -ne 0) { Stop-Deploy "tailscale serve failed. Enable HTTPS at login.tailscale.com/admin/dns (HTTPS Certificates), then run deploy again." }
    $publicUrl = "https://$dns"
    Write-Ok "Serve on; Funnel off"
}

if ($Expose -eq "cloudflare") {
    $cf = Find-Exe "cloudflared" @("${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe", "$env:ProgramFiles\cloudflared\cloudflared.exe")
    if (-not $cf) { Install-WithWinget "Cloudflare.cloudflared" "cloudflared"; $cf = Find-Exe "cloudflared" @("${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe", "$env:ProgramFiles\cloudflared\cloudflared.exe") }
    if (-not $cf) { Stop-Deploy "cloudflared installed but not found yet. Close this window and run deploy again." }
    $svc = Get-Service -Name "cloudflared" -ErrorAction SilentlyContinue
    if (-not $svc) {
        if (-not $TunnelToken) { Stop-Deploy "no cloudflared service yet. Create the tunnel in the Cloudflare dashboard (docs/atlas-local-host.md, section 5) and pass its token with -TunnelToken." }
        & $cf service install $TunnelToken | Out-Host
        if ($LASTEXITCODE -ne 0) { Stop-Deploy "cloudflared service install failed." }
    } elseif ($svc.Status -ne "Running") {
        Start-Service cloudflared
    }
    $publicUrl = "https://$PublicHost"

    # Anonymous probe: anything other than an Access login means the world can open Atlas.
    Start-Sleep -Seconds 5
    $probe = Get-Health "$publicUrl/api/health" 15
    $gated = ($probe.status -in 302, 401, 403) -or ($probe.body -match "cloudflareaccess|Cloudflare Access|cdn-cgi/access")
    if (-not $gated) {
        Stop-Service cloudflared -ErrorAction SilentlyContinue
        Stop-Deploy "$publicUrl answered without Cloudflare Access (status $($probe.status)). The tunnel was stopped so Atlas is not public. Add the Access application from docs/atlas-local-host.md, section 5, then run deploy again."
    }
    Write-Ok "tunnel up; anonymous requests get the Access login"
}

# ---------------------------------------------------------------- 10. end to end

Write-Step "10/10  End to end"
if ($Expose -eq "tailscale") {
    $remote = Wait-Health "$publicUrl/api/health?web=1" 60
    if (-not $remote -or $remote.status -ne 200) {
        $detail = ""
        if ($remote) { $detail = "status $($remote.status)" }
        Write-Warn2 "$publicUrl/api/health did not answer yet ($detail). The first certificate can take a minute; open the link in a browser to finish it."
    } else {
        Write-Ok "$publicUrl answers healthy over HTTPS"
        # The dashboard's writes must pass the request guard over the real
        # link (core/web/guard.py checks the Origin against the Host). Pin the
        # link's host as allowed once, then prove a harmless write gets through.
        $linkHost = ([Uri]$publicUrl).Host
        $allowed = [string](Get-EnvMap $envPath)["ATLAS_ALLOWED_ORIGINS"]
        if (-not ($allowed -split "," | Where-Object { $_.Trim() -eq $linkHost })) {
            $newAllowed = (@($allowed -split "," | Where-Object { $_.Trim() }) + $linkHost) -join ","
            Update-EnvValue $envPath "ATLAS_ALLOWED_ORIGINS" $newAllowed
            Write-Note "allowed the link's host for dashboard writes; restarting the web service"
            Restart-AtlasService "AtlasWeb" "web.log"
            Wait-Health "$localUrl`?web=1" 60 | Out-Null
        }
        try {
            $w = Invoke-WebRequest -Uri "$publicUrl/api/notifications/read" -Method Post -UseBasicParsing -TimeoutSec 15 `
                -ContentType "application/json" -Body '{"ids": []}' `
                -Headers @{ "X-Atlas-Client" = "1"; "Origin" = $publicUrl } -ErrorAction Stop
            Write-Ok "dashboard writes pass the request guard over the link"
        } catch {
            $code = ""
            if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
            Write-Warn2 "a dashboard write over the link was refused ($code). Approvals from the phone may fail; set ATLAS_WRITE_GUARD=false in .env and run deploy again, then report it."
        }
    }
} elseif ($Expose -eq "none") {
    Write-Note "no link published (-Expose none)"
} else {
    Write-Ok "$publicUrl is gated by Cloudflare Access"
}

New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot "logs") | Out-Null
if ($publicUrl) {
    Set-Content -LiteralPath (Join-Path $RepoRoot "logs\public_url.txt") -Value $publicUrl -Encoding ASCII
}

Write-Host ""
if ($claudeProblem) {
    Write-Host "Atlas $version is up, but Claude is not reachable:" -ForegroundColor Yellow
    Write-Host "  $claudeProblem" -ForegroundColor Yellow
    Write-Host "  Fix it (usually ANTHROPIC_API_KEY in .env or credit at console.anthropic.com), then run scripts\deploy.bat again." -ForegroundColor Yellow
} else {
    Write-Host "Atlas $version is live." -ForegroundColor Green
}
Write-Host ""
Write-Host "  On this laptop:  http://localhost:$WebPort"
if ($publicUrl) {
    Write-Host "  Permanent link:  $publicUrl" -ForegroundColor White
    Write-Host ""
    Write-Host "  iPhone, once:"
    if ($Expose -eq "tailscale") {
        Write-Host "    1. Install Tailscale from the App Store and sign in with the same account. Leave it connected."
        Write-Host "    2. Open $publicUrl in Safari."
    } else {
        Write-Host "    1. Open $publicUrl in Safari and enter the one-time code Cloudflare emails you."
        Write-Host "    2. Stay on the dashboard that loads."
    }
    Write-Host "    3. Share, Add to Home Screen. Open Atlas from the home screen icon, not from Safari."
    Write-Host "    4. In Atlas, tap the bell at the top right and allow notifications."
}
Write-Host ""
Write-Host "  Update later: run scripts\deploy.bat again. It pulls, migrates and restarts in one go."
Write-Host "  Logs: logs\web.log, logs\worker.log (and logs\*.service.log for crashes before Atlas starts)"
if ($publicUrl) { Start-Process $publicUrl } else { Start-Process "http://localhost:$WebPort" }
if (-not $NoPause) { Read-Host "Press Enter to close" | Out-Null }
