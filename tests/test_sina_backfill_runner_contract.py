from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SinaBackfillRunnerContractTest(unittest.TestCase):
    def test_cli_progress_line_is_immediate_json_and_redacted(self) -> None:
        from stock_mcp import cli

        printer = getattr(cli, "_print_sina_backfill_progress", None)
        self.assertTrue(callable(printer), "backfill CLI must print live stage progress")
        if not callable(printer):
            return
        output = StringIO()
        event = {
            "symbol": "600000.SH",
            "ordinal": 1,
            "total_symbols": 3087,
            "stage": "history_fetch",
            "event": "complete",
            "elapsed_seconds": 0.5,
        }

        with redirect_stdout(output):
            printer(event)

        prefix = "stock-mcp: sina-backfill-stage "
        line = output.getvalue()
        self.assertTrue(line.startswith(prefix))
        self.assertEqual(event, json.loads(line.removeprefix(prefix)))
        self.assertNotIn("payload", line)
        self.assertNotIn("token", line.lower())

    def test_status_script_reports_checkpoint_progress_without_network(self) -> None:
        script = ROOT / "scripts" / "sina_backfill_status.py"
        self.assertTrue(script.is_file(), "the standalone Sina backfill status script must exist")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "stock-mcp.sqlite3"
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "run_id": "sina-backfill-fixture",
                        "symbols": ["000001.SZ", "000002.SZ", "600000.SH"],
                    }
                ),
                encoding="utf-8",
            )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE sina_backfill_checkpoints("
                    "run_id TEXT, symbol TEXT, status TEXT, checkpoint_json TEXT, "
                    "PRIMARY KEY(run_id, symbol))"
                )
                for symbol in ("000001.SZ", "000002.SZ"):
                    connection.execute(
                        "INSERT INTO sina_backfill_checkpoints VALUES (?, ?, 'completed', ?)",
                        ("sina-backfill-fixture", symbol, json.dumps({"symbol": symbol})),
                    )
                connection.execute(
                    "CREATE TABLE provider_fetch_evidence("
                    "fetch_id TEXT PRIMARY KEY, source TEXT, endpoint_kind TEXT, "
                    "request_key TEXT, retrieved_at TEXT, http_status INTEGER, "
                    "status TEXT, error_class TEXT)"
                )
                connection.execute(
                    "INSERT INTO provider_fetch_evidence VALUES "
                    "('fetch-ok', 'sina', 'history', 'sz000001', "
                    "'2026-08-11T08:00:00Z', 200, 'success', NULL)"
                )
                connection.execute(
                    "INSERT INTO provider_fetch_evidence VALUES "
                    "('fetch-failed', 'sina', 'share_capital', 'sz000003', "
                    "'2026-08-11T08:05:00Z', 502, 'failed', 'HTTPError')"
                )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--database",
                    str(database),
                    "--manifest",
                    str(manifest),
                    "--since",
                    "2026-08-11T08:01:00Z",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            {
                "run_id": "sina-backfill-fixture",
                "total_symbols": 3,
                "completed_symbols": 2,
                "pending_symbols": 1,
                "progress_bps": 6666,
                "last_completed_symbol": "000002.SZ",
                "recorded_fetches": 1,
                "failed_fetches": 1,
                "failure_breakdown": [
                    {
                        "endpoint_kind": "share_capital",
                        "http_status": 502,
                        "error_class": "HTTPError",
                        "count": 1,
                        "sample_request_keys": ["sz000003"],
                    }
                ],
                "latest_failure": {
                    "endpoint_kind": "share_capital",
                    "request_key": "sz000003",
                    "retrieved_at": "2026-08-11T08:05:00Z",
                    "http_status": 502,
                    "error_class": "HTTPError",
                },
            },
            json.loads(completed.stdout),
        )

    def test_stage_summary_reports_average_maximum_failure_and_last_event(self) -> None:
        script = ROOT / "scripts" / "sina_backfill_stage_summary.py"
        self.assertTrue(script.is_file(), "a read-only stage summary script must exist")
        if not script.is_file():
            return
        prefix = "stock-mcp: sina-backfill-stage "
        events = (
            {
                "symbol": "000001.SZ",
                "ordinal": 1,
                "total_symbols": 2,
                "stage": "checkpoint",
                "event": "complete",
                "elapsed_seconds": 0.1,
            },
            {
                "symbol": "000002.SZ",
                "ordinal": 2,
                "total_symbols": 2,
                "stage": "checkpoint",
                "event": "complete",
                "elapsed_seconds": 0.3,
            },
            {
                "symbol": "000002.SZ",
                "ordinal": 2,
                "total_symbols": 2,
                "stage": "history_fetch",
                "event": "failed",
                "elapsed_seconds": 60.0,
                "error_class": "SinaProviderError",
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "run.log"
            log.write_text(
                "RUN fixed\n" + "\n".join(prefix + json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, str(script), "--log", str(log)],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(3, report["event_count"])
        self.assertEqual(events[-1], report["last_event"])
        self.assertEqual(
            {
                "stage": "checkpoint",
                "completed": 2,
                "average_seconds": 0.2,
                "maximum_seconds": 0.3,
                "maximum_symbol": "000002.SZ",
            },
            report["stages"][0],
        )
        self.assertEqual(
            [
                {
                    "stage": "history_fetch",
                    "error_class": "SinaProviderError",
                    "count": 1,
                }
            ],
            report["failures"],
        )

    def test_stage_summary_reads_windows_powershell_utf16_logs(self) -> None:
        script = ROOT / "scripts" / "sina_backfill_stage_summary.py"
        event = {
            "symbol": "000060.SZ",
            "ordinal": 36,
            "total_symbols": 3087,
            "stage": "database_write",
            "event": "start",
        }
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "run.log"
            log.write_text(
                "stock-mcp: sina-backfill-stage " + json.dumps(event) + "\n",
                encoding="utf-16",
            )
            completed = subprocess.run(
                [sys.executable, str(script), "--log", str(log)],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(event, json.loads(completed.stdout)["last_event"])

    def test_runner_appends_worker_output_with_explicit_utf8_encoding(self) -> None:
        runner = (ROOT / "deploy" / "windows" / "start-sina-backfill.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("[IO.File]::AppendAllText", runner)
        self.assertIn("[Text.UTF8Encoding]::new($false)", runner)
        self.assertNotIn("Tee-Object -FilePath $log", runner)

    def test_powershell_runner_is_detached_observable_and_single_instance(self) -> None:
        runner = ROOT / "deploy" / "windows" / "start-sina-backfill.ps1"
        self.assertTrue(runner.is_file(), "the durable Sina backfill runner must exist")
        content = runner.read_text(encoding="utf-8")

        self.assertIn("Start-Process powershell.exe", content)
        self.assertIn("[switch] $Worker", content)
        self.assertIn("Global\\StockMcpSinaBackfill", content)
        self.assertIn("latest-run.txt", content)
        self.assertIn("pid.txt", content)
        self.assertIn("run.log", content)
        self.assertIn("exit-code.txt", content)
        self.assertIn("run-metadata.json", content)
        self.assertIn("source_commit", content)
        self.assertIn("installed_source_commit", content)
        self.assertIn("manifest_hash", content)
        self.assertIn("status --porcelain", content)
        self.assertIn("rev-parse origin/main", content)
        self.assertIn("backfill-sina --root $InstallRoot --manifest $Manifest", content)
        self.assertNotIn("TUSHARE_TOKEN", content)
        self.assertNotIn("TUNNEL_API_KEY", content)


if __name__ == "__main__":
    unittest.main()
