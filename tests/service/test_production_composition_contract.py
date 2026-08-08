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
                    "rule_engine_version": 1,
                    "offensive_min_bps": 5_500,
                    "defensive_max_bps": 4_000,
                    "neutral_limit": 2,
                    "offensive_limit": 3,
                    "min_liquidity_amount_fen": 0,
                    "max_consecutive_limit_up_days": 2,
                    "strong_pullback_min_prior_gain_bps": 1_000,
                    "strong_pullback_max_pullback_bps": 800,
                    "volume_breakout_min_volume_ratio_bps": 15_000,
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
                minimum_main_board_count=2,
                required_prior_sessions=2,
                required_observation_sessions=0,
            )()
            second = ProductionPostMarketTask(
                settings,
                database,
                clock=lambda: as_of.replace(hour=17),
                context_loader=context,
                provider_loader=providers,
                minimum_main_board_count=2,
                required_prior_sessions=2,
                required_observation_sessions=0,
            )()

            self.assertEqual("ready", first.status)
            self.assertEqual("ready", second.status)
            self.assertEqual(1, primary.calls)
            self.assertEqual(1, provider_loads)
            self.assertEqual("published", database.get_daily_review(DAY).status)
            self.assertTrue(tuple((root / "backups").glob("stock-mcp-*.sqlite3")))
            self.assertFalse((root / "state" / "schedule-state.json").exists())
            comparison = HistoricalReplayService(
                database, DatabaseStrategyRegistry(database)
            ).compare(strategy.version, strategy.version, DAY, DAY)
            self.assertEqual(1, comparison["days_compared"])
            self.assertEqual(
                comparison["left_candidate_count"], comparison["right_candidate_count"]
            )

    def test_truncated_baostock_universe_cannot_be_promoted_by_complete_prices(self) -> None:
        """A 100%-complete price file is not meaningful against a 1-stock universe."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = Database(root / "data" / "stock-mcp.sqlite3")
            database.initialize()
            as_of = datetime(2026, 8, 7, 16, 30, tzinfo=SHANGHAI)
            security = Security(
                "600000.SH", "截断样本", "SSE", "MAIN", date(2020, 1, 1), "银行", False
            )
            snapshot = MarketSnapshot(
                DAY,
                "tushare",
                as_of,
                (security,),
                (
                    DailyBar(
                        security.symbol,
                        DAY,
                        100_000,
                        106_000,
                        99_000,
                        105_000,
                        100_000,
                        1_000_000,
                        10_000_000_000,
                        "tushare",
                        as_of,
                    ),
                ),
                6_500,
                6_500,
            )
            primary = _Provider(snapshot)

            outcome = ProductionPostMarketTask(
                Settings(root=root, tushare_token="fixture"),
                database,
                clock=lambda: as_of,
                context_loader=lambda _day: ((security,), BaoStockTradingCalendar({DAY})),
                provider_loader=lambda _securities: (primary, _Provider(snapshot)),
                minimum_main_board_count=2,
            )()

            self.assertNotEqual("ready", outcome.status)
            self.assertIn("coverage", outcome.error or "")
            self.assertEqual(0, primary.calls)

    def test_baostock_context_failure_is_retried_then_persisted_at_deadline(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = Database(root / "data" / "stock-mcp.sqlite3")
            database.initialize()
            settings = Settings(root=root, tushare_token="fixture")

            def unavailable(_day: date):
                raise RuntimeError("BaoStock query failed: fixture outage")

            first = ProductionPostMarketTask(
                settings,
                database,
                clock=lambda: datetime(2026, 8, 7, 16, 30, tzinfo=SHANGHAI),
                context_loader=unavailable,
            )()
            final_task = ProductionPostMarketTask(
                settings,
                database,
                clock=lambda: datetime(2026, 8, 7, 18, 1, tzinfo=SHANGHAI),
                context_loader=unavailable,
            )
            final = final_task()

            self.assertEqual("retry_scheduled", first.status)
            self.assertIn("BaoStock query failed", first.error or "")
            self.assertEqual("failed", final.status)
            self.assertIn("BaoStock query failed", final.error or "")
            persisted = final_task.schedule_state.get(DAY)
            self.assertIsNotNone(persisted)
            self.assertEqual("failed", persisted.status)


if __name__ == "__main__":
    unittest.main()
