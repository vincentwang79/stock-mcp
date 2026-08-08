"""Pure post-market scheduling policy.

The service owns the APScheduler wiring; this module intentionally has no
clock, network, or APScheduler dependency.  That makes restart/misfire and
deadline policy deterministic and straightforward to exercise in tests.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from .pipeline import PipelineRun, TradingCalendar


class ScheduleStateRepository(Protocol):
    """Small persistence seam for one date's published scheduling outcome."""

    def get(self, trade_date: date) -> ScheduleOutcome | None: ...

    def save(self, outcome: ScheduleOutcome) -> None: ...


@dataclass(frozen=True, slots=True)
class ScheduleOutcome:
    """A date-keyed scheduler decision suitable for durable storage."""

    trade_date: date
    status: str
    next_at: datetime | None = None
    run: PipelineRun | None = None
    error: str | None = None


class PostMarketCoordinator:
    """Coordinate bounded after-close publication attempts for one process.

    A tick is deliberately safe to invoke repeatedly (including after a
    service restart).  Terminal decisions come from ``state_repository``;
    otherwise a missing 17:xx state is treated as an APScheduler misfire and
    attempted immediately.
    """

    _START_HOUR = 16
    _START_MINUTE = 30
    _DEADLINE_HOUR = 18
    _TERMINAL = frozenset({"ready", "degraded_no_screen", "degraded_observation", "failed"})

    def __init__(
        self,
        *,
        calendar: TradingCalendar,
        run_attempt: Callable[[date], PipelineRun],
        backup: Callable[[PipelineRun], None],
        state_repository: ScheduleStateRepository,
        timezone: str = "Asia/Shanghai",
    ) -> None:
        if timezone != "Asia/Shanghai":
            raise ValueError("post-market scheduling requires Asia/Shanghai")
        self._calendar = calendar
        self._run_attempt = run_attempt
        self._backup = backup
        self._state = state_repository
        self._timezone = ZoneInfo(timezone)

    def tick(self, now: datetime) -> ScheduleOutcome:
        """Process one deterministic after-close scheduling decision."""
        self._validate_now(now)
        trade_date = now.date()
        if not self._calendar.is_trading_day(trade_date):
            return ScheduleOutcome(trade_date=trade_date, status="skipped")

        start = now.replace(
            hour=self._START_HOUR, minute=self._START_MINUTE, second=0, microsecond=0
        )
        if now < start:
            return ScheduleOutcome(trade_date=trade_date, status="waiting", next_at=start)

        persisted = self._state.get(trade_date)
        if persisted is not None and persisted.status in self._TERMINAL:
            return persisted

        deadline = now.replace(hour=self._DEADLINE_HOUR, minute=0, second=0, microsecond=0)
        if now > deadline:
            final = ScheduleOutcome(
                trade_date=trade_date,
                status="failed",
                run=persisted.run if persisted is not None else None,
                error=self._deadline_error(persisted),
            )
            self._state.save(final)
            return final

        if (
            persisted is not None
            and persisted.status == "retry_scheduled"
            and persisted.next_at is not None
            and now < persisted.next_at
        ):
            return persisted

        return self._attempt(trade_date, now)

    def _attempt(self, trade_date: date, now: datetime) -> ScheduleOutcome:
        run = self._run_attempt(trade_date)
        if run.status in {"ready", "degraded_observation"}:
            try:
                self._backup(run)
            except Exception as error:
                outcome = ScheduleOutcome(
                    trade_date=trade_date,
                    status="backup_failed",
                    run=run,
                    error=str(error),
                )
            else:
                outcome = ScheduleOutcome(
                    trade_date=trade_date,
                    status=run.status,
                    run=run,
                    error=run.error,
                )
        elif run.status == "degraded_no_screen":
            outcome = ScheduleOutcome(
                trade_date=trade_date, status=run.status, run=run, error=run.error
            )
        else:
            outcome = ScheduleOutcome(
                trade_date=trade_date,
                status="retry_scheduled",
                next_at=self._next_retry_boundary(now),
                run=run,
                error=run.error,
            )
        self._state.save(outcome)
        return outcome

    def _next_retry_boundary(self, now: datetime) -> datetime:
        """Return the next ten-minute wall-clock boundary after ``now``."""
        boundary = now.replace(second=0, microsecond=0)
        remainder = boundary.minute % 10
        minutes = 10 - remainder if remainder else 10
        return boundary + timedelta(minutes=minutes)

    @staticmethod
    def _deadline_error(persisted: ScheduleOutcome | None) -> str:
        detail = (
            persisted.error
            if persisted is not None and persisted.error
            else "publication unavailable"
        )
        return f"post-market publication deadline exceeded: {detail}"

    def _validate_now(self, now: datetime) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be an aware Asia/Shanghai datetime")
        if getattr(now.tzinfo, "key", None) != self._timezone.key:
            raise ValueError("now must use Asia/Shanghai timezone")
