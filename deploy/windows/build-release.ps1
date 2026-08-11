[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $Version,
    [Parameter(Mandatory = $true)][string] $ToolsManifest,
    [Parameter(Mandatory = $true)][string] $ToolsDirectory,
    [string] $OutputDirectory = (Join-Path $PSScriptRoot 'dist')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'deploy\lib.ps1')

function Copy-VerifiedTool([string] $Name, $Entry, [string] $SourceDirectory, [string] $DestinationDirectory) {
    $source = Join-Path $SourceDirectory $Entry.path
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Missing $Name tool payload: $source" }
    Assert-Sha256 $source $Entry.sha256
    $destination = Join-Path $DestinationDirectory $Entry.path
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

if ($Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$') { throw 'Version must be a safe semantic version.' }
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$lock = Join-Path $repo 'uv.lock'
if (-not (Test-Path -LiteralPath $lock -PathType Leaf)) { throw 'uv.lock is required; run uv lock before a release.' }
$manifest = Get-ToolManifest $ToolsManifest
$uv = Join-Path $ToolsDirectory $manifest.tools.uv.path
if (-not (Test-Path -LiteralPath $uv -PathType Leaf)) { throw "Verified uv is required for release preflight: $uv" }
Assert-Sha256 $uv $manifest.tools.uv.sha256
& $uv lock --check --project $repo
if ($LASTEXITCODE -ne 0) { throw 'uv.lock is stale.' }
& $uv run --project $repo ruff check .
if ($LASTEXITCODE -ne 0) { throw 'Ruff release gate failed.' }
& $uv run --project $repo pytest -q
if ($LASTEXITCODE -ne 0) { throw 'Test release gate failed.' }
& git -C $repo diff --check
if ($LASTEXITCODE -ne 0) { throw 'Git whitespace release gate failed.' }
$secretHits = Get-ChildItem -LiteralPath $repo -Recurse -File |
    Where-Object { $_.FullName -notmatch '[\\/](\.git|\.venv|dist|build)[\\/]' } |
    Select-String -Pattern 'sk-(?:proj-)?[A-Za-z0-9_-]{20,}|TUSHARE_TOKEN\s*=\s*[^<\s][^\s]{20,}'
if ($secretHits) { throw 'Potential production secret detected in release inputs.' }

$work = Join-Path ([IO.Path]::GetTempPath()) ('stock-mcp-release-' + [guid]::NewGuid().ToString('N'))
$releaseRoot = Join-Path $work 'stock-mcp-windows-x64'
New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
try {
    foreach ($name in @('install.ps1', 'configure.ps1', 'configure-input.psd1.example', 'update.ps1', 'diagnose.ps1', 'uninstall.ps1', 'README-WINDOWS.md')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $releaseRoot $name) -Force
    }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'deploy') -Destination (Join-Path $releaseRoot 'deploy') -Recurse -Force
    $app = Join-Path $releaseRoot 'app'
    New-Item -ItemType Directory -Path $app -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $repo 'src') -Destination (Join-Path $app 'src') -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $repo 'pyproject.toml') -Destination (Join-Path $app 'pyproject.toml') -Force
    Copy-Item -LiteralPath (Join-Path $repo 'README.md') -Destination (Join-Path $app 'README.md') -Force
    Copy-Item -LiteralPath (Join-Path $repo 'a_share_mainboard_code_name.json') -Destination (Join-Path $app 'a_share_mainboard_code_name.json') -Force
    Copy-Item -LiteralPath $lock -Destination (Join-Path $app 'uv.lock') -Force
    Copy-Item -LiteralPath $ToolsManifest -Destination (Join-Path $releaseRoot 'tools-manifest.json') -Force
    $tools = Join-Path $releaseRoot 'tools'
    New-Item -ItemType Directory -Path $tools -Force | Out-Null
    foreach ($name in @('uv', 'winsw', 'tunnel-client')) { Copy-VerifiedTool $name $manifest.tools.$name $ToolsDirectory $tools }
    @{ version = $Version; architecture = 'windows-x64'; created_utc = [DateTime]::UtcNow.ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $releaseRoot 'release-manifest.json') -Encoding UTF8

    $checksumLines = Get-ChildItem -LiteralPath $releaseRoot -Recurse -File |
        Where-Object { $_.Name -ne 'checksums.txt' } |
        ForEach-Object {
            $relative = $_.FullName.Substring($releaseRoot.Length).TrimStart('\')
            "$(Get-FileSha256 $_.FullName)  $relative"
        }
    Set-Content -LiteralPath (Join-Path $releaseRoot 'checksums.txt') -Value $checksumLines -Encoding ASCII
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    $zip = Join-Path $OutputDirectory 'stock-mcp-windows-x64.zip'
    if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
    Compress-Archive -Path $releaseRoot -DestinationPath $zip -Force
    $zipDigest = Get-FileSha256 $zip
    Set-Content -LiteralPath (Join-Path $OutputDirectory 'stock-mcp-windows-x64.zip.sha256') -Value $zipDigest -Encoding ASCII
    Write-Host "Built verified release: $zip"
} finally {
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
}
