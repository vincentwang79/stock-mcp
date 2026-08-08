Set-StrictMode -Version Latest

function Get-ConfiguredHttpsProxy {
    $value = $env:HTTPS_PROXY
    if ([string]::IsNullOrWhiteSpace($value)) { $value = $env:HTTP_PROXY }
    if ([string]::IsNullOrWhiteSpace($value)) { return $null }
    $proxy = $null
    if (-not [Uri]::TryCreate($value, [UriKind]::Absolute, [ref] $proxy) -or
        $proxy.Scheme -notin @('http', 'https')) {
        throw 'HTTPS_PROXY or HTTP_PROXY must be an absolute HTTP(S) proxy URL.'
    }
    return $proxy.AbsoluteUri
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Administrator privileges are required. Start PowerShell as Administrator."
    }
}

function Assert-WindowsX64 {
    if ($env:OS -ne "Windows_NT") { throw "This installer runs on Windows only." }
    if (-not [Environment]::Is64BitOperatingSystem) { throw "64-bit Windows is required." }
}

function Get-FileSha256([Parameter(Mandatory = $true)][string] $Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "File not found: $Path" }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-Sha256([Parameter(Mandatory = $true)][string] $Path, [Parameter(Mandatory = $true)][string] $Expected) {
    if ($Expected -notmatch '^[0-9a-fA-F]{64}$') { throw "Invalid or unpinned SHA-256 for $Path." }
    $actual = Get-FileSha256 $Path
    if ($actual -ne $Expected.ToLowerInvariant()) { throw "SHA-256 verification failed for $Path." }
}

function Get-ReleaseVersion([Parameter(Mandatory = $true)][string] $Directory) {
    $manifest = Join-Path $Directory 'release-manifest.json'
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw 'Release package has no release-manifest.json.' }
    $version = (Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json).version
    if ($version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$') {
        throw 'Version must be a safe semantic version.'
    }
    return $version
}

