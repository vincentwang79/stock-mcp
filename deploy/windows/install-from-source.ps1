[CmdletBinding()]
param(
    [string] $SourceRoot = (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent),
    [string] $InstallRoot = 'E:\StockMcp',
    [switch] $SkipConnectivityCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-GitText([Parameter(Mandatory = $true)][string[]] $Arguments) {
    $output = & git -C $SourceRoot @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Git command failed: git -C $SourceRoot $($Arguments -join ' ')" }
    return ($output | Out-String).Trim()
}

$SourceRoot = [IO.Path]::GetFullPath($SourceRoot)
foreach ($required in @('src', 'pyproject.toml', 'uv.lock', 'README.md', 'a_share_mainboard_code_name.json', 'deploy\windows\tools-manifest.json')) {
    if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot $required))) {
        throw "Source checkout is incomplete: missing $required"
    }
}

$dirty = Get-GitText @('status', '--porcelain')
if (-not [string]::IsNullOrWhiteSpace($dirty)) {
    throw 'Source checkout has uncommitted or untracked changes. Commit, stash, or remove them before installation.'
}
$null = & git -C $SourceRoot fetch origin main
if ($LASTEXITCODE -ne 0) { throw 'Could not fetch origin/main for source deployment verification.' }
$sourceCommit = Get-GitText @('rev-parse', 'HEAD')
$remoteMainCommit = Get-GitText @('rev-parse', 'origin/main')
if ($sourceCommit -ne $remoteMainCommit) {
    throw 'Source checkout does not match origin/main. Run git pull --ff-only before installation.'
}
$sourceOrigin = Get-GitText @('remote', 'get-url', 'origin')
$project = Get-Content -LiteralPath (Join-Path $SourceRoot 'pyproject.toml') -Raw
$versionMatch = [regex]::Match($project, '(?m)^version\s*=\s*"([^"]+)"\s*$')
if (-not $versionMatch.Success -or $versionMatch.Groups[1].Value -notmatch '^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$') {
    throw 'pyproject.toml must contain a safe semantic project version.'
}
$releaseVersion = "$($versionMatch.Groups[1].Value)+git.$($sourceCommit.Substring(0, 12))"

$sourceWindows = Join-Path $SourceRoot 'deploy\windows'
$work = Join-Path ([IO.Path]::GetTempPath()) ('stock-mcp-source-release-' + [guid]::NewGuid().ToString('N'))
$releaseRoot = Join-Path $work 'stock-mcp-windows-x64'
try {
    New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
    foreach ($name in @('install.ps1', 'configure.ps1', 'configure-input.psd1.example', 'update.ps1', 'diagnose.ps1', 'uninstall.ps1', 'README-WINDOWS.md')) {
        Copy-Item -LiteralPath (Join-Path $sourceWindows $name) -Destination (Join-Path $releaseRoot $name) -Force
    }
    Copy-Item -LiteralPath (Join-Path $sourceWindows 'deploy') -Destination (Join-Path $releaseRoot 'deploy') -Recurse -Force

    $app = Join-Path $releaseRoot 'app'
    New-Item -ItemType Directory -Path $app -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $SourceRoot 'src') -Destination (Join-Path $app 'src') -Recurse -Force
    foreach ($name in @('pyproject.toml', 'README.md', 'uv.lock')) {
        Copy-Item -LiteralPath (Join-Path $SourceRoot $name) -Destination (Join-Path $app $name) -Force
    }
    Copy-Item -LiteralPath (Join-Path $SourceRoot 'a_share_mainboard_code_name.json') -Destination (Join-Path $app 'a_share_mainboard_code_name.json') -Force
    Copy-Item -LiteralPath (Join-Path $sourceWindows 'tools-manifest.json') -Destination (Join-Path $releaseRoot 'tools-manifest.json') -Force

    $tools = Join-Path $releaseRoot 'tools'
    $toolCache = Join-Path $InstallRoot 'cache\tools'
    . (Join-Path $sourceWindows 'deploy\lib.ps1')
    Set-PrivateAcl $toolCache
    $toolManifest = Get-ToolManifest (Join-Path $sourceWindows 'tools-manifest.json')
    $installedTools = Join-Path $InstallRoot 'runtime\tools'
    foreach ($name in @('uv', 'winsw', 'tunnel-client')) {
        $entry = $toolManifest.tools.$name
        $installed = Join-Path $installedTools $entry.path
        $cached = Join-Path $toolCache $entry.path
        if (
            -not (Test-Path -LiteralPath $cached -PathType Leaf) -and
            (Test-Path -LiteralPath $installed -PathType Leaf)
        ) {
            $actual = (Get-FileHash -LiteralPath $installed -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actual -eq ([string] $entry.sha256).ToLowerInvariant()) {
                Copy-Item -LiteralPath $installed -Destination $cached -Force
                Write-Host "Seeded verified $name $($entry.version) from installed runtime."
            }
        }
    }
    & (Join-Path $sourceWindows 'fetch-tools.ps1') `
        -Manifest (Join-Path $sourceWindows 'tools-manifest.json') `
        -OutputDirectory $tools `
        -CacheDirectory $toolCache
    if ($LASTEXITCODE -ne 0) { throw 'Could not fetch the verified Windows tool payloads.' }

    [ordered]@{
        version = $releaseVersion
        architecture = 'windows-x64'
        created_utc = [DateTime]::UtcNow.ToString('o')
        source_commit = $sourceCommit
        source_origin = $sourceOrigin
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $releaseRoot 'release-manifest.json') -Encoding UTF8

    . (Join-Path $releaseRoot 'deploy\lib.ps1')
    $checksumLines = Get-ChildItem -LiteralPath $releaseRoot -Recurse -File |
        Where-Object { $_.Name -ne 'checksums.txt' } |
        Sort-Object FullName |
        ForEach-Object {
            $relative = $_.FullName.Substring($releaseRoot.Length).TrimStart('\\')
            "$(Get-FileSha256 $_.FullName)  $relative"
        }
    Set-Content -LiteralPath (Join-Path $releaseRoot 'checksums.txt') -Value $checksumLines -Encoding ASCII

    if (Test-Path -LiteralPath (Join-Path $InstallRoot 'current')) {
        & (Join-Path $releaseRoot 'update.ps1') -PackageDirectory $releaseRoot -InstallRoot $InstallRoot
    } else {
        & (Join-Path $releaseRoot 'install.ps1') -PackageDirectory $releaseRoot -InstallRoot $InstallRoot `
            -SkipConnectivityCheck:$SkipConnectivityCheck
    }
    if ($LASTEXITCODE -ne 0) { throw 'Source deployment installer failed.' }
    Write-Host "Installed verified source commit $sourceCommit to $InstallRoot."
} finally {
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
}
