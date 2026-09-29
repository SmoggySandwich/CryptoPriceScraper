# Registers the scraper as a Windows scheduled task that runs at logon and then
# loops forever. Run once, from an ordinary (non-elevated) PowerShell -- no
# administrator prompt is needed:
#
#   powershell -ExecutionPolicy Bypass -File deploy\install-windows-task.ps1
#
# Remove it again with:
#
#   Unregister-ScheduledTask -TaskName CryptoPriceScraper -Confirm:$false
#
# Why a logon trigger rather than a repeating one: Task Scheduler refuses any
# repetition below one minute outright (HRESULT 0x80041318), so a ten-second
# cadence cannot be expressed as a schedule. It does not need to be. The program
# loops internally and aligns itself to the wall-clock grid, so the trigger only
# has to start it once -- at logon, which is a one-shot trigger and therefore
# exempt from the sub-minute restriction. A *repeating* task would also be
# wrong on its own terms: it would start a fresh process every tick, which is
# what loop mode exists to avoid.
#
# The task runs pythonw.exe so no console window appears at logon. The cost is
# that the process has no stdout or stderr: every diagnostic goes to
# logs\scraper.log, which the program writes itself, so nothing is lost.

[CmdletBinding()]
param(
    # Python interpreter to use. Defaults to pythonw.exe on PATH, falling back
    # to python.exe if this is a minimal install with no windowed launcher.
    [string]$Python,
    [string]$TaskName = 'CryptoPriceScraper'
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$entry = Join-Path $root 'main.py'
if (-not (Test-Path -LiteralPath $entry)) {
    throw "main.py not found at $entry -- run this from the repository checkout."
}

if (-not $Python) {
    foreach ($candidate in 'pythonw.exe', 'python.exe') {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($found) { $Python = $found.Source; break }
    }
}
if (-not $Python -or -not (Test-Path -LiteralPath $Python)) {
    throw "No Python interpreter found. Pass one explicitly: -Python C:\Python313\pythonw.exe"
}

# Register only ever runs the executable by path, so an unqualified name from
# PATH would be resolved at task-run time against a different environment.
$Python = (Resolve-Path -LiteralPath $Python).Path

$action = New-ScheduledTaskAction -Execute $Python -Argument 'main.py' -WorkingDirectory $root

# The explicit -User is what keeps this an ordinary-user operation. A bare
# -AtLogOn produces a trigger with no principal, meaning "any user", and
# registering that is an administrative act: it fails with Access is denied from
# a normal prompt. Pinning the trigger to the account running this script both
# describes what we actually want and avoids needing elevation. The name must be
# qualified -- DOMAIN\user, which is what WindowsIdentity.Name returns on a
# domain-joined machine and MACHINE\user on a standalone one -- because the bare
# account name is rejected as a malformed principal.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
# Let the network come up first. Without this the first tick usually fails,
# which is harmless but noisy.
$trigger.Delay = 'PT30S'

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 3

# -ExecutionTimeLimit ([TimeSpan]::Zero) is the load-bearing one. Task
# Scheduler's default limit is three days, so leaving it unset would kill a
# correctly working scraper every third day -- silently, as a "task stopped"
# event nobody is watching. Zero means no limit.
#
# -RestartInterval/-RestartCount only fire on a non-zero exit. The program exits
# 4 after thirty consecutive failed ticks, about five minutes of collecting
# nothing, precisely so this can restart it; transient failures are ridden out
# in-process and never reach here.
#
# -MultipleInstances IgnoreNew is belt and braces. The program takes its own
# instance lock, so a second copy would exit 2 immediately anyway.

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description 'Polls BTC/ETH/HYPE prices into daily CSVs.' `
    -Force | Out-Null

$info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
Write-Host "Registered '$TaskName'."
Write-Host "  interpreter : $Python"
Write-Host "  working dir : $root"
Write-Host ''
Write-Host 'It starts at your next logon. To start it now:'
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host 'To watch it:'
Write-Host "  Get-Content '$root\logs\scraper.log' -Wait -Tail 20"
