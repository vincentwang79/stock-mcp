from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LiveObservationStatusContractTest(unittest.TestCase):
    def test_reports_latest_post_cutoff_observation_and_acceptance_checks(self) -> None:
        script = ROOT / "scripts" / "live_observation_status.py"
        self.assertTrue(script.is_file(), "the read-only live observation status script must exist")
        if not script.is_file():
            return
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "stock-mcp.sqlite3"
            self._create_fixture(database)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--database",
                    str(database),
                    "--after",
                    "2026-08-21",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual("2026-08-24", report["trade_date"])
        self.assertEqual("v0.3-policy-1", report["active_strategy"])
        self.assertEqual("degraded_observation", report["schedule"]["status"])
        self.assertEqual("observation", report["daily_review"]["status"])
        self.assertEqual(2, report["candidate_count"])
        self.assertEqual(3, report["target_bar_count"])
        self.assertEqual(3, report["price_limit_count"])
        self.assertEqual(3, report["v3_feature_count"])
        self.assertEqual(1, report["live_observation_sessions"])
        self.assertEqual(20, report["historical_simulation_sessions"])
        self.assertEqual(3, report["required_live_observation_sessions"])
        self.assertEqual(0, report["forward_observation_count"])
        self.assertEqual("pass", report["validation"]["status"])
        self.assertEqual([], report["validation"]["failures"])

    @staticmethod
    def _create_fixture(database: Path) -> None:
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                CREATE TABLE active_strategy(singleton INTEGER PRIMARY KEY, version TEXT);
                INSERT INTO active_strategy VALUES(1, 'v0.3-policy-1');
                CREATE TABLE schedule_outcomes(
                    trade_date TEXT PRIMARY KEY, status TEXT, next_at TEXT,
                    pipeline_version TEXT, error TEXT
                );
                INSERT INTO schedule_outcomes VALUES(
                    '2026-08-24', 'degraded_observation', NULL, 'pipeline-v0.1',
                    'live observation period is not yet complete'
                );
                CREATE TABLE pipeline_runs(
                    trade_date TEXT, pipeline_version TEXT, status TEXT, attempts INTEGER,
                    strategy_version TEXT, error TEXT,
                    PRIMARY KEY(trade_date, pipeline_version)
                );
                INSERT INTO pipeline_runs VALUES(
                    '2026-08-24', 'pipeline-v0.1', 'degraded_observation', 1,
                    'v0.3-policy-1', 'live observation period is not yet complete'
                );
                CREATE TABLE daily_reviews(
                    trade_date TEXT, strategy_version TEXT, status TEXT, source TEXT,
                    source_timestamp TEXT, market_regime TEXT,
                    PRIMARY KEY(trade_date, strategy_version)
                );
                INSERT INTO daily_reviews VALUES(
                    '2026-08-24', 'v0.3-policy-1', 'observation', 'tushare',
                    '2026-08-24T09:00:00+00:00', 'neutral'
                );
                CREATE TABLE candidates(trade_date TEXT, strategy_version TEXT);
                INSERT INTO candidates VALUES('2026-08-24', 'v0.3-policy-1');
                INSERT INTO candidates VALUES('2026-08-24', 'v0.3-policy-1');
                CREATE TABLE daily_bars(trade_date TEXT, source TEXT);
                CREATE TABLE daily_price_limits(trade_date TEXT, source TEXT);
                CREATE TABLE v3_snapshot_features(trade_date TEXT, source TEXT);
                CREATE TABLE research_forward_observations(observation_id TEXT);
                CREATE TABLE historical_observation_bootstrap_runs(
                    bootstrap_id TEXT PRIMARY KEY,
                    pipeline_version TEXT,
                    strategy_version TEXT,
                    source TEXT,
                    policy_version TEXT,
                    start_date TEXT,
                    end_date TEXT,
                    session_count INTEGER,
                    manifest_hash TEXT,
                    manifest_json TEXT,
                    recorded_at TEXT
                );
                INSERT INTO historical_observation_bootstrap_runs VALUES(
                    'fixture-bootstrap', 'pipeline-v0.1', 'v0.3-policy-1', 'tushare',
                    'historical-production-simulation-v1', '2026-07-27', '2026-08-21', 20,
                    'fixture', '{}', '2026-08-24T09:00:00+00:00'
                );
                """
            )
            for table in ("daily_bars", "daily_price_limits", "v3_snapshot_features"):
                connection.executemany(
                    f"INSERT INTO {table} VALUES('2026-08-24', 'tushare')",
                    ((), (), ()),
                )


if __name__ == "__main__":
    unittest.main()
