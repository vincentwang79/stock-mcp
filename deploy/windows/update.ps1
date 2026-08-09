[CmdletBinding(DefaultParameterSetName = 'Archive')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Archive')][string] $PackagePath,
    [Parameter(Mandatory = $true, ParameterSetName = 'Archive')][string] $PackageSha256,
    [Parameter(Mandatory = $true, ParameterSetName = 'Directory')][string] $PackageDirectory,
    [string] $InstallRoot = 'E:\StockMcp'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'deploy\lib.ps1')

function Set-UpdateState([Parameter(Mandatory = $true)][string] $State) {
    $stateDirectory = Join-Path $InstallRoot 'state'
    New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
    $State | Set-Content -LiteralPath (Join-Path $stateDirectory 'service-status') -Encoding ASCII
}

function Get-ConfigurationState([Parameter(Mandatory = $true)][string] $Root) {
    $stateFile = Join-Path $Root 'state\service-status'
    if (-not (Test-Path -LiteralPath $stateFile -PathType Leaf)) { return 'configuration_required' }
    $state = (Get-Content -LiteralPath $stateFile -Raw).Trim()
    if ($state -in @('ready', 'rollback_ready')) { return 'ready' }
    return 'configuration_required'
}

function Get-CurrentReleaseTarget([Parameter(Mandatory = $true)][string] $Root) {
    $current = Get-Item -LiteralPath (Join-Path $Root 'current')
    $targets = @($current.Target)
    if ($targets.Count -ne 1 -or [string]::IsNullOrWhiteSpace([string] $targets[0])) {
        throw "Current release junction does not have one usable target: $($current.FullName)"
    }
    return [string] $targets[0]
}

function Invoke-UpdateDoctor {
    param(
        [Parameter(Mandatory = $true)][string] $Python,
        [Parameter(Mandatory = $true)][string] $Root
    )
    # The CLI deliberately returns 2 for the valid first-install state.  Capture
    # output so it remains visible, then distinguish that state from a real failure.
    $doctorOutput = & $Python -m stock_mcp.cli doctor --root $Root 2>&1 | Out-String
    if (-not [string]::IsNullOrWhiteSpace($doctorOutput)) { Write-Host $doctorOutput.TrimEnd() }
    if ($LASTEXITCODE -eq 0) { return 'ready' }
    if ($LASTEXITCODE -eq 2 -and $doctorOutput -match '(?m)^stock-mcp:\s+configuration_required\s*$') {
        return 'configuration_required'
    }
    throw "stock-mcp doctor failed. $doctorOutput"
}

function Wait-DatabaseExclusiveAccess {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [int] $Attempts = 30
    )
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $stream = $null
        try {
            $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
            return
        } catch [IO.IOException] {
            if ($attempt -eq $Attempts) { break }
            Start-Sleep -Seconds 1
        } finally {
            if ($null -ne $stream) { $stream.Dispose() }
        }
    }
    throw "Database is still in use after services stopped: $Path"
}

function New-WinSWServiceDefinitions {
    param(
        [Parameter(Mandatory = $true)][string] $Root,
        [Parameter(Mandatory = $true)][string] $Release,
        [Parameter(Mandatory = $true)][string] $Destination
    )
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    foreach ($name in @('StockMcpService', 'StockMcpTunnel')) {
        $template = Join-Path $Release ("deploy\services\{0}.xml.tmpl" -f $name)
        if (-not (Test-Path -LiteralPath $template -PathType Leaf)) { throw "Missing service template: $template" }
        $xml = Join-Path $Destination ("{0}.xml" -f $name)
        (Get-Content -LiteralPath $template -Raw).Replace('__INSTALL_ROOT__', $Root) |
            Set-Content -LiteralPath $xml -Encoding UTF8
    }
}

