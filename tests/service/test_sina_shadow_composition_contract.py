"""Offline contract for an atomic, complete Sina shadow snapshot."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from stock_mcp import production
from stock_mcp.domain import DailyBar, MarketSnapshot, Security, ShareCapitalFact
from stock_mcp.providers.sina import FetchEvidence, SpotBatch
from stock_mcp.storage import Database


class SinaShadowCompositionContractTest(unittest.TestCase):
    def test_complete_spot_batch_publishes_one_sina_snapshot_and_success_run(self) -> None:
        task_type = getattr(production, "SinaShadowTask", None)
        self.assertTrue(callable(task_type), "Sina shadow composition is not implemented")
        if not callable(task_type):
            return
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "stock.sqlite3")
            database.initialize()
            target = date(2026, 8, 7)
            prior = tuple(target - timedelta(days=offset) for offset in range(60, 0, -1))
            database.save_expected_trading_days("tushare", (*prior, target))
            security = Security("600001.SH", "样本", "SH", "MAIN", date(2000, 1, 1), "银行", False)
            timestamp = datetime(2026, 8, 7, 8, 40, tzinfo=UTC)
            database.save_daily_bars(
                tuple(_bar(day, source="sina", timestamp=timestamp) for day in prior)
            )
            database.save_market_snapshot(
                MarketSnapshot(
                    target,
                    "tushare",
                    timestamp,
                    (security,),
                    (_bar(target, source="tushare", timestamp=timestamp),),
                    10_000,
                    10_000,
                )
            )
            database.save_share_capital_facts(
                (ShareCapitalFact("600001.SH", prior[0], "sina", 1_000_000, timestamp, "a" * 64),)
            )
            database.save_daily_security_statuses(
                (
                    {
                        "symbol": "600001.SH",
                        "trade_date": target,
                        "source": "baostock",
                        "tradestatus": "1",
                        "is_st": False,
                        "source_timestamp": timestamp,
                        "batch_sha256": "b" * 64,
                    },
                )
            )
            evidence = FetchEvidence(
                "sina",
                "spot_page",
                target.isoformat(),
                None,
                timestamp,
                200,
                100,
                "c" * 64,
                "sina-adapter-v1",
            )
            batch = SpotBatch(
                target,
                (
                    {
                        "symbol": "sh600001",
                        "name": "样本",
                        "trade": "10.10",
                        "settlement": "10.00",
                        "open": "10.00",
                        "high": "10.20",
                        "low": "9.90",
                        "volume": "100000",
                        "amount": "1005000",
                        "ticktime": "15:00:00",
                        "mktcap": "101",
                        "nmc": "10.1",
                        "turnoverratio": "10",
                    },
                ),
                1,
                1,
                (evidence,),
            )

            result = task_type(database, _SpotProvider(batch)).run(target)

            self.assertEqual("success", result["status"])
            self.assertTrue(result["same_source_history_ok"])
            self.assertEqual(10_000, result["status_coverage_bps"])
            snapshot = database.load_market_snapshot(target, source="sina", history_limit=1)
            self.assertEqual(("600001.SH",), tuple(bar.symbol for bar in snapshot.bars))
            self.assertEqual(1_010_000_000, snapshot.bars[0].close_1e4 * 1_000_000 // 100)


class _SpotProvider:
    def __init__(self, batch: SpotBatch) -> None:
        self.batch = batch

    def fetch_pages(self, trade_date: date) -> SpotBatch:
        self.asserted_date = trade_date
        return self.batch


def _bar(day: date, *, source: str, timestamp: datetime) -> DailyBar:
    return DailyBar(
        "600001.SH",
        day,
        100_000,
        102_000,
        99_000,
        101_000,
        100_000,
        100_000,
        100_500_000,
        source,
        timestamp,
    )


if __name__ == "__main__":
    unittest.main()
