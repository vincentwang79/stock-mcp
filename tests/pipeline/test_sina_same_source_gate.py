"""Offline RED contract for the 60-session same-source Sina screening gate."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta

from stock_mcp.domain import DailyBar, MarketSnapshot, Security, StrategyVersion
from stock_mcp.pipeline import DailyReviewPipeline

TRADE_DATE = date(2026, 8, 7)
TIMESTAMP = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)


class _Calendar:
    def is_trading_day(self, target: date) -> bool:
        return target == TRADE_DATE


class _Provider:
    def __init__(self, source: str, result: MarketSnapshot | Exception) -> None:
        self.source = source
        self.result = result
        self.has_historical_mirror = True

    def fetch_snapshot(self, _target: date) -> MarketSnapshot:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _Repository:
    def __init__(self) -> None:
        self.runs: list[object] = []

    def get_run(self, *_args: object) -> None:
        return None

    def save_run(self, run: object) -> None:
        self.runs.append(run)


def _strategy() -> StrategyVersion:
    return StrategyVersion(
        version="v0.4-test-active",
        status="active",
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


def _sina_snapshot(prior_sessions: int) -> MarketSnapshot:
    securities = tuple(
        Security(
            symbol=symbol,
            name=f"fixture-{index}",
            exchange="SSE",
            board="MAIN",
            list_date=date(2020, 1, 1),
            industry="recorded",
            is_st=False,
        )
        for index, symbol in enumerate(("600000.SH", "600001.SH"))
    )
    dates = tuple(TRADE_DATE - timedelta(days=index + 1) for index in range(prior_sessions))
    bars = tuple(
        DailyBar(
            symbol=security.symbol,
            trade_date=day,
            open_1e4=100_000,
            high_1e4=102_000,
            low_1e4=99_000,
            close_1e4=101_000,
            pre_close_1e4=100_000,
            volume_shares=1_000_000,
            amount_fen=10_100_000_000,
            source="sina",
            source_timestamp=TIMESTAMP,
        )
        for security in securities
        for day in (*dates, TRADE_DATE)
    )
    return MarketSnapshot(
        trade_date=TRADE_DATE,
        source="sina",
        source_timestamp=TIMESTAMP,
        securities=securities,
        bars=bars,
        advance_ratio_bps=6_000,
        above_ma20_ratio_bps=6_000,
    )


class SinaSameSourceGateContractTest(unittest.TestCase):
    def test_tushare_failure_with_only_59_sina_prior_sessions_is_degraded_no_screen(self) -> None:
        repository = _Repository()
        pipeline = DailyReviewPipeline(
            calendar=_Calendar(),
            primary_provider=_Provider("tushare", RuntimeError("recorded outage")),
            backup_provider=_Provider("sina", _sina_snapshot(prior_sessions=59)),
            repository=repository,
            strategy=_strategy(),
            pipeline_version="pipeline-v0.4",
            expected_main_board_count=2,
            required_prior_sessions=60,
        )

        result = pipeline.run(TRADE_DATE)

        self.assertEqual("degraded_no_screen", result.status)
        self.assertEqual("sina", result.snapshot.source)
        self.assertIsNone(result.review)
        self.assertIn("60", result.error)

    def test_complete_sina_fallback_keeps_tushare_out_of_price_inputs(self) -> None:
        repository = _Repository()
        pipeline = DailyReviewPipeline(
            calendar=_Calendar(),
            primary_provider=_Provider("tushare", RuntimeError("recorded outage")),
            backup_provider=_Provider("sina", _sina_snapshot(prior_sessions=60)),
            repository=repository,
            strategy=_strategy(),
            pipeline_version="pipeline-v0.4",
            expected_main_board_count=2,
            required_prior_sessions=60,
        )

        result = pipeline.run(TRADE_DATE)

        self.assertEqual("ready", result.status)
        self.assertEqual("sina", result.snapshot.source)
        self.assertTrue(all(bar.source == "sina" for bar in result.snapshot.bars))


if __name__ == "__main__":
    unittest.main()