function Refresh-WinSWServiceDefinitions {
    param(
        [Parameter(Mandatory = $true)][string] $ServiceDirectory,
        [Parameter(Mandatory = $true)][string] $WinSw
    )
    foreach ($name in @('StockMcpService', 'StockMcpTunnel')) {
        $xml = Join-Path $ServiceDirectory ("{0}.xml" -f $name)
        if ($null -eq (Get-Service -Name $name -ErrorAction SilentlyContinue)) {
            & $WinSw install $xml
            if ($LASTEXITCODE -ne 0) { throw "WinSW could not install $name." }
        } else {
            & $WinSw refresh $xml
            if ($LASTEXITCODE -ne 0) { throw "WinSW could not refresh $name." }
        }
        Set-StockMcpServiceIdentity $name
    }
}

Test-Administrator
Assert-WindowsX64
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot 'current'))) { throw 'Stock MCP is not installed.' }
$PackageRoot = $null
$work = $null
$packageIsArchive = $PSCmdlet.ParameterSetName -eq 'Archive'
if ($packageIsArchive) {
    $PackagePath = [IO.Path]::GetFullPath($PackagePath)
    if (-not (Test-Path -LiteralPath $PackagePath -PathType Leaf)) { throw "Package not found: $PackagePath" }
    Assert-Sha256 $PackagePath $PackageSha256
    $work = Join-Path ([IO.Path]::GetTempPath()) ("stock-mcp-update-" + [guid]::NewGuid().ToString('N'))
} else {
    $PackageRoot = [IO.Path]::GetFullPath($PackageDirectory)
    if (-not (Test-Path -LiteralPath $PackageRoot -PathType Container)) { throw "Package directory not found: $PackageRoot" }
}

