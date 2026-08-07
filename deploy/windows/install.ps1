[CmdletBinding()]
param(
    [string] $InstallRoot = 'C:\ProgramData\StockMcp',
    [switch] $SkipConnectivityCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'deploy\lib.ps1')

function Assert-FreeSpace([string] $Path, [int64] $MinimumBytes = 4294967296) {
    $drive = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Path))
    $info = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($drive.TrimEnd('\\'))'"
    if ($null -eq $info -or [int64]$info.FreeSpace -lt $MinimumBytes) {
        throw "At least 4 GB free disk space is required on $drive."
    }
}

function Assert-OutboundHttps {
    try { Invoke-WebRequest -Uri 'https://api.github.com' -UseBasicParsing -TimeoutSec 15 | Out-Null }
    catch { throw "Outbound HTTPS is required for verified tool retrieval and Secure MCP Tunnel. $($_.Exception.Message)" }
}

function Install-Release([string] $Root, [string] $ReleaseVersion) {
    $releases = Join-Path $Root 'releases'
    $target = Join-Path $releases $ReleaseVersion
    if (Test-Path -LiteralPath $target) { return $target }
    $staging = Join-Path $releases ('.staging-' + $ReleaseVersion + '-' + [guid]::NewGuid().ToString('N'))
    try {
        New-Item -ItemType Directory -Path $staging -Force | Out-Null
        Copy-Item -Path (Join-Path $PSScriptRoot 'app\*') -Destination $staging -Recurse -Force
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'deploy') -Destination (Join-Path $staging 'deploy') -Recurse -Force
        Move-Item -LiteralPath $staging -Destination $target
    } finally {
        if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    }
    return $target
}

function Install-WinSWServices([string] $Root, [string] $WinSw) {
    $serviceDirectory = Join-Path $Root 'runtime\services'
    New-Item -ItemType Directory -Path $serviceDirectory -Force | Out-Null
    foreach ($name in @('StockMcpService', 'StockMcpTunnel')) {
        $template = Join-Path $Root ("current\deploy\services\{0}.xml.tmpl" -f $name)
        $xml = Join-Path $serviceDirectory ("{0}.xml" -f $name)
        (Get-Content -LiteralPath $template -Raw).Replace('__INSTALL_ROOT__', $Root) | Set-Content -LiteralPath $xml -Encoding UTF8
        if ($null -eq (Get-Service -Name $name -ErrorAction SilentlyContinue)) {
            & $WinSw install $xml
            if ($LASTEXITCODE -ne 0) { throw "WinSW could not install $name." }
        }
        & sc.exe config $name obj= "NT SERVICE\$name" password= "" type= own | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not configure virtual service identity for $name." }
    }
}

Test-Administrator
Assert-WindowsX64
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
Assert-FreeSpace $InstallRoot
if (-not $SkipConnectivityCheck) { Assert-OutboundHttps }
Assert-ReleaseContents $PSScriptRoot
$Version = Get-ReleaseVersion $PSScriptRoot

$manifest = Get-ToolManifest (Join-Path $PSScriptRoot 'tools-manifest.json')
foreach ($directory in @('config', 'data', 'logs', 'backups', 'releases', 'runtime', 'state')) {
    New-Item -ItemType Directory -Path (Join-Path $InstallRoot $directory) -Force | Out-Null
}
Set-PrivateAcl $InstallRoot
foreach ($directory in @('config', 'data', 'logs', 'backups', 'releases', 'runtime', 'state')) {
    Set-PrivateAcl (Join-Path $InstallRoot $directory)
}

$toolsDestination = Join-Path $InstallRoot 'runtime\tools'
$packageTools = Join-Path $PSScriptRoot 'tools'
$uv = Get-VerifiedTool $manifest.tools.uv $packageTools $toolsDestination
$winsw = Get-VerifiedTool $manifest.tools.winsw $packageTools $toolsDestination
[void](Get-VerifiedTool $manifest.tools.'tunnel-client' $packageTools $toolsDestination)

$env:UV_PYTHON_INSTALL_DIR = Join-Path $InstallRoot 'runtime\python'
$env:UV_CACHE_DIR = Join-Path $InstallRoot 'runtime\uv-cache'
$release = Install-Release $InstallRoot $Version
& $uv venv (Join-Path $release '.venv') --python 3.12
if ($LASTEXITCODE -ne 0) { throw 'uv could not install isolated Python 3.12.' }
Push-Location $release
try {
    # Deliberately no system Python, editable package, or development dependencies.
    & $uv sync --locked --no-dev --no-editable --extra providers
    if ($LASTEXITCODE -ne 0) { throw 'uv dependency synchronization failed.' }
} finally { Pop-Location }

$appConfig = Join-Path $InstallRoot 'config\app.toml'
if (-not (Test-Path -LiteralPath $appConfig)) {
    (Get-Content -LiteralPath (Join-Path $release 'deploy\config\app.toml.example') -Raw).Replace('__INSTALL_ROOT__', $InstallRoot) |
        Set-Content -LiteralPath $appConfig -Encoding UTF8
}
Set-PrivateAcl (Join-Path $InstallRoot 'config') -ReadableByApp -ReadableByTunnel
New-CurrentJunction $InstallRoot $release
Install-WinSWServices $InstallRoot $winsw

# Virtual service identities become resolvable after registration. Grant the minimum
# runtime rights only now; program, tools, releases, services and config stay read-only.
Set-PrivateAcl $InstallRoot -ReadableByApp -ReadableByTunnel
Set-PrivateAcl (Join-Path $InstallRoot 'config') -ReadableByApp -ReadableByTunnel
Set-PrivateAcl (Join-Path $InstallRoot 'data') -WritableByApp
Set-PrivateAcl (Join-Path $InstallRoot 'backups') -WritableByApp
Set-PrivateAcl (Join-Path $InstallRoot 'state') -WritableByApp
Set-PrivateAcl (Join-Path $InstallRoot 'logs') -WritableByApp -WritableByTunnel
Set-PrivateAcl (Join-Path $InstallRoot 'releases') -ReadableByApp -ReadableByTunnel
Set-PrivateAcl (Join-Path $InstallRoot 'runtime') -ReadableByApp -ReadableByTunnel

# A newly installed machine has no secret file. Keeping services registered but stopped
# is the non-crashing configuration_required state; configure.ps1 starts them only after validation.
$secretFile = Join-Path $InstallRoot 'config\secrets.env'
if (-not (Test-Path -LiteralPath $secretFile)) {
    'configuration_required' | Set-Content -LiteralPath (Join-Path $InstallRoot 'state\service-status') -Encoding ASCII
    Write-Host "Installed. Run .\configure.ps1 -InstallRoot '$InstallRoot' to provide secrets."
} else {
    Start-Service -Name StockMcpService
    if (-not (Wait-LocalReady)) { throw 'MCP local readiness check failed after install.' }
    Start-Service -Name StockMcpTunnel
}
