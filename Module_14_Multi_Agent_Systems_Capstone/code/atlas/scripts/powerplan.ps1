<#
.SYNOPSIS
    Apply an "Atlas" power plan: never sleep on AC, lid close does nothing,
    network stays up. Battery (DC) behaviour is left at Windows defaults.

.DESCRIPTION
    Atlas died in June because the laptop slept. This script makes the laptop
    behave like a small server while it is plugged in:

      * standby, hibernate and hybrid sleep on AC set to never
      * lid close on AC does nothing (lid closed, laptop keeps running)
      * disk idle timeout on AC set to never
      * wake timers allowed, so a scheduled job can wake a dozing machine
      * network connectivity in standby enabled (Modern Standby machines)
      * every physical network adapter told not to power down to save energy
      * the display still turns off after 10 minutes, which is harmless

    Idempotent: re-running updates the existing "Atlas" plan. Run from an
    elevated PowerShell at the repo root:

        powershell -ExecutionPolicy Bypass -File scripts\powerplan.ps1

    Revert (Balanced active again, Atlas plan deleted, NIC power saving on):

        powershell -ExecutionPolicy Bypass -File scripts\powerplan.ps1 -Revert
#>
[CmdletBinding()]
param(
    [string]$PlanName = "Atlas",
    [switch]$Revert
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this from an elevated (Administrator) PowerShell."
    }
}

function Get-PlanGuid([string]$name) {
    # powercfg /list prints lines like: "Power Scheme GUID: <guid>  (<name>) *"
    $pattern = "\(\s*" + [regex]::Escape($name) + "\s*\)"
    $line = powercfg /list | Where-Object { $_ -match $pattern } | Select-Object -First 1
    if ($line -and $line -match "([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})") { return $Matches[1] }
    return $null
}

function Set-AcValue([string]$scheme, [string]$sub, [string]$setting, [int]$value) {
    $out = powercfg /setacvalueindex $scheme $sub $setting $value 2>&1
    if ($LASTEXITCODE -ne 0) { throw "powercfg /setacvalueindex $sub $setting failed: $out" }
}

function Set-NicPowerOff([bool]$allowOff) {
    # "Allow the computer to turn off this device to save power" is the WMI
    # class MSPower_DeviceEnable; Set-NetAdapterPowerManagement cannot set it.
    $switches = @()
    try { $switches = @(Get-CimInstance -Namespace root\wmi -ClassName MSPower_DeviceEnable -ErrorAction Stop) }
    catch { Write-Warning "[powerplan] cannot read device power settings: $($_.Exception.Message)"; return }
    foreach ($nic in @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue)) {
        $name = $nic.Name
        $pnp = [string]$nic.PnPDeviceID
        $sw = $switches | Where-Object { $pnp -and ([string]$_.InstanceName).StartsWith($pnp, [StringComparison]::OrdinalIgnoreCase) } | Select-Object -First 1
        if (-not $sw) { Write-Host "[powerplan] '$name': no power-off switch exposed (fine)"; continue }
        try {
            Set-CimInstance -InputObject $sw -Property @{ Enable = $allowOff } -ErrorAction Stop
            if ($allowOff) { Write-Host "[powerplan] '$name': power saving restored" }
            else { Write-Host "[powerplan] '$name': Windows may not turn off this device" }
        } catch {
            Write-Warning "[powerplan] could not change power management on '$name': $($_.Exception.Message)"
        }
    }
}

Assert-Admin

# Well-known GUIDs. The SUB_* aliases work on every supported Windows; the
# standby-networking setting has no alias and only exists on Modern Standby.
$SchemeHighPerf   = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
$SchemeBalanced   = "381b4222-f694-41f0-9685-ff5bb260df2e"
$SubNone          = "fea3413e-7e05-4911-9a71-700331f1c294"
$NetworkInStandby = "f15576e8-98b7-4186-b944-eafa664402d9"

if ($Revert) {
    $guid = Get-PlanGuid $PlanName
    powercfg /setactive $SchemeBalanced | Out-Null
    if ($guid) { powercfg /delete $guid | Out-Null }
    Set-NicPowerOff $true
    Write-Host "[powerplan] Balanced is active again; '$PlanName' removed; NIC power saving restored."
    exit 0
}

$guid = Get-PlanGuid $PlanName
if (-not $guid) {
    $out = powercfg /duplicatescheme $SchemeHighPerf
    if ("$out" -notmatch "([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})") { throw "powercfg /duplicatescheme failed: $out" }
    $guid = $Matches[1]
    powercfg /changename $guid $PlanName "Atlas host: never sleep on AC, lid close ignored, network stays up." | Out-Null
    Write-Host "[powerplan] created plan '$PlanName' ($guid) from High performance"
} else {
    Write-Host "[powerplan] updating existing plan '$PlanName' ($guid)"
}

# AC values only. 0 is "never" for timeouts and "do nothing" for the lid.
Set-AcValue $guid SUB_SLEEP   STANDBYIDLE   0
Set-AcValue $guid SUB_SLEEP   HIBERNATEIDLE 0
Set-AcValue $guid SUB_SLEEP   HYBRIDSLEEP   0
Set-AcValue $guid SUB_SLEEP   RTCWAKE       1
Set-AcValue $guid SUB_BUTTONS LIDACTION     0
Set-AcValue $guid SUB_DISK    DISKIDLE      0
Set-AcValue $guid SUB_VIDEO   VIDEOIDLE   600

# Networking connectivity in Standby = Enabled. Absent on older hardware.
try { Set-AcValue $guid $SubNone $NetworkInStandby 1 }
catch { Write-Host "[powerplan] no Modern Standby networking setting on this machine (fine)" }

powercfg /setactive $guid | Out-Null
Write-Host "[powerplan] '$PlanName' is the active plan"

# Stop Windows from powering down the NICs. Covers Wi-Fi and Ethernet.
Set-NicPowerOff $false

Write-Host ""
Write-Host "[powerplan] done. Verify with:  powercfg /query $guid SUB_SLEEP STANDBYIDLE"
Write-Host "[powerplan] Battery behaviour is unchanged; unplugged, the laptop still sleeps."
