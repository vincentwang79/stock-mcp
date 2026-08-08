[CmdletBinding()]
param(
    [string] $InstallRoot = 'E:\StockMcp',
    [string] $OutputDirectory = (Get-Location).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$stage = Join-Path ([IO.Path]::GetTempPath()) ('stock-mcp-diagnose-' + [guid]::NewGuid().ToString('N'))
$zip = Join-Path ([IO.Path]::GetFullPath($OutputDirectory)) ('stock-mcp-diagnostics-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.zip')
New-Item -ItemType Directory -Path $stage -Force | Out-Null

function Redact-DiagnosticText([string] $Text) {
    $Text = [regex]::Replace(
        $Text,
        '(?im)^\s*(TUSHARE_TOKEN|TUNNEL_API_KEY|API_KEY|HTTPS?_PROXY|STOCK_MCP_CA_FILE)\s*=\s*.*$',
        '$1=[REDACTED]'
    )
    $Text = [regex]::Replace(
        $Text,
        '(?i)"(?:api_key|token)"\s*:\s*"[^"]+"',
        '"secret":"[REDACTED]"'
    )
    $Text = [regex]::Replace(
        $Text,
        '(?i)Authorization\s*[:=]\s*Bearer\s+[^\s]+',
        'Authorization=[REDACTED]'
    )
    $Text = [regex]::Replace(
        $Text,
        'sk-[A-Za-z0-9_-]{16,}',
        '[REDACTED]'
    )
    # Remove URL userinfo without exposing either its username or password.
    $Text = [regex]::Replace(
        $Text,
        '(?i)(https?://)(?:[^/\s:@]+(?::[^@/\s]+)?@)',
        '$1[REDACTED]@'
    )
    # Remove query secret values while retaining enough URL shape for diagnosis.
    $Text = [regex]::Replace(
        $Text,
        '(?i)([?&](?:api_key|token|access_token|key)=)[^&#\s]+',
        '$1[REDACTED]'
    )
    $Text = [regex]::Replace(
        $Text,
        '(?im)(api_key|authorization|token|password)\s*[:=]\s*[^\s]+',
        '$1=[REDACTED]'
    )
    return $Text
}

function Write-RedactedText([Parameter(Mandatory = $true)][string] $Path, [AllowEmptyString()][string] $Text) {
    [IO.File]::WriteAllText($Path, (Redact-DiagnosticText $Text), [Text.UTF8Encoding]::new($false))
}

function Write-RedactedCommandOutput([Parameter(Mandatory = $true)][string] $Path, [Parameter(Mandatory = $true)][scriptblock] $Command) {
    $output = (& $Command 2>&1 | Out-String)
    Write-RedactedText $Path $output
}

function Copy-RedactedTextFile([Parameter(Mandatory = $true)][string] $Source, [Parameter(Mandatory = $true)][string] $Destination) {
    if (Test-Path -LiteralPath $Source -PathType Leaf) {
        Write-RedactedText $Destination ([IO.File]::ReadAllText($Source))
    }
}

function Assert-DiagnosticStageHasNoSecrets([Parameter(Mandatory = $true)][string] $Stage) {
    $secretPattern = '(?im)(?:^\s*(?:TUSHARE_TOKEN|TUNNEL_API_KEY|API_KEY|HTTPS?_PROXY|STOCK_MCP_CA_FILE)\s*=\s*(?!\[REDACTED\])\S+|"(?:api_key|token)"\s*:\s*"(?!\[REDACTED\])[^\"]+"|Authorization\s*[:=]\s*Bearer\s+(?!\[REDACTED\])\S+|(?:api_key|authorization|token|password)\s*[:=]\s*(?!\[REDACTED\])\S+|sk-[A-Za-z0-9_-]{16,}|https?://(?!\[REDACTED\]@)(?:[^/\s:@]+(?::[^@/\s]+)?@)|[?&](?:api_key|token|access_token|key)=(?!\[REDACTED\])[^&#\s]+)'
    foreach ($file in Get-ChildItem -LiteralPath $Stage -Recurse -File) {
        $text = [IO.File]::ReadAllText($file.FullName)
        if ([regex]::IsMatch($text, $secretPattern)) {
            throw "Diagnostic staging contains secret-pattern: $($file.FullName)"
        }
    }
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
                Write-RedactedText $target ([IO.File]::ReadAllText($_.FullName))
            } else {
                'File omitted because it exceeds 10 MB.' | Set-Content -LiteralPath ($target + '.omitted.txt') -Encoding UTF8
            }
        }
}

try {
    Write-RedactedCommandOutput (Join-Path $stage 'windows.txt') {
        Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' |
            Select-Object ProductName, DisplayVersion, CurrentBuild | Format-List *
    }
    Write-RedactedCommandOutput (Join-Path $stage 'services.txt') {
        Get-Service -Name StockMcpService, StockMcpTunnel -ErrorAction SilentlyContinue | Format-List *
    }
    Copy-RedactedTextFile (Join-Path $InstallRoot 'state\service-status') (Join-Path $stage 'service-status.txt')
    $releaseManifest = Join-Path $InstallRoot 'current\release-manifest.json'
    Copy-RedactedTextFile $releaseManifest (Join-Path $stage 'release-manifest.json')
    $backupDirectory = Join-Path $InstallRoot 'backups'
    if (Test-Path -LiteralPath $backupDirectory) {
        Write-RedactedCommandOutput (Join-Path $stage 'backups.txt') {
            Get-ChildItem -LiteralPath $backupDirectory -Recurse -File |
                Select-Object FullName, Length, LastWriteTimeUtc | Format-Table -AutoSize
        }
    }
    $logDestination = Join-Path $stage 'logs'
    New-Item -ItemType Directory -Path $logDestination -Force | Out-Null
    Copy-RedactedLogs (Join-Path $InstallRoot 'logs') $logDestination
    $database = Join-Path $InstallRoot 'data\stock-mcp.sqlite3'
    $python = Join-Path $InstallRoot 'current\.venv\Scripts\python.exe'
    if ((Test-Path -LiteralPath $database) -and (Test-Path -LiteralPath $python)) {
        Write-RedactedCommandOutput (Join-Path $stage 'database-integrity.txt') {
            & $python -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0])" $database
        }
    }
    $cli = Join-Path $InstallRoot 'current\.venv\Scripts\stock-mcp.exe'
    if (Test-Path -LiteralPath $cli) {
        Write-RedactedCommandOutput (Join-Path $stage 'doctor.txt') { & $cli doctor --root $InstallRoot 2>&1 | Out-String }
    }
    $tunnel = Join-Path $InstallRoot 'runtime\tools\tunnel-client.exe'
    $tunnelConfig = Join-Path $InstallRoot 'config\tunnel-client.yaml'
    if ((Test-Path -LiteralPath $tunnel) -and (Test-Path -LiteralPath $tunnelConfig)) {
        Write-RedactedCommandOutput (Join-Path $stage 'tunnel-doctor.txt') {
            & $tunnel doctor --config $tunnelConfig --explain 2>&1 | Out-String
        }
    }
    # Explicitly remove secret material even if a future collector accidentally copied it.
    Get-ChildItem -LiteralPath $stage -Recurse -Force -Include 'secrets.env', 'tunnel-api-key', '*.key', '*.pfx', '*.pem' | Remove-Item -Force -ErrorAction SilentlyContinue
    Assert-DiagnosticStageHasNoSecrets $stage
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -Force
    Write-Host "Diagnostic package written to $zip (secrets.env excluded)."
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
}
