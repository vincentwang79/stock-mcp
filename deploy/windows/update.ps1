[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $PackagePath,
    [Parameter(Mandatory = $true)][string] $PackageSha256,
    [string] $InstallRoot = 'C:\ProgramData\StockMcp'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'deploy\lib.ps1')

Test-Administrator
Assert-WindowsX64
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot 'current'))) { throw 'Stock MCP is not installed.' }
if (-not (Test-Path -LiteralPath $PackagePath -PathType Leaf)) { throw "Package not found: $PackagePath" }
Assert-Sha256 $PackagePath $PackageSha256

$work = Join-Path ([IO.Path]::GetTempPath()) ("stock-mcp-update-" + [guid]::NewGuid().ToString('N'))
$backupRoot = Join-Path $InstallRoot ('backups\update-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$oldTarget = (Get-Item -LiteralPath (Join-Path $InstallRoot 'current')).Target
$newRelease = $null
$newReleaseStaging = $null
$newReleaseCreated = $false
$oldPython = $null
$databaseBackup = $null
$servicesStopped = $false
New-Item -ItemType Directory -Path $work, $backupRoot -Force | Out-Null
try {
    Expand-Archive -LiteralPath $PackagePath -DestinationPath $work -Force
    $topLevel = @(Get-ChildItem -LiteralPath $work -Force)
    $directories = @($topLevel | Where-Object { $_.PSIsContainer })
    if ($directories.Count -ne 1 -or $topLevel.Count -ne 1) { throw 'Update ZIP must contain exactly one release directory.' }
    $extracted = $directories[0]
    Assert-ReleaseContents $extracted.FullName
    $version = Get-ReleaseVersion $extracted.FullName
    $newRelease = Join-Path $InstallRoot ("releases\" + $version)
    if (Test-Path -LiteralPath $newRelease) { throw "Release is already installed: $version" }
    $newReleaseStaging = Join-Path $InstallRoot ("releases\.staging-" + $version + '-' + [guid]::NewGuid().ToString('N'))

    $oldPython = Join-Path $oldTarget '.venv\Scripts\python.exe'
    $databaseBackup = Join-Path $backupRoot 'stock-mcp-before-update.sqlite3'
    & $oldPython -m stock_mcp.cli backup --root $InstallRoot --destination $databaseBackup
    if ($LASTEXITCODE -ne 0) { throw 'Verified online database backup failed.' }
    $servicesStopped = $true
    Stop-StockServices
    Copy-Item -LiteralPath $oldTarget -Destination (Join-Path $backupRoot 'previous-release') -Recurse -Force
    New-Item -ItemType Directory -Path $newReleaseStaging -Force | Out-Null
    Copy-Item -Path (Join-Path $extracted.FullName 'app\*') -Destination $newReleaseStaging -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $extracted.FullName 'deploy') -Destination (Join-Path $newReleaseStaging 'deploy') -Recurse -Force

    $uv = Join-Path $InstallRoot 'runtime\tools\uv.exe'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $InstallRoot 'runtime\python'
    $env:UV_CACHE_DIR = Join-Path $InstallRoot 'runtime\uv-cache'
    & $uv venv (Join-Path $newReleaseStaging '.venv') --python 3.12
    if ($LASTEXITCODE -ne 0) { throw 'uv could not prepare Python 3.12.' }
    Push-Location $newReleaseStaging
    try { & $uv sync --locked --no-dev --no-editable --extra providers; if ($LASTEXITCODE -ne 0) { throw 'uv sync failed.' } }
    finally { Pop-Location }

    $python = Join-Path $newReleaseStaging '.venv\Scripts\python.exe'
    & $python -m stock_mcp.cli migrate --root $InstallRoot
    if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }
    # Keep the literal command as an operator-visible acceptance criterion as well.
    & $python -m stock_mcp.cli doctor --root $InstallRoot # stock-mcp doctor
    if ($LASTEXITCODE -ne 0) { throw 'stock-mcp doctor failed.' }

    Move-Item -LiteralPath $newReleaseStaging -Destination $newRelease
    $newReleaseCreated = $true
    $newReleaseStaging = $null
    New-CurrentJunction $InstallRoot $newRelease
    Start-Service -Name StockMcpService
    if (-not (Wait-LocalReady)) { throw 'Readiness check failed after update.' }
    Start-Service -Name StockMcpTunnel
    Write-Host "Updated to $version. Backup: $backupRoot"
} catch {
    $failure = $_
    Write-Warning "Update failed; Rollback is starting: $($failure.Exception.Message)"
    if ($servicesStopped) {
        try {
            Stop-StockServices
            if ($oldTarget -and (Test-Path -LiteralPath $oldTarget)) { New-CurrentJunction $InstallRoot $oldTarget }
            if ($databaseBackup -and (Test-Path -LiteralPath $databaseBackup)) {
                & $oldPython -m stock_mcp.cli restore --root $InstallRoot --source $databaseBackup
                if ($LASTEXITCODE -ne 0) { throw 'Database restore failed during Rollback.' }
            }
            Start-Service -Name StockMcpService -ErrorAction SilentlyContinue
            Start-Service -Name StockMcpTunnel -ErrorAction SilentlyContinue
        } catch { Write-Warning "Rollback encountered an error: $($_.Exception.Message)" }
    }
    if ($newReleaseCreated -and $newRelease -and (Test-Path -LiteralPath $newRelease) -and
        $newRelease -ne (Get-Item -LiteralPath (Join-Path $InstallRoot 'current')).Target) {
        try { Remove-Item -LiteralPath $newRelease -Recurse -Force }
        catch { Write-Warning "Could not remove failed release: $newRelease" }
    }
    if ($newReleaseStaging -and (Test-Path -LiteralPath $newReleaseStaging)) {
        try { Remove-Item -LiteralPath $newReleaseStaging -Recurse -Force }
        catch { Write-Warning "Could not remove failed staging release: $newReleaseStaging" }
    }
    throw $failure
} finally {
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
}
