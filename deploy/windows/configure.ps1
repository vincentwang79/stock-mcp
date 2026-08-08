[CmdletBinding()]
param([string] $InstallRoot = 'C:\ProgramData\StockMcp')

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'deploy\lib.ps1')

function Get-Plaintext([Security.SecureString] $Value) {
    return (New-Object System.Net.NetworkCredential('', $Value)).Password
}

Test-Administrator
Assert-WindowsX64
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$configDirectory = Join-Path $InstallRoot 'config'
if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot 'current'))) { throw 'Stock MCP is not installed.' }

$tushare = Read-Host 'Tushare token' -AsSecureString
$tunnelId = Read-Host 'Platform Tunnel ID' -AsSecureString
$tunnelKey = Read-Host 'Tunnel runtime API key' -AsSecureString
$proxy = Read-Host 'Optional HTTPS proxy (leave blank)' -AsSecureString
$customCa = Read-Host 'Optional custom CA file path (leave blank)' -AsSecureString

$customCaValue = Get-Plaintext $customCa
$managedCa = ''
if (-not [string]::IsNullOrWhiteSpace($customCaValue)) {
    if (-not (Test-Path -LiteralPath $customCaValue -PathType Leaf)) { throw "Custom CA file not found: $customCaValue" }
    $managedCa = Join-Path $configDirectory 'custom-ca.pem'
    Copy-Item -LiteralPath $customCaValue -Destination $managedCa -Force
    Set-PrivateFileAcl $managedCa -Reader StockMcpService -SharedWithOtherService
}

$secretFile = Join-Path $configDirectory 'secrets.env'
$lines = @(
    'TUSHARE_TOKEN=' + (Get-Plaintext $tushare),
    'HTTPS_PROXY=' + (Get-Plaintext $proxy),
    'STOCK_MCP_CA_FILE=' + $managedCa
)
[IO.File]::WriteAllLines($secretFile, $lines, [Text.UTF8Encoding]::new($false))
# The MCP application can read only its Tushare configuration. It cannot modify
# code/tools and it cannot read the Tunnel runtime-key file.
Set-PrivateFileAcl $secretFile -Reader StockMcpService
Set-PrivateAcl $configDirectory -ReadableByApp -ReadableByTunnel

# The profile contains only a file reference to the runtime key. The key itself is
# never passed in argv and never embedded in service XML or the profile export.
$tunnelKeyFile = Join-Path $configDirectory 'tunnel-api-key'
[IO.File]::WriteAllText($tunnelKeyFile, (Get-Plaintext $tunnelKey), [Text.UTF8Encoding]::new($false))
$profilePath = (Join-Path $configDirectory 'tunnel-client.yaml').Replace('\', '/')
$keyReference = $tunnelKeyFile.Replace('\', '/')
$profileLines = @(
    'config_version: 1',
    'control_plane:',
    "  tunnel_id: '$(Get-Plaintext $tunnelId)'",
    "  api_key: 'file:$keyReference'",
    'health:',
    '  listen_addr: 127.0.0.1:8766',
    'admin_ui:',
    '  open_browser: false',
    'mcp:',
    '  server_urls:',
    '    - channel: main',
    '      url: http://127.0.0.1:8765/mcp'
)
$proxyValue = Get-Plaintext $proxy
if (-not [string]::IsNullOrWhiteSpace($proxyValue)) {
    $profileLines += "http_proxy: '$proxyValue'"
}
if (-not [string]::IsNullOrWhiteSpace($managedCa)) {
    $profileLines += "ca_bundle: '$($managedCa.Replace('\', '/'))'"
}
[IO.File]::WriteAllLines($profilePath, $profileLines, [Text.UTF8Encoding]::new($false))
Set-PrivateFileAcl $tunnelKeyFile -Reader StockMcpTunnel
Set-PrivateFileAcl $profilePath -Reader StockMcpTunnel
Set-PrivateAcl $configDirectory -ReadableByApp -ReadableByTunnel

$servicePython = Join-Path $InstallRoot 'current\.venv\Scripts\python.exe'
$cli = Join-Path $InstallRoot 'current\.venv\Scripts\stock-mcp.exe'
if (-not (Test-Path -LiteralPath $servicePython)) { throw 'Isolated application Python is missing.' }
& $servicePython -m stock_mcp.cli doctor --root $InstallRoot
if ($LASTEXITCODE -ne 0) { throw 'Data permissions/configuration probe failed.' }

# Build the replay baseline before the scheduler can publish a normal daily review.
# Yesterday is used so configuration never mistakes an in-progress trading day for
# an incomplete historical source response. The command is idempotent and resumes
# only missing dates after an interruption.
$chinaTimeZone = [TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time')
$chinaNow = [TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow, $chinaTimeZone)
$backfillEnd = $chinaNow.Date.AddDays(-1)
$backfillStart = $backfillEnd.AddYears(-3)
& $servicePython -m stock_mcp.cli backfill --root $InstallRoot `
    --start $backfillStart.ToString('yyyy-MM-dd') --end $backfillEnd.ToString('yyyy-MM-dd')
if ($LASTEXITCODE -ne 0) { throw 'Three-year historical backfill is incomplete; rerun configure.ps1.' }

$tunnelClient = Join-Path $InstallRoot 'runtime\tools\tunnel-client.exe'
# Always restart both services so readiness proves the configured virtual service
# identities can read their respective protected files, rather than an old process.
Stop-StockServices
Start-Service -Name StockMcpService
if (-not (Wait-LocalReady)) { throw 'MCP local readiness check failed.' }
& $tunnelClient doctor --config $profilePath --explain
if ($LASTEXITCODE -ne 0) { throw 'Tunnel doctor failed.' }
Start-Service -Name StockMcpTunnel
if (-not (Wait-TunnelReady)) { throw 'Tunnel readiness check failed after service startup.' }
'ready' | Set-Content -LiteralPath (Join-Path $InstallRoot 'state\service-status') -Encoding ASCII
Write-Host 'Configuration validated. Secrets were not printed.'
