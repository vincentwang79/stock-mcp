[CmdletBinding()]
param(
    [string] $InstallRoot = 'C:\ProgramData\StockMcp',
    [string] $OutputDirectory = (Get-Location).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$stage = Join-Path ([IO.Path]::GetTempPath()) ('stock-mcp-diagnose-' + [guid]::NewGuid().ToString('N'))
$zip = Join-Path ([IO.Path]::GetFullPath($OutputDirectory)) ('stock-mcp-diagnostics-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.zip')
New-Item -ItemType Directory -Path $stage -Force | Out-Null

function Redact-DiagnosticText([string] $Text) {
    foreach ($pattern in @(
        '(?im)^\s*(TUSHARE_TOKEN|TUNNEL_API_KEY|API_KEY|HTTPS?_PROXY|STOCK_MCP_CA_FILE)\s*=\s*.*$',
        '(?im)(api_key|authorization|token|password)\s*[:=]\s*[^\s]+'
    )) {
        $Text = [regex]::Replace($Text, $pattern, '$1=[REDACTED]')
    }
    return $Text
}

function Copy-RedactedLogs([string] $Source, [string] $Destination) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    if (-not (Test-Path -LiteralPath $Source)) { return }
    Get-ChildItem -LiteralPath $Source -Recurse -File -Exclude 'secrets.env', 'tunnel-api-key', '*.key', '*.pfx', '*.pem' |
        ForEach-Object {
            $relative = $_.FullName.Substring($Source.Length).TrimStart('\')
            $target = Join-Path $Destination $relative
            New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
            if ($_.Length -le 10485760) {
                $redacted = Redact-DiagnosticText ([IO.File]::ReadAllText($_.FullName))
                [IO.File]::WriteAllText($target, $redacted, [Text.UTF8Encoding]::new($false))
            } else {
                'File omitted because it exceeds 10 MB.' | Set-Content -LiteralPath ($target + '.omitted.txt') -Encoding UTF8
            }
        }
}

try {
    Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' | Select-Object ProductName, DisplayVersion, CurrentBuild |
        Out-File (Join-Path $stage 'windows.txt')
    Get-Service -Name StockMcpService, StockMcpTunnel -ErrorAction SilentlyContinue |
        Format-List * | Out-File (Join-Path $stage 'services.txt')
    if (Test-Path -LiteralPath (Join-Path $InstallRoot 'state\service-status')) {
        Copy-Item -LiteralPath (Join-Path $InstallRoot 'state\service-status') -Destination (Join-Path $stage 'service-status.txt')
    }
    $releaseManifest = Join-Path $InstallRoot 'current\release-manifest.json'
    if (Test-Path -LiteralPath $releaseManifest) { Copy-Item -LiteralPath $releaseManifest -Destination (Join-Path $stage 'release-manifest.json') }
    $backupDirectory = Join-Path $InstallRoot 'backups'
    if (Test-Path -LiteralPath $backupDirectory) {
        Get-ChildItem -LiteralPath $backupDirectory -Recurse -File | Select-Object FullName, Length, LastWriteTimeUtc |
            Format-Table -AutoSize | Out-File (Join-Path $stage 'backups.txt')
    }
    $logDestination = Join-Path $stage 'logs'
    New-Item -ItemType Directory -Path $logDestination -Force | Out-Null
    Copy-RedactedLogs (Join-Path $InstallRoot 'logs') $logDestination
    $database = Join-Path $InstallRoot 'data\stock-mcp.sqlite3'
    $python = Join-Path $InstallRoot 'current\.venv\Scripts\python.exe'
    if ((Test-Path -LiteralPath $database) -and (Test-Path -LiteralPath $python)) {
        & $python -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0])" $database |
            Out-File (Join-Path $stage 'database-integrity.txt')
    }
    $cli = Join-Path $InstallRoot 'current\.venv\Scripts\stock-mcp.exe'
    if (Test-Path -LiteralPath $cli) { & $cli doctor --root $InstallRoot 2>&1 | Out-File (Join-Path $stage 'doctor.txt') }
    $tunnel = Join-Path $InstallRoot 'runtime\tools\tunnel-client.exe'
    $tunnelConfig = Join-Path $InstallRoot 'config\tunnel-client.yaml'
    if ((Test-Path -LiteralPath $tunnel) -and (Test-Path -LiteralPath $tunnelConfig)) {
        & $tunnel doctor --config $tunnelConfig --explain 2>&1 | Out-File (Join-Path $stage 'tunnel-doctor.txt')
    }
    # Explicitly remove secret material even if a future collector accidentally copied it.
    Get-ChildItem -LiteralPath $stage -Recurse -Force -Include 'secrets.env', 'tunnel-api-key', '*.key', '*.pfx', '*.pem' | Remove-Item -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -Force
    Write-Host "Diagnostic package written to $zip (secrets.env excluded)."
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
}
