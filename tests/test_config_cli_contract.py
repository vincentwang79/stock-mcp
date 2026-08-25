from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from hashlib import sha256
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from stock_mcp.backfill import BackfillResult
from stock_mcp.cli import doctor, main
from stock_mcp.config import Settings, _read_toml


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
            b'bind_host = "127.0.0.1"\n'
            b"bind_port = 9876\n"
            b"\n[sina]\n"
            b"shadow_enabled = false\n"
        )

        settings = Settings.load(root=self.root, environ={})

        self.assertEqual(9876, settings.port)
        self.assertFalse(settings.sina_shadow_enabled)

    def test_legacy_windows_app_toml_repairs_only_unescaped_path_separators(self) -> None:
        config = self.root / "config"
        config.mkdir(parents=True)
        app_toml = config / "app.toml"
        app_toml.write_text(
            r"""[storage]
database = "E:\StockMcp\\data\\stock-mcp.sqlite3"
backup_directory = "E:\StockMcp\\backups"

[secrets]
environment_file = "E:\StockMcp\\config\\secrets.env"
""",
            encoding="utf-8",
        )

        values = _read_toml(app_toml)

        self.assertEqual(
            r"E:\StockMcp\data\stock-mcp.sqlite3",
            values["storage"]["database"],
        )
        self.assertEqual(
            r"E:\StockMcp\config\secrets.env",
            values["secrets"]["environment_file"],
        )

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
        self.assertEqual(14, report["schema"])
        self.assertEqual("ok", report["integrity"])
        self.assertEqual(0, report["tushare_days"])
        self.assertEqual(0, report["tushare_rows"])

    def test_cli_imports_existing_v4_diagnostic_into_lifetime_ledger(self) -> None:
        arms = {
            f"v4-{name}": {"eligibility": {"eligible": False}}
            for name in (
                "breadth-five-day-median",
                "breakout-overextension-cap",
                "no-recent-limit-up",
                "signal-quality-rank",
                "size-bottom-30pct-filter",
                "trend-quality",
            )
        }
        diagnostic = {
            "schema": "v4-study-diagnostic-v1",
            "source_study_id": "v4-study-fixed",
            "manifest_hash": "a" * 64,
            "source_result_hash": "b" * 64,
            "diagnostic_hash": "c" * 64,
            "arms": {"v0.3-policy-1": {}, **arms},
        }
        with patch(
            "stock_mcp.v4_research.derive_v4_study_diagnostics",
            return_value=diagnostic,
        ):
            self.assertEqual(
                0,
                main(
                    [
                        "initialize-research-program",
                        "--root",
                        str(self.root),
                        "--study-id",
                        "v4-study-fixed",
                    ]
                ),
            )
        from stock_mcp.storage import Database

        database = Database(self.root / "data" / "stock-mcp.sqlite3")
        self.assertEqual(11, len(database.list_research_hypotheses()))
        self.assertEqual(6, len(database.list_research_trials()))

    def test_cli_collects_one_research_fact_day_with_an_injected_sdk(self) -> None:
        config = self.root / "config"
        config.mkdir(parents=True)
        (config / "secrets.env").write_text(
            "TUSHARE_TOKEN=fixed-offline-test-token\n", encoding="utf-8"
        )

        class Client:
            def daily_basic(self, **_arguments):
                return [
                    {
                        "ts_code": "600001.SH",
                        "trade_date": "20260814",
                        "pe_ttm": 8.5,
                    }
                ]

            def fina_indicator_vip(self, **_arguments):
                return [
                    {
                        "ts_code": "600001.SH",
                        "ann_date": "20260814",
                        "end_date": "20260630",
                        "roe": 10.5,
                        "update_flag": "1",
                    }
                ]

        output = StringIO()
        module = SimpleNamespace(pro_api=lambda _token: Client())
        with (
            patch.dict(sys.modules, {"tushare": module}),
            redirect_stdout(output),
        ):
            exit_code = main(
                [
                    "collect-research-facts",
                    "--root",
                    str(self.root),
                    "--trade-date",
                    "2026-08-14",
                ]
            )
        self.assertEqual(0, exit_code)
        self.assertNotIn("fixed-offline-test-token", output.getvalue())
        from stock_mcp.storage import Database

        facts = Database(self.root / "data" / "stock-mcp.sqlite3").load_point_in_time_fundamentals(
            symbol="600001.SH", as_of=date(2026, 8, 14)
        )
        self.assertEqual(2, len(facts))

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
        self.assertEqual(14, json.loads(completed.stdout)["schema"])

    def test_cli_writes_a_read_only_v4_study_amendment(self) -> None:
        destination = self.root / "state" / "v4-amendment.json"
        amendment = {
            "schema": "v4-study-amendment-v1",
            "source_study_id": "study-source",
            "amendment_hash": "a" * 64,
            "corrected_outcome_count": 4,
        }
        output = StringIO()

        with (
            patch(
                "stock_mcp.v4_research.derive_v4_study_amendment",
                return_value=amendment,
            ),
            redirect_stdout(output),
        ):
            try:
                exit_code = main(
                    [
                        "derive-v4-study-report",
                        "--root",
                        str(self.root),
                        "--study-id",
                        "study-source",
                        "--destination",
                        str(destination),
                    ]
                )
            except SystemExit:
                self.fail("stock-mcp CLI does not expose the v4 amendment command")

        self.assertEqual(0, exit_code)
        self.assertEqual(amendment, json.loads(destination.read_text(encoding="utf-8")))
        self.assertIn(str(destination), output.getvalue())

    def test_cli_writes_read_only_v4_study_diagnostics(self) -> None:
        destination = self.root / "state" / "v4-diagnostic.json"
        diagnostic = {
            "schema": "v4-study-diagnostic-v1",
            "source_study_id": "study-source",
            "diagnostic_hash": "d" * 64,
            "signal_day_count": 642,
        }
        output = StringIO()

        with (
            patch(
                "stock_mcp.v4_research.derive_v4_study_diagnostics",
                return_value=diagnostic,
            ),
            redirect_stdout(output),
        ):
            try:
                exit_code = main(
                    [
                        "derive-v4-study-diagnostics",
                        "--root",
                        str(self.root),
                        "--study-id",
                        "study-source",
                        "--destination",
                        str(destination),
                    ]
                )
            except SystemExit:
                self.fail("stock-mcp CLI does not expose the v4 diagnostic command")

        self.assertEqual(0, exit_code)
        self.assertEqual(diagnostic, json.loads(destination.read_text(encoding="utf-8")))
        self.assertIn(str(destination), output.getvalue())
        self.assertIn(diagnostic["diagnostic_hash"], output.getvalue())

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

    def test_cli_freezes_explicit_v4_capital_exclusions_in_the_manifest(self) -> None:
        from stock_mcp.storage import Database

        database = Database(self.root / "data" / "stock-mcp.sqlite3")
        database.initialize()
        start = date(2023, 8, 8)
        end = date(2026, 8, 7)
        span = (end - start).days
        sessions = tuple(start + timedelta(days=index * span // 726) for index in range(727))
        symbols = ("600001.SH", "600002.SH", "600003.SH")
        timestamp = "2026-08-10T00:00:00+00:00"
        with database.connect() as connection:
            connection.executemany(
                "INSERT INTO expected_trading_days(source, trade_date) VALUES('tushare', ?)",
                ((session.isoformat(),) for session in sessions),
            )
            connection.executemany(
                "INSERT INTO market_snapshots VALUES(?, 'tushare', ?, 5000, 5000)",
                ((session.isoformat(), timestamp) for session in sessions),
            )
            connection.executemany(
                "INSERT INTO snapshot_securities VALUES(?, 'tushare', ?, ?, 'SSE', "
                "'MAIN', '2000-01-01', 'fixture', 0)",
                (
                    (session.isoformat(), symbol, symbol)
                    for session in sessions
                    for symbol in symbols
                ),
            )
            connection.executemany(
                "INSERT INTO daily_bars VALUES(?, ?, 100000, 101000, 99000, 100000, "
                "100000, 1000, 10000000, 'tushare', ?)",
                (
                    (symbol, session.isoformat(), timestamp)
                    for session in sessions
                    for symbol in symbols
                ),
            )
            connection.executemany(
                "INSERT INTO daily_security_status VALUES(?, ?, 'baostock', '1', 0, ?, ?)",
                (
                    (symbol, session.isoformat(), timestamp, "a" * 64)
                    for session in sessions
                    for symbol in symbols
                ),
            )
            connection.executemany(
                "INSERT INTO share_capital_facts VALUES(?, ?, 'sina', 1000000, ?, ?)",
                ((symbol, start.isoformat(), timestamp, "b" * 64) for symbol in symbols[:2]),
            )
            connection.executemany(
                "INSERT INTO v3_snapshot_features(trade_date,source,symbol,feature_json) "
                "VALUES(?,'tushare',?,?)",
                (
                    (
                        session.isoformat(),
                        symbol,
                        json.dumps({"industry_mapping_sha256": "c" * 64}),
                    )
                    for session in sessions
                    for symbol in symbols
                ),
            )
            lifecycle_end = sessions[300].isoformat()
            connection.execute(
                "DELETE FROM snapshot_securities WHERE source='tushare' "
                "AND symbol='600002.SH' AND trade_date>?",
                (lifecycle_end,),
            )
            connection.execute(
                "DELETE FROM daily_bars WHERE source='tushare' "
                "AND symbol='600002.SH' AND trade_date>?",
                (lifecycle_end,),
            )
            connection.execute(
                "DELETE FROM daily_security_status WHERE source='baostock' "
                "AND symbol='600002.SH' AND trade_date>?",
                (lifecycle_end,),
            )
            connection.execute(
                "DELETE FROM v3_snapshot_features WHERE source='tushare' "
                "AND symbol='600002.SH' AND trade_date>?",
                (lifecycle_end,),
            )

        backfill = {
            "schema": "sina-backfill-manifest-v1",
            "run_id": "fixture-run",
            "symbols": list(symbols),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "adapter_version": "sina-adapter-v1",
        }
        backfill["manifest_hash"] = sha256(
            json.dumps(backfill, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        backfill_path = self.root / "sina-manifest.json"
        backfill_path.write_text(json.dumps(backfill), encoding="utf-8")
        exclusions_path = self.root / "exclusions.json"
        exclusions_path.write_text(
            json.dumps(
                {
                    "schema": "v4-capital-exclusions-v1",
                    "reason": "sina_share_capital_unavailable",
                    "symbols": ["600003.SH"],
                }
            ),
            encoding="utf-8",
        )
        destination = self.root / "v4-manifest.json"
        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(
                [
                    "prepare-v4-study-manifest",
                    "--root",
                    str(self.root),
                    "--sina-backfill-manifest",
                    str(backfill_path),
                    "--capital-exclusions",
                    str(exclusions_path),
                    "--manifest",
                    str(destination),
                ]
            )

        self.assertEqual(0, exit_code, output.getvalue())
        manifest = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(["600003.SH"], manifest["excluded_symbols"])
        self.assertEqual(["600001.SH", "600002.SH"], manifest["included_symbols"])
        self.assertEqual(6666, manifest["capital_coverage_bps"])
        self.assertEqual(backfill["manifest_hash"], manifest["universe_source_manifest_hash"])
        self.assertEqual(
            database.compute_v4_evidence_hashes(
                start=start,
                end=end,
                included_symbols=("600001.SH", "600002.SH"),
            ),
            {
                "prices_hash": manifest["prices_hash"],
                "statuses_hash": manifest["statuses_hash"],
                "share_capital_hash": manifest["share_capital_hash"],
                "industry_mapping_hash": manifest["industry_mapping_hash"],
            },
        )
        self.assertEqual(
            manifest,
            database.get_v4_dataset_manifest(str(manifest["manifest_hash"])),
        )

    def test_cli_builds_v4_status_facts_without_network_access(self) -> None:
        from stock_mcp.storage import Database

        database = Database(self.root / "data" / "stock-mcp.sqlite3")
        database.initialize()
        with database.connect() as connection:
            connection.execute(
                "INSERT INTO market_snapshots VALUES"
                "('2026-08-07','tushare','2026-08-07T08:00:00+00:00',5000,5000)"
            )
            connection.execute(
                "INSERT INTO snapshot_securities VALUES"
                "('2026-08-07','tushare','600001.SH','one','SSE','MAIN',"
                "'2000-01-01','fixture',0)"
            )
            connection.execute(
                "INSERT INTO daily_bars VALUES"
                "('600001.SH','2026-08-07',100000,101000,99000,100000,100000,"
                "1000,10000000,'tushare','2026-08-07T08:00:00+00:00')"
            )
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(
                [
                    "build-v4-status-facts",
                    "--root",
                    str(self.root),
                    "--start",
                    "2026-08-07",
                    "--end",
                    "2026-08-07",
                ]
            )

        self.assertEqual(0, exit_code)
        report = json.loads(output.getvalue())
        self.assertEqual("legacy-baostock-snapshot-status-v1", report["schema"])
        self.assertEqual(1, report["inserted_rows"])

    def test_cli_exposes_complete_baostock_status_backfill_command(self) -> None:
        with self.assertRaisesRegex(ValueError, "calendar"):
            main(
                [
                    "backfill-baostock-statuses",
                    "--root",
                    str(self.root),
                    "--start",
                    "2026-08-07",
                    "--end",
                    "2026-08-07",
                ]
            )

    def test_v4_manifest_failure_reports_each_failed_gate(self) -> None:
        from stock_mcp.storage import Database

        database = Database(self.root / "data" / "stock-mcp.sqlite3")
        database.initialize()
        start = date(2023, 8, 8)
        end = date(2026, 8, 7)
        span = (end - start).days
        sessions = tuple(start + timedelta(days=index * span // 726) for index in range(727))
        database.save_expected_trading_days("tushare", sessions)
        backfill = {
            "schema": "sina-backfill-manifest-v1",
            "run_id": "fixture-run",
            "symbols": ["600001.SH"],
            "start": start.isoformat(),
            "end": end.isoformat(),
            "adapter_version": "sina-adapter-v1",
        }
        backfill["manifest_hash"] = sha256(
            json.dumps(backfill, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        backfill_path = self.root / "sina-manifest.json"
        backfill_path.write_text(json.dumps(backfill), encoding="utf-8")
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(
                [
                    "prepare-v4-study-manifest",
                    "--root",
                    str(self.root),
                    "--sina-backfill-manifest",
                    str(backfill_path),
                ]
            )

        self.assertEqual(2, exit_code)
        self.assertIn("price_rows=0", output.getvalue())
        self.assertIn("status_rows=0", output.getvalue())
        self.assertIn("capital_rows=0", output.getvalue())
        self.assertIn("missing_status_rows=0", output.getvalue())

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