$backupRoot = Join-Path $InstallRoot ('backups\update-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$oldTarget = Get-CurrentReleaseTarget $InstallRoot
$configurationState = Get-ConfigurationState $InstallRoot
$newRelease = $null
$newReleaseStaging = $null
$newReleaseCreated = $false
$oldPython = $null
$databaseBackup = $null
$servicesStopped = $false
$toolsDestination = Join-Path $InstallRoot 'runtime\tools'
$toolsStaging = $null
$toolsBackup = $null
$toolsReplaced = $false
$toolsReplacementStarted = $false
$toolsAcl = $null
$servicesDirectory = Join-Path $InstallRoot 'runtime\services'
$servicesBackup = $null
$servicesAcl = $null
$servicesStaging = $null
$servicesReplaced = $false
if ($work) { New-Item -ItemType Directory -Path $work -Force | Out-Null }
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
try {
    if ($packageIsArchive) {
        Expand-Archive -LiteralPath $PackagePath -DestinationPath $work -Force
        $topLevel = @(Get-ChildItem -LiteralPath $work -Force)
        $directories = @($topLevel | Where-Object { $_.PSIsContainer })
        if ($directories.Count -ne 1 -or $topLevel.Count -ne 1) { throw 'Update ZIP must contain exactly one release directory.' }
        $extracted = $directories[0]
    } else {
        $extracted = Get-Item -LiteralPath $PackageRoot
    }
    Assert-ReleaseContents $extracted.FullName
    $version = Get-ReleaseVersion $extracted.FullName
    $manifest = Get-ToolManifest (Join-Path $extracted.FullName 'tools-manifest.json')
    $packageTools = Join-Path $extracted.FullName 'tools'
    $newRelease = Join-Path $InstallRoot ("releases\" + $version)
    if (Test-Path -LiteralPath $newRelease) { throw "Release is already installed: $version" }
    $newReleaseStaging = Join-Path $InstallRoot ("releases\.staging-" + $version + '-' + [guid]::NewGuid().ToString('N'))

    $oldPython = Join-Path $oldTarget '.venv\Scripts\python.exe'
    $databaseBackup = Join-Path $backupRoot 'stock-mcp-before-update.sqlite3'
    & $oldPython -m stock_mcp.cli backup --root $InstallRoot --destination $databaseBackup
    if ($LASTEXITCODE -ne 0) { throw 'Verified online database backup failed.' }
    $servicesStopped = $true
    Stop-StockServices
    Wait-DatabaseExclusiveAccess (Join-Path $InstallRoot 'data\stock-mcp.sqlite3')
    Copy-Item -LiteralPath $oldTarget -Destination (Join-Path $backupRoot 'previous-release') -Recurse -Force
    if (Test-Path -LiteralPath $toolsDestination) {
        $toolsAcl = Get-Acl -LiteralPath $toolsDestination
        $toolsBackup = Join-Path $backupRoot 'tools'
        Copy-Item -LiteralPath $toolsDestination -Destination $toolsBackup -Recurse -Force
    }
    if (Test-Path -LiteralPath $servicesDirectory) {
        $servicesAcl = Get-Acl -LiteralPath $servicesDirectory
        $servicesBackup = Join-Path $backupRoot 'services'
        Copy-Item -LiteralPath $servicesDirectory -Destination $servicesBackup -Recurse -Force
    }

    $toolsStaging = Join-Path $InstallRoot ('runtime\tools.staging-' + [guid]::NewGuid().ToString('N'))
    $stagedUv = Get-VerifiedTool $manifest.tools.uv $packageTools $toolsStaging
    [void](Get-VerifiedTool $manifest.tools.winsw $packageTools $toolsStaging)
    [void](Get-VerifiedTool $manifest.tools.'tunnel-client' $packageTools $toolsStaging)
    if (Test-Path -LiteralPath $toolsDestination) {
        # From this point rollback must restore the backup even if Move-Item fails.
        $toolsReplacementStarted = $true
        Remove-Item -LiteralPath $toolsDestination -Recurse -Force
    }
    Move-Item -LiteralPath $toolsStaging -Destination $toolsDestination
    $toolsStaging = $null
    $toolsReplaced = $true
    if ($null -ne $toolsAcl) { Set-Acl -LiteralPath $toolsDestination -AclObject $toolsAcl }

    New-Item -ItemType Directory -Path $newReleaseStaging -Force | Out-Null
    Copy-Item -Path (Join-Path $extracted.FullName 'app\*') -Destination $newReleaseStaging -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $extracted.FullName 'deploy') -Destination (Join-Path $newReleaseStaging 'deploy') -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $extracted.FullName 'release-manifest.json') -Destination (Join-Path $newReleaseStaging 'release-manifest.json') -Force
    $servicesStaging = Join-Path $InstallRoot ('runtime\services.staging-' + [guid]::NewGuid().ToString('N'))
    New-WinSWServiceDefinitions $InstallRoot $newReleaseStaging $servicesStaging

    $uv = Join-Path $toolsDestination ([IO.Path]::GetFileName($manifest.tools.uv.path))
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $InstallRoot 'runtime\python'
    $env:UV_CACHE_DIR = Join-Path $InstallRoot 'runtime\uv-cache'
    & $uv venv (Join-Path $newReleaseStaging '.venv') --python 3.12
    if ($LASTEXITCODE -ne 0) { throw 'uv could not prepare Python 3.12.' }
    Push-Location $newReleaseStaging
    try { & $uv sync --locked --no-dev --no-editable --extra providers; if ($LASTEXITCODE -ne 0) { throw 'uv sync failed.' } }
    finally { Pop-Location }

    $python = Join-Path $newReleaseStaging '.venv\Scripts\python.exe'
    & $python -m stock_mcp.cli migrate --root $InstallRoot
    if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }
    # Keep the literal command as an operator-visible acceptance criterion as well.
    $doctorStatus = Invoke-UpdateDoctor $python $InstallRoot # stock-mcp doctor

    Move-Item -LiteralPath $newReleaseStaging -Destination $newRelease
    $newReleaseCreated = $true
    $newReleaseStaging = $null
    New-CurrentJunction $InstallRoot $newRelease
    $winSw = Join-Path $toolsDestination ([IO.Path]::GetFileName($manifest.tools.winsw.path))
    if (Test-Path -LiteralPath $servicesDirectory) { Remove-Item -LiteralPath $servicesDirectory -Recurse -Force }
    Move-Item -LiteralPath $servicesStaging -Destination $servicesDirectory
    $servicesStaging = $null
    $servicesReplaced = $true
    if ($null -ne $servicesAcl) { Set-Acl -LiteralPath $servicesDirectory -AclObject $servicesAcl }
    Refresh-WinSWServiceDefinitions $servicesDirectory $winSw
    # Account identities may have changed since a partial or older installation.
    # Reapply all runtime ACLs before any service starts.
    Set-PrivateAcl $InstallRoot -ReadableByApp -ReadableByTunnel
    Set-PrivateAcl (Join-Path $InstallRoot 'config') -ReadableByApp -ReadableByTunnel
    Set-PrivateAcl (Join-Path $InstallRoot 'data') -WritableByApp
    Set-PrivateAcl (Join-Path $InstallRoot 'backups') -WritableByApp
    Set-PrivateAcl (Join-Path $InstallRoot 'state') -WritableByApp
    Set-PrivateAcl (Join-Path $InstallRoot 'logs') -WritableByApp -WritableByTunnel
    Set-PrivateAcl (Join-Path $InstallRoot 'releases') -ReadableByApp -ReadableByTunnel
    Set-PrivateAcl (Join-Path $InstallRoot 'runtime') -ReadableByApp -ReadableByTunnel
    $secretFile = Join-Path $InstallRoot 'config\secrets.env'
    if (Test-Path -LiteralPath $secretFile -PathType Leaf) {
        Set-PrivateFileAcl $secretFile -Reader StockMcpService
    }
    $tunnelKeyFile = Join-Path $InstallRoot 'config\tunnel-api-key'
    if (Test-Path -LiteralPath $tunnelKeyFile -PathType Leaf) {
        Set-PrivateFileAcl $tunnelKeyFile -Reader StockMcpTunnel
    }
    $tunnelProfile = Join-Path $InstallRoot 'config\tunnel-client.yaml'
    if (Test-Path -LiteralPath $tunnelProfile -PathType Leaf) {
        Set-PrivateFileAcl $tunnelProfile -Reader StockMcpTunnel
    }
    $customCa = Join-Path $InstallRoot 'config\custom-ca.pem'
    if (Test-Path -LiteralPath $customCa -PathType Leaf) {
        Set-PrivateFileAcl $customCa -Reader StockMcpService -SharedWithOtherService
    }
    if ($doctorStatus -eq 'configuration_required' -or $configurationState -ne 'ready') {
        Set-UpdateState 'configuration_required'
        Write-Host "Updated to $version. Configuration is still required. Backup: $backupRoot"
    } else {
        Start-Service -Name StockMcpService
        if (-not (Wait-LocalReady -PythonExe $python)) { throw 'Readiness check failed after update.' }
        Start-Service -Name StockMcpTunnel
        if (-not (Wait-TunnelReady -PythonExe $python)) { throw 'Tunnel readiness check failed after update.' }
        Set-UpdateState 'ready'
        Write-Host "Updated to $version. Backup: $backupRoot"
    }
} catch {
    $failure = $_
    Set-UpdateState 'update_failed'
    Write-Warning "Update failed; Rollback is starting: $($failure.Exception.Message)"
    if ($servicesStopped) {
        try {
            Stop-StockServices
            Wait-DatabaseExclusiveAccess (Join-Path $InstallRoot 'data\stock-mcp.sqlite3')
            if ($oldTarget -and (Test-Path -LiteralPath $oldTarget)) { New-CurrentJunction $InstallRoot $oldTarget }
            if ($databaseBackup -and (Test-Path -LiteralPath $databaseBackup)) {
                & $oldPython -m stock_mcp.cli restore --root $InstallRoot --source $databaseBackup
                if ($LASTEXITCODE -ne 0) { throw 'Database restore failed during Rollback.' }
            }
            if ($toolsReplacementStarted -and $toolsBackup -and (Test-Path -LiteralPath $toolsBackup)) {
                if (Test-Path -LiteralPath $toolsDestination) { Remove-Item -LiteralPath $toolsDestination -Recurse -Force }
                Copy-Item -LiteralPath $toolsBackup -Destination (Join-Path $InstallRoot 'runtime') -Recurse -Force
                if ($null -ne $toolsAcl) { Set-Acl -LiteralPath $toolsDestination -AclObject $toolsAcl }
            }
            if ($servicesBackup -and (Test-Path -LiteralPath $servicesBackup)) {
                if (Test-Path -LiteralPath $servicesDirectory) { Remove-Item -LiteralPath $servicesDirectory -Recurse -Force }
                Copy-Item -LiteralPath $servicesBackup -Destination (Join-Path $InstallRoot 'runtime') -Recurse -Force
                if ($null -ne $servicesAcl) { Set-Acl -LiteralPath $servicesDirectory -AclObject $servicesAcl }
            }
            $oldWinSw = Join-Path $toolsDestination 'WinSW.exe'
            if (-not (Test-Path -LiteralPath $oldWinSw -PathType Leaf)) { throw 'Rollback WinSW executable is missing.' }
            Refresh-WinSWServiceDefinitions $servicesDirectory $oldWinSw
            $rollbackDoctorStatus = Invoke-UpdateDoctor $oldPython $InstallRoot
            if ($rollbackDoctorStatus -eq 'configuration_required' -or $configurationState -ne 'ready') {
                Set-UpdateState 'configuration_required'
                Write-Warning 'Rollback succeeded; configuration is still required.'
            } else {
                Start-Service -Name StockMcpService
                if (-not (Wait-LocalReady -PythonExe $oldPython)) { throw 'Rollback MCP readiness check failed.' }
                Start-Service -Name StockMcpTunnel
                if (-not (Wait-TunnelReady -PythonExe $oldPython)) { throw 'Rollback Tunnel readiness check failed.' }
                Set-UpdateState 'rollback_ready'
                Write-Warning 'Rollback succeeded; the previous release is ready.'
            }
        } catch {
            Set-UpdateState 'rollback_failed'
            Write-Warning "Rollback encountered an error: $($_.Exception.Message)"
        }
    }
    if ($newReleaseCreated -and $newRelease -and (Test-Path -LiteralPath $newRelease) -and
        $newRelease -ne (Get-CurrentReleaseTarget $InstallRoot)) {
        try { Remove-Item -LiteralPath $newRelease -Recurse -Force }
        catch { Write-Warning "Could not remove failed release: $newRelease" }
    }
    if ($newReleaseStaging -and (Test-Path -LiteralPath $newReleaseStaging)) {
        try { Remove-Item -LiteralPath $newReleaseStaging -Recurse -Force }
        catch { Write-Warning "Could not remove failed staging release: $newReleaseStaging" }
    }
    if ($toolsStaging -and (Test-Path -LiteralPath $toolsStaging)) {
        try { Remove-Item -LiteralPath $toolsStaging -Recurse -Force }
        catch { Write-Warning "Could not remove failed staging tools: $toolsStaging" }
    }
    if ($servicesStaging -and (Test-Path -LiteralPath $servicesStaging)) {
        try { Remove-Item -LiteralPath $servicesStaging -Recurse -Force }
        catch { Write-Warning "Could not remove failed staging service definitions: $servicesStaging" }
    }
    throw $failure
} finally {
    if ($work -and (Test-Path -LiteralPath $work)) { Remove-Item -LiteralPath $work -Recurse -Force }
}
