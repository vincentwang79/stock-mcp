from __future__ import annotations

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from stock_mcp.pipeline import PipelineRun
from stock_mcp.scheduler import PostMarketCoordinator

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 8, 7)


def shanghai_time(hour: int, minute: int) -> datetime:
    return datetime(2026, 8, 7, hour, minute, tzinfo=SHANGHAI)


def pipeline_run(status: str, *, error: str | None = None) -> PipelineRun:
    return PipelineRun(
        trade_date=TRADE_DATE,
        pipeline_version="pipeline-v0.1",
        status=status,
        attempts=1,
        snapshot=None,
        review=None,
        error=error,
    )


class FakeCalendar:
    def __init__(self, trading_days: set[date]) -> None:
        self.trading_days = trading_days

    def is_trading_day(self, trade_date: date) -> bool:
        return trade_date in self.trading_days


class FakeRunAttempt:
    def __init__(self, outcomes: list[PipelineRun]) -> None:
        self.outcomes = outcomes
        self.calls: list[date] = []

    def __call__(self, trade_date: date) -> PipelineRun:
        self.calls.append(trade_date)
        return self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]


class FakeBackup:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.runs: list[PipelineRun] = []

    def __call__(self, run: PipelineRun) -> None:
        self.runs.append(run)
        if self.error is not None:
            raise self.error


class MemoryScheduleState:
    """Minimum persistence seam: date-keyed schedule outcomes survive restart."""

    def __init__(self) -> None:
        self.outcomes: dict[date, object] = {}
        self.save_calls = 0

    def get(self, trade_date: date) -> object | None:
        return self.outcomes.get(trade_date)

    def save(self, outcome: object) -> None:
        self.save_calls += 1
        self.outcomes[outcome.trade_date] = outcome


def coordinator(
    *,
    calendar: FakeCalendar | None = None,
    attempt: FakeRunAttempt | None = None,
    backup: FakeBackup | None = None,
    state: MemoryScheduleState | None = None,
) -> PostMarketCoordinator:
    return PostMarketCoordinator(
        calendar=calendar or FakeCalendar({TRADE_DATE}),
        run_attempt=attempt or FakeRunAttempt([pipeline_run("ready")]),
        backup=backup or FakeBackup(),
        state_repository=state or MemoryScheduleState(),
        timezone="Asia/Shanghai",
    )


class PostMarketCoordinatorContractTest(unittest.TestCase):
    def test_non_trading_day_and_pre_1630_never_run_pipeline(self) -> None:
        non_trading_attempt = FakeRunAttempt([pipeline_run("ready")])
        non_trading = coordinator(calendar=FakeCalendar(set()), attempt=non_trading_attempt)
        skipped = non_trading.tick(shanghai_time(16, 30))

        before_attempt = FakeRunAttempt([pipeline_run("ready")])
        before_open = coordinator(attempt=before_attempt)
        waiting = before_open.tick(shanghai_time(16, 29))

        self.assertEqual(skipped.status, "skipped")
        self.assertEqual(waiting.status, "waiting")
        self.assertEqual(non_trading_attempt.calls, [])
        self.assertEqual(before_attempt.calls, [])

    def test_failure_retries_only_at_ten_minute_boundary_then_ready_backs_up_once(self) -> None:
        attempt = FakeRunAttempt([pipeline_run("failed", error="upstream"), pipeline_run("ready")])
        backup = FakeBackup()
        state = MemoryScheduleState()
        subject = coordinator(attempt=attempt, backup=backup, state=state)

        first = subject.tick(shanghai_time(16, 30))
        early = subject.tick(shanghai_time(16, 35))
        second = subject.tick(shanghai_time(16, 40))

        self.assertEqual(first.status, "retry_scheduled")
        self.assertEqual(first.next_at, shanghai_time(16, 40))
        self.assertEqual(early.status, "retry_scheduled")
        self.assertEqual(early.next_at, shanghai_time(16, 40))
        self.assertEqual(second.status, "ready")
        self.assertEqual(attempt.calls, [TRADE_DATE, TRADE_DATE])
        self.assertEqual(backup.runs, [pipeline_run("ready")])
        self.assertEqual(state.save_calls, 2)

    def test_restart_at_17xx_runs_immediately_if_today_has_no_terminal_publication(self) -> None:
        attempt = FakeRunAttempt([pipeline_run("ready")])
        persisted_state = MemoryScheduleState()

        restarted_service = coordinator(attempt=attempt, state=persisted_state)
        outcome = restarted_service.tick(shanghai_time(17, 5))

        self.assertEqual(outcome.status, "ready")
        self.assertEqual(attempt.calls, [TRADE_DATE])

    def test_deadline_records_final_failure_without_another_pipeline_attempt(self) -> None:
        attempt = FakeRunAttempt([pipeline_run("failed", error="primary unavailable")])
        state = MemoryScheduleState()
        subject = coordinator(attempt=attempt, state=state)

        retry = subject.tick(shanghai_time(17, 50))
        deadline = subject.tick(shanghai_time(18, 1))
        repeated = subject.tick(shanghai_time(18, 10))

        self.assertEqual(retry.status, "retry_scheduled")
        self.assertEqual(deadline.status, "failed")
        self.assertEqual(repeated.status, "failed")
        self.assertEqual(attempt.calls, [TRADE_DATE])
        self.assertIn("deadline", deadline.error)

    def test_ready_and_degraded_runs_are_terminal_and_never_duplicate(self) -> None:
        for terminal_status, expected_backup_calls in (
            ("ready", 1),
            ("degraded_observation", 1),
            ("degraded_no_screen", 0),
        ):
            with self.subTest(status=terminal_status):
                attempt = FakeRunAttempt([pipeline_run(terminal_status)])
                backup = FakeBackup()
                state = MemoryScheduleState()
                subject = coordinator(attempt=attempt, backup=backup, state=state)

                first = subject.tick(shanghai_time(16, 30))
                repeated = subject.tick(shanghai_time(17, 0))

                self.assertEqual(first.status, terminal_status)
                self.assertEqual(repeated.status, terminal_status)
                self.assertEqual(attempt.calls, [TRADE_DATE])
                self.assertEqual(len(backup.runs), expected_backup_calls)

    def test_backup_failure_is_explicit_and_retries_without_erasing_ready_review(self) -> None:
        ready = pipeline_run("ready")
        attempt = FakeRunAttempt([ready])
        backup = FakeBackup(error=OSError("disk full"))
        state = MemoryScheduleState()
        subject = coordinator(attempt=attempt, backup=backup, state=state)

        failed_backup = subject.tick(shanghai_time(16, 30))
        backup.error = None
        repeated = subject.tick(shanghai_time(16, 40))

        self.assertEqual(failed_backup.status, "backup_failed")
        self.assertIs(failed_backup.run, ready)
        self.assertIn("disk full", failed_backup.error)
        self.assertEqual(repeated.status, "ready")
        self.assertEqual(attempt.calls, [TRADE_DATE, TRADE_DATE])
        self.assertEqual(backup.runs, [ready, ready])

    def test_tick_requires_aware_asia_shanghai_datetime(self) -> None:
        subject = coordinator()

        with self.assertRaises(ValueError):
            subject.tick(datetime(2026, 8, 7, 16, 30))
        with self.assertRaises(ValueError):
            subject.tick(datetime(2026, 8, 7, 8, 30, tzinfo=ZoneInfo("UTC")))


if __name__ == "__main__":
    unittest.main()
