from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "providers" / "fixtures" / "sina"


class SinaStageProfileContractTest(unittest.TestCase):
    def test_offline_profile_reports_each_stage_without_touching_production_database(self) -> None:
        script = ROOT / "scripts" / "sina_stage_profile.py"
        self.assertTrue(script.is_file(), "the standalone Sina stage profiler must exist")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "host-root"
            output = Path(directory) / "profile.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--root",
                    str(root),
                    "--symbol",
                    "000001.SZ",
                    "--start",
                    "2024-04-02",
                    "--end",
                    "2024-04-02",
                    "--history-fixture",
                    str(FIXTURES / "recorded_klc_kl.js"),
                    "--capital-fixture",
                    str(FIXTURES / "recorded_share_capital.js"),
                    "--output",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertEqual(report, json.loads(output.read_text(encoding="utf-8")))
            self.assertFalse((root / "data" / "stock-mcp.sqlite3").exists())

        self.assertEqual("ok", report["status"])
        self.assertEqual("000001.SZ", report["symbol"])
        self.assertEqual(1, report["history"]["decoded_rows"])
        self.assertEqual(1, report["history"]["selected_rows"])
        self.assertEqual(1, report["history"]["normalized_bars"])
        self.assertEqual(2, report["share_capital"]["decoded_rows"])
        self.assertEqual(2, report["share_capital"]["normalized_facts"])
        self.assertEqual(1, report["sqlite"]["daily_bar_rows"])
        self.assertEqual(2, report["sqlite"]["share_capital_rows"])
        expected_stages = {
            "history_rate_limit_wait",
            "history_http",
            "history_decode",
            "history_window",
            "history_normalize",
            "capital_rate_limit_wait",
            "capital_http",
            "capital_decode",
            "capital_normalize",
            "sqlite_initialize",
            "sqlite_atomic_write",
            "total",
        }
        self.assertEqual(expected_stages, set(report["wall_seconds"]))
        self.assertEqual(expected_stages, set(report["cpu_seconds"]))
        self.assertTrue(all(value >= 0 for value in report["wall_seconds"].values()))
        self.assertTrue(all(value >= 0 for value in report["cpu_seconds"].values()))
        self.assertEqual(64, len(report["history"]["payload_sha256"]))
        self.assertEqual(64, len(report["share_capital"]["payload_sha256"]))


if __name__ == "__main__":
    unittest.main()
