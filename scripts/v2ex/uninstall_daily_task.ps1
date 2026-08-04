[CmdletBinding()]
param(
    [ValidatePattern("^[^\\/]+$")]
    [string]$TaskName = "CzcGoodJob-V2EX-Daily-Archive",

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if ($DryRun) {
    Write-Output "Would remove scheduled task: $TaskName"
    exit 0
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Output "Scheduled task not found: $TaskName"
    exit 0
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Output "Removed scheduled task: $TaskName"
