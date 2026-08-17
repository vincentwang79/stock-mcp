"""Offline SQLite proof for stored-price forward research evidence."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from stock_mcp import research_program
from stock_mcp.application import StockMcpApplication
from stock_mcp.cli import main
from stock_mcp.domain import DailyBar, MarketSnapshot, Security
from stock_mcp.mcp_tools import build_tool_catalog
from stock_mcp.storage import Database


class ResearchForwardHistoryE2ETest(unittest.TestCase):
    def test_stored_prices_generate_atomic_observation_outcomes_and_mcp_readback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(0, main(("initialize-research-program", "--root", str(root))))
            database = Database(root / "data" / "stock-mcp.sqlite3")
            sessions = tuple(date(2025, 1, 2) + timedelta(days=index) for index in range(26))
            database.save_expected_trading_days("tushare", sessions)
            securities = (
                Security("600001.SH", "Alpha", "SSE", "MAIN", date(2000, 1, 1), "A", False),
                Security("600002.SH", "Beta", "SSE", "MAIN", date(2000, 1, 1), "B", False),
            )
            for index, session in enumerate(sessions):
                timestamp = datetime.combine(session, datetime.min.time(), tzinfo=UTC)
                bars = (
                    _bar("600001.SH", session, 100_000 + index * 1_000, timestamp),
                    _bar("600002.SH", session, 200_000 + index * 500, timestamp),
                )
                database.save_market_snapshot(
                    MarketSnapshot(session, "tushare", timestamp, securities, bars, 5_000, 5_000)
                )

            record = getattr(research_program, "record_stored_price_research_bundle", None)
            self.assertTrue(callable(record), "stored history needs an offline coordinator")
            report = record(
                database,
                symbol="600001.SH",
                signal_date=sessions[5],
                through=sessions[-1],
                source="tushare",
                recorded_at=datetime(2025, 2, 28, tzinfo=UTC),
                hypothesis_ids=("overnight-intraday-separation-v1",),
            )
            self.assertEqual({"observations": 1, "outcomes": 3}, report)
            repeated = record(
                database,
                symbol="600001.SH",
                signal_date=sessions[5],
                through=sessions[-1],
                source="tushare",
                recorded_at=datetime(2025, 3, 1, tzinfo=UTC),
                hypothesis_ids=("overnight-intraday-separation-v1",),
            )
            self.assertEqual(report, repeated)
            outcomes = database.list_research_forward_outcomes(
                hypothesis_id="overnight-intraday-separation-v1", symbol="600001.SH"
            )
            self.assertEqual([5, 10, 20], [item["horizon_sessions"] for item in outcomes])
            self.assertTrue(all(item["outcome"]["source"] == "tushare" for item in outcomes))

            application = StockMcpApplication(database, object(), object())
            detail = {tool.name: tool for tool in build_tool_catalog(application)}[
                "get_research_hypothesis"
            ].handler(hypothesis_id="overnight-intraday-separation-v1")
            self.assertTrue(detail["ok"])
            self.assertEqual(1, len(detail["data"]["forward_observations"]))
            self.assertEqual(3, len(detail["data"]["forward_outcomes"]))

            completed = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "stock_mcp.cli",
                    "build-research-forward-evidence",
                    "--root",
                    str(root),
                    "--symbol",
                    "600001.SH",
                    "--trade-date",
                    sessions[5].isoformat(),
                    "--through",
                    sessions[-1].isoformat(),
                    "--hypothesis-id",
                    "overnight-intraday-separation-v1",
                ),
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(
                {"status": "recorded", "observations": 1, "outcomes": 3},
                json.loads(completed.stdout),
            )

            derived = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "stock_mcp.cli",
                    "derive-research-forward-report",
                    "--root",
                    str(root),
                    "--hypothesis-id",
                    "overnight-intraday-separation-v1",
                    "--horizon-sessions",
                    "20",
                ),
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, derived.returncode, derived.stderr)
            forward_report = json.loads(derived.stdout)
            self.assertEqual("research-forward-report-v1", forward_report["schema"])
            self.assertEqual("descriptive-only", forward_report["analysis_mode"])
            self.assertEqual(1, forward_report["evidence"]["mature_observation_count"])
            self.assertFalse(forward_report["decision"]["promotion_eligible"])


def _bar(symbol: str, session: date, close: int, timestamp: datetime) -> DailyBar:
    return DailyBar(
        symbol=symbol,
        trade_date=session,
        open_1e4=close - 100,
        high_1e4=close + 100,
        low_1e4=close - 200,
        close_1e4=close,
        pre_close_1e4=close - 1_000 if symbol == "600001.SH" else close - 500,
        volume_shares=1_000,
        amount_fen=1_000_000,
        source="tushare",
        source_timestamp=timestamp,
    )


if __name__ == "__main__":
    unittest.main()
