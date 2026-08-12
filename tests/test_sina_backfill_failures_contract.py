from __future__ import annotations

import csv
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SinaBackfillFailuresContractTest(unittest.TestCase):
    def test_exports_every_pending_symbol_without_the_verify_limit(self) -> None:
        script = ROOT / "scripts" / "sina_backfill_failures.py"
        self.assertTrue(script.is_file(), "the complete failure exporter must exist")
        if not script.is_file():
            return
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "database.sqlite3"
            manifest = root / "manifest.json"
            output = root / "failures.csv"
            symbols = [f"{value:06d}.SZ" for value in range(1, 135)]
            manifest.write_text(
                json.dumps({"run_id": "run-1", "symbols": symbols}), encoding="utf-8"
            )
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    "CREATE TABLE sina_backfill_checkpoints "
                    "(run_id TEXT, symbol TEXT, status TEXT);"
                    "CREATE TABLE provider_fetch_evidence "
                    "(fetch_id TEXT, source TEXT, endpoint_kind TEXT, request_key TEXT, "
                    "retrieved_at TEXT, http_status INTEGER, status TEXT, error_class TEXT);"
                )
                connection.execute(
                    "INSERT INTO sina_backfill_checkpoints VALUES (?,?,?)",
                    ("run-1", symbols[0], "completed"),
                )
                connection.execute(
                    "INSERT INTO provider_fetch_evidence VALUES (?,?,?,?,?,?,?,?)",
                    (
                        "f1",
                        "sina",
                        "share_capital",
                        "sz000002",
                        "2026-08-12T00:00:00+00:00",
                        200,
                        "failed",
                        "SinaNormalizationError",
                    ),
                )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--database",
                    str(database),
                    "--manifest",
                    str(manifest),
                    "--output",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertEqual(133, report["pending_count"])
            with output.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(133, len(rows))
            self.assertEqual("000002.SZ", rows[0]["symbol"])
            self.assertEqual("SinaNormalizationError", rows[0]["error_class"])
            self.assertEqual("000134.SZ", rows[-1]["symbol"])


if __name__ == "__main__":
    unittest.main()
