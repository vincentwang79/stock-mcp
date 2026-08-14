"""Offline RED contracts for the production v4 study execution chain.

The fixtures intentionally contain all recorded evidence in memory.  A study
executor must therefore obtain no quote, calendar, or outcome data from a live
endpoint while it is being exercised here.
"""

from __future__ import annotations

import unittest
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta

from stock_mcp import v4_research
from stock_mcp.v4_research import V4ResearchCoordinator

_ARMS = (
    "v0.3-policy-1",
    "v4-trend-quality",
    "v4-breakout-overextension-cap",
    "v4-no-recent-limit-up",
    "v4-breadth-five-day-median",
    "v4-size-bottom-30pct-filter",
    "v4-signal-quality-rank",
)
_DEFAULT = object()


class V4StudyExecutionChainContractTest(unittest.TestCase):
    def test_unexecutable_zero_return_does_not_pay_a_fictional_trading_cost(self) -> None:
        metric = getattr(v4_research, "v4_daily_primary_metric_bps", None)
        self.assertTrue(callable(metric))
        if not callable(metric):
            return
        outcomes = {
            "candidate": {
                "next_open_path": {
                    "status": "unexecutable",
                    "gross_return_20d_bps": 0,
                    "benchmark": {"market_cap_decile_return_bps": {20: 0}},
                }
            }
        }
        self.assertEqual(0, metric(outcomes=outcomes, candidate_count=1))

    def test_persists_each_of_the_seven_arms_for_the_only_signal_session(self) -> None:
        repository = _StudyRepository()
        loader = _FixedStudyDataLoader()
        coordinator = self._coordinator(repository, loader)

        for _ in _ARMS:
            self.assertTrue(coordinator.run_next_step())

        self.assertEqual(
            [(loader.signal_session.isoformat(), arm_id) for arm_id in _ARMS],
            [(step["signal_date"], step["arm_id"]) for step in repository.saved_steps],
        )
        self.assertEqual([], repository.completed_reports)
        for step in repository.saved_steps:
            result = step["result"]
            self.assertIn("candidates", result)
            self.assertIn("candidate_outcomes", result)
            self.assertIn("daily_primary_metric_bps", result)
            self.assertEqual("complete", result["completeness_status"])

    def test_batches_all_missing_arms_for_one_signal_date(self) -> None:
        repository = _StudyRepository()
        loader = _BatchStudyDataLoader()
        coordinator = self._coordinator(repository, loader)

        self.assertTrue(coordinator.run_next_step())

        self.assertEqual(1, loader.batch_calls)
        self.assertEqual(0, loader.individual_calls)
        self.assertEqual(1, repository.batch_writes)
        self.assertEqual(list(_ARMS), [str(step["arm_id"]) for step in repository.saved_steps])

    def test_reserves_the_final_twenty_five_sessions_for_outcomes_not_signals(self) -> None:
        repository = _StudyRepository()
        loader = _FixedStudyDataLoader()
        coordinator = self._coordinator(repository, loader)

        for _ in _ARMS:
            coordinator.run_next_step()

        self.assertEqual({loader.signal_session}, set(loader.requested_signal_dates))
        self.assertNotIn(loader.sessions[-25], loader.requested_signal_dates)
        self.assertEqual(1, len(set(loader.requested_signal_dates)))

    def test_incomplete_candidate_outcome_finishes_fail_closed_without_a_proposal(self) -> None:
        repository = _StudyRepository()
        loader = _FixedStudyDataLoader(completeness_status="incomplete")
        coordinator = self._coordinator(repository, loader)

        for _ in _ARMS:
            coordinator.run_next_step()
        self.assertTrue(coordinator.run_next_step(), "statistics must be a durable terminal step")

        self.assertEqual(1, len(repository.completed_reports))
        report = repository.completed_reports[0]
        self.assertEqual("incomplete", report["completeness_status"])
        self.assertEqual("retain_baseline", report["winner"]["decision"])
        self.assertIsNone(report["winner"]["arm_id"])
        self.assertEqual([], report["proposals"])

    def test_terminal_report_exposes_the_actual_incomplete_day_rate(self) -> None:
        """One bad day must not be reported as though every day were bad."""

        sessions = (date(2026, 4, 1), date(2026, 4, 2))
        days_by_arm = {
            arm_id: tuple(
                {
                    "signal_date": day.isoformat(),
                    "result": {
                        "daily_primary_metric_bps": 0,
                        "completeness_status": (
                            "incomplete"
                            if arm_id == "v0.3-policy-1" and day == sessions[-1]
                            else "complete"
                        ),
                        "candidate_outcomes": {},
                        "replication_evidence": None,
                    },
                }
                for day in sessions
            )
            for arm_id in _ARMS
        }

        report, statistics = v4_research._terminal_report(  # noqa: SLF001
            manifest_hash="a" * 64,
            signal_dates=sessions,
            days_by_arm=days_by_arm,
        )

        self.assertEqual("incomplete", report["completeness_status"])
        self.assertEqual(5_000, report["outcome_completeness_rate_bps"])
        self.assertEqual(5_000, report["benchmark_completeness_rate_bps"])
        self.assertEqual(10_000, statistics["arms"]["v4-trend-quality"]["completeness_rate_bps"])
        self.assertEqual(
            {"eligible": False, "arm_id": None, "decision": "retain_baseline"},
            report["winner"],
        )

    def test_missing_replication_evidence_retains_baseline_in_a_deterministic_report(self) -> None:
        first_repository = _StudyRepository()
        second_repository = _StudyRepository()
        first_loader = _FixedStudyDataLoader(replication_evidence=None)
        second_loader = _FixedStudyDataLoader(replication_evidence=None)

        self._run_to_completion(first_repository, first_loader)
        self._run_to_completion(second_repository, second_loader)

        first = first_repository.completed_reports[0]
        second = second_repository.completed_reports[0]
        self.assertEqual(first, second)
        self.assertEqual("complete", first["completeness_status"])
        self.assertFalse(first["sina_replication_complete"])
        self.assertEqual(
            {"eligible": False, "arm_id": None, "decision": "retain_baseline"},
            first["winner"],
        )
        self.assertEqual([], first["proposals"])

    def test_restarted_execution_uses_persisted_day_cursor_and_never_recomputes_it(self) -> None:
        repository = _StudyRepository()
        loader = _FixedStudyDataLoader()
        repository.persisted_days[(loader.signal_session.isoformat(), _ARMS[0])] = _day_step(
            loader.signal_session, _ARMS[0]
        )
        coordinator = self._coordinator(repository, loader)

        self.assertTrue(coordinator.run_next_step())

        self.assertEqual(1, len(repository.saved_steps))
        self.assertEqual(_ARMS[1], repository.saved_steps[0]["arm_id"])
        self.assertEqual([(loader.signal_session, _ARMS[1])], loader.executed_arm_days)
        self.assertEqual(0, repository.day_result_reads)

    def test_terminal_report_derives_execution_rates_from_persisted_outcomes(self) -> None:
        repository = _StudyRepository()
        loader = _FixedStudyDataLoader(next_open_status="unavailable")
        self._run_to_completion(repository, loader)

        statistics = repository.saved_statistics[0]
        challenger = statistics["arms"]["v4-trend-quality"]
        self.assertEqual(0, challenger["executable_rate_bps"])
        self.assertEqual(10_000, challenger["unexecutable_rate_bps"])

    def _run_to_completion(
        self, repository: _StudyRepository, loader: _FixedStudyDataLoader
    ) -> None:
        coordinator = self._coordinator(repository, loader)
        for _ in range(len(_ARMS) + 1):
            self.assertTrue(coordinator.run_next_step())
        self.assertEqual(1, len(repository.completed_reports))

    def _coordinator(
        self, repository: _StudyRepository, loader: _FixedStudyDataLoader
    ) -> V4ResearchCoordinator:
        executor_type = getattr(v4_research, "V4StudyExecutor", None)
        self.assertTrue(
            callable(executor_type),
            "v4 needs V4StudyExecutor to execute persisted seven-arm research steps",
        )
        assert callable(executor_type)  # narrows the type after the behavioral assertion
        return V4ResearchCoordinator(
            repository,
            step_executor=executor_type(repository, data_loader=loader),
            allowed=lambda _now: True,
            clock=lambda: datetime(2026, 8, 12, 18, 0, tzinfo=UTC),
        )


