[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string] $InstallRoot = 'C:\ProgramData\StockMcp',
    [switch] $PurgeData
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'deploy\lib.ps1')

function Wait-ServiceRemoval([string] $Name, [int] $Attempts = 12) {
    for ($i = 1; $i -le $Attempts; $i++) {
        if ($null -eq (Get-Service -Name $Name -ErrorAction SilentlyContinue)) { return }
        Start-Sleep -Seconds 1
    }
    throw "Service still exists after uninstall: $Name"
}

Test-Administrator
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
Stop-StockServices
$winsw = Join-Path $InstallRoot 'runtime\tools\WinSW.exe'
if (Test-Path -LiteralPath $winsw) {
    foreach ($service in @('StockMcpTunnel', 'StockMcpService')) {
        $xml = Join-Path $InstallRoot ("runtime\services\{0}.xml" -f $service)
        if (Test-Path -LiteralPath $xml) { & $winsw uninstall $xml | Out-Null }
    }
}
foreach ($service in @('StockMcpTunnel', 'StockMcpService')) {
    if ($null -ne (Get-Service -Name $service -ErrorAction SilentlyContinue)) {
        & sc.exe delete $service | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not remove service: $service" }
    }
    Wait-ServiceRemoval $service
}

foreach ($path in @('current', 'releases', 'runtime')) {
    $target = Join-Path $InstallRoot $path
    if ((Test-Path -LiteralPath $target) -and $PSCmdlet.ShouldProcess($target, 'Remove program and service runtime')) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

if ($PurgeData) {
    if (-not $PSCmdlet.ShouldContinue('Delete all Stock MCP data, configuration, logs and backups? This cannot be undone.', 'Confirm -PurgeData')) {
        Write-Host 'Data was retained.'
        exit 0
    }
    foreach ($path in @('config', 'data', 'logs', 'backups', 'state')) {
        $target = Join-Path $InstallRoot $path
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
    }
    if (Test-Path -LiteralPath $InstallRoot) { Remove-Item -LiteralPath $InstallRoot -Force -ErrorAction SilentlyContinue }
} else {
    Write-Host "Services and program files removed. Configuration, data, logs and backups remain in $InstallRoot."
}
