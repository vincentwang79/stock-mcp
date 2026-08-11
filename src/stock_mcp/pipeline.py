"""Idempotent, single-source post-market review publication.

The scheduler is deliberately kept outside this module.  This object receives a
single requested trading date and either publishes a review from one complete
provider snapshot or records an explicit non-screening/failed outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from .domain import DailyReview, MarketSnapshot, StrategyVersion
from .review import MixedSourceSnapshotError, generate_daily_review
from .strategy import validate_strategy_parameters


class SnapshotValidationError(ValueError):
    """A provider snapshot cannot safely be used as a daily report input."""


class TradingCalendar(Protocol):
    def is_trading_day(self, trade_date: date) -> bool: ...


class SnapshotProvider(Protocol):
    has_historical_mirror: bool

    def fetch_snapshot(self, trade_date: date) -> MarketSnapshot: ...


class PipelineRepository(Protocol):
    def get_run(self, trade_date: date, pipeline_version: str) -> PipelineRun | None: ...

    def save_run(self, run: PipelineRun) -> None: ...


@dataclass(frozen=True, slots=True)
class PipelineRun:
    """The idempotent publication record for a date and pipeline version."""

    trade_date: date
    pipeline_version: str
    status: str
    attempts: int
    snapshot: MarketSnapshot | None
    review: DailyReview | None
    error: str | None = None


class DailyReviewPipeline:
    """Publish one validated, source-consistent daily review.

    A retry consists of a primary attempt followed by a complete fallback
    snapshot attempt.  The fallback must explicitly advertise a full same-source
    historical mirror before screening can be performed.
    """

    def __init__(
        self,
        *,
        calendar: TradingCalendar,
        primary_provider: SnapshotProvider,
        backup_provider: SnapshotProvider,
        repository: PipelineRepository,
        strategy: StrategyVersion,
        pipeline_version: str,
        expected_main_board_count: int,
        required_prior_sessions: int = 20,
        observation_only: bool = False,
        max_attempts: int = 1,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if expected_main_board_count < 1:
            raise ValueError("expected_main_board_count must be positive")
        if required_prior_sessions < 0:
            raise ValueError("required_prior_sessions cannot be negative")
        if strategy.status != "active":
            raise ValueError("daily publication requires an active strategy version")
        validate_strategy_parameters(strategy.parameters, require_complete=True)
        self._calendar = calendar
        self._primary = primary_provider
        self._backup = backup_provider
        self._repository = repository
        self._strategy = strategy
        self._pipeline_version = pipeline_version
        self._expected_main_board_count = expected_main_board_count
        self._required_prior_sessions = required_prior_sessions
        self._observation_only = observation_only
        self._max_attempts = max_attempts

    def run(self, trade_date: date) -> PipelineRun:
        """Run or reuse the immutable publication for ``trade_date``."""
        if not self._calendar.is_trading_day(trade_date):
            return PipelineRun(
                trade_date=trade_date,
                pipeline_version=self._pipeline_version,
                status="skipped",
                attempts=0,
                snapshot=None,
                review=None,
            )

        existing = self._repository.get_run(trade_date, self._pipeline_version)
        if existing is not None and existing.status != "failed":
            return existing

        failures: list[str] = []
        for attempt in range(1, self._max_attempts + 1):
            primary_snapshot = self._fetch_complete(self._primary, trade_date, "primary", failures)
            if primary_snapshot is not None:
                return self._publish_ready(primary_snapshot, attempt)

            backup_snapshot = self._fetch_complete(self._backup, trade_date, "backup", failures)
            if backup_snapshot is None:
                continue
            if not self._backup.has_historical_mirror:
                return self._publish_degraded(backup_snapshot, attempt)
            return self._publish_ready(backup_snapshot, attempt)

        failed = PipelineRun(
            trade_date=trade_date,
            pipeline_version=self._pipeline_version,
            status="failed",
            attempts=self._max_attempts,
            snapshot=None,
            review=None,
            error="; ".join(failures) or "no complete source snapshot available",
        )
        self._repository.save_run(failed)
        return failed

    def _fetch_complete(
        self,
        provider: SnapshotProvider,
        trade_date: date,
        label: str,
        failures: list[str],
    ) -> MarketSnapshot | None:
        try:
            snapshot = provider.fetch_snapshot(trade_date)
            self._validate_snapshot(snapshot, trade_date)
            return snapshot
        except Exception as error:  # Provider failures are retryable publication failures.
            failures.append(f"{label}: {error}")
            return None

    def _publish_ready(self, snapshot: MarketSnapshot, attempts: int) -> PipelineRun:
        insufficient = self._symbols_without_required_history(snapshot)
        if insufficient:
            if snapshot.source == "sina":
                degraded = PipelineRun(
                    trade_date=snapshot.trade_date,
                    pipeline_version=self._pipeline_version,
                    status="degraded_no_screen",
                    attempts=attempts,
                    snapshot=snapshot,
                    review=None,
                    error=(
                        f"Sina backup requires {self._required_prior_sessions} complete "
                        f"same-source prior sessions; insufficient history for "
                        f"{len(insufficient)} securities"
                    ),
                )
                self._repository.save_run(degraded)
                return degraded
            observation = PipelineRun(
                trade_date=snapshot.trade_date,
                pipeline_version=self._pipeline_version,
                status="degraded_observation",
                attempts=attempts,
                snapshot=snapshot,
                review=None,
                error=(
                    f"observation requires {self._required_prior_sessions} prior sessions; "
                    f"insufficient history for {len(insufficient)} securities"
                ),
            )
            self._repository.save_run(observation)
            return observation
        try:
            review = generate_daily_review(snapshot, self._strategy)
        except (MixedSourceSnapshotError, ValueError) as error:
            # The snapshot was validated before this point, but preserving an
            # explicit failed record is safer than leaking a partially published
            # daily review if the strategy boundary rejects it.
            failed = PipelineRun(
                trade_date=snapshot.trade_date,
                pipeline_version=self._pipeline_version,
                status="failed",
                attempts=attempts,
                snapshot=None,
                review=None,
                error=f"review: {error}",
            )
            self._repository.save_run(failed)
            return failed
        if self._observation_only:
            observation = PipelineRun(
                trade_date=snapshot.trade_date,
                pipeline_version=self._pipeline_version,
                status="degraded_observation",
                attempts=attempts,
                snapshot=snapshot,
                review=review,
                error="live observation period is not yet complete",
            )
            self._repository.save_run(observation)
            return observation
        ready = PipelineRun(
            trade_date=snapshot.trade_date,
            pipeline_version=self._pipeline_version,
            status="ready",
            attempts=attempts,
            snapshot=snapshot,
            review=review,
        )
        self._repository.save_run(ready)
        return ready

    def _publish_degraded(self, snapshot: MarketSnapshot, attempts: int) -> PipelineRun:
        degraded = PipelineRun(
            trade_date=snapshot.trade_date,
            pipeline_version=self._pipeline_version,
            status="degraded_no_screen",
            attempts=attempts,
            snapshot=snapshot,
            review=None,
            error="backup provider has no same-source historical mirror",
        )
        self._repository.save_run(degraded)
        return degraded

    def _symbols_without_required_history(self, snapshot: MarketSnapshot) -> tuple[str, ...]:
        if self._required_prior_sessions == 0:
            return ()
        eligible_symbols = {
            security.symbol
            for security in snapshot.securities
            if security.board == "MAIN"
            and not security.is_st
            and security.list_date <= snapshot.trade_date - timedelta(days=180)
        }
        target_symbols = {
            bar.symbol
            for bar in snapshot.bars
            if bar.trade_date == snapshot.trade_date and bar.symbol in eligible_symbols
        }
        prior_dates: dict[str, set[date]] = {symbol: set() for symbol in target_symbols}
        market_prior_dates: set[date] = set()
        for bar in snapshot.bars:
            if bar.symbol in prior_dates and bar.trade_date < snapshot.trade_date:
                prior_dates[bar.symbol].add(bar.trade_date)
                market_prior_dates.add(bar.trade_date)
        expected_dates = set(sorted(market_prior_dates)[-self._required_prior_sessions :])
        return tuple(
            symbol
            for symbol in sorted(target_symbols)
            if len(expected_dates) < self._required_prior_sessions
            or not expected_dates.issubset(prior_dates[symbol])
        )

    def _validate_snapshot(self, snapshot: MarketSnapshot, requested_date: date) -> None:
        if snapshot.trade_date != requested_date:
            raise SnapshotValidationError("snapshot trade date does not match request")
        main_symbols = {
            security.symbol
            for security in snapshot.securities
            if security.board == "MAIN" and not security.is_st
        }
        known_symbols = {security.symbol for security in snapshot.securities}
        bar_keys = {(bar.symbol, bar.trade_date) for bar in snapshot.bars}
        bar_symbols = {bar.symbol for bar in snapshot.bars}
        target_bar_symbols = {
            bar.symbol for bar in snapshot.bars if bar.trade_date == requested_date
        }
        if len(main_symbols) < self._expected_main_board_count:
            raise SnapshotValidationError("metadata has insufficient main-board coverage")
        if len(bar_keys) != len(snapshot.bars) or not bar_symbols.issubset(known_symbols):
            raise SnapshotValidationError("snapshot contains duplicate or unknown securities")
        if len(target_bar_symbols & main_symbols) < self._expected_main_board_count:
            raise SnapshotValidationError("snapshot has insufficient main-board coverage")

        for bar in snapshot.bars:
            if bar.trade_date > requested_date:
                raise SnapshotValidationError("snapshot contains a future price bar")
            if bar.source != snapshot.source:
                raise SnapshotValidationError("snapshot contains price bars from multiple sources")
            if min(bar.open_1e4, bar.close_1e4, bar.low_1e4, bar.high_1e4) <= 0:
                raise SnapshotValidationError("OHLC prices must be positive")
            if bar.pre_close_1e4 <= 0:
                raise SnapshotValidationError("pre-close price must be positive")
            if not (
                bar.low_1e4 <= min(bar.open_1e4, bar.close_1e4)
                and bar.high_1e4 >= max(bar.open_1e4, bar.close_1e4)
            ):
                raise SnapshotValidationError("invalid OHLC range")
