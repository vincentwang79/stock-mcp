from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SinaBackfillRunnerContractTest(unittest.TestCase):
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
                "recorded_fetches": 2,
                "failed_fetches": 1,
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
