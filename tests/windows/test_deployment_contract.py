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

        self.assertIn("[Parameter(Mandatory = $true)][string] $PackageSha256", update)
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

    def test_install_uses_the_release_manifest_and_checksums_not_a_user_version(self) -> None:
        install = self._read_required("install.ps1")

        self.assertIn("Assert-ReleaseContents $PSScriptRoot", install)
        self.assertIn("Get-ReleaseVersion $PSScriptRoot", install)
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


if __name__ == "__main__":
    unittest.main()
