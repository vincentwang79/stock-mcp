from __future__ import annotations

import json
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
from stock_mcp.v3 import v3_proposal_parameters

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
    def test_v3_task_synchronizes_live_evidence_before_fetching_the_snapshot(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "current").mkdir()
            (root / "current" / "a_share_mainboard_code_name.json").write_text(
                json.dumps(
                    {
                        "metadata": {
                            "standard": "fixture",
                            "mode": "retrospective_current_mapping",
                            "as_of": "2026-08-10",
                        },
                        "stocks": [{"code": "600001", "exchange": "SSE", "industry": "银行"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            database = Database(root / "data" / "stock-mcp.sqlite3")
            database.initialize()
            strategy = StrategyVersion(
                version="v0.3-policy-1",
                status="proposed",
                parameters=v3_proposal_parameters(1),
            )
            database.save_strategy_version(strategy)
            database.set_active_strategy_version(strategy.version)
            security = Security("600001.SH", "样本", "SSE", "MAIN", date(2020, 1, 1), "银行", False)
            sessions = tuple(DAY - timedelta(days=60 - index) for index in range(61))
            timestamp = datetime(2026, 8, 7, 16, 30, tzinfo=SHANGHAI)
            snapshot = MarketSnapshot(
                DAY,
                "tushare",
                timestamp,
                (security,),
                tuple(_stable_bar(security.symbol, session, timestamp) for session in sessions),
                5_000,
                5_000,
            )
            events: list[str] = []

            def synchronize(*_args, **_kwargs):
                events.append("sync")
                return {"status": "ready"}

            class OrderedProvider(_Provider):
                def fetch_snapshot(self, trade_date: date) -> MarketSnapshot:
                    events.append("fetch")
                    return super().fetch_snapshot(trade_date)

            outcome = ProductionPostMarketTask(
                Settings(root=root, tushare_token="fixture"),
                database,
                clock=lambda: timestamp,
                context_loader=lambda _day: ((security,), BaoStockTradingCalendar(sessions)),
                provider_loader=lambda _securities: (
                    OrderedProvider(snapshot),
                    OrderedProvider(snapshot),
                ),
                evidence_synchronizer=synchronize,
                minimum_main_board_count=1,
                required_prior_sessions=60,
            )()

            self.assertEqual(["sync", "fetch"], events[:2])
            self.assertIn(outcome.status, {"degraded_observation", "ready"})

    def test_v3_live_observation_excludes_one_suspended_history_without_blocking_market(
        self,
    ) -> None:
        """A legitimate per-security suspension cannot degrade the whole market day."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "current").mkdir()
            (root / "current" / "a_share_mainboard_code_name.json").write_text(
                json.dumps(
                    {
                        "metadata": {
                            "standard": "fixture-industry-v1",
                            "mode": "retrospective_current_mapping",
                            "as_of": "2026-08-10",
                        },
                        "stocks": [
                            {"code": "600001", "exchange": "SSE", "industry": "银行"},
                            {"code": "600002", "exchange": "SSE", "industry": "制造"},
                            {"code": "600003", "exchange": "SSE", "industry": "制造"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            database = Database(root / "data" / "stock-mcp.sqlite3")
            database.initialize()
            strategy = StrategyVersion(
                version="v0.3-policy-1",
                status="proposed",
                parameters=v3_proposal_parameters(1),
            )
            database.save_strategy_version(strategy)
            database.set_active_strategy_version(strategy.version)
            securities = (
                Security("600001.SH", "完整历史", "SSE", "MAIN", date(2020, 1, 1), "银行", False),
                Security("600002.SH", "停牌历史", "SSE", "MAIN", date(2020, 1, 1), "制造", False),
                Security(
                    "600003.SH",
                    "新上市",
                    "SSE",
                    "MAIN",
                    DAY - timedelta(days=10),
                    "制造",
                    False,
                ),
            )
            sessions = tuple(DAY - timedelta(days=60 - index) for index in range(61))
            as_of = datetime(2026, 8, 7, 16, 30, tzinfo=SHANGHAI)
            complete_bars = tuple(
                _stable_bar(securities[0].symbol, session, as_of) for session in sessions
            )
            missing_session = sessions[-10]
            database.save_daily_security_statuses(
                (
                    {
                        "symbol": securities[1].symbol,
                        "trade_date": missing_session,
                        "source": "baostock",
                        "tradestatus": "0",
                        "is_st": False,
                        "source_timestamp": as_of,
                        "batch_sha256": "a" * 64,
                    },
                )
            )
            suspended_bars = tuple(
                _stable_bar(securities[1].symbol, session, as_of)
                for session in sessions
                if session != missing_session
            )
            older_substitute = _stable_bar(
                securities[1].symbol, sessions[0] - timedelta(days=1), as_of
            )
            newly_listed_bar = _stable_bar(securities[2].symbol, DAY, as_of)
            snapshot = MarketSnapshot(
                DAY,
                "tushare",
                as_of,
                securities,
                (*complete_bars, older_substitute, *suspended_bars, newly_listed_bar),
                5_000,
                5_000,
            )
            primary = _Provider(snapshot)
            research_calls: list[date] = []

            outcome = ProductionPostMarketTask(
                Settings(root=root, tushare_token="fixture"),
                database,
                clock=lambda: as_of,
                context_loader=lambda _day: (
                    securities,
                    BaoStockTradingCalendar(sessions),
                ),
                provider_loader=lambda _securities: (primary, _Provider(snapshot)),
                minimum_main_board_count=3,
                required_prior_sessions=60,
                required_observation_sessions=20,
                research_batch=lambda **values: research_calls.append(values["trade_date"]),
                forward_research_start=DAY,
            )()

            self.assertEqual("degraded_observation", outcome.status)
            self.assertIsNotNone(outcome.run)
            self.assertIsNotNone(outcome.run.review)
            self.assertEqual(strategy.version, outcome.run.review.strategy_version)
            self.assertNotIn(
                securities[1].symbol,
                {candidate.symbol for candidate in outcome.run.review.candidates},
            )
            self.assertEqual((), outcome.run.review.candidates)
            self.assertEqual(1, database.count_live_observation_sessions("pipeline-v0.1"))
            self.assertEqual(
                set(security.symbol for security in securities),
                set(database.load_v3_snapshot_features(DAY, source="tushare")),
            )
            self.assertEqual([], research_calls)

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
            research_calls: list[tuple[date, datetime]] = []

            def research_batch(*, trade_date: date, recorded_at: datetime) -> None:
                research_calls.append((trade_date, recorded_at))
                raise ValueError("fixture research evidence unavailable")

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
                research_batch=research_batch,
                forward_research_start=DAY,
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
                research_batch=research_batch,
                forward_research_start=DAY,
            )()

            self.assertEqual("ready", first.status)
            self.assertEqual("ready", second.status)
            self.assertEqual(1, primary.calls)
            self.assertEqual(1, provider_loads)
            self.assertEqual([(DAY, as_of)], research_calls)
            self.assertEqual(
                (DAY,), database.load_expected_trading_days(DAY, DAY, source="tushare")
            )
            self.assertEqual(
                set(security.symbol for security in securities),
                set(database.load_daily_price_limits(DAY, source="tushare")),
            )
            self.assertEqual("published", database.get_daily_review(DAY).status)
            self.assertTrue(tuple((root / "backups").glob("stock-mcp-*.sqlite3")))
            self.assertFalse((root / "state" / "schedule-state.json").exists())
            comparison_strategy = StrategyVersion(
                version="v0.2-proposed",
                status="proposed",
                parameters=strategy.parameters,
            )
            database.save_strategy_version(comparison_strategy)
            comparison = HistoricalReplayService(
                database, DatabaseStrategyRegistry(database)
            ).compare(strategy.version, comparison_strategy.version, DAY, DAY)
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


def _stable_bar(symbol: str, trade_date: date, timestamp: datetime) -> DailyBar:
    return DailyBar(
        symbol,
        trade_date,
        100_000,
        101_000,
        99_000,
        100_000,
        100_000,
        1_000_000,
        10_000_000_000,
        "tushare",
        timestamp,
    )


if __name__ == "__main__":
    unittest.main()
