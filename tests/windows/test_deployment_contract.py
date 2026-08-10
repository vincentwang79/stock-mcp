import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / "deploy" / "windows"


class WindowsDeploymentContractTest(unittest.TestCase):
    def _read_required(self, name: str) -> str:
        path = WINDOWS / name
        self.assertTrue(path.is_file(), f"missing deployment artifact: {path}")
        return path.read_text(encoding="utf-8")

    def test_governance_replay_docs_describe_the_durable_mcp_and_windows_gates(self) -> None:
        """Published Chinese docs keep the replay governance boundary auditable."""
        root = ROOT
        strategy = (root / "docs" / "task-specs" / "02-strategy-replay.md").read_text(
            encoding="utf-8"
        )
        workflow = (root / "docs" / "task-specs" / "03-mcp-workflow.md").read_text(
            encoding="utf-8"
        )
        windows = self._read_required("README-WINDOWS.md")

        for tool in (
            "start_strategy_replay",
            "get_strategy_replay",
            "list_strategy_replays",
            "get_strategy_replay_days",
            "certify_strategy_replay",
        ):
            self.assertIn(tool, workflow)
        for phrase in (
            "queued",
            "running",
            "completed",
            "failed",
            "after_trade_date",
            "分页",
            "证明永久保留",
            "同版本",
            "纯只读",
        ):
            self.assertIn(phrase, strategy + workflow)
        self.assertIn("Schema v9", strategy + workflow + windows)
        self.assertIn("2023-08-08", windows)
        self.assertIn("2026-08-07", windows)
        self.assertIn("certify_strategy_replay", windows)
        self.assertIn("approve-strategy", windows)
        self.assertIn("activate_strategy_version", windows)
        self.assertIn("外部门禁", windows)
        self.assertNotIn("727日回放已验证", windows)

    def test_release_contains_one_command_install_surface(self) -> None:
        required = {
            "install.ps1",
            "install-from-source.ps1",
            "configure.ps1",
            "update.ps1",
            "diagnose.ps1",
            "uninstall.ps1",
            "build-release.ps1",
            "fetch-tools.ps1",
            "README-WINDOWS.md",
        }

        self.assertTrue(WINDOWS.is_dir(), f"missing deployment directory: {WINDOWS}")
        self.assertEqual(
            required, {path.name for path in WINDOWS.iterdir() if path.is_file()} & required
        )

    def test_tool_fetch_is_versioned_archive_verified_and_reproducible(self) -> None:
        fetch = self._read_required("fetch-tools.ps1")
        manifest = self._read_required("tools-manifest.json")

        self.assertIn("archive_sha256", manifest)
        self.assertNotIn("example.invalid", manifest)
        self.assertIn("Assert-Sha256 $download $entry.archive_sha256", fetch)
        self.assertIn("Expand-Archive", fetch)
        self.assertIn("Assert-Sha256 $destination $entry.sha256", fetch)

    def test_tunnel_client_includes_plain_http_mcp_oauth_discovery_fix(self) -> None:
        manifest = json.loads(self._read_required("tools-manifest.json"))
        tunnel = manifest["tools"]["tunnel-client"]

        self.assertEqual("0.0.11", tunnel["version"])
        self.assertEqual(
            "https://github.com/openai/tunnel-client/releases/download/v0.0.11/"
            "tunnel-client-v0.0.11-windows-amd64.zip",
            tunnel["url"],
        )
        self.assertEqual(
            "eb912c86c6ccde90cda805cb17009507176a656725cf86c36fabe1901a12e29b",
            tunnel["archive_sha256"],
        )
        self.assertEqual(
            "7d3c7d492ce84b52835e11865a835a8a5bcd4a669dee84e169aa11b314dc952a",
            tunnel["sha256"],
        )

    def test_tool_fetch_uses_tls12_and_retries_transient_https_failures(self) -> None:
        fetch = self._read_required("fetch-tools.ps1")

        self.assertIn("SecurityProtocolType]::Tls12", fetch)
        self.assertIn("function Invoke-ToolDownload", fetch)
        self.assertIn("$attempt -le 3", fetch)
        self.assertIn("TimeoutSec = 120", fetch)
        self.assertIn("Failed to download", fetch)

    def test_tool_fetch_accepts_a_direct_executable_without_archive_member(self) -> None:
        fetch = self._read_required("fetch-tools.ps1")

        self.assertIn("PSObject.Properties['archive_member']", fetch)
        self.assertIn("IsNullOrWhiteSpace($archiveMember)", fetch)

    def test_external_https_downloads_and_preflight_honor_session_proxy(self) -> None:
        fetch = self._read_required("fetch-tools.ps1")
        install = self._read_required("install.ps1")
        library = (WINDOWS / "deploy" / "lib.ps1").read_text(encoding="utf-8")

        self.assertIn("function Get-ConfiguredHttpsProxy", library)
        self.assertIn("$env:HTTPS_PROXY", library)
        self.assertIn("Get-ConfiguredHttpsProxy", fetch)
        self.assertIn("$request.Proxy = $proxy", fetch)
        self.assertIn("Get-ConfiguredHttpsProxy", install)
        self.assertIn("$request.Proxy = $proxy", install)
        self.assertNotIn("Test-NetConnection -ComputerName 'api.openai.com'", install)

    def test_install_uses_isolated_python_and_two_services(self) -> None:
        install = self._read_required("install.ps1")
        library = (WINDOWS / "deploy" / "lib.ps1").read_text(encoding="utf-8")

        self.assertIn("UV_PYTHON_INSTALL_DIR", install)
        self.assertIn("uv sync --locked --no-dev --no-editable", install)
        self.assertIn("StockMcpService", install)
        self.assertIn("StockMcpTunnel", install)
        self.assertIn("Set-StockMcpServiceIdentity $name", install)
        self.assertIn("NT AUTHORITY\\LocalService", library)
        self.assertIn("NT AUTHORITY\\NetworkService", library)
        self.assertIn("StockMcpService", install)
        self.assertIn("StockMcpTunnel", install)
        self.assertNotIn("LocalSystem", install)

    def test_configuration_reads_secrets_interactively_not_from_parameters(self) -> None:
        configure = self._read_required("configure.ps1")
        library = (WINDOWS / "deploy" / "lib.ps1").read_text(encoding="utf-8")

        self.assertIn("Read-Host", configure)
        self.assertIn("-AsSecureString", configure)
        self.assertIn("secrets.env", configure)
        self.assertIn("Set-PrivateFileAcl", configure)
        self.assertIn("icacls", library)
        self.assertNotIn("param([string]$TushareToken", configure.replace(" ", ""))

    def test_configuration_can_read_runtime_key_from_clipboard_without_argv_secret(self) -> None:
        configure = self._read_required("configure.ps1")
        readme = self._read_required("README-WINDOWS.md")

        self.assertIn("[switch] $TunnelRuntimeKeyFromClipboard", configure)
        self.assertIn("Get-Clipboard -Raw", configure)
        self.assertIn("ConvertTo-SecureString", configure)
        self.assertIn("-TunnelRuntimeKeyFromClipboard", readme)

    def test_configuration_can_override_file_tushare_token_from_clipboard(self) -> None:
        configure = self._read_required("configure.ps1")
        readme = self._read_required("README-WINDOWS.md")

        self.assertIn("[switch] $TushareTokenFromClipboard", configure)
        self.assertIn("if ($TushareTokenFromClipboard)", configure)
        self.assertIn("Tushare token must be exactly 56 hexadecimal characters", configure)
        self.assertIn("'^[0-9a-fA-F]{56}$'", configure)
        self.assertIn("-TushareTokenFromClipboard", readme)

    def test_configuration_clears_ambient_tushare_token_before_python_checks(self) -> None:
        configure = self._read_required("configure.ps1")

        clear = "Remove-Item Env:TUSHARE_TOKEN -ErrorAction SilentlyContinue"
        self.assertIn(clear, configure)
        self.assertLess(configure.index(clear), configure.index("-m stock_mcp.cli doctor"))

    def test_configuration_passes_only_the_validated_token_to_python(self) -> None:
        configure = self._read_required("configure.ps1")

        assign = "$env:TUSHARE_TOKEN = $tushareValue"
        doctor = "-m stock_mcp.cli doctor"
        backfill = "-m stock_mcp.cli backfill"
        clear = "Remove-Item Env:TUSHARE_TOKEN -ErrorAction SilentlyContinue"
        self.assertIn(assign, configure)
        self.assertLess(configure.index(assign), configure.index(doctor))
        self.assertLess(configure.index(backfill), configure.rindex(clear))
        self.assertIn("finally", configure)

    def test_configuration_writes_each_host_setting_on_its_own_line(self) -> None:
        configure = self._read_required("configure.ps1")

        self.assertIn("$secretLines = [string[]]::new(3)", configure)
        self.assertIn("$secretLines[0] = 'TUSHARE_TOKEN=' + $tushareValue", configure)
        self.assertIn("$secretLines[1] = 'HTTPS_PROXY=' + (Get-Plaintext $proxy)", configure)
        self.assertIn("$secretLines[2] = 'STOCK_MCP_CA_FILE=' + $managedCa", configure)
        self.assertIn("WriteAllLines($secretFile, $secretLines", configure)
        self.assertNotIn("$lines = @(", configure)

    def test_configuration_can_use_one_acl_protected_input_file(self) -> None:
        configure = self._read_required("configure.ps1")
        example = self._read_required("configure-input.psd1.example")
        library = (WINDOWS / "deploy" / "lib.ps1").read_text(encoding="utf-8")
        readme = self._read_required("README-WINDOWS.md")

        for key in (
            "TushareToken",
            "TunnelId",
            "TunnelRuntimeApiKey",
            "HttpsProxy",
            "CustomCaFilePath",
        ):
            self.assertIn(key, example)
        self.assertIn("[string] $ConfigurationFile", configure)
        self.assertIn("[switch] $WriteConfigurationTemplate", configure)
        self.assertIn("Import-PowerShellDataFile", configure)
        self.assertIn("Set-AdministratorOnlyFileAcl", configure)
        self.assertIn("Remove-Item -LiteralPath $configurationInputFile", configure)
        self.assertIn("function Set-AdministratorOnlyFileAcl", library)
        self.assertIn("-WriteConfigurationTemplate", readme)
        self.assertIn("-ConfigurationFile", readme)

    def test_configuration_file_allows_blank_optional_proxy_and_ca_values(self) -> None:
        configure = self._read_required("configure.ps1")

        self.assertIn("function ConvertTo-ConfigurationSecureString", configure)
        self.assertIn("[AllowEmptyString()][string] $Value", configure)
        self.assertIn("return [Security.SecureString]::new()", configure)
        self.assertIn(
            "ConvertTo-ConfigurationSecureString "
            "(Get-ConfigurationText $configuration 'HttpsProxy' -Optional)",
            configure,
        )
        self.assertIn(
            "ConvertTo-ConfigurationSecureString "
            "(Get-ConfigurationText $configuration 'CustomCaFilePath' -Optional)",
            configure,
        )

    def test_release_builders_include_the_configuration_template(self) -> None:
        build = self._read_required("build-release.ps1")
        source_install = self._read_required("install-from-source.ps1")

        self.assertIn("'configure-input.psd1.example'", build)
        self.assertIn("'configure-input.psd1.example'", source_install)

    def test_update_has_health_checked_rollback_and_uninstall_preserves_data(self) -> None:
        update = self._read_required("update.ps1")
        uninstall = self._read_required("uninstall.ps1")

        self.assertIn("stock-mcp doctor", update)
        self.assertIn("stock_mcp.cli backup", update)
        self.assertIn("stock_mcp.cli restore", update)
        self.assertIn("Rollback", update)
        self.assertIn("current", update)
        self.assertIn("PurgeData", uninstall)
        self.assertIn("ShouldContinue", uninstall)

    def test_release_is_self_contained_and_emits_an_external_digest(self) -> None:
        build = self._read_required("build-release.ps1")

        self.assertIn("README.md", build)
        self.assertIn("stock-mcp-windows-x64.zip.sha256", build)
        self.assertIn("Get-FileSha256 $zip", build)

    def test_update_requires_a_trusted_zip_digest_and_rejects_unsafe_manifests(self) -> None:
        update = self._read_required("update.ps1")
        library = (WINDOWS / "deploy" / "lib.ps1").read_text(encoding="utf-8")

        self.assertIn("ParameterSetName = 'Archive'", update)
        self.assertIn("[string] $PackageSha256", update)
        self.assertIn("Assert-Sha256 $PackagePath $PackageSha256", update)
        self.assertIn("Version must be a safe semantic version", library)
        self.assertIn("Checksum path escapes the release root", library)
        self.assertIn("Release file set does not match checksums.txt", library)
        self.assertIn("if ($servicesStopped)", update)

    def test_diagnostics_explicitly_excludes_secret_files(self) -> None:
        diagnose = self._read_required("diagnose.ps1")

        self.assertIn("secrets.env", diagnose)
        self.assertIn("Exclude", diagnose)

    def test_tunnel_uses_official_run_config_and_file_backed_runtime_key(self) -> None:
        configure = self._read_required("configure.ps1")
        runner = (WINDOWS / "deploy" / "services" / "run-tunnel.ps1").read_text(encoding="utf-8")

        self.assertIn("api_key: 'file:", configure)
        self.assertNotIn("'TUNNEL_API_KEY=' +", configure)
        self.assertIn("doctor --config $profilePath --explain", configure)
        self.assertIn("run --config $clientConfig", runner)
        self.assertNotIn("connect --config", runner)

    def test_tunnel_proxy_is_limited_to_the_openai_control_plane(self) -> None:
        configure = self._read_required("configure.ps1")

        self.assertIn("\"  http_proxy: '$proxyValue'\"", configure)
        self.assertNotIn("$profileLines += \"http_proxy: '$proxyValue'\"", configure)
        self.assertNotIn("mcp.http_proxy", configure)

    def test_windows_scripts_use_the_supported_root_cli_and_healthz(self) -> None:
        configure = self._read_required("configure.ps1")
        update = self._read_required("update.ps1")
        library = (WINDOWS / "deploy" / "lib.ps1").read_text(encoding="utf-8")

        self.assertIn("doctor --root $InstallRoot", configure)
        self.assertIn("migrate --root $InstallRoot", update)
        self.assertNotIn("stock_mcp.cli doctor --config", configure + update)
        self.assertIn("127.0.0.1:8765/readyz", library)

    def test_loopback_readiness_uses_isolated_python_probe(self) -> None:
        library = (WINDOWS / "deploy" / "lib.ps1").read_text(encoding="utf-8")
        configure = self._read_required("configure.ps1")

        self.assertIn("function Test-LoopbackReady", library)
        self.assertIn("[string] $PythonExe", library)
        self.assertIn("-m stock_mcp.loopback_probe $Url", library)
        self.assertIn("[int] $Attempts = 30", library)
        self.assertIn("Test-LoopbackReady -Url $Url -PythonExe $PythonExe", library)
        self.assertIn("Wait-LocalReady -PythonExe $servicePython", configure)
        self.assertIn("Wait-TunnelReady -PythonExe $servicePython", configure)
        self.assertNotIn("[System.Net.HttpWebRequest]::Create", library)
        self.assertNotIn("Invoke-WebRequest -Uri $Url -UseBasicParsing", library)

    def test_configuration_starts_the_app_before_tunnel_doctor(self) -> None:
        configure = self._read_required("configure.ps1")

        app_start = configure.index("Start-Service -Name StockMcpService")
        health_check = configure.index("Wait-LocalReady")
        tunnel_doctor = configure.index("doctor --config $profilePath --explain")
        tunnel_start = configure.index("Start-Service -Name StockMcpTunnel")
        self.assertLess(app_start, health_check)
        self.assertLess(health_check, tunnel_doctor)
        self.assertLess(tunnel_doctor, tunnel_start)

    def test_configuration_completes_three_year_backfill_before_starting_services(self) -> None:
        configure = self._read_required("configure.ps1")

        self.assertIn("AddYears(-3)", configure)
        self.assertIn("stock_mcp.cli backfill --root $InstallRoot", configure)
        self.assertLess(
            configure.index("stock_mcp.cli backfill"),
            configure.index("Start-Service -Name StockMcpService"),
        )

    def test_install_uses_the_release_manifest_and_checksums_not_a_user_version(self) -> None:
        install = self._read_required("install.ps1")

        self.assertIn("Assert-ReleaseContents $PackageRoot", install)
        self.assertIn("Get-ReleaseVersion $PackageRoot", install)
        self.assertNotIn("[string] $Version", install)

    def test_custom_ca_is_copied_into_protected_host_configuration(self) -> None:
        configure = self._read_required("configure.ps1")

        self.assertIn("Copy-Item -LiteralPath $customCaValue", configure)
        self.assertIn("custom-ca.pem", configure)
        self.assertIn("Set-PrivateFileAcl $managedCa", configure)

    def test_diagnostics_are_redacted_and_collect_operational_context(self) -> None:
        diagnose = self._read_required("diagnose.ps1")

        self.assertIn("New-Item -ItemType Directory -Path $logDestination", diagnose)
        self.assertIn("Redact-DiagnosticText", diagnose)
        self.assertIn("doctor --root $InstallRoot", diagnose)
        self.assertIn("release-manifest.json", diagnose)
        self.assertIn("backups.txt", diagnose)

    def test_uninstall_verifies_that_services_were_removed(self) -> None:
        uninstall = self._read_required("uninstall.ps1")

        self.assertIn("Wait-ServiceRemoval", uninstall)
        self.assertIn("Service still exists after uninstall", uninstall)

    def test_readiness_is_required_before_tunnel_startup(self) -> None:
        install = self._read_required("install.ps1")
        configure = self._read_required("configure.ps1")
        update = self._read_required("update.ps1")

        for script in (install, configure, update):
            self.assertIn("Wait-LocalReady", script)
            self.assertIn("Start-Service -Name StockMcpService", script)

    def test_update_prepares_a_staging_release_before_switching_current(self) -> None:
        update = self._read_required("update.ps1")

        self.assertIn(".staging-", update)
        self.assertIn("Move-Item -LiteralPath $newReleaseStaging -Destination $newRelease", update)

    def test_diagnostics_redact_json_secrets_and_complete_bearer_values(self) -> None:
        diagnose = self._read_required("diagnose.ps1")

        self.assertIn('"(?:api_key|token)"\\s*:\\s*"[^"]+"', diagnose)
        self.assertIn("Authorization\\s*[:=]\\s*Bearer\\s+[^\\s]+", diagnose)
        self.assertIn("[REDACTED]", diagnose)

    def test_first_install_requires_an_external_package_digest(self) -> None:
        install = self._read_required("install.ps1")

        self.assertIn("ParameterSetName = 'Archive'", install)
        self.assertIn("[string] $PackageSha256", install)
        self.assertIn("Assert-Sha256 $PackageArchive $PackageSha256", install)
        self.assertIn("Expand-Archive -LiteralPath $PackageArchive", install)

    def test_install_and_update_keep_release_manifest_with_versioned_code(self) -> None:
        install = self._read_required("install.ps1")
        update = self._read_required("update.ps1")

        for script in (install, update):
            self.assertIn("release-manifest.json", script)
            self.assertIn("Copy-Item -LiteralPath", script)

    def test_custom_root_and_empty_log_diagnostics_are_safe(self) -> None:
        install = self._read_required("install.ps1")
        diagnose = self._read_required("diagnose.ps1")

        self.assertIn("$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)", install)
        self.assertIn("$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)", diagnose)
        self.assertIn("New-Item -ItemType Directory -Path $logDestination -Force", diagnose)

    def test_backfill_uses_china_standard_time_for_its_complete_day_boundary(self) -> None:
        configure = self._read_required("configure.ps1")

        self.assertIn("FindSystemTimeZoneById('China Standard Time')", configure)
        self.assertIn("ConvertTime([DateTimeOffset]::UtcNow, $chinaTimeZone)", configure)
        self.assertNotIn("$backfillEnd = (Get-Date).Date.AddDays(-1)", configure)

    def test_tunnel_readiness_is_checked_after_each_service_startup_path(self) -> None:
        configure = self._read_required("configure.ps1")
        update = self._read_required("update.ps1")
        library = (WINDOWS / "deploy" / "lib.ps1").read_text(encoding="utf-8")

        self.assertIn("function Wait-TunnelReady", library)
        self.assertIn("127.0.0.1:8766", library)
        self.assertIn("Stop-StockServices", configure)
        self.assertIn("Wait-TunnelReady", configure)
        self.assertIn("Wait-TunnelReady", update)
        self.assertIn("update_failed", update)
        self.assertIn("rollback_failed", update)

    def test_update_stages_verified_tools_and_refreshes_service_xml_without_acl_drift(self) -> None:
        update = self._read_required("update.ps1")

        self.assertIn("tools.staging-", update)
        self.assertIn("services.staging-", update)
        self.assertIn("Get-VerifiedTool $manifest.tools.uv", update)
        self.assertIn('"deploy\\services\\{0}.xml.tmpl" -f $name', update)
        self.assertIn("'StockMcpService', 'StockMcpTunnel'", update)
        self.assertIn("Get-Acl -LiteralPath $toolsDestination", update)
        self.assertIn("Set-Acl -LiteralPath $toolsDestination", update)
        self.assertIn("refresh $xml", update)

    def test_diagnostics_redact_every_external_command_and_scan_staging_before_archiving(
        self,
    ) -> None:
        diagnose = self._read_required("diagnose.ps1")

        self.assertIn("Write-RedactedCommandOutput", diagnose)
        self.assertIn("doctor --root $InstallRoot 2>&1 |", diagnose)
        self.assertIn("tunnel doctor --config $tunnelConfig --explain 2>&1 |", diagnose)
        self.assertIn("Assert-DiagnosticStageHasNoSecrets", diagnose)
        self.assertIn("secret-pattern", diagnose)

    def test_readme_requires_manual_hash_verification_before_untrusted_bootstrap_runs(self) -> None:
        readme = self._read_required("README-WINDOWS.md")

        self.assertIn("在解压 ZIP 或执行任何脚本之前", readme)
        self.assertIn("Get-FileHash", readme)
        self.assertIn("未使用 Authenticode 签名", readme)

    def test_update_rollback_refreshes_restored_services_using_the_old_winsw(self) -> None:
        update = self._read_required("update.ps1")

        self.assertIn("$oldWinSw", update)
        self.assertIn("Refresh-WinSWServiceDefinitions $servicesDirectory $oldWinSw", update)
        self.assertLess(
            update.index("Copy-Item -LiteralPath $servicesBackup"),
            update.index("Refresh-WinSWServiceDefinitions $servicesDirectory $oldWinSw"),
        )

    def test_update_marks_tool_replacement_before_removing_current_tools(self) -> None:
        update = self._read_required("update.ps1")

        self.assertIn("$toolsReplacementStarted = $true", update)
        self.assertLess(
            update.index("$toolsReplacementStarted = $true"),
            update.index("Remove-Item -LiteralPath $toolsDestination -Recurse -Force"),
        )
        self.assertIn("if ($toolsReplacementStarted -and $toolsBackup", update)

    def test_diagnostics_redact_and_sentinel_scan_bare_keys_url_userinfo_and_query_secrets(
        self,
    ) -> None:
        diagnose = self._read_required("diagnose.ps1")

        self.assertIn("sk-[A-Za-z0-9_-]{16,}", diagnose)
        self.assertIn("https?://", diagnose)
        self.assertIn("(?:api_key|token|access_token|key)", diagnose)
        self.assertIn("URL userinfo", diagnose)
        self.assertIn("query secret", diagnose)

    def test_install_existing_configuration_waits_for_tunnel_readiness(self) -> None:
        install = self._read_required("install.ps1")

        tunnel_start = install.rindex("Start-Service -Name StockMcpTunnel")
        self.assertIn("Wait-TunnelReady", install[tunnel_start:])

    def test_service_account_acl_grants_follow_winsw_service_registration(self) -> None:
        install = self._read_required("install.ps1")
        config_acl = (
            "Set-PrivateAcl (Join-Path $InstallRoot 'config') -ReadableByApp -ReadableByTunnel"
        )

        self.assertEqual(1, install.count(config_acl))
        self.assertLess(
            install.index("Install-WinSWServices $InstallRoot $winsw"), install.index(config_acl)
        )

    def test_services_use_separate_restricted_accounts_and_preserve_identity_errors(self) -> None:
        install = self._read_required("install.ps1")
        library = (WINDOWS / "deploy" / "lib.ps1").read_text(encoding="utf-8")

        account_lookup = "function Get-StockMcpServiceAccount"
        account_config = "$serviceOutput = & sc.exe config $Name obj= $account"
        self.assertIn(account_lookup, library)
        self.assertIn(account_config, library)
        self.assertIn("Could not configure service identity for $Name.", library)
        self.assertNotIn("NT SERVICE\\$name", install)

    def test_restricted_service_accounts_do_not_auto_refresh_winsw_configuration(self) -> None:
        service = (WINDOWS / "deploy" / "services" / "StockMcpService.xml.tmpl").read_text(
            encoding="utf-8"
        )
        tunnel = (WINDOWS / "deploy" / "services" / "StockMcpTunnel.xml.tmpl").read_text(
            encoding="utf-8"
        )

        self.assertIn("<autoRefresh>false</autoRefresh>", service)
        self.assertIn("<autoRefresh>false</autoRefresh>", tunnel)

    def test_acl_separates_app_and_tunnel_built_in_service_accounts(self) -> None:
        library = (WINDOWS / "deploy" / "lib.ps1").read_text(encoding="utf-8")

        self.assertIn("NT AUTHORITY\\LocalService", library)
        self.assertIn("NT AUTHORITY\\NetworkService", library)
        self.assertNotIn("NT SERVICE\\StockMcp", library)

    def test_update_keeps_an_unconfigured_installation_in_configuration_required_state(
        self,
    ) -> None:
        update = self._read_required("update.ps1")

        self.assertIn("function Invoke-UpdateDoctor", update)
        self.assertIn("$doctorStatus = Invoke-UpdateDoctor $python $InstallRoot", update)
        self.assertIn(
            "if ($doctorStatus -eq 'configuration_required' -or $configurationState -ne 'ready')",
            update,
        )
        self.assertIn("Set-UpdateState 'configuration_required'", update)
        self.assertLess(
            update.index(
                "if ($doctorStatus -eq 'configuration_required' "
                "-or $configurationState -ne 'ready')"
            ),
            update.index("Start-Service -Name StockMcpService"),
        )

    def test_update_does_not_start_services_after_an_interrupted_configuration(self) -> None:
        update = self._read_required("update.ps1")

        self.assertIn("function Get-ConfigurationState", update)
        self.assertIn("$configurationState = Get-ConfigurationState $InstallRoot", update)
        self.assertIn(
            "if ($doctorStatus -eq 'configuration_required' -or $configurationState -ne 'ready')",
            update,
        )
        self.assertLess(
            update.index(
                "if ($doctorStatus -eq 'configuration_required' "
                "-or $configurationState -ne 'ready')"
            ),
            update.index("Start-Service -Name StockMcpService"),
        )

    def test_update_normalizes_a_junction_target_to_one_string_before_rollback(self) -> None:
        update = self._read_required("update.ps1")

        self.assertIn("function Get-CurrentReleaseTarget", update)
        self.assertIn("$targets = @($current.Target)", update)
        self.assertIn("return [string] $targets[0]", update)
        self.assertIn("$oldTarget = Get-CurrentReleaseTarget $InstallRoot", update)

    def test_current_junction_is_replaced_without_recursive_prompt_or_target_deletion(self) -> None:
        library = (WINDOWS / "deploy" / "lib.ps1").read_text(encoding="utf-8")

        self.assertIn("[IO.Directory]::Delete($current)", library)
        self.assertIn("FileAttributes]::ReparsePoint", library)
        self.assertNotIn("Remove-Item -LiteralPath $current", library)

    def test_update_repairs_missing_service_and_restricted_identity_before_refresh(self) -> None:
        update = self._read_required("update.ps1")
        library = (WINDOWS / "deploy" / "lib.ps1").read_text(encoding="utf-8")

        self.assertIn("function Set-StockMcpServiceIdentity", library)
        self.assertIn("& $WinSw install $xml", update)
        self.assertIn("& $WinSw refresh $xml", update)
        self.assertIn("Set-StockMcpServiceIdentity $name", update)

    def test_update_reapplies_restricted_service_acl_after_account_repair(self) -> None:
        update = self._read_required("update.ps1")

        identity_repair = update.index("Refresh-WinSWServiceDefinitions $servicesDirectory $winSw")
        app_data_acl = "Set-PrivateAcl (Join-Path $InstallRoot 'data') -WritableByApp"
        self.assertIn(app_data_acl, update)
        self.assertGreater(update.index(app_data_acl), identity_repair)

    def test_update_waits_for_database_handle_release_before_restore(self) -> None:
        update = self._read_required("update.ps1")

        self.assertIn("function Wait-DatabaseExclusiveAccess", update)
        self.assertIn("[IO.FileShare]::None", update)
        self.assertIn(
            "Wait-DatabaseExclusiveAccess (Join-Path $InstallRoot 'data\\stock-mcp.sqlite3')",
            update,
        )

    def test_update_readiness_uses_python_from_the_installed_release_not_staging(self) -> None:
        update = self._read_required("update.ps1")

        release_move = "Move-Item -LiteralPath $newReleaseStaging -Destination $newRelease"
        installed_python = "$python = Join-Path $newRelease '.venv\\Scripts\\python.exe'"
        readiness = "Wait-LocalReady -PythonExe $python"
        self.assertIn(installed_python, update)
        self.assertLess(update.index(release_move), update.index(installed_python))
        self.assertLess(update.index(installed_python), update.index(readiness))

    def test_update_stops_residual_install_root_processes_before_database_restore(self) -> None:
        update = self._read_required("update.ps1")
        library = (WINDOWS / "deploy" / "lib.ps1").read_text(encoding="utf-8")

        self.assertGreaterEqual(
            update.count("Stop-StockServices -InstallRoot $InstallRoot"), 2
        )
        self.assertIn("[string] $InstallRoot", library)
        self.assertIn("Get-CimInstance Win32_Process", library)
        self.assertIn("ExecutablePath", library)
        self.assertIn("Stop-Process -Id", library)
        self.assertIn("Refusing to stop a process outside the install root", library)

    def test_source_checkout_can_install_without_a_release_zip(self) -> None:
        install = self._read_required("install.ps1")
        source_install = self._read_required("install-from-source.ps1")

        self.assertIn("ParameterSetName = 'Directory'", install)
        self.assertIn("[string] $PackageDirectory", install)
        self.assertIn("$PackageRoot = [IO.Path]::GetFullPath($PackageDirectory)", install)
        self.assertIn("Assert-ReleaseContents $PackageRoot", install)
        self.assertIn("$dirty = Get-GitText @('status', '--porcelain')", source_install)
        self.assertIn("& git -C $SourceRoot @Arguments", source_install)
        self.assertIn("fetch-tools.ps1", source_install)
        self.assertIn("-PackageDirectory $releaseRoot", source_install)
        self.assertNotIn("Compress-Archive", source_install)

    def test_source_checkout_uses_the_rollback_capable_updater_after_first_install(self) -> None:
        update = self._read_required("update.ps1")
        source_install = self._read_required("install-from-source.ps1")

        self.assertIn("ParameterSetName = 'Directory'", update)
        self.assertIn("[string] $PackageDirectory", update)
        self.assertIn("$PackageRoot = [IO.Path]::GetFullPath($PackageDirectory)", update)
        self.assertIn(
            "if (Test-Path -LiteralPath (Join-Path $InstallRoot 'current'))", source_install
        )
        self.assertIn("update.ps1') -PackageDirectory $releaseRoot", source_install)

    def test_source_checkout_uses_its_commit_in_the_installed_release_version(self) -> None:
        source_install = self._read_required("install-from-source.ps1")

        self.assertIn("$releaseVersion =", source_install)
        self.assertIn("+git.", source_install)
        self.assertIn("version = $releaseVersion", source_install)

    def test_source_checkout_must_match_the_trusted_remote_main_commit(self) -> None:
        source_install = self._read_required("install-from-source.ps1")

        self.assertIn("git -C $SourceRoot fetch origin main", source_install)
        self.assertIn("rev-parse', 'origin/main'", source_install)
        self.assertIn("does not match origin/main", source_install)

    def test_windows_service_root_defaults_to_e_drive(self) -> None:
        for name in (
            "install.ps1",
            "configure.ps1",
            "update.ps1",
            "diagnose.ps1",
            "uninstall.ps1",
        ):
            self.assertIn("'E:\\StockMcp'", self._read_required(name))


if __name__ == "__main__":
    unittest.main()
