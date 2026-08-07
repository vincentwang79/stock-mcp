[CmdletBinding()]
param([Parameter(Mandatory = $true)][string] $InstallRoot)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# The YAML refers to the ACL-protected runtime-key file with a `file:` value.
# Neither the secret nor its value appears in this process command line.
$clientConfig = Join-Path $InstallRoot 'config\tunnel-client.yaml'
if (-not (Test-Path -LiteralPath $clientConfig -PathType Leaf)) { exit 0 }
$client = Join-Path $InstallRoot 'runtime\tools\tunnel-client.exe'
if (-not (Test-Path -LiteralPath $client -PathType Leaf)) { throw 'tunnel-client.exe is missing.' }

& $client run --config $clientConfig
exit $LASTEXITCODE
