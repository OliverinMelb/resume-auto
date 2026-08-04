[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PythonPath,

    [string]$RepositoryPath = "",

    [string]$DatabasePath = "",

    [ValidatePattern("^(?:[01]\d|2[0-3]):[0-5]\d$")]
    [string]$At = "09:00",

    [ValidatePattern("^[^\\/]+$")]
    [string]$TaskName = "CzcGoodJob-V2EX-Daily-Archive",

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if (-not [IO.Path]::IsPathRooted($PythonPath)) {
    throw "PythonPath must be absolute."
}
$resolvedPython = (Resolve-Path -LiteralPath $PythonPath).Path
if (-not (Test-Path -LiteralPath $resolvedPython -PathType Leaf)) {
    throw "PythonPath must identify a file."
}

if ([string]::IsNullOrWhiteSpace($RepositoryPath)) {
    $RepositoryPath = [IO.Path]::GetFullPath(
        (Join-Path $PSScriptRoot "..\..")
    )
}
if (-not [IO.Path]::IsPathRooted($RepositoryPath)) {
    throw "RepositoryPath must be absolute."
}
$resolvedRepository = (Resolve-Path -LiteralPath $RepositoryPath).Path
$runScript = Join-Path $resolvedRepository "scripts\v2ex\run_archive.ps1"
if (-not (Test-Path -LiteralPath $runScript -PathType Leaf)) {
    throw "The V2EX archive runner was not found in RepositoryPath."
}

$resolvedDatabase = ""
if (-not [string]::IsNullOrWhiteSpace($DatabasePath)) {
    if (-not [IO.Path]::IsPathRooted($DatabasePath)) {
        throw "DatabasePath must be absolute when supplied."
    }
    $resolvedDatabase = [IO.Path]::GetFullPath($DatabasePath)
}

$actionArguments = (
    "-NoProfile -ExecutionPolicy Bypass -File `"$runScript`" " +
    "-PythonPath `"$resolvedPython`" " +
    "-RepositoryPath `"$resolvedRepository`""
)
if ($resolvedDatabase) {
    $actionArguments += " -DatabasePath `"$resolvedDatabase`""
}

if ($DryRun) {
    Write-Output "task=$TaskName"
    Write-Output "schedule=Daily $At"
    Write-Output "start_when_available=True"
    Write-Output "multiple_instances=IgnoreNew"
    Write-Output "python=$resolvedPython"
    Write-Output "repository=$resolvedRepository"
    Write-Output "action=powershell.exe $actionArguments"
    exit 0
}

$atTime = [datetime]::ParseExact(
    $At,
    "HH:mm",
    [Globalization.CultureInfo]::InvariantCulture
)
$action = New-ScheduledTaskAction `
    -Execute (Join-Path $PSHOME "powershell.exe") `
    -Argument $actionArguments `
    -WorkingDirectory $resolvedRepository
$trigger = New-ScheduledTaskTrigger -Daily -At $atTime
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal `
    -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Archive the free V2EX jobs JSON Feed once per day." `
    -Force | Out-Null

Write-Output "Installed scheduled task: $TaskName"
