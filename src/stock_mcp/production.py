"""Lazy production composition for the single Windows service process.

No provider is imported and no network request is made at service startup.  The
scheduled callable constructs a point-in-time BaoStock universe/calendar and
then runs one Tushare-primary/AKShare-degraded pipeline attempt.  Publication
and scheduling state live in the same SQLite backup boundary as market facts.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .backup import BackupManager
from .config import Settings
from .domain import DailyBar, MarketSnapshot, Security
from .industry import load_industry_reference
from .pipeline import DailyReviewPipeline, PipelineRun
from .providers.metadata import normalize_baostock_securities
from .providers.runtime import (
    AKShareQuoteProvider,
    AKShareSnapshotProvider,
    BaoStockTradingCalendar,
    TushareDailyProvider,
)
from .providers.sina_normalization import derive_sina_share_metrics, normalize_sina_spot
from .scheduler import PostMarketCoordinator, ScheduleOutcome
from .strategy import DatabaseStrategyRegistry
from .v3_facts import build_live_v3_market_input

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MINIMUM_MAIN_BOARD_COUNT = 2_000


class LazyAKShareQuoteProvider:
    """Import AKShare only for the explicit ``check_next_day`` tool call."""

    def fetch_quote(self, symbol: str) -> dict[str, object]:
        import akshare  # type: ignore[import-not-found]

        return AKShareQuoteProvider(
            client=akshare, clock=lambda: datetime.now(_SHANGHAI)
        ).fetch_quote(symbol)


class SinaShadowTask:
    """Normalize and atomically publish one complete, non-production Sina spot shadow."""

    def __init__(self, database: Any, spot_provider: Any) -> None:
        self._database = database
        self._spot_provider = spot_provider

    def run(self, trade_date: date) -> dict[str, object]:
        batch = self._spot_provider.fetch_pages(trade_date)
        if batch.trade_date != trade_date:
            raise ValueError("Sina shadow batch date does not match the requested date")
        source_snapshot = self._database.load_market_snapshot(
            trade_date, source="tushare", history_limit=1
        )
        securities = tuple(
            security for security in source_snapshot.securities if security.board == "MAIN"
        )
        expected_symbols = {security.symbol for security in securities}
        timestamp = max(item.retrieved_at for item in batch.evidence)
        records = normalize_sina_spot(batch.rows, trade_date=trade_date, source_timestamp=timestamp)
        by_symbol = {
            record.symbol: record for record in records if record.symbol in expected_symbols
        }
        missing = expected_symbols - set(by_symbol)
        if missing:
            raise ValueError("Sina shadow batch does not match the expected main-board universe")
        bars = tuple(
            DailyBar(
                record.symbol,
                trade_date,
                record.open_1e4,
                record.high_1e4,
                record.low_1e4,
                record.close_1e4,
                record.pre_close_1e4,
                record.volume_shares,
                record.amount_fen,
                "sina",
                record.source_timestamp,
            )
            for record in sorted(by_symbol.values(), key=lambda item: item.symbol)
        )
        prior_dates = tuple(
            self._database.load_expected_trading_days(
                trade_date - timedelta(days=180), trade_date - timedelta(days=1), source="tushare"
            )
        )[-60:]
        histories: dict[str, tuple[DailyBar, ...]] = {}
        same_source_history = len(prior_dates) == 60
        for security in securities:
            history = tuple(
                self._database.load_symbol_history(
                    security.symbol,
                    end_date=trade_date - timedelta(days=1),
                    source="sina",
                    limit=60,
                )
            )
            histories[security.symbol] = history
            same_source_history = (
                same_source_history and tuple(bar.trade_date for bar in history) == prior_dates
            )
        with self._database.connect() as connection:
            statuses = {
                str(row[0]): (str(row[1]), bool(row[2]))
                for row in connection.execute(
                    "SELECT symbol, tradestatus, is_st FROM daily_security_status "
                    "WHERE source='baostock' AND trade_date=?",
                    (trade_date.isoformat(),),
                ).fetchall()
                if str(row[0]) in expected_symbols
            }
        status_coverage_bps = (
            len(statuses) * 10_000 // len(expected_symbols) if expected_symbols else 0
        )
        daily_metrics: list[dict[str, object]] = []
        missing_capital = 0
        for bar in bars:
            capital = self._database.load_share_capital_fact(bar.symbol, on_date=trade_date)
            if capital is None:
                missing_capital += 1
                continue
            record = by_symbol[bar.symbol]
            derived = derive_sina_share_metrics(
                close_1e4=bar.close_1e4,
                volume_shares=bar.volume_shares,
                outstanding_shares=int(capital["outstanding_shares"]),
            )
            metric_evidence = {
                "symbol": bar.symbol,
                "trade_date": trade_date.isoformat(),
                "close_1e4": bar.close_1e4,
                "outstanding_shares": capital["outstanding_shares"],
                "upstream_nmc": record.upstream_circulating_market_cap_fen,
                "upstream_turnover": None
                if record.upstream_turnover_rate is None
                else str(record.upstream_turnover_rate),
            }
            daily_metrics.append(
                {
                    "symbol": bar.symbol,
                    "trade_date": trade_date,
                    "price_source": "sina",
                    "capital_source": "sina",
                    "upstream_market_cap_fen": record.upstream_circulating_market_cap_fen,
                    "derived_market_cap_fen": derived.market_cap_fen,
                    "upstream_turnover_rate": record.upstream_turnover_rate,
                    "derived_turnover_rate": derived.turnover_rate,
                    "evidence_sha256": hashlib.sha256(
                        json.dumps(metric_evidence, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                }
            )
        advances = sum(bar.close_1e4 > bar.pre_close_1e4 for bar in bars)
        ma20_eligible = 0
        above_ma20 = 0
        for bar in bars:
            history = histories[bar.symbol]
            closes = tuple(item.close_1e4 for item in history[-19:]) + (bar.close_1e4,)
            if len(closes) != 20:
                continue
            ma20_eligible += 1
            above_ma20 += bar.close_1e4 > sum(closes) / 20
        snapshot = MarketSnapshot(
            trade_date,
            "sina",
            timestamp,
            securities,
            bars,
            advances * 10_000 // len(bars),
            above_ma20 * 10_000 // ma20_eligible if ma20_eligible else 0,
        )
        evidence_records = []
        for item in batch.evidence:
            evidence = asdict(item)
            evidence["fetch_id"] = (
                f"sina:{item.endpoint_kind}:"
                + hashlib.sha256(
                    f"{item.request_key}|{item.retrieved_at.isoformat()}|"
                    f"{item.payload_sha256}".encode()
                ).hexdigest()[:24]
            )
            evidence["trade_date"] = trade_date
            evidence_records.append(evidence)
        dataset_payload = {
            "adapter": "sina-adapter-v1",
            "trade_date": trade_date.isoformat(),
            "bars": [
                (
                    bar.symbol,
                    bar.open_1e4,
                    bar.high_1e4,
                    bar.low_1e4,
                    bar.close_1e4,
                    bar.pre_close_1e4,
                    bar.volume_shares,
                    bar.amount_fen,
                )
                for bar in bars
            ],
            "fetches": [item.payload_sha256 for item in batch.evidence],
            "status_coverage_bps": status_coverage_bps,
            "same_source_history_ok": same_source_history,
        }
        dataset_hash = hashlib.sha256(
            json.dumps(dataset_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        success = (
            same_source_history
            and status_coverage_bps == 10_000
            and missing_capital == 0
            and len(bars) == len(expected_symbols)
        )
        run = {
            "source": "sina",
            "trade_date": trade_date,
            "adapter_version": "sina-adapter-v1",
            "expected_security_count": len(expected_symbols),
            "actual_security_count": len(bars),
            "expected_page_count": len(batch.evidence) - 1,
            "actual_page_count": len(batch.evidence) - 1,
            "missing_count": len(missing),
            "duplicate_count": 0,
            "invalid_count": missing_capital,
            "field_coverage_bps": 10_000 if missing_capital == 0 else 0,
            "status_coverage_bps": status_coverage_bps,
            "same_source_history_ok": same_source_history,
            "fetch_evidence_complete": True,
            "dataset_hash": dataset_hash,
            "status": "success" if success else "failed",
        }
        self._database.save_sina_spot_batch(
            snapshot=snapshot,
            fetch_evidence=evidence_records,
            metrics=dataset_payload,
            daily_metrics=daily_metrics,
            shadow_run=run,
        )
        return run


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
        research_batch: Callable[..., object] | None = None,
        forward_research_start: date = date(2026, 8, 8),
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
        self.research_batch = research_batch or self._run_research_batch
        self.forward_research_start = forward_research_start
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
            review_input_builder = None
            if int(strategy.parameters["rule_engine_version"]) == 3:

                def review_input_builder(snapshot: MarketSnapshot):
                    industry_reference = load_industry_reference(
                        self.settings.root / "current" / "a_share_mainboard_code_name.json"
                    )
                    prior_dates = calendar.prior_trading_days(target, 60)
                    trading_statuses = self.database.load_daily_security_statuses(
                        prior_dates[0], prior_dates[-1], source="baostock"
                    )
                    return build_live_v3_market_input(
                        snapshot,
                        prior_dates=prior_dates,
                        industry_reference=industry_reference,
                        trading_statuses=trading_statuses,
                    )

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
                review_input_builder=review_input_builder,
            ).run(target)

        def backup(_run: PipelineRun) -> None:
            if (
                _run.status == "ready"
                and _run.trade_date >= self.forward_research_start
                and _run.review is not None
                and _run.snapshot is not None
                and _run.snapshot.source == "tushare"
            ):
                try:
                    self.research_batch(trade_date=_run.trade_date, recorded_at=now)
                except Exception as error:
                    print(
                        "stock-mcp: forward-research-batch failed "
                        f"trade_date={_run.trade_date.isoformat()} "
                        f"error={type(error).__name__}",
                        flush=True,
                    )
            BackupManager(self.settings.root / "backups", retention=14).create(
                self.database, label=now.date().isoformat()
            )

        return PostMarketCoordinator(
            calendar=calendar,
            run_attempt=run_attempt,
            backup=backup,
            state_repository=self.schedule_state,
        ).tick(now)

    def _run_research_batch(self, *, trade_date: date, recorded_at: datetime) -> object:
        from .research_program import run_stored_price_research_batch

        return run_stored_price_research_batch(
            self.database,
            trade_date=trade_date,
            source="tushare",
            recorded_at=recorded_at.astimezone(UTC),
        )

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
            baostock.query_trade_dates(
                start_date=(target - timedelta(days=180)).isoformat(),
                end_date=target.isoformat(),
            )
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


class SinaShadowCoordinator:
    """Resume a bounded, page-oriented shadow capture without publishing candidates."""

    def __init__(
        self, *, store: Any, fetch_page: Callable[[int], bytes], page_count: Callable[[], int]
    ) -> None:
        self._store = store
        self._fetch_page = fetch_page
        self._page_count = page_count

    def run(self, *, trade_date: str) -> dict[str, object]:
        count = int(self._page_count())
        if count < 1:
            raise ValueError("Sina shadow page count must be positive")
        completed = self._store.completed_pages
        for page in range(1, count + 1):
            if page in completed:
                continue
            payload = self._fetch_page(page)
            completed[page] = hashlib.sha256(payload).hexdigest()
        if set(completed) != set(range(1, count + 1)):
            raise ValueError("Sina shadow pagination is incomplete")
        encoded = "|".join(f"{page}:{completed[page]}" for page in sorted(completed))
        return {
            "trade_date": trade_date,
            "status": "completed",
            "adapter_version": "sina-adapter-v1",
            "dataset_hash": hashlib.sha256(encoded.encode()).hexdigest(),
            "source_active": False,
        }


def evaluate_sina_provider_qualification(
    shadow_runs: list[dict[str, object]],
) -> dict[str, object]:
    ordered = sorted(shadow_runs, key=lambda item: str(item.get("trade_date", "")))
    complete = len(ordered) >= 20 and all(
        run.get("status") in {"completed", "success"}
        and bool(run.get("dataset_hash"))
        and run.get("fetch_evidence_complete") is True
        and run.get("same_source_history") is True
        and run.get("status_coverage_bps") == 10_000
        and run.get("manual_difference_reviewed") is True
        for run in ordered[-20:]
    )
    status = "qualified_for_manual_approval" if complete else "collecting"
    payload = "|".join(str(run.get("dataset_hash")) for run in ordered[-20:])
    return {
        "source": "sina",
        "status": status,
        "window_days": min(len(ordered), 20),
        "window_hash": hashlib.sha256(payload.encode()).hexdigest() if payload else None,
        "source_active": False,
        "manual_approval_required": True,
    }


def is_v4_research_allowed(now: datetime) -> bool:
    local = now.astimezone(_SHANGHAI)
    return not ((16, 20) <= (local.hour, local.minute) <= (18, 10))