function Assert-ReleaseContents([Parameter(Mandatory = $true)][string] $Extracted) {
    $checksums = Join-Path $Extracted 'checksums.txt'
    if (-not (Test-Path -LiteralPath $checksums -PathType Leaf)) { throw 'Release package has no checksums.txt.' }
    $root = [IO.Path]::GetFullPath($Extracted).TrimEnd('\') + '\'
    $expected = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($line in Get-Content -LiteralPath $checksums) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $parts = $line -split '\s{2,}', 2
        if ($parts.Count -ne 2 -or $parts[0] -notmatch '^[0-9a-f]{64}$') { throw "Invalid checksum entry: $line" }
        if ([IO.Path]::IsPathRooted($parts[1])) { throw 'Checksum path escapes the release root.' }
        $target = [IO.Path]::GetFullPath((Join-Path $Extracted $parts[1]))
        if (-not $target.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { throw 'Checksum path escapes the release root.' }
        if (-not $expected.Add($target)) { throw "Duplicate checksum entry: $($parts[1])" }
        Assert-Sha256 $target $parts[0]
    }
    $actual = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    Get-ChildItem -LiteralPath $Extracted -Recurse -File |
        Where-Object { $_.FullName -ne $checksums } |
        ForEach-Object { [void] $actual.Add($_.FullName) }
    if (-not $actual.SetEquals($expected)) { throw 'Release file set does not match checksums.txt.' }
}

function Get-ToolManifest([Parameter(Mandatory = $true)][string] $Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Missing tools manifest: $Path" }
    $manifest = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    foreach ($required in @('uv', 'winsw', 'tunnel-client')) {
        if ($null -eq $manifest.tools.$required) { throw "Tools manifest has no '$required' entry." }
        $entry = $manifest.tools.$required
        if ([string]::IsNullOrWhiteSpace($entry.path) -or [string]::IsNullOrWhiteSpace($entry.url)) {
            throw "Tools manifest '$required' must contain path and HTTPS url."
        }
        if ($entry.url -notmatch '^https://') { throw "Tool '$required' must use HTTPS." }
        if ($entry.sha256 -notmatch '^[0-9a-fA-F]{64}$') { throw "Tool '$required' has no pinned SHA-256." }
        if ($entry.archive_sha256 -notmatch '^[0-9a-fA-F]{64}$') { throw "Tool '$required' has no pinned archive SHA-256." }
    }
    return $manifest
}

function Get-VerifiedTool {
    param(
        [Parameter(Mandatory = $true)] $Entry,
        [Parameter(Mandatory = $true)][string] $PackageTools,
        [Parameter(Mandatory = $true)][string] $DestinationDirectory
    )
    New-Item -ItemType Directory -Path $DestinationDirectory -Force | Out-Null
    $destination = Join-Path $DestinationDirectory ([IO.Path]::GetFileName($Entry.path))
    $packaged = Join-Path $PackageTools $Entry.path
    if (-not (Test-Path -LiteralPath $packaged -PathType Leaf)) {
        throw "Release is incomplete: missing packaged tool $($Entry.path)."
    }
    Assert-Sha256 $packaged $Entry.sha256
    Copy-Item -LiteralPath $packaged -Destination $destination -Force
    Assert-Sha256 $destination $Entry.sha256
    return $destination
}

function Set-PrivateAcl {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [switch] $WritableByApp,
        [switch] $WritableByTunnel,
        [switch] $ReadableByApp,
        [switch] $ReadableByTunnel
    )
    if (-not (Test-Path -LiteralPath $Path)) { New-Item -ItemType Directory -Path $Path -Force | Out-Null }
    & icacls $Path /inheritance:r /grant:r "BUILTIN\Administrators:(OI)(CI)F" "NT AUTHORITY\SYSTEM:(OI)(CI)F" | Out-Null
    if ($WritableByApp -or $ReadableByApp) {
        $rights = if ($WritableByApp) { 'M' } else { 'RX' }
        & icacls $Path /grant "NT SERVICE\StockMcpService:(OI)(CI)$rights" | Out-Null
    }
    if ($WritableByTunnel -or $ReadableByTunnel) {
        $rights = if ($WritableByTunnel) { 'M' } else { 'RX' }
        & icacls $Path /grant "NT SERVICE\StockMcpTunnel:(OI)(CI)$rights" | Out-Null
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not set ACL on $Path." }
}

function Set-PrivateFileAcl {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [ValidateSet('StockMcpService', 'StockMcpTunnel')][string] $Reader,
        [switch] $SharedWithOtherService
    )
    & icacls $Path /inheritance:r /grant:r "BUILTIN\Administrators:F" "NT AUTHORITY\SYSTEM:F" "NT SERVICE\${Reader}:R" | Out-Null
    if ($SharedWithOtherService) {
        $other = if ($Reader -eq 'StockMcpService') { 'StockMcpTunnel' } else { 'StockMcpService' }
        & icacls $Path /grant "NT SERVICE\${other}:R" | Out-Null
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not protect $Path." }
}

function New-CurrentJunction([Parameter(Mandatory = $true)][string] $Root, [Parameter(Mandatory = $true)][string] $Target) {
    $current = Join-Path $Root 'current'
    if (Test-Path -LiteralPath $current) {
        $currentItem = Get-Item -LiteralPath $current -Force
        if (-not ($currentItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Refusing to replace non-junction current directory: $current"
        }
        # Directory.Delete removes the junction itself without recursively traversing
        # the target release, and does not trigger PowerShell's child-item prompt.
        [IO.Directory]::Delete($current)
    }
    New-Item -ItemType Junction -Path $current -Target $Target | Out-Null
}

function Wait-LocalReady([string] $Url = 'http://127.0.0.1:8765/readyz', [int] $Attempts = 12) {
    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) { return $true }
        } catch { Start-Sleep -Seconds 2 }
    }
    return $false
}

function Wait-TunnelReady([string] $Url = 'http://127.0.0.1:8766/readyz', [int] $Attempts = 12) {
    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) { return $true }
        } catch { Start-Sleep -Seconds 2 }
    }
    return $false
}

function Stop-StockServices {
    foreach ($name in @('StockMcpTunnel', 'StockMcpService')) {
        $service = Get-Service -Name $name -ErrorAction SilentlyContinue
        if ($null -ne $service -and $service.Status -ne 'Stopped') {
            Stop-Service -Name $name -Force -ErrorAction Stop
            $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
        }
    }
}
