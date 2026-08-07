"""Lazy production composition for the single Windows service process.

No provider is imported and no network request is made at service startup.  The
scheduled callable constructs a point-in-time BaoStock universe/calendar and
then runs one Tushare-primary/AKShare-degraded pipeline attempt.  Small JSON
state files are used only for scheduling decisions; market facts and reviews
remain in SQLite.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
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


class LazyAKShareQuoteProvider:
    """Import AKShare only for the explicit ``check_next_day`` tool call."""

    def fetch_quote(self, symbol: str) -> dict[str, object]:
        import akshare  # type: ignore[import-not-found]

        return AKShareQuoteProvider(
            client=akshare, clock=lambda: datetime.now(_SHANGHAI)
        ).fetch_quote(symbol)


class FilePipelineRepository:
    """Durable pipeline decision metadata backed by SQLite market records."""

    def __init__(self, database: Any, path: Path) -> None:
        self.database = database
        self.path = path

    def get_run(self, trade_date: date, pipeline_version: str) -> PipelineRun | None:
        record = self._records().get(f"{trade_date.isoformat()}|{pipeline_version}")
        if record is None:
            return None
        strategy_version = record.get("strategy_version")
        review = (
            self.database.load_daily_review(trade_date, strategy_version)
            if isinstance(strategy_version, str)
            else None
        )
        return PipelineRun(
            trade_date=trade_date,
            pipeline_version=pipeline_version,
            status=str(record["status"]),
            attempts=int(record["attempts"]),
            snapshot=None,
            review=review,
            error=record.get("error"),
        )

    def save_run(self, run: PipelineRun) -> None:
        if run.snapshot is not None:
            self.database.save_market_snapshot(run.snapshot)
            self.database.prune_market_data_before(run.trade_date - timedelta(days=3 * 366))
        if run.review is not None:
            published = replace(run.review, status="published")
            self.database.save_daily_review(published)
        records = self._records()
        records[f"{run.trade_date.isoformat()}|{run.pipeline_version}"] = {
            "status": run.status,
            "attempts": run.attempts,
            "error": run.error,
            "strategy_version": None if run.review is None else run.review.strategy_version,
        }
        _write_json_atomic(self.path, records)

    def _records(self) -> dict[str, dict[str, object]]:
        return _read_json_object(self.path)


class FileScheduleState:
    def __init__(self, path: Path, pipeline_repository: FilePipelineRepository) -> None:
        self.path = path
        self.pipeline_repository = pipeline_repository

    def get(self, trade_date: date) -> ScheduleOutcome | None:
        record = self._records().get(trade_date.isoformat())
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
        records = self._records()
        records[outcome.trade_date.isoformat()] = {
            "status": outcome.status,
            "next_at": None if outcome.next_at is None else outcome.next_at.isoformat(),
            "pipeline_version": (None if outcome.run is None else outcome.run.pipeline_version),
            "error": outcome.error,
        }
        _write_json_atomic(self.path, records)

    def _records(self) -> dict[str, dict[str, object]]:
        return _read_json_object(self.path)


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
    ) -> None:
        self.settings = settings
        self.database = database
        self.clock = clock or (lambda: datetime.now(_SHANGHAI))
        self.context_loader = context_loader or _load_baostock_context
        self.provider_loader = provider_loader or self._load_providers
        state = settings.root / "state"
        self.pipeline_repository = FilePipelineRepository(database, state / "pipeline-runs.json")
        self.schedule_state = FileScheduleState(
            state / "schedule-state.json", self.pipeline_repository
        )

    def __call__(self) -> ScheduleOutcome | None:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("production clock must return an aware datetime")
        now = now.astimezone(_SHANGHAI)
        if now.weekday() >= 5 or (now.hour, now.minute) < (16, 30):
            return None
        securities, calendar = self.context_loader(now.date())
        registry = DatabaseStrategyRegistry(self.database)
        active_version = registry.active_version
        strategy = None if active_version is None else registry.get(active_version)

        def run_attempt(target: date) -> PipelineRun:
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
            expected = max(1, math.ceil(len(securities) * 0.97))
            return DailyReviewPipeline(
                calendar=calendar,
                primary_provider=primary,
                backup_provider=backup,
                repository=self.pipeline_repository,
                strategy=strategy,
                pipeline_version="pipeline-v0.1",
                expected_main_board_count=expected,
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


def _read_json_object(path: Path) -> dict[str, dict[str, object]]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"invalid scheduler state: {path}")
    return value


def _write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)