class _StudyRepository:
    """A small durable repository double; it preserves every saved day across restart."""

    def __init__(self) -> None:
        self.study = {"study_id": "study-v4-1", "manifest_hash": "a" * 64, "status": "queued"}
        self.manifest = {
            "manifest_hash": "a" * 64,
            "source": "tushare",
            "sessions": [day.isoformat() for day in _sessions()],
        }
        self.arms = tuple({"arm_id": arm_id, "status": "queued"} for arm_id in _ARMS)
        self.persisted_days: dict[tuple[str, str], dict[str, object]] = {}
        self.saved_steps: list[dict[str, object]] = []
        self.completed_reports: list[dict[str, object]] = []
        self.saved_statistics: list[dict[str, object]] = []
        self.day_result_reads = 0
        self.batch_writes = 0

    def claim_next_v4_study(self) -> dict[str, object] | None:
        if self.completed_reports:
            return None
        self.study["status"] = "running"
        return dict(self.study)

    def save_v4_study_step(self, *, study_id: str, step: dict[str, object]) -> None:
        assert study_id == self.study["study_id"]
        key = (str(step["signal_date"]), str(step["arm_id"]))
        previous = self.persisted_days.get(key)
        if previous is not None and previous != step:
            raise ValueError("immutable v4 study day conflict")
        if previous is None:
            copied = _copy_step(step)
            self.persisted_days[key] = copied
            self.saved_steps.append(copied)

    def save_v4_study_steps(self, *, study_id: str, steps: tuple[dict[str, object], ...]) -> None:
        self.batch_writes += 1
        for step in steps:
            self.save_v4_study_step(study_id=study_id, step=step)

    def complete_v4_study(self, *, study_id: str, report: dict[str, object]) -> None:
        assert study_id == self.study["study_id"]
        self.completed_reports.append(dict(report))
        self.study["status"] = "completed"

    def save_v4_study_statistics(self, *, study_id: str, statistics: dict[str, object]) -> None:
        assert study_id == self.study["study_id"]
        self.saved_statistics.append(dict(statistics))

    def fail_v4_study(self, *, study_id: str, error: str) -> None:
        raise AssertionError(f"production executor failed unexpectedly: {study_id}: {error}")

    def requeue_interrupted_v4_studies(self) -> int:
        return 0

    def get_v4_dataset_manifest(self, manifest_hash: str) -> dict[str, object] | None:
        return dict(self.manifest) if manifest_hash == self.manifest["manifest_hash"] else None

    def list_v4_study_arms(self, study_id: str) -> tuple[dict[str, object], ...]:
        assert study_id == self.study["study_id"]
        return tuple(dict(arm) for arm in self.arms)

    def list_v4_study_days(
        self, *, study_id: str, arm_id: str, after_signal_date: date | None, limit: int
    ) -> tuple[dict[str, object], ...]:
        self.day_result_reads += 1
        assert study_id == self.study["study_id"]
        after = date.min if after_signal_date is None else after_signal_date
        records = (
            {"signal_date": signal_date, "result": step["result"]}
            for (signal_date, recorded_arm), step in self.persisted_days.items()
            if recorded_arm == arm_id and date.fromisoformat(signal_date) > after
        )
        return tuple(sorted(records, key=lambda item: str(item["signal_date"])))[:limit]

    def get_v4_study_progress(self, *, study_id: str) -> dict[str, dict[str, object]]:
        assert study_id == self.study["study_id"]
        progress: dict[str, dict[str, object]] = {}
        for arm_id in _ARMS:
            dates = sorted(
                signal_date
                for signal_date, recorded_arm in self.persisted_days
                if recorded_arm == arm_id
            )
            progress[arm_id] = {
                "completed_count": len(dates),
                "last_signal_date": None if not dates else dates[-1],
            }
        return progress


