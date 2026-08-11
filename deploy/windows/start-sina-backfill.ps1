[CmdletBinding()]
param(
    [string] $InstallRoot = 'E:\StockMcp',
    [string] $Manifest,
    [string] $EvidenceRoot,
    [switch] $Worker,
    [string] $RunDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
if ([string]::IsNullOrWhiteSpace($Manifest)) {
    $Manifest = Join-Path $InstallRoot 'config\sina-backfill-manifest.json'
}
$Manifest = [IO.Path]::GetFullPath($Manifest)
if ([string]::IsNullOrWhiteSpace($EvidenceRoot)) {
    $EvidenceRoot = Join-Path $InstallRoot 'state\sina-backfill-runs'
}
$EvidenceRoot = [IO.Path]::GetFullPath($EvidenceRoot)
$python = Join-Path $InstallRoot 'current\.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Installed Python was not found: $python"
}
if (-not (Test-Path -LiteralPath $Manifest -PathType Leaf)) {
    throw "Sina backfill manifest was not found: $Manifest"
}

if ($Worker) {
    if ([string]::IsNullOrWhiteSpace($RunDirectory)) { throw 'Worker requires RunDirectory.' }
    $RunDirectory = [IO.Path]::GetFullPath($RunDirectory)
    New-Item -ItemType Directory -Path $RunDirectory -Force | Out-Null
    $log = Join-Path $RunDirectory 'run.log'
    $exitCodeFile = Join-Path $RunDirectory 'exit-code.txt'
    $mutex = New-Object Threading.Mutex($false, 'Global\StockMcpSinaBackfill')
    $acquired = $false
    $exitCode = 1
    try {
        $acquired = $mutex.WaitOne(0)
        if (-not $acquired) { throw 'Another Sina backfill worker is already running.' }
        $PID | Set-Content -LiteralPath (Join-Path $RunDirectory 'pid.txt') -Encoding ASCII
        "RUN $(Get-Date -Format o)" | Set-Content -LiteralPath $log -Encoding UTF8
        & $python -m stock_mcp.cli backfill-sina --root $InstallRoot --manifest $Manifest 2>&1 |
            Tee-Object -FilePath $log -Append
        $exitCode = $LASTEXITCODE
        "RESULT exit_code=$exitCode $(Get-Date -Format o)" | Add-Content -LiteralPath $log -Encoding UTF8
    } catch {
        "FAIL $($_.Exception.Message)" | Add-Content -LiteralPath $log -Encoding UTF8
        $exitCode = 1
    } finally {
        $exitCode | Set-Content -LiteralPath $exitCodeFile -Encoding ASCII
        if ($acquired) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
    exit $exitCode
}

New-Item -ItemType Directory -Path $EvidenceRoot -Force | Out-Null
$latest = Join-Path $EvidenceRoot 'latest-run.txt'
if (Test-Path -LiteralPath $latest -PathType Leaf) {
    $previousRun = (Get-Content -LiteralPath $latest -Raw).Trim()
    $previousPidFile = Join-Path $previousRun 'pid.txt'
    $previousExitCode = Join-Path $previousRun 'exit-code.txt'
    if ((Test-Path -LiteralPath $previousPidFile) -and -not (Test-Path -LiteralPath $previousExitCode)) {
        $previousPid = [int](Get-Content -LiteralPath $previousPidFile -Raw).Trim()
        if ($null -ne (Get-Process -Id $previousPid -ErrorAction SilentlyContinue)) {
            throw "A Sina backfill is already running with PID $previousPid."
        }
    }
}

$sourceRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$sourceStatus = (& git -C $sourceRoot status --porcelain | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the source checkout.' }
if (-not [string]::IsNullOrWhiteSpace($sourceStatus)) {
    throw 'Source checkout has uncommitted or untracked changes.'
}
$sourceCommit = (& git -C $sourceRoot rev-parse HEAD | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $sourceCommit -notmatch '^[0-9a-f]{40}$') {
    throw 'Could not record the source commit for the Sina backfill round.'
}
$originMain = (& git -C $sourceRoot rev-parse origin/main | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $originMain -ne $sourceCommit) {
    throw 'Source checkout does not match origin/main. Run git pull --ff-only first.'
}
$runName = 'run-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
$RunDirectory = Join-Path $EvidenceRoot $runName
New-Item -ItemType Directory -Path $RunDirectory -Force | Out-Null
$installedManifest = Get-Content -LiteralPath (Join-Path $InstallRoot 'current\release-manifest.json') `
    -Raw | ConvertFrom-Json
$backfillManifest = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
$metadata = [ordered]@{
    round_id = $runName
    source_commit = $sourceCommit
    installed_source_commit = [string] $installedManifest.source_commit
    manifest_hash = [string] $backfillManifest.manifest_hash
    install_root = $InstallRoot
    manifest = $Manifest
    created_at = [DateTimeOffset]::UtcNow.ToString('o')
    command = 'stock-mcp backfill-sina'
}
[IO.File]::WriteAllText(
    (Join-Path $RunDirectory 'run-metadata.json'),
    ($metadata | ConvertTo-Json),
    [Text.UTF8Encoding]::new($false)
)
$arguments = (
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Worker ' +
    '-InstallRoot "{1}" -Manifest "{2}" -EvidenceRoot "{3}" -RunDirectory "{4}"'
) -f $PSCommandPath, $InstallRoot, $Manifest, $EvidenceRoot, $RunDirectory
$process = Start-Process powershell.exe -ArgumentList $arguments -WindowStyle Hidden -PassThru
$process.Id | Set-Content -LiteralPath (Join-Path $RunDirectory 'pid.txt') -Encoding ASCII
$RunDirectory | Set-Content -LiteralPath $latest -Encoding UTF8
[ordered]@{
    status = 'started'
    pid = $process.Id
    run_directory = $RunDirectory
    log = (Join-Path $RunDirectory 'run.log')
    exit_code = (Join-Path $RunDirectory 'exit-code.txt')
} | ConvertTo-Json
