[CmdletBinding()]
param([string]$TaskName = "AutoSearch", [int]$Minutes = 30)
$ErrorActionPreference = "Stop"
if ($Minutes -lt 1) { throw "Minutes must be positive." }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw "Task already exists. Choose a different TaskName or use the existing task."
}
$pythonPath = (Get-Command python.exe).Source
$entryPath = Join-Path $PSScriptRoot "autosearch.py"
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument ('"{0}" run' -f $entryPath) -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$options = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $options -Description "AutoSearch research reports (current user)"