class _FixedStudyDataLoader:
    """Fixed, source-stamped evidence for one signal date and 25 outcome-only dates."""

    def __init__(
        self,
        *,
        completeness_status: str = "complete",
        replication_evidence: object = _DEFAULT,
        next_open_status: str = "available",
    ) -> None:
        self.sessions = _sessions()
        self.signal_session = self.sessions[60]
        self.completeness_status = completeness_status
        self.replication_evidence = (
            {"status": "complete", "completeness_rate_bps": 10_000, "primary_metric_bps": 0}
            if replication_evidence is _DEFAULT
            else replication_evidence
        )
        self.next_open_status = next_open_status
        self.requested_signal_dates: list[date] = []
        self.executed_arm_days: list[tuple[date, str]] = []

    def load_v4_signal_evidence(
        self, *, manifest_hash: str, signal_date: date, arm_id: str
    ) -> dict[str, object]:
        assert manifest_hash == "a" * 64
        assert signal_date == self.signal_session, "outcome-only sessions must never be signals"
        assert arm_id in _ARMS
        self.requested_signal_dates.append(signal_date)
        self.executed_arm_days.append((signal_date, arm_id))
        return {
            "source": "tushare",
            "source_timestamp": "2026-08-12T00:00:00+00:00",
            "candidates": [{"candidate_id": f"{arm_id}-candidate", "symbol": "600001.SH"}],
            "candidate_outcomes": {
                f"{arm_id}-candidate": {
                    "schema": "v4-outcome-v2",
                    "source": "tushare",
                    "completeness_status": self.completeness_status,
                    "next_open_path": {
                        "status": self.next_open_status,
                        "benchmark": {"market_cap_matched_excess_bps": {20: 0}},
                    },
                }
            },
            "daily_primary_metric_bps": 0,
            "completeness_status": self.completeness_status,
            "replication_evidence": self.replication_evidence,
        }


