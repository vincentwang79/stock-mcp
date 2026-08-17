"""Fixed SQLite-to-MCP proof for paired forward research summaries."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from stock_mcp.application import StockMcpApplication
from stock_mcp.cli import main
from stock_mcp.mcp_tools import build_tool_catalog
from stock_mcp.storage import Database


class ResearchForwardReportE2ETest(unittest.TestCase):
    def test_paired_forward_report_is_deterministic_through_public_read_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(0, main(("initialize-research-program", "--root", str(root))))
            database = Database(root / "data" / "stock-mcp.sqlite3")
            observations: list[dict[str, object]] = []
            outcomes: list[dict[str, object]] = []
            for day, rows in (
                ("2026-08-10", (("600001.SH", True, 100), ("600002.SH", False, 0))),
                ("2026-08-11", (("600001.SH", True, 50), ("600002.SH", False, -50))),
            ):
                for symbol, passes, excess in rows:
                    identity = f"{day}|{symbol}"
                    result_hash = hashlib.sha256(identity.encode()).hexdigest()
                    observation = {
                        "hypothesis_id": "no-recent-limit-up-v1",
                        "trade_date": day,
                        "symbol": symbol,
                        "input_hash": hashlib.sha256(f"input|{identity}".encode()).hexdigest(),
                        "result_hash": result_hash,
                        "observation": {
                            "recent_limit_up_days": 0 if passes else 1,
                            "passes_no_recent_limit_up": passes,
                        },
                        "recorded_at": f"{day}T10:00:00+00:00",
                    }
                    observations.append(observation)
                    outcome_payload = {
                        "path": "signal-close-diagnostic",
                        "gross_return_bps": excess + 10,
                        "benchmark_return_bps": 10,
                        "excess_return_bps": excess,
                    }
                    outcomes.append(
                        {
                            "hypothesis_id": "no-recent-limit-up-v1",
                            "signal_date": day,
                            "symbol": symbol,
                            "horizon_sessions": 20,
                            "observation_result_hash": result_hash,
                            "outcome": outcome_payload,
                            "outcome_hash": hashlib.sha256(
                                f"outcome|{identity}|{excess}".encode()
                            ).hexdigest(),
                            "recorded_at": datetime(2026, 8, 16, tzinfo=UTC).isoformat(),
                        }
                    )
            database.save_research_forward_bundle(
                observations=observations,
                outcomes=outcomes,
            )

            application = StockMcpApplication(database, object(), object())
            tool = {item.name: item for item in build_tool_catalog(application)}[
                "get_research_forward_report"
            ]
            first = tool.handler(hypothesis_id="no-recent-limit-up-v1", horizon_sessions=20)
            second = tool.handler(hypothesis_id="no-recent-limit-up-v1", horizon_sessions=20)

            self.assertEqual(first, second)
            self.assertTrue(first["ok"])
            report = first["data"]
            self.assertEqual("paired-cohort", report["analysis_mode"])
            self.assertEqual(2, report["evidence"]["paired_signal_date_count"])
            self.assertEqual(100, report["summary"]["paired_delta_mean_bps"])
            self.assertFalse(report["decision"]["promotion_eligible"])


if __name__ == "__main__":
    unittest.main()
