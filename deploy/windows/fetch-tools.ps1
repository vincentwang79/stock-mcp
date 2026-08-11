[CmdletBinding()]
param(
    [string] $Manifest = (Join-Path $PSScriptRoot 'tools-manifest.json'),
    [string] $OutputDirectory = (Join-Path $PSScriptRoot 'tools-cache'),
    [string] $CacheDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'deploy\lib.ps1')

[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Invoke-ToolDownload {
    param(
        [Parameter(Mandatory = $true)][string] $Name,
        [Parameter(Mandatory = $true)][string] $Uri,
        [Parameter(Mandatory = $true)][string] $Destination
    )
    $hostName = ([Uri] $Uri).Host
    $lastError = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Force }
            Write-Host "Downloading $Name from $hostName (attempt $attempt/3)"
            $request = @{ Uri = $Uri; OutFile = $Destination; UseBasicParsing = $true; TimeoutSec = 120 }
            $proxy = Get-ConfiguredHttpsProxy
            if ($proxy) { $request.Proxy = $proxy }
            Invoke-WebRequest @request
            return
        } catch {
            $lastError = $_.Exception.Message
            if ($attempt -lt 3) {
                Write-Warning "Download attempt $attempt for $Name failed: $lastError"
                Start-Sleep -Seconds (3 * $attempt)
            }
        }
    }
    throw "Failed to download $Name from $hostName after 3 attempts. Confirm outbound HTTPS, proxy, and TLS access. Last error: $lastError"
}

$tools = Get-ToolManifest $Manifest
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
if ([string]::IsNullOrWhiteSpace($CacheDirectory)) {
    $CacheDirectory = $OutputDirectory
}
$CacheDirectory = [IO.Path]::GetFullPath($CacheDirectory)
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $CacheDirectory -Force | Out-Null
foreach ($name in @('uv', 'winsw', 'tunnel-client')) {
    $entry = $tools.tools.$name
    $destination = Join-Path $OutputDirectory $entry.path
    $cached = Join-Path $CacheDirectory $entry.path
    $cacheValid = $false
    if (Test-Path -LiteralPath $cached -PathType Leaf) {
        try {
            Assert-Sha256 $cached $entry.sha256
            $cacheValid = $true
        } catch {
            Remove-Item -LiteralPath $cached -Force
            Write-Warning "Discarded invalid cached tool $name."
        }
    }
    if ($cacheValid) {
        if ([IO.Path]::GetFullPath($cached) -ne [IO.Path]::GetFullPath($destination)) {
            Copy-Item -LiteralPath $cached -Destination $destination -Force
        }
        Assert-Sha256 $destination $entry.sha256
        Write-Host "Reused verified $name $($entry.version)"
        continue
    }
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ('stock-mcp-tool-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temporary -Force | Out-Null
    try {
        $download = Join-Path $temporary 'asset'
        Invoke-ToolDownload $name $entry.url $download
        Assert-Sha256 $download $entry.archive_sha256
        $archiveMember = ''
        if ($entry.PSObject.Properties['archive_member']) {
            $archiveMember = [string] $entry.archive_member
        }
        if ([string]::IsNullOrWhiteSpace($archiveMember)) {
            Copy-Item -LiteralPath $download -Destination $destination -Force
        } else {
            $archive = Join-Path $temporary 'asset.zip'
            Move-Item -LiteralPath $download -Destination $archive
            $expanded = Join-Path $temporary 'expanded'
            Expand-Archive -LiteralPath $archive -DestinationPath $expanded
            $member = [IO.Path]::GetFullPath((Join-Path $expanded $archiveMember))
            $root = [IO.Path]::GetFullPath($expanded).TrimEnd('\') + '\'
            if (-not $member.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Archive member escapes extraction root for $name."
            }
            Copy-Item -LiteralPath $member -Destination $destination -Force
        }
        Assert-Sha256 $destination $entry.sha256
        if ([IO.Path]::GetFullPath($cached) -ne [IO.Path]::GetFullPath($destination)) {
            Copy-Item -LiteralPath $destination -Destination $cached -Force
            Assert-Sha256 $cached $entry.sha256
        }
        Write-Host "Fetched verified $name $($entry.version)"
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force }
    }
}
