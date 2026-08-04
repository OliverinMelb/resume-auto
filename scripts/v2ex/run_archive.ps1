[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PythonPath,

    [string]$RepositoryPath = "",

    [string]$DatabasePath = "",

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
if (-not (Test-Path -LiteralPath $resolvedRepository -PathType Container)) {
    throw "RepositoryPath must identify a directory."
}

$arguments = @("-m", "job_sources.v2ex.archive")
if (-not [string]::IsNullOrWhiteSpace($DatabasePath)) {
    if (-not [IO.Path]::IsPathRooted($DatabasePath)) {
        throw "DatabasePath must be absolute when supplied."
    }
    $arguments += @("--database", [IO.Path]::GetFullPath($DatabasePath))
}

if ($DryRun) {
    Write-Output "python=$resolvedPython"
    Write-Output "module=job_sources.v2ex.archive"
    Write-Output "repository=$resolvedRepository"
    if (-not [string]::IsNullOrWhiteSpace($DatabasePath)) {
        Write-Output "database=$([IO.Path]::GetFullPath($DatabasePath))"
    }
    exit 0
}

$exitCode = 1
Push-Location -LiteralPath $resolvedRepository
try {
    & $resolvedPython @arguments
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $exitCode
