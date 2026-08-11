from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from hashlib import sha256
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from stock_mcp.backfill import BackfillResult
from stock_mcp.cli import doctor, main
from stock_mcp.config import Settings


class ConfigAndCliContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def test_default_http_endpoint_is_loopback_only(self) -> None:
        settings = Settings.load(root=self.root, environ={})

        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 8765)
        self.assertEqual(settings.mcp_path, "/mcp")
        self.assertEqual(settings.timezone, "Asia/Shanghai")

    def test_non_loopback_bind_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Settings.load(root=self.root, environ={"STOCK_MCP_HOST": "0.0.0.0"})

    def test_doctor_reports_configuration_required_without_crashing(self) -> None:
        report = doctor(Settings.load(root=self.root, environ={}))

        self.assertEqual(report.status, "configuration_required")
        self.assertIn("TUSHARE_TOKEN", report.missing)
        self.assertNotIn("TUNNEL_API_KEY", report.missing)

    def test_doctor_output_never_contains_secret_values(self) -> None:
        secrets_dir = self.root / "config"
        secrets_dir.mkdir(parents=True)
        secret_value = "top-secret-value-for-contract-test"
        (secrets_dir / "secrets.env").write_text(
            "\n".join((f"TUSHARE_TOKEN={secret_value}",)),
            encoding="utf-8",
        )
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(["doctor", "--root", str(self.root), "--json"])

        self.assertEqual(exit_code, 0)
        self.assertNotIn(secret_value, output.getvalue())
        self.assertIn('"status": "ready"', output.getvalue())

    def test_host_secret_file_wins_over_ambient_process_secret(self) -> None:
        config = self.root / "config"
        config.mkdir(parents=True)
        (config / "secrets.env").write_text("TUSHARE_TOKEN=file-token\n", encoding="utf-8")

        with patch.dict("stock_mcp.config.os.environ", {"TUSHARE_TOKEN": "stale-token"}):
            settings = Settings.load(root=self.root)

        self.assertEqual("file-token", settings.tushare_token)

    def test_windows_utf8_bom_app_toml_is_accepted(self) -> None:
        config = self.root / "config"
        config.mkdir(parents=True)
        (config / "app.toml").write_bytes(
            b"\xef\xbb\xbf[service]\n"
            b"bind_host = \"127.0.0.1\"\n"
            b"bind_port = 9876\n"
            b"\n[sina]\n"
            b"shadow_enabled = false\n"
        )

        settings = Settings.load(root=self.root, environ={})

        self.assertEqual(9876, settings.port)
        self.assertFalse(settings.sina_shadow_enabled)

    def test_cli_creates_and_verifies_online_database_backup(self) -> None:
        self.assertEqual(0, main(["migrate", "--root", str(self.root)]))
        destination = self.root / "backups" / "stock-mcp-before-update.sqlite3"

        exit_code = main(["backup", "--root", str(self.root), "--destination", str(destination)])

        self.assertEqual(0, exit_code)
        self.assertTrue(destination.is_file())
        self.assertTrue(destination.with_suffix(".sqlite3.sha256").is_file())

    def test_cli_inspects_database_without_python_command_string_quoting(self) -> None:
        self.assertEqual(0, main(["migrate", "--root", str(self.root)]))
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(["inspect-database", "--root", str(self.root)])

        self.assertEqual(0, exit_code)
        report = json.loads(output.getvalue())
        self.assertEqual(11, report["schema"])
        self.assertEqual("ok", report["integrity"])
        self.assertEqual(0, report["tushare_days"])
        self.assertEqual(0, report["tushare_rows"])

    def test_standalone_database_inspector_works_before_source_install(self) -> None:
        self.assertEqual(0, main(["migrate", "--root", str(self.root)]))
        script = Path(__file__).resolve().parents[1] / "scripts" / "database_preflight.py"

        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--database",
                str(self.root / "data" / "stock-mcp.sqlite3"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(11, json.loads(completed.stdout)["schema"])

    def test_cli_runs_a_bounded_resumable_historical_backfill(self) -> None:
        config = self.root / "config"
        config.mkdir(parents=True)
        (config / "secrets.env").write_text("TUSHARE_TOKEN=fixture\n", encoding="utf-8")
        output = StringIO()
        expected = BackfillResult(
            published_dates=(date(2023, 8, 7),),
            incomplete_dates=(date(2023, 8, 8),),
        )

        with (
            patch("stock_mcp.backfill.run_production_backfill", return_value=expected) as run,
            redirect_stdout(output),
        ):
            try:
                exit_code = main(
                    [
                        "backfill",
                        "--root",
                        str(self.root),
                        "--start",
                        "2023-08-07",
                        "--end",
                        "2023-08-08",
                    ]
                )
            except SystemExit:
                self.fail("stock-mcp CLI does not expose the historical backfill command")

        self.assertEqual(exit_code, 2, "an incomplete source day requires a resumable rerun")
        self.assertEqual(run.call_args.args[2:], (date(2023, 8, 7), date(2023, 8, 8)))
        self.assertIn("published=1 incomplete=1", output.getvalue())

    def test_cli_reports_a_safe_reason_for_the_first_backfill_failure(self) -> None:
        config = self.root / "config"
        config.mkdir(parents=True)
        (config / "secrets.env").write_text("TUSHARE_TOKEN=fixture\n", encoding="utf-8")
        output = StringIO()

        def report_first_failure(*_args: object, **kwargs: object) -> BackfillResult:
            callback = kwargs["on_incomplete"]
            kwargs["on_tushare_probe"](date(2023, 8, 7), 4_321)
            callback(date(2023, 8, 7), ValueError("fixture validation failed"))
            return BackfillResult((), (date(2023, 8, 7),))

        with (
            patch("stock_mcp.backfill.run_production_backfill", side_effect=report_first_failure),
            redirect_stdout(output),
        ):
            exit_code = main(
                [
                    "backfill",
                    "--root",
                    str(self.root),
                    "--start",
                    "2023-08-07",
                    "--end",
                    "2023-08-07",
                ]
            )

        self.assertEqual(exit_code, 2)
        expected_fingerprint = sha256(b"fixture").hexdigest()[:12]
        self.assertIn(
            f"Tushare credential length=7 sha256={expected_fingerprint}",
            output.getvalue(),
        )
        self.assertIn("Tushare latest-day probe trade_date=2023-08-07 rows=4321", output.getvalue())
        self.assertIn("trade_date=2023-08-07", output.getvalue())
        self.assertIn("reason=fixture validation failed", output.getvalue())

    def test_project_console_script_points_to_the_real_cli_entrypoint(self) -> None:
        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn('stock-mcp = "stock_mcp.cli:main"', pyproject)

    def test_custom_ca_from_protected_host_file_is_loaded(self) -> None:
        config = self.root / "config"
        config.mkdir(parents=True)
        ca = config / "custom-ca.pem"
        ca.write_text("test-ca", encoding="utf-8")
        (config / "secrets.env").write_text(
            f"STOCK_MCP_CA_FILE={ca}\nTUSHARE_TOKEN=test-token\n",
            encoding="utf-8",
        )

        settings = Settings.load(root=self.root, environ={})

        self.assertEqual(ca, settings.custom_ca_file)


if __name__ == "__main__":
    unittest.main()
