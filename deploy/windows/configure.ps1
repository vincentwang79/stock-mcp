[CmdletBinding()]
param(
    [string] $InstallRoot = 'E:\StockMcp',
    [switch] $TushareTokenFromClipboard,
    [switch] $TunnelRuntimeKeyFromClipboard,
    [string] $ConfigurationFile,
    [switch] $WriteConfigurationTemplate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'deploy\lib.ps1')

function Get-Plaintext([Security.SecureString] $Value) {
    return (New-Object System.Net.NetworkCredential('', $Value)).Password
}

function ConvertTo-ConfigurationSecureString([AllowEmptyString()][string] $Value) {
    if ([string]::IsNullOrEmpty($Value)) { return [Security.SecureString]::new() }
    return ConvertTo-SecureString -String $Value -AsPlainText -Force
}

function Get-ConfigurationText {
    param(
        [Parameter(Mandatory = $true)][hashtable] $Configuration,
        [Parameter(Mandatory = $true)][string] $Name,
        [switch] $Optional
    )
    if (-not $Configuration.ContainsKey($Name) -or $Configuration[$Name] -isnot [string]) {
        throw "Configuration file must contain string '$Name'."
    }
    $value = [string] $Configuration[$Name]
    if (-not $Optional -and [string]::IsNullOrWhiteSpace($value)) {
        throw "Configuration file value '$Name' cannot be blank."
    }
    return $value
}

Test-Administrator
Assert-WindowsX64
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$configDirectory = Join-Path $InstallRoot 'config'
if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot 'current'))) { throw 'Stock MCP is not installed.' }
if ($WriteConfigurationTemplate -and ($TushareTokenFromClipboard -or $TunnelRuntimeKeyFromClipboard)) {
    throw '-WriteConfigurationTemplate cannot be combined with clipboard input switches.'
}
if ($WriteConfigurationTemplate) {
    if ([string]::IsNullOrWhiteSpace($ConfigurationFile)) {
        $ConfigurationFile = Join-Path $configDirectory 'configure-input.psd1'
    }
    $ConfigurationFile = [IO.Path]::GetFullPath($ConfigurationFile)
    if (Test-Path -LiteralPath $ConfigurationFile) {
        throw "Configuration template already exists: $ConfigurationFile"
    }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'configure-input.psd1.example') `
        -Destination $ConfigurationFile -Force
    Set-AdministratorOnlyFileAcl $ConfigurationFile
    Write-Host "Created protected configuration template: $ConfigurationFile"
    return
}
if ($ConfigurationFile -and $TunnelRuntimeKeyFromClipboard) {
    throw '-ConfigurationFile cannot be combined with -TunnelRuntimeKeyFromClipboard.'
}

$configurationInputFile = $null
if ($ConfigurationFile) {
    $configurationInputFile = [IO.Path]::GetFullPath($ConfigurationFile)
    if (-not (Test-Path -LiteralPath $configurationInputFile -PathType Leaf)) {
        throw "Configuration file not found: $configurationInputFile"
    }
    Set-AdministratorOnlyFileAcl $configurationInputFile
    $configuration = Import-PowerShellDataFile -Path $configurationInputFile
    $expectedNames = @('TushareToken', 'TunnelId', 'TunnelRuntimeApiKey', 'HttpsProxy', 'CustomCaFilePath')
    foreach ($name in $configuration.Keys) {
        if ($name -notin $expectedNames) { throw "Configuration file has unsupported key '$name'." }
    }
    if ($TushareTokenFromClipboard) {
        if ($null -eq (Get-Command Get-Clipboard -ErrorAction SilentlyContinue)) {
            throw 'Clipboard input is unavailable in this PowerShell host.'
        }
        $clipboardToken = Get-Clipboard -Raw
        if ([string]::IsNullOrWhiteSpace($clipboardToken)) {
            throw 'Clipboard does not contain a Tushare token.'
        }
        $tushare = ConvertTo-SecureString $clipboardToken.Trim() -AsPlainText -Force
        $clipboardToken = $null
    } else {
        $tushare = ConvertTo-ConfigurationSecureString (Get-ConfigurationText $configuration 'TushareToken')
    }
    $tunnelId = ConvertTo-ConfigurationSecureString (Get-ConfigurationText $configuration 'TunnelId')
    $tunnelKey = ConvertTo-ConfigurationSecureString (Get-ConfigurationText $configuration 'TunnelRuntimeApiKey')
    $proxy = ConvertTo-ConfigurationSecureString (Get-ConfigurationText $configuration 'HttpsProxy' -Optional)
    $customCa = ConvertTo-ConfigurationSecureString (Get-ConfigurationText $configuration 'CustomCaFilePath' -Optional)
} else {
    if ($TushareTokenFromClipboard) {
        if ($null -eq (Get-Command Get-Clipboard -ErrorAction SilentlyContinue)) {
            throw 'Clipboard input is unavailable in this PowerShell host.'
        }
        $clipboardToken = Get-Clipboard -Raw
        if ([string]::IsNullOrWhiteSpace($clipboardToken)) {
            throw 'Clipboard does not contain a Tushare token.'
        }
        $tushare = ConvertTo-SecureString $clipboardToken.Trim() -AsPlainText -Force
        $clipboardToken = $null
    } else {
        $tushare = Read-Host 'Tushare token' -AsSecureString
    }
    $tunnelId = Read-Host 'Platform Tunnel ID' -AsSecureString
    if ($TunnelRuntimeKeyFromClipboard) {
        if ($null -eq (Get-Command Get-Clipboard -ErrorAction SilentlyContinue)) {
            throw 'Clipboard input is unavailable in this PowerShell host. Run without -TunnelRuntimeKeyFromClipboard.'
        }
        $clipboardKey = Get-Clipboard -Raw
        if ([string]::IsNullOrWhiteSpace($clipboardKey)) {
            throw 'Clipboard does not contain a Tunnel runtime API key.'
        }
        $tunnelKey = ConvertTo-SecureString $clipboardKey.Trim() -AsPlainText -Force
        $clipboardKey = $null
    } else {
        $tunnelKey = Read-Host 'Tunnel runtime API key' -AsSecureString
    }
    $proxy = Read-Host 'Optional HTTPS proxy (leave blank)' -AsSecureString
    $customCa = Read-Host 'Optional custom CA file path (leave blank)' -AsSecureString
}

$tushareValue = (Get-Plaintext $tushare).Trim()
if ($tushareValue -notmatch '^[0-9a-fA-F]{56}$') {
    throw 'Tushare token must be exactly 56 hexadecimal characters.'
}

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
    'TUSHARE_TOKEN=' + $tushareValue,
    'HTTPS_PROXY=' + (Get-Plaintext $proxy),
    'STOCK_MCP_CA_FILE=' + $managedCa
)
[IO.File]::WriteAllLines($secretFile, $lines, [Text.UTF8Encoding]::new($false))
$tushareValue = $null
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

# The protected host file is the only credential source.  Remove any stale
# process-level value before launching Python, including values inherited by an
# elevated PowerShell window from an earlier troubleshooting session.
Remove-Item Env:TUSHARE_TOKEN -ErrorAction SilentlyContinue

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
if ($configurationInputFile) { Remove-Item -LiteralPath $configurationInputFile -Force }
Write-Host 'Configuration validated. Secrets were not printed.'
