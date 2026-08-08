import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / "deploy" / "windows"


class WindowsDeploymentContractTest(unittest.TestCase):
    def _read_required(self, name: str) -> str:
        path = WINDOWS / name
        self.assertTrue(path.is_file(), f"missing deployment artifact: {path}")
        return path.read_text(encoding="utf-8")

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

    def test_tool_fetch_uses_tls12_and_retries_transient_https_failures(self) -> None:
        fetch = self._read_required("fetch-tools.ps1")

        self.assertIn("SecurityProtocolType]::Tls12", fetch)
        self.assertIn("function Invoke-ToolDownload", fetch)
        self.assertIn("$attempt -le 3", fetch)
        self.assertIn("-TimeoutSec 120", fetch)
        self.assertIn("Failed to download", fetch)

    def test_tool_fetch_accepts_a_direct_executable_without_archive_member(self) -> None:
        fetch = self._read_required("fetch-tools.ps1")

        self.assertIn("PSObject.Properties['archive_member']", fetch)
        self.assertIn("IsNullOrWhiteSpace($archiveMember)", fetch)

    def test_install_uses_isolated_python_and_two_services(self) -> None:
        install = self._read_required("install.ps1")

        self.assertIn("UV_PYTHON_INSTALL_DIR", install)
        self.assertIn("uv sync --locked --no-dev --no-editable", install)
        self.assertIn("StockMcpService", install)
        self.assertIn("StockMcpTunnel", install)
        self.assertIn("NT SERVICE\\$name", install)
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

    def test_windows_scripts_use_the_supported_root_cli_and_healthz(self) -> None:
        configure = self._read_required("configure.ps1")
        update = self._read_required("update.ps1")
        library = (WINDOWS / "deploy" / "lib.ps1").read_text(encoding="utf-8")

        self.assertIn("doctor --root $InstallRoot", configure)
        self.assertIn("migrate --root $InstallRoot", update)
        self.assertNotIn("stock_mcp.cli doctor --config", configure + update)
        self.assertIn("127.0.0.1:8765/readyz", library)

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
