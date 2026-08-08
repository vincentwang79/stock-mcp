# Stock MCP for Windows Server

This is a native Windows x64 deployment: no Docker, WSL, Git, Node, system Python,
or database server is required. The MCP process listens only on
`127.0.0.1:8765/mcp`; Secure MCP Tunnel is a separate outbound-HTTPS service, so no
inbound firewall rule is created.

## Install and configure

1. Obtain `stock-mcp-windows-x64.zip` and its separately published `.sha256` value from
   the release channel. **Before extracting the ZIP or running any script**, compare the
   published digest with `Get-FileHash C:\path\stock-mcp-windows-x64.zip -Algorithm SHA256`.
   The bootstrap scripts are not Authenticode-signed and therefore cannot establish their
   own trust; only run a freshly extracted package after this external comparison.
   Then extract the ZIP to a local folder.
2. In an elevated PowerShell console, run
   `./install.ps1 -PackageArchive C:\path\stock-mcp-windows-x64.zip -PackageSha256 <published-64-hex-digest>`.
   The installer first accepts the original archive against that external trust root,
   then checks x64,
   free disk space, outbound HTTPS, all package checksums, and the pinned SHA-256 of
   `uv`, WinSW, and `tunnel-client`. It installs to `C:\ProgramData\StockMcp` by default.
3. Run `./configure.ps1`. Each production secret is requested securely and kept only in
   ACL-protected host configuration. It validates data access, Tunnel doctor, and MCP
   loopback health before starting `StockMcpService` and `StockMcpTunnel`.

The tunnel service runs the official `tunnel-client run --config ...` flow. Its YAML
profile contains an `api_key: file:...` reference, not the runtime key itself; doctor
uses `--explain`, and the tunnel health/UI remain on loopback port 8766.

Until step 3, both services are registered but deliberately stopped and
`state/service-status` reads `configuration_required`; this avoids a crash loop.

Strategy activation has a host-side approval gate. After comparing a proposed version
over the complete three-year recorded trading calendar (the first 20 sessions are warm-up),
an administrator runs `stock-mcp approve-strategy --root C:\ProgramData\StockMcp
--version <version>` and types the version at the prompt. The approval is bound to the
stored parameter hash, consumed once, and cannot be created through MCP; the user can
then invoke `activate_strategy_version` from ChatGPT.

## Upgrade, recovery, and removal

First verify the separately published SHA-256 sidecar, then run
`./update.ps1 -PackagePath C:\path\stock-mcp-windows-x64.zip -PackageSha256 <published-64-hex-digest>` in an elevated
console. It validates the archive, stops both services, creates release/database
backups, performs migration and `stock-mcp doctor`, switches the `current` junction,
and rolls back program and database if local health fails.

Run `./diagnose.ps1` to make a redacted ZIP containing service state, recent logs,
database integrity and doctor outputs. It explicitly excludes `secrets.env` and key
files. Run `./uninstall.ps1` to remove services and program runtime while retaining
configuration, data, logs and backups. Only `./uninstall.ps1 -PurgeData` asks for a
second confirmation before deleting retained information.

## Release construction

Run `./fetch-tools.ps1` to download the fixed assets in `tools-manifest.json`, verify
both archive and extracted executable SHA-256 values, and create `tools-cache`.
Then run `build-release.ps1 -Version <version> -ToolsManifest ./tools-manifest.json
-ToolsDirectory ./tools-cache`. The builder refuses placeholders,
missing `uv.lock`, or a binary whose hash differs from the manifest; it produces the
fixed filename `stock-mcp-windows-x64.zip`, its externally distributed
`stock-mcp-windows-x64.zip.sha256`, and an internal `checksums.txt`. Treat the
published SHA-256 as the trust root: `checksums.txt` only detects damage after the ZIP
has already been accepted.

The two services use separate Windows virtual accounts (`NT SERVICE\StockMcpService`
and `NT SERVICE\StockMcpTunnel`). Program/releases are read-only to both; only the app
account can modify data/backups/state, and only the Tunnel account can read its runtime
key. Do not put tokens in PowerShell command history, service XML, logs, Issue text, or
this README.
