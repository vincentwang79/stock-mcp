from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from stock_mcp import production
from stock_mcp.cli import main
from stock_mcp.config import Settings
from stock_mcp.domain import DailyBar, MarketSnapshot, Security, StrategyVersion
from stock_mcp.production import ProductionPostMarketTask
from stock_mcp.providers.runtime import BaoStockTradingCalendar
from stock_mcp.storage import Database
from stock_mcp.v3 import v3_proposal_parameters

DAY = date(2026, 8, 24)
SHANGHAI = ZoneInfo("Asia/Shanghai")


class _Provider:
    has_historical_mirror = True

    def __init__(self, snapshot: MarketSnapshot) -> None:
        self.snapshot = snapshot

    def fetch_snapshot(self, _trade_date: date) -> MarketSnapshot:
        return self.snapshot


class HistoricalObservationBootstrapContractTest(unittest.TestCase):
    def test_verified_recent_history_reduces_live_observation_gate_to_three_sessions(self) -> None:
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
            security = Security(
                "600001.SH", "完整历史", "SSE", "MAIN", date(2020, 1, 1), "银行", False
            )
            sessions = tuple(DAY - timedelta(days=60 - index) for index in range(61))
            as_of = datetime(2026, 8, 24, 16, 30, tzinfo=SHANGHAI)
            snapshot = MarketSnapshot(
                DAY,
                "tushare",
                as_of,
                (security,),
                tuple(_bar(security.symbol, session, as_of) for session in sessions),
                5_000,
                5_000,
            )
            database.count_live_observation_sessions = (  # type: ignore[method-assign]
                lambda *_args, **_kwargs: 3
            )
            database.count_recent_historical_observation_sessions = (  # type: ignore[attr-defined]
                lambda *_args, **_kwargs: 20
            )

            outcome = ProductionPostMarketTask(
                Settings(root=root, tushare_token="fixture"),
                database,
                clock=lambda: as_of,
                context_loader=lambda _day: ((security,), BaoStockTradingCalendar(sessions)),
                provider_loader=lambda _securities: (_Provider(snapshot), _Provider(snapshot)),
                minimum_main_board_count=1,
                required_prior_sessions=60,
                required_observation_sessions=20,
            )()

            self.assertEqual("ready", outcome.status)

    def test_historical_simulation_evidence_is_immutable_and_counted_separately(self) -> None:
        with TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "stock-mcp.sqlite3")
            database.initialize()
            recorder = getattr(database, "record_historical_observation_bootstrap", None)
            self.assertTrue(
                callable(recorder),
                "database must persist immutable historical observation bootstrap evidence",
            )
            if not callable(recorder):
                return
            days = tuple(
                {
                    "trade_date": (date(2026, 7, 27) + timedelta(days=index)).isoformat(),
                    "input_hash": f"{index:064x}",
                    "result_hash": f"{index + 20:064x}",
                    "candidate_count": index % 4,
                    "market_regime": "neutral",
                    "source_timestamp": "2026-08-25T00:00:00+00:00",
                }
                for index in range(20)
            )
            first = recorder(
                pipeline_version="pipeline-v0.1",
                strategy_version="v0.3-policy-1",
                source="tushare",
                policy_version="historical-production-simulation-v1",
                days=days,
                recorded_at="2026-08-25T00:00:00+00:00",
            )
            repeated = recorder(
                pipeline_version="pipeline-v0.1",
                strategy_version="v0.3-policy-1",
                source="tushare",
                policy_version="historical-production-simulation-v1",
                days=days,
                recorded_at="2026-08-25T00:00:00+00:00",
            )

            self.assertEqual(first, repeated)
            self.assertEqual(20, first["session_count"])
            self.assertEqual(
                20,
                database.count_recent_historical_observation_sessions(
                    "pipeline-v0.1", "v0.3-policy-1", anchor_date=DAY
                ),
            )
            changed = (
                *days[:-1],
                {**days[-1], "candidate_count": (int(days[-1]["candidate_count"]) + 1) % 4},
            )
            with self.assertRaisesRegex(ValueError, "immutable"):
                recorder(
                    pipeline_version="pipeline-v0.1",
                    strategy_version="v0.3-policy-1",
                    source="tushare",
                    policy_version="historical-production-simulation-v1",
                    days=changed,
                    recorded_at="2026-08-25T00:00:00+00:00",
                )

    def test_historical_simulation_reuses_recorded_v3_inputs_without_publishing_reviews(
        self,
    ) -> None:
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
                        "stocks": [{"code": "600001", "exchange": "SSE", "industry": "银行"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            database = Database(root / "data" / "stock-mcp.sqlite3")
            database.initialize()
            security = Security(
                "600001.SH", "完整历史", "SSE", "MAIN", date(2020, 1, 1), "银行", False
            )
            timestamp = datetime(2026, 8, 25, 16, 30, tzinfo=SHANGHAI)
            sessions = tuple(date(2026, 1, 1) + timedelta(days=index) for index in range(80))
            database.save_expected_trading_days("tushare", sessions)
            for index, session in enumerate(sessions):
                close = 100_000 + index
                database.save_market_snapshot(
                    MarketSnapshot(
                        session,
                        "tushare",
                        timestamp,
                        (security,),
                        (
                            DailyBar(
                                security.symbol,
                                session,
                                close,
                                close,
                                close,
                                close,
                                close - 1,
                                1_000_000,
                                10_000_000_000,
                                "tushare",
                                timestamp,
                            ),
                        ),
                        5_000,
                        5_000,
                    )
                )
            strategy = StrategyVersion(
                version="v0.3-policy-1",
                status="active",
                parameters=v3_proposal_parameters(1),
            )
            runner = getattr(production, "bootstrap_historical_v3_observations", None)
            self.assertTrue(
                callable(runner),
                "historical production simulation must be available to operators",
            )
            if not callable(runner):
                return

            report = runner(
                database=database,
                root=root,
                strategy=strategy,
                start=sessions[60],
                end=sessions[79],
                recorded_at=timestamp,
            )

            self.assertEqual("historical_simulation_not_live", report["evidence_class"])
            self.assertEqual(20, report["session_count"])
            self.assertEqual(
                20,
                database.count_recent_historical_observation_sessions(
                    "pipeline-v0.1", strategy.version, anchor_date=date(2026, 3, 22)
                ),
            )
            with database.connect() as connection:
                pipeline_runs = connection.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[
                    0
                ]
                reviews = connection.execute("SELECT COUNT(*) FROM daily_reviews").fetchone()[0]
                self.assertEqual(0, pipeline_runs)
                self.assertEqual(0, reviews)

    def test_operator_cli_exposes_historical_simulation_command(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "active strategy"):
                main(
                    (
                        "bootstrap-live-observation",
                        "--root",
                        str(root),
                        "--start",
                        "2026-07-27",
                        "--end",
                        "2026-08-21",
                    )
                )


def _bar(symbol: str, trade_date: date, timestamp: datetime) -> DailyBar:
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