class _BatchStudyDataLoader(_FixedStudyDataLoader):
    def __init__(self) -> None:
        super().__init__()
        self.batch_calls = 0
        self.individual_calls = 0

    def load_v4_signal_evidence(
        self, *, manifest_hash: str, signal_date: date, arm_id: str
    ) -> dict[str, object]:
        self.individual_calls += 1
        return super().load_v4_signal_evidence(
            manifest_hash=manifest_hash,
            signal_date=signal_date,
            arm_id=arm_id,
        )

    def load_v4_signal_evidence_batch(
        self, *, manifest_hash: str, signal_date: date, arm_ids: tuple[str, ...]
    ) -> dict[str, dict[str, object]]:
        self.batch_calls += 1
        return {
            arm_id: _FixedStudyDataLoader.load_v4_signal_evidence(
                self,
                manifest_hash=manifest_hash,
                signal_date=signal_date,
                arm_id=arm_id,
            )
            for arm_id in arm_ids
        }


def _sessions() -> tuple[date, ...]:
    start = date(2026, 1, 2)
    return tuple(start + timedelta(days=index) for index in range(86))


def _day_step(signal_date: date, arm_id: str) -> dict[str, object]:
    return {
        "kind": "day",
        "signal_date": signal_date.isoformat(),
        "arm_id": arm_id,
        "result": {
            "candidates": [],
            "candidate_outcomes": {},
            "daily_primary_metric_bps": 0,
            "completeness_status": "complete",
        },
    }


def _copy_step(step: Mapping[str, object]) -> dict[str, object]:
    result = step["result"]
    return {**step, "result": dict(result) if isinstance(result, Mapping) else result}


if __name__ == "__main__":
    unittest.main()
