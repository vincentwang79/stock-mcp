[CmdletBinding()]
param(
    [string] $Manifest = (Join-Path $PSScriptRoot 'tools-manifest.json'),
    [string] $OutputDirectory = (Join-Path $PSScriptRoot 'tools-cache')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'deploy\lib.ps1')

$tools = Get-ToolManifest $Manifest
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
foreach ($name in @('uv', 'winsw', 'tunnel-client')) {
    $entry = $tools.tools.$name
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ('stock-mcp-tool-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temporary -Force | Out-Null
    try {
        $download = Join-Path $temporary 'asset'
        Invoke-WebRequest -Uri $entry.url -OutFile $download -UseBasicParsing
        Assert-Sha256 $download $entry.archive_sha256
        $destination = Join-Path $OutputDirectory $entry.path
        if ([string]::IsNullOrWhiteSpace($entry.archive_member)) {
            Copy-Item -LiteralPath $download -Destination $destination -Force
        } else {
            $archive = Join-Path $temporary 'asset.zip'
            Move-Item -LiteralPath $download -Destination $archive
            $expanded = Join-Path $temporary 'expanded'
            Expand-Archive -LiteralPath $archive -DestinationPath $expanded
            $member = [IO.Path]::GetFullPath((Join-Path $expanded $entry.archive_member))
            $root = [IO.Path]::GetFullPath($expanded).TrimEnd('\') + '\'
            if (-not $member.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Archive member escapes extraction root for $name."
            }
            Copy-Item -LiteralPath $member -Destination $destination -Force
        }
        Assert-Sha256 $destination $entry.sha256
        Write-Host "Fetched verified $name $($entry.version)"
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force }
    }
}
