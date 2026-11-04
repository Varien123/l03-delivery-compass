# Register a daily 09:00 auto-update task for the current Windows user.
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\install_task.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = (Get-Command python -ErrorAction Stop).Source
$script = Join-Path $root "auto_update.py"
$taskName = "L03-Delivery-Compass-Auto-Update"

$action = New-ScheduledTaskAction `
  -Execute $python `
  -Argument "`"$script`"" `
  -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -Daily -At 09:00

$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
  -TaskName $taskName `
  -Action $action `
  -Trigger $trigger `
  -Settings $settings `
  -Description "Daily L03 delivery/capacity source fetch and forecast rebuild." `
  -Force | Out-Null

Write-Host "Registered task: $taskName"
Write-Host "Schedule: daily 09:00 local time"
Write-Host "Python: $python"
Write-Host "Script: $script"
Write-Host ""
Write-Host "Query: Get-ScheduledTask -TaskName '$taskName'"
Write-Host "Run now: Start-ScheduledTask -TaskName '$taskName'"
Write-Host "Delete: Unregister-ScheduledTask -TaskName '$taskName' -Confirm:`$false"
