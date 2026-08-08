[CmdletBinding(DefaultParameterSetName = 'Archive')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Archive')][string] $PackageArchive,
    [Parameter(Mandatory = $true, ParameterSetName = 'Archive')][string] $PackageSha256,
    [Parameter(Mandatory = $true, ParameterSetName = 'Directory')][string] $PackageDirectory,
    [string] $InstallRoot = 'E:\StockMcp',
    [switch] $SkipConnectivityCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$packageWork = $null
$PackageRoot = $null
$packageIsArchive = $PSCmdlet.ParameterSetName -eq 'Archive'
if ($packageIsArchive) {
    $PackageArchive = [IO.Path]::GetFullPath($PackageArchive)
    if (-not (Test-Path -LiteralPath $PackageArchive -PathType Leaf)) { throw "Package not found: $PackageArchive" }
    if ($PackageSha256 -notmatch '^[0-9a-fA-F]{64}$') { throw 'PackageSha256 must be a 64-character SHA-256 digest.' }
    $actualPackageSha256 = (Get-FileHash -LiteralPath $PackageArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualPackageSha256 -ne $PackageSha256.ToLowerInvariant()) { throw 'Package archive SHA-256 does not match the published digest.' }
    $packageWork = Join-Path ([IO.Path]::GetTempPath()) ('stock-mcp-install-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $packageWork -Force | Out-Null
} else {
    $PackageRoot = [IO.Path]::GetFullPath($PackageDirectory)
    if (-not (Test-Path -LiteralPath $PackageRoot -PathType Container)) { throw "Package directory not found: $PackageRoot" }
}
try {
    if ($packageIsArchive) {
        Expand-Archive -LiteralPath $PackageArchive -DestinationPath $packageWork
        $topLevel = @(Get-ChildItem -LiteralPath $packageWork -Force)
        $packageDirectories = @($topLevel | Where-Object { $_.PSIsContainer })
        if ($packageDirectories.Count -ne 1 -or $topLevel.Count -ne 1) { throw 'Install ZIP must contain exactly one release directory.' }
        $PackageRoot = $packageDirectories[0].FullName
    }
    . (Join-Path $PackageRoot 'deploy\lib.ps1')
    if ($packageIsArchive) { Assert-Sha256 $PackageArchive $PackageSha256 }

function Assert-FreeSpace([string] $Path, [int64] $MinimumBytes = 4294967296) {
    $drive = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Path))
    $info = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($drive.TrimEnd('\\'))'"
    if ($null -eq $info -or [int64]$info.FreeSpace -lt $MinimumBytes) {
        throw "At least 4 GB free disk space is required on $drive."
    }
}

function Assert-OutboundHttps {
    try {
        Invoke-WebRequest -Uri 'https://api.github.com' -UseBasicParsing -TimeoutSec 15 | Out-Null
        if (-not (Test-NetConnection -ComputerName 'api.openai.com' -Port 443 -InformationLevel Quiet)) {
            throw 'api.openai.com:443 is unreachable.'
        }
    }
    catch { throw "Outbound HTTPS is required for verified tool retrieval and Secure MCP Tunnel. $($_.Exception.Message)" }
}

function Install-Release([string] $Root, [string] $ReleaseVersion) {
    $releases = Join-Path $Root 'releases'
    $target = Join-Path $releases $ReleaseVersion
    if (Test-Path -LiteralPath $target) { return $target }
    $staging = Join-Path $releases ('.staging-' + $ReleaseVersion + '-' + [guid]::NewGuid().ToString('N'))
    try {
        New-Item -ItemType Directory -Path $staging -Force | Out-Null
        Copy-Item -Path (Join-Path $PackageRoot 'app\*') -Destination $staging -Recurse -Force
        Copy-Item -LiteralPath (Join-Path $PackageRoot 'deploy') -Destination (Join-Path $staging 'deploy') -Recurse -Force
        Copy-Item -LiteralPath (Join-Path $PackageRoot 'release-manifest.json') -Destination (Join-Path $staging 'release-manifest.json') -Force
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
Assert-ReleaseContents $PackageRoot
$Version = Get-ReleaseVersion $PackageRoot

$manifest = Get-ToolManifest (Join-Path $PackageRoot 'tools-manifest.json')
foreach ($directory in @('config', 'data', 'logs', 'backups', 'releases', 'runtime', 'state')) {
    New-Item -ItemType Directory -Path (Join-Path $InstallRoot $directory) -Force | Out-Null
}
Set-PrivateAcl $InstallRoot
foreach ($directory in @('config', 'data', 'logs', 'backups', 'releases', 'runtime', 'state')) {
    Set-PrivateAcl (Join-Path $InstallRoot $directory)
}

$toolsDestination = Join-Path $InstallRoot 'runtime\tools'
$packageTools = Join-Path $PackageRoot 'tools'
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
    if (-not (Wait-TunnelReady)) { throw 'Tunnel readiness check failed after install.' }
}
} finally {
    if ($packageWork -and (Test-Path -LiteralPath $packageWork)) { Remove-Item -LiteralPath $packageWork -Recurse -Force }
}
