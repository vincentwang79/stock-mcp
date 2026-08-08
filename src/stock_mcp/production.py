"""Lazy production composition for the single Windows service process.

No provider is imported and no network request is made at service startup.  The
scheduled callable constructs a point-in-time BaoStock universe/calendar and
then runs one Tushare-primary/AKShare-degraded pipeline attempt.  Publication
and scheduling state live in the same SQLite backup boundary as market facts.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .backup import BackupManager
from .config import Settings
from .domain import Security
from .pipeline import DailyReviewPipeline, PipelineRun
from .providers.metadata import normalize_baostock_securities
from .providers.runtime import (
    AKShareQuoteProvider,
    AKShareSnapshotProvider,
    BaoStockTradingCalendar,
    TushareDailyProvider,
)
from .scheduler import PostMarketCoordinator, ScheduleOutcome
from .strategy import DatabaseStrategyRegistry

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MINIMUM_MAIN_BOARD_COUNT = 2_000


class LazyAKShareQuoteProvider:
    """Import AKShare only for the explicit ``check_next_day`` tool call."""

    def fetch_quote(self, symbol: str) -> dict[str, object]:
        import akshare  # type: ignore[import-not-found]

        return AKShareQuoteProvider(
            client=akshare, clock=lambda: datetime.now(_SHANGHAI)
        ).fetch_quote(symbol)


class SQLitePipelineRepository:
    """Crash-consistent publication state stored with normalized facts."""

    def __init__(self, database: Any) -> None:
        self.database = database

    def get_run(self, trade_date: date, pipeline_version: str) -> PipelineRun | None:
        return self.database.load_pipeline_run(trade_date, pipeline_version)

    def save_run(self, run: PipelineRun) -> None:
        self.database.save_pipeline_run(run)


class SQLiteScheduleState:
    def __init__(self, database: Any, pipeline_repository: SQLitePipelineRepository) -> None:
        self.database = database
        self.pipeline_repository = pipeline_repository

    def get(self, trade_date: date) -> ScheduleOutcome | None:
        record = self.database.load_schedule_outcome_record(trade_date)
        if record is None:
            return None
        pipeline_version = record.get("pipeline_version")
        run = (
            self.pipeline_repository.get_run(trade_date, str(pipeline_version))
            if pipeline_version
            else None
        )
        next_at = record.get("next_at")
        return ScheduleOutcome(
            trade_date=trade_date,
            status=str(record["status"]),
            next_at=datetime.fromisoformat(str(next_at)) if next_at else None,
            run=run,
            error=record.get("error"),
        )

    def save(self, outcome: ScheduleOutcome) -> None:
        self.database.save_schedule_outcome_record(
            trade_date=outcome.trade_date,
            status=outcome.status,
            next_at=outcome.next_at,
            pipeline_version=(None if outcome.run is None else outcome.run.pipeline_version),
            error=outcome.error,
        )


class ProductionPostMarketTask:
    """Callable registered once with APScheduler; provider work is lazy."""

    def __init__(
        self,
        settings: Settings,
        database: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        context_loader: Callable[[date], tuple[tuple[Security, ...], BaoStockTradingCalendar]]
        | None = None,
        provider_loader: Callable[[tuple[Security, ...]], tuple[Any, Any]] | None = None,
        minimum_main_board_count: int = _MINIMUM_MAIN_BOARD_COUNT,
        required_prior_sessions: int = 20,
        required_observation_sessions: int = 20,
    ) -> None:
        if minimum_main_board_count < 1:
            raise ValueError("minimum_main_board_count must be positive")
        if required_prior_sessions < 0:
            raise ValueError("required_prior_sessions cannot be negative")
        if required_observation_sessions < 0:
            raise ValueError("required_observation_sessions cannot be negative")
        self.settings = settings
        self.database = database
        self.clock = clock or (lambda: datetime.now(_SHANGHAI))
        self.context_loader = context_loader or _load_baostock_context
        self.provider_loader = provider_loader or self._load_providers
        self.minimum_main_board_count = minimum_main_board_count
        self.required_prior_sessions = required_prior_sessions
        self.required_observation_sessions = required_observation_sessions
        self.pipeline_repository = SQLitePipelineRepository(database)
        self.schedule_state = SQLiteScheduleState(database, self.pipeline_repository)

    def __call__(self) -> ScheduleOutcome | None:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("production clock must return an aware datetime")
        now = now.astimezone(_SHANGHAI)
        if now.weekday() >= 5 or (now.hour, now.minute) < (16, 30):
            return None
        context_error: str | None = None
        expected_main_board_count = self.minimum_main_board_count
        try:
            securities, calendar = self.context_loader(now.date())
            eligible_count = sum(
                security.board == "MAIN" and not security.is_st for security in securities
            )
            if eligible_count < self.minimum_main_board_count:
                context_error = (
                    "metadata has insufficient main-board coverage: "
                    f"{eligible_count} < {self.minimum_main_board_count}"
                )
            else:
                expected_main_board_count = max(
                    self.minimum_main_board_count, math.ceil(eligible_count * 0.97)
                )
        except Exception as error:
            securities = ()
            calendar = BaoStockTradingCalendar({now.date()})
            context_error = f"market context unavailable: {error}"
        registry = DatabaseStrategyRegistry(self.database)
        active_version = registry.active_version
        strategy = None if active_version is None else registry.get(active_version)

        def run_attempt(target: date) -> PipelineRun:
            if context_error is not None:
                return PipelineRun(
                    trade_date=target,
                    pipeline_version="pipeline-v0.1",
                    status="failed",
                    attempts=1,
                    snapshot=None,
                    review=None,
                    error=context_error,
                )
            if strategy is None:
                return PipelineRun(
                    trade_date=target,
                    pipeline_version="pipeline-v0.1",
                    status="failed",
                    attempts=1,
                    snapshot=None,
                    review=None,
                    error="no active strategy version",
                )
            primary, backup = self.provider_loader(securities)
            observed = self.database.count_live_observation_sessions("pipeline-v0.1")
            return DailyReviewPipeline(
                calendar=calendar,
                primary_provider=primary,
                backup_provider=backup,
                repository=self.pipeline_repository,
                strategy=strategy,
                pipeline_version="pipeline-v0.1",
                expected_main_board_count=expected_main_board_count,
                required_prior_sessions=self.required_prior_sessions,
                observation_only=observed < self.required_observation_sessions,
                max_attempts=1,
            ).run(target)

        def backup(_run: PipelineRun) -> None:
            BackupManager(self.settings.root / "backups", retention=14).create(
                self.database, label=now.date().isoformat()
            )

        return PostMarketCoordinator(
            calendar=calendar,
            run_attempt=run_attempt,
            backup=backup,
            state_repository=self.schedule_state,
        ).tick(now)

    def _load_providers(self, securities: tuple[Security, ...]) -> tuple[Any, Any]:
        import akshare  # type: ignore[import-not-found]
        import tushare  # type: ignore[import-not-found]

        primary = TushareDailyProvider(
            client=tushare.pro_api(self.settings.tushare_token),
            securities=securities,
            history_loader=lambda symbol, cutoff: self.database.load_symbol_history(
                symbol, end_date=cutoff, source="tushare", limit=60
            ),
            clock=self.clock,
        )
        backup = AKShareSnapshotProvider(client=akshare, securities=securities, clock=self.clock)
        return primary, backup


def _load_baostock_context(
    target: date,
) -> tuple[tuple[Security, ...], BaoStockTradingCalendar]:
    import baostock  # type: ignore[import-not-found]

    login = baostock.login()
    if str(getattr(login, "error_code", "0")) != "0":
        raise RuntimeError("BaoStock login failed")
    try:
        basic = _baostock_rows(baostock.query_stock_basic())
        industry = _baostock_rows(baostock.query_stock_industry())
        calendar_rows = _baostock_rows(
            baostock.query_trade_dates(start_date=target.isoformat(), end_date=target.isoformat())
        )
    finally:
        baostock.logout()
    securities = normalize_baostock_securities(basic, industry)
    if not securities:
        raise RuntimeError("BaoStock returned no eligible main-board securities")
    return securities, BaoStockTradingCalendar.from_rows(calendar_rows)


def _baostock_rows(result: Any) -> tuple[dict[str, object], ...]:
    if str(getattr(result, "error_code", "0")) != "0":
        raise RuntimeError("BaoStock query failed")
    fields = tuple(getattr(result, "fields", ()))
    rows: list[dict[str, object]] = []
    while result.next():
        values = result.get_row_data()
        rows.append(dict(zip(fields, values, strict=True)))
    return tuple(rows)
