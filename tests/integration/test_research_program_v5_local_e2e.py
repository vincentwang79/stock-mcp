"""Local, fixed-data end-to-end proof for Research Program v5."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from stock_mcp import research_program
from stock_mcp.application import StockMcpApplication
from stock_mcp.cli import main
from stock_mcp.mcp_tools import build_tool_catalog
from stock_mcp.research_program import (
    evaluate_lifetime_research_statistics,
    extreme_return_abnormal_turnover_facts,
    normalize_tushare_fina_indicator,
)
from stock_mcp.storage import Database


class ResearchProgramV5LocalE2ETest(unittest.TestCase):
    def test_small_sqlite_chain_initializes_observes_and_reads_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(0, main(("initialize-research-program", "--root", str(root))))
            database = Database(root / "data" / "stock-mcp.sqlite3")
            hypotheses = database.list_research_hypotheses()
            self.assertEqual(11, len(hypotheses))
            self.assertEqual(12, database.schema_version())

            timestamp = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)
            march = normalize_tushare_fina_indicator(
                {
                    "ts_code": "600001.SH",
                    "ann_date": "20260320",
                    "end_date": "20251231",
                    "roe": 12.5,
                    "update_flag": "1",
                },
                source_timestamp=timestamp,
            )
            april = normalize_tushare_fina_indicator(
                {
                    "ts_code": "600001.SH",
                    "ann_date": "20260410",
                    "end_date": "20251231",
                    "roe": 11.8,
                    "update_flag": "1",
                },
                source_timestamp=timestamp,
            )
            database.save_point_in_time_fundamentals((march, april))
            self.assertEqual(
                "12.5",
                database.load_point_in_time_fundamentals(
                    symbol="600001.SH", as_of=date(2026, 3, 31)
                )[0]["payload"]["roe"],
            )

            facts = extreme_return_abnormal_turnover_facts(
                current_return_bps=1_200,
                industry_return_bps=300,
                prior_turnover_bps=(100, 200, 300),
                current_turnover_bps=400,
            )
            database.save_research_forward_observation(
                {
                    "hypothesis_id": "no-recent-limit-up-v1",
                    "trade_date": "2026-08-10",
                    "input_hash": "1" * 64,
                    "result_hash": "2" * 64,
                    "observation": facts,
                    "recorded_at": timestamp.isoformat(),
                }
            )
            statistics = evaluate_lifetime_research_statistics(
                manifest_hash="3" * 64,
                baseline=(0, 0, 0, 0),
                challengers={"attention": (10, 20, 10, 20)},
                lifetime_trial_count=1,
                block_sessions=2,
                bootstrap_samples=100,
            )
            self.assertEqual("romano_wolf_stepdown", statistics["stepdown_test"]["method"])
            trial_arms = {
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
            build_trials = getattr(research_program, "v4_discovery_trials_from_diagnostic", None)
            self.assertTrue(callable(build_trials), "v4 attempts need a durable import path")
            for trial in build_trials(
                {
                    "schema": "v4-study-diagnostic-v1",
                    "source_study_id": "v4-study-local",
                    "manifest_hash": "4" * 64,
                    "source_result_hash": "5" * 64,
                    "diagnostic_hash": "6" * 64,
                    "arms": {"v0.3-policy-1": {}, **trial_arms},
                },
                recorded_at=timestamp,
            ):
                database.save_research_trial(trial)

            application = StockMcpApplication(database, object(), object())
            catalog = {tool.name: tool for tool in build_tool_catalog(application)}
            listed = catalog["list_research_hypotheses"].handler()
            detail = catalog["get_research_hypothesis"].handler(
                hypothesis_id="no-recent-limit-up-v1"
            )
            self.assertTrue(listed["ok"])
            self.assertEqual(11, len(listed["data"]["hypotheses"]))
            self.assertEqual(6, listed["data"]["lifetime_trial_count"])
            self.assertEqual(1, len(detail["data"]["forward_observations"]))


if __name__ == "__main__":
    unittest.main()
