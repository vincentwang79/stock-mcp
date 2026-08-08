from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, date, datetime

from stock_mcp.domain import DailyBar, MarketSnapshot, Security, StrategyVersion
from stock_mcp.pipeline import DailyReviewPipeline

TRADE_DATE = date(2026, 8, 7)
PIPELINE_VERSION = "pipeline-v0.1"
TIMESTAMP = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)


class FakeCalendar:
    def __init__(self, trading_days: set[date]) -> None:
        self.trading_days = trading_days

    def is_trading_day(self, trade_date: date) -> bool:
        return trade_date in self.trading_days


class FakeProvider:
    """In-memory implementation of the provider protocol used by the pipeline."""

    def __init__(
        self,
        *,
        source: str,
        outcomes: list[MarketSnapshot | Exception],
        has_historical_mirror: bool = True,
    ) -> None:
        self.source = source
        self.outcomes = outcomes
        self.has_historical_mirror = has_historical_mirror
        self.fetch_calls = 0

    def fetch_snapshot(self, trade_date: date) -> MarketSnapshot:
        self.fetch_calls += 1
        outcome = self.outcomes[min(self.fetch_calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeRepository:
    """In-memory implementation of the repository protocol used by the pipeline."""

    def __init__(self) -> None:
        self.runs: dict[tuple[date, str], object] = {}
        self.save_calls = 0

    def get_run(self, trade_date: date, pipeline_version: str) -> object | None:
        return self.runs.get((trade_date, pipeline_version))

    def save_run(self, run: object) -> None:
        self.save_calls += 1
        self.runs[(run.trade_date, run.pipeline_version)] = run


def _strategy() -> StrategyVersion:
    return StrategyVersion(
        version="strategy-v0.1",
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


def _snapshot(
    *,
    source: str,
    trade_date: date = TRADE_DATE,
    breadth_bps: int = 6_000,
) -> MarketSnapshot:
    securities = tuple(
        Security(
            symbol=f"60000{index}.SH",
            name=f"样本{index}",
            exchange="SSE",
            board="MAIN",
            list_date=date(2020, 1, 1),
            industry="测试行业",
            is_st=False,
        )
        for index in range(2)
    )
    bars = tuple(
        DailyBar(
            symbol=security.symbol,
            trade_date=trade_date,
            open_1e4=100_000 + index * 1_000,
            high_1e4=108_000 + index * 1_000,
            low_1e4=99_000 + index * 1_000,
            close_1e4=106_000 + index * 1_000,
            pre_close_1e4=100_000 + index * 1_000,
            volume_shares=1_000_000,
            amount_fen=12_000_000_000,
            source=source,
            source_timestamp=TIMESTAMP,
        )
        for index, security in enumerate(securities)
    )
    return MarketSnapshot(
        trade_date=trade_date,
        source=source,
        source_timestamp=TIMESTAMP,
        securities=securities,
        bars=bars,
        advance_ratio_bps=breadth_bps,
        above_ma20_ratio_bps=breadth_bps,
    )


def _pipeline(
    *,
    calendar: FakeCalendar,
    primary: FakeProvider,
    backup: FakeProvider,
    repository: FakeRepository,
    max_attempts: int = 3,
) -> DailyReviewPipeline:
    return DailyReviewPipeline(
        calendar=calendar,
        primary_provider=primary,
        backup_provider=backup,
        repository=repository,
        strategy=_strategy(),
        pipeline_version=PIPELINE_VERSION,
        expected_main_board_count=2,
        required_prior_sessions=0,
        max_attempts=max_attempts,
    )


class DailyReviewPipelineContractTest(unittest.TestCase):
    def test_non_trading_day_is_skipped_without_fetching_or_persisting(self) -> None:
        primary = FakeProvider(source="primary", outcomes=[_snapshot(source="primary")])
        backup = FakeProvider(source="backup", outcomes=[_snapshot(source="backup")])
        repository = FakeRepository()

        result = _pipeline(
            calendar=FakeCalendar(set()),
            primary=primary,
            backup=backup,
            repository=repository,
        ).run(TRADE_DATE)

        self.assertEqual(result.status, "skipped")
        self.assertIsNone(result.review)
        self.assertEqual(primary.fetch_calls, 0)
        self.assertEqual(backup.fetch_calls, 0)
        self.assertEqual(repository.save_calls, 0)

    def test_complete_primary_snapshot_is_published_ready(self) -> None:
        primary_snapshot = _snapshot(source="primary")
        primary = FakeProvider(source="primary", outcomes=[primary_snapshot])
        backup = FakeProvider(source="backup", outcomes=[_snapshot(source="backup")])
        repository = FakeRepository()

        result = _pipeline(
            calendar=FakeCalendar({TRADE_DATE}),
            primary=primary,
            backup=backup,
            repository=repository,
        ).run(TRADE_DATE)

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.snapshot, primary_snapshot)
        self.assertEqual(result.review.source, "primary")
        self.assertEqual(backup.fetch_calls, 0)
        self.assertEqual(repository.save_calls, 1)

    def test_invalid_primary_is_replaced_by_one_complete_backup_snapshot_not_merged(self) -> None:
        primary_snapshot = _snapshot(source="primary")
        incomplete_primary = replace(primary_snapshot, bars=primary_snapshot.bars[:1])
        backup_snapshot = _snapshot(source="backup")
        primary = FakeProvider(source="primary", outcomes=[incomplete_primary])
        backup = FakeProvider(source="backup", outcomes=[backup_snapshot])
        repository = FakeRepository()

        result = _pipeline(
            calendar=FakeCalendar({TRADE_DATE}),
            primary=primary,
            backup=backup,
            repository=repository,
        ).run(TRADE_DATE)

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.snapshot, backup_snapshot)
        self.assertTrue(all(bar.source == "backup" for bar in result.snapshot.bars))
        self.assertEqual(result.review.source, "backup")

    def test_backup_without_same_source_history_is_degraded_without_screening(self) -> None:
        primary = FakeProvider(source="primary", outcomes=[RuntimeError("primary unavailable")])
        backup = FakeProvider(
            source="backup",
            outcomes=[_snapshot(source="backup")],
            has_historical_mirror=False,
        )
        repository = FakeRepository()

        result = _pipeline(
            calendar=FakeCalendar({TRADE_DATE}),
            primary=primary,
            backup=backup,
            repository=repository,
        ).run(TRADE_DATE)

        self.assertEqual(result.status, "degraded_no_screen")
        self.assertEqual(result.snapshot.source, "backup")
        self.assertIsNone(result.review)
        self.assertEqual(repository.save_calls, 1)

    def test_missing_main_board_coverage_rejects_primary_before_publication(self) -> None:
        primary_snapshot = _snapshot(source="primary")
        primary = FakeProvider(
            source="primary", outcomes=[replace(primary_snapshot, bars=primary_snapshot.bars[:1])]
        )
        backup = FakeProvider(source="backup", outcomes=[_snapshot(source="backup")])

        result = _pipeline(
            calendar=FakeCalendar({TRADE_DATE}),
            primary=primary,
            backup=backup,
            repository=FakeRepository(),
        ).run(TRADE_DATE)

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.snapshot.source, "backup")

    def test_metadata_security_without_a_target_day_bar_is_treated_as_suspended(self) -> None:
        primary_snapshot = _snapshot(source="primary")
        suspended = Security(
            symbol="600099.SH",
            name="停牌样本",
            exchange="SSE",
            board="MAIN",
            list_date=date(2020, 1, 1),
            industry="测试行业",
            is_st=False,
        )
        primary_snapshot = replace(
            primary_snapshot,
            securities=(*primary_snapshot.securities, suspended),
        )
        primary = FakeProvider(source="primary", outcomes=[primary_snapshot])
        backup = FakeProvider(source="backup", outcomes=[_snapshot(source="backup")])

        result = _pipeline(
            calendar=FakeCalendar({TRADE_DATE}),
            primary=primary,
            backup=backup,
            repository=FakeRepository(),
        ).run(TRADE_DATE)

        self.assertEqual("ready", result.status)
        self.assertEqual("primary", result.snapshot.source)
        self.assertEqual(0, backup.fetch_calls)

    def test_invalid_ohlc_rejects_primary_before_publication(self) -> None:
        primary_snapshot = _snapshot(source="primary")
        invalid_bar = replace(primary_snapshot.bars[0], high_1e4=98_000)
        primary = FakeProvider(
            source="primary",
            outcomes=[replace(primary_snapshot, bars=(invalid_bar, *primary_snapshot.bars[1:]))],
        )
        backup = FakeProvider(source="backup", outcomes=[_snapshot(source="backup")])

        result = _pipeline(
            calendar=FakeCalendar({TRADE_DATE}),
            primary=primary,
            backup=backup,
            repository=FakeRepository(),
        ).run(TRADE_DATE)

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.snapshot.source, "backup")

    def test_wrong_trade_date_rejects_primary_before_publication(self) -> None:
        primary = FakeProvider(
            source="primary",
            outcomes=[_snapshot(source="primary", trade_date=date(2026, 8, 6))],
        )
        backup = FakeProvider(source="backup", outcomes=[_snapshot(source="backup")])

        result = _pipeline(
            calendar=FakeCalendar({TRADE_DATE}),
            primary=primary,
            backup=backup,
            repository=FakeRepository(),
        ).run(TRADE_DATE)

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.snapshot.source, "backup")

    def test_repeat_run_reuses_the_persisted_trade_date_and_pipeline_version(self) -> None:
        primary = FakeProvider(source="primary", outcomes=[_snapshot(source="primary")])
        backup = FakeProvider(source="backup", outcomes=[_snapshot(source="backup")])
        repository = FakeRepository()
        pipeline = _pipeline(
            calendar=FakeCalendar({TRADE_DATE}),
            primary=primary,
            backup=backup,
            repository=repository,
        )

        first = pipeline.run(TRADE_DATE)
        second = pipeline.run(TRADE_DATE)

        self.assertEqual(first, second)
        self.assertEqual(primary.fetch_calls, 1)
        self.assertEqual(repository.save_calls, 1)
        self.assertIn((TRADE_DATE, PIPELINE_VERSION), repository.runs)

    def test_failed_run_is_retryable_but_a_published_run_is_immutable(self) -> None:
        primary = FakeProvider(
            source="primary",
            outcomes=[RuntimeError("temporarily unavailable"), _snapshot(source="primary")],
        )
        backup = FakeProvider(source="backup", outcomes=[RuntimeError("unavailable")])
        repository = FakeRepository()
        pipeline = _pipeline(
            calendar=FakeCalendar({TRADE_DATE}),
            primary=primary,
            backup=backup,
            repository=repository,
            max_attempts=1,
        )

        first = pipeline.run(TRADE_DATE)
        second = pipeline.run(TRADE_DATE)
        third = pipeline.run(TRADE_DATE)

        self.assertEqual("failed", first.status)
        self.assertEqual("ready", second.status)
        self.assertEqual(second, third)
        self.assertEqual(2, primary.fetch_calls)

    def test_same_source_history_is_allowed_but_future_bars_are_rejected(self) -> None:
        snapshot = _snapshot(source="primary")
        historical = replace(snapshot.bars[0], trade_date=date(2026, 8, 6))
        with_history = replace(snapshot, bars=(historical, *snapshot.bars))
        primary = FakeProvider(source="primary", outcomes=[with_history])
        backup = FakeProvider(source="backup", outcomes=[_snapshot(source="backup")])

        result = _pipeline(
            calendar=FakeCalendar({TRADE_DATE}),
            primary=primary,
            backup=backup,
            repository=FakeRepository(),
        ).run(TRADE_DATE)

        self.assertEqual("ready", result.status)
        self.assertEqual("primary", result.snapshot.source)

        future = replace(snapshot.bars[0], trade_date=date(2026, 8, 8))
        primary = FakeProvider(
            source="primary", outcomes=[replace(snapshot, bars=(future, *snapshot.bars))]
        )
        result = _pipeline(
            calendar=FakeCalendar({TRADE_DATE}),
            primary=primary,
            backup=backup,
            repository=FakeRepository(),
        ).run(TRADE_DATE)
        self.assertEqual("backup", result.snapshot.source)

    def test_pipeline_refuses_to_publish_a_proposed_strategy(self) -> None:
        proposed = replace(_strategy(), status="proposed")
        with self.assertRaises(ValueError):
            DailyReviewPipeline(
                calendar=FakeCalendar({TRADE_DATE}),
                primary_provider=FakeProvider(
                    source="primary", outcomes=[_snapshot(source="primary")]
                ),
                backup_provider=FakeProvider(
                    source="backup", outcomes=[_snapshot(source="backup")]
                ),
                repository=FakeRepository(),
                strategy=proposed,
                pipeline_version=PIPELINE_VERSION,
                expected_main_board_count=2,
            )

    def test_valid_defensive_snapshot_with_zero_candidates_is_still_ready(self) -> None:
        primary = FakeProvider(
            source="primary",
            outcomes=[_snapshot(source="primary", breadth_bps=3_500)],
        )
        backup = FakeProvider(source="backup", outcomes=[_snapshot(source="backup")])

        result = _pipeline(
            calendar=FakeCalendar({TRADE_DATE}),
            primary=primary,
            backup=backup,
            repository=FakeRepository(),
        ).run(TRADE_DATE)

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.review.candidates, ())

    def test_new_history_is_observation_only_until_twenty_prior_sessions_exist(self) -> None:
        """A fresh database must not turn an under-warmed snapshot into a formal review."""
        primary = FakeProvider(source="primary", outcomes=[_snapshot(source="primary")])
        backup = FakeProvider(source="backup", outcomes=[_snapshot(source="backup")])
        repository = FakeRepository()

        result = DailyReviewPipeline(
            calendar=FakeCalendar({TRADE_DATE}),
            primary_provider=primary,
            backup_provider=backup,
            repository=repository,
            strategy=_strategy(),
            pipeline_version=PIPELINE_VERSION,
            expected_main_board_count=2,
            required_prior_sessions=20,
        ).run(TRADE_DATE)

        self.assertEqual("degraded_observation", result.status)
        self.assertIsNone(result.review)
        self.assertIn("20", result.error or "")
        self.assertEqual(1, repository.save_calls)

    def test_live_observation_mode_keeps_an_auditable_review_without_publishing_ready(
        self,
    ) -> None:
        repository = FakeRepository()
        result = DailyReviewPipeline(
            calendar=FakeCalendar({TRADE_DATE}),
            primary_provider=FakeProvider(source="primary", outcomes=[_snapshot(source="primary")]),
            backup_provider=FakeProvider(source="backup", outcomes=[_snapshot(source="backup")]),
            repository=repository,
            strategy=_strategy(),
            pipeline_version=PIPELINE_VERSION,
            expected_main_board_count=2,
            required_prior_sessions=0,
            observation_only=True,
        ).run(TRADE_DATE)

        self.assertEqual("degraded_observation", result.status)
        self.assertIsNotNone(result.review)
        self.assertIn("live observation", result.error or "")
        self.assertEqual(1, repository.save_calls)

    def test_retry_deadline_returns_failed_when_no_complete_source_can_be_fetched(self) -> None:
        primary = FakeProvider(source="primary", outcomes=[RuntimeError("primary unavailable")])
        backup = FakeProvider(source="backup", outcomes=[RuntimeError("backup unavailable")])
        repository = FakeRepository()

        result = _pipeline(
            calendar=FakeCalendar({TRADE_DATE}),
            primary=primary,
            backup=backup,
            repository=repository,
            max_attempts=3,
        ).run(TRADE_DATE)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.attempts, 3)
        self.assertIsNone(result.snapshot)
        self.assertIsNone(result.review)
        self.assertEqual(repository.save_calls, 1)


if __name__ == "__main__":
    unittest.main()
