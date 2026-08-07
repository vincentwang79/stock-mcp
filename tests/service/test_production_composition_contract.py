from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from stock_mcp.config import Settings
from stock_mcp.domain import DailyBar, MarketSnapshot, Security, StrategyVersion
from stock_mcp.production import ProductionPostMarketTask
from stock_mcp.providers.runtime import BaoStockTradingCalendar
from stock_mcp.replay import HistoricalReplayService
from stock_mcp.storage import Database
from stock_mcp.strategy import DatabaseStrategyRegistry

DAY = date(2026, 8, 7)
SHANGHAI = ZoneInfo("Asia/Shanghai")


class _Provider:
    has_historical_mirror = True

    def __init__(self, snapshot: MarketSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def fetch_snapshot(self, trade_date: date) -> MarketSnapshot:
        self.calls += 1
        return self.snapshot


class ProductionCompositionContractTest(unittest.TestCase):
    def test_real_sqlite_task_publishes_backs_up_and_reuses_terminal_state(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = Database(root / "data" / "stock-mcp.sqlite3")
            database.initialize()
            strategy = StrategyVersion(
                version="v0.1-active",
                status="proposed",
                parameters={
                    "offensive_min_bps": 5_500,
                    "defensive_max_bps": 4_000,
                    "neutral_limit": 2,
                    "offensive_limit": 3,
                },
            )
            database.save_strategy_version(strategy)
            database.set_active_strategy_version(strategy.version)
            securities = tuple(
                Security(
                    f"60000{index}.SH",
                    f"样本{index}",
                    "SSE",
                    "MAIN",
                    date(2020, 1, 1),
                    "银行",
                    False,
                )
                for index in range(2)
            )
            as_of = datetime(2026, 8, 7, 16, 30, tzinfo=SHANGHAI)
            bars = tuple(
                DailyBar(
                    security.symbol,
                    DAY - timedelta(days=offset),
                    100_000,
                    106_000,
                    99_000,
                    105_000 if offset == 0 else 100_000,
                    100_000,
                    2_000_000 if offset == 0 else 500_000,
                    10_000_000_000,
                    "tushare",
                    as_of,
                )
                for security in securities
                for offset in (2, 1, 0)
            )
            snapshot = MarketSnapshot(DAY, "tushare", as_of, securities, bars, 6_500, 6_500)
            primary = _Provider(snapshot)
            backup = _Provider(snapshot)
            provider_loads = 0

            def providers(_securities: tuple[Security, ...]):
                nonlocal provider_loads
                provider_loads += 1
                return primary, backup

            settings = Settings(root=root, tushare_token="fixture")

            def context(_day: date):
                return securities, BaoStockTradingCalendar({DAY})

            first = ProductionPostMarketTask(
                settings,
                database,
                clock=lambda: as_of,
                context_loader=context,
                provider_loader=providers,
            )()
            second = ProductionPostMarketTask(
                settings,
                database,
                clock=lambda: as_of.replace(hour=17),
                context_loader=context,
                provider_loader=providers,
            )()

            self.assertEqual("ready", first.status)
            self.assertEqual("ready", second.status)
            self.assertEqual(1, primary.calls)
            self.assertEqual(1, provider_loads)
            self.assertEqual("published", database.get_daily_review(DAY).status)
            self.assertTrue(tuple((root / "backups").glob("stock-mcp-*.sqlite3")))
            self.assertTrue((root / "state" / "schedule-state.json").is_file())
            comparison = HistoricalReplayService(
                database, DatabaseStrategyRegistry(database)
            ).compare(strategy.version, strategy.version, DAY, DAY)
            self.assertEqual(1, comparison["days_compared"])
            self.assertEqual(
                comparison["left_candidate_count"], comparison["right_candidate_count"]
            )


if __name__ == "__main__":
    unittest.main()
