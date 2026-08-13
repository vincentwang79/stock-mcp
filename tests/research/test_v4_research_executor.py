"""Behavior contracts for the durable v4 research executor."""

from __future__ import annotations

import sqlite3
import threading
import unittest
from datetime import UTC, datetime
from typing import Any

from stock_mcp.v4_research import V4ResearchCoordinator


class V4ResearchExecutorTest(unittest.TestCase):
    def test_runs_one_claimed_study_step_when_route_is_allowed(self) -> None:
        repository = _Repository(claimed={"study_id": "study-1", "status": "running"})
        observed_clock: list[datetime] = []
        fixed_now = datetime(2026, 8, 11, 16, 20, tzinfo=UTC)
        executor = _Executor({"step": {"sequence": 1}, "complete": False})
        coordinator = V4ResearchCoordinator(
            repository,
            step_executor=executor,
            allowed=lambda now: observed_clock.append(now) or True,
            clock=lambda: fixed_now,
        )

        self.assertTrue(coordinator.run_next_step())
        self.assertEqual([fixed_now], observed_clock)
        self.assertEqual([{"study_id": "study-1", "status": "running"}], executor.calls)
        self.assertEqual([("study-1", {"sequence": 1})], repository.saved_steps)
        self.assertEqual([], repository.completed)
        self.assertEqual([], repository.failed)

    def test_does_not_claim_a_study_when_route_is_not_allowed(self) -> None:
        repository = _Repository(claimed={"study_id": "study-1", "status": "queued"})
        coordinator = V4ResearchCoordinator(
            repository,
            step_executor=_Executor({"step": {"sequence": 1}, "complete": False}),
            allowed=lambda _now: False,
            clock=lambda: datetime(2026, 8, 11, 18, 10, tzinfo=UTC),
        )

        self.assertFalse(coordinator.run_next_step())
        self.assertEqual(0, repository.claims)

    def test_completes_a_study_after_persisting_its_final_step(self) -> None:
        repository = _Repository(claimed={"study_id": "study-1", "status": "running"})
        coordinator = V4ResearchCoordinator(
            repository,
            step_executor=_Executor(
                {
                    "step": {"sequence": 3},
                    "complete": True,
                    "report": {"winner": None, "completeness_status": "complete"},
                }
            ),
            allowed=lambda _now: True,
        )

        self.assertTrue(coordinator.run_next_step())
        self.assertEqual([("study-1", {"sequence": 3})], repository.saved_steps)
        self.assertEqual(
            [("study-1", {"winner": None, "completeness_status": "complete"})],
            repository.completed,
        )

    def test_persists_a_failed_study_when_step_execution_raises(self) -> None:
        repository = _Repository(claimed={"study_id": "study-1", "status": "running"})
        coordinator = V4ResearchCoordinator(
            repository,
            step_executor=_RaisingExecutor(),
            allowed=lambda _now: True,
        )

        self.assertTrue(coordinator.run_next_step())
        self.assertEqual([], repository.saved_steps)
        self.assertEqual([], repository.completed)
        self.assertEqual(1, len(repository.failed))
        self.assertEqual("study-1", repository.failed[0][0])
        self.assertEqual(
            "v4 research step failed (RuntimeError): outcome unavailable",
            repository.failed[0][1],
        )

    def test_background_start_requeues_interrupted_work_before_running(self) -> None:
        repository = _Repository(claimed=None)
        requeued = threading.Event()
        repository.requeued = requeued
        coordinator = V4ResearchCoordinator(
            repository,
            step_executor=_Executor({"step": {"sequence": 1}, "complete": False}),
            allowed=lambda _now: False,
        )

        coordinator.start_background()
        try:
            self.assertTrue(requeued.wait(timeout=1), "background runner did not recover studies")
            self.assertEqual(1, repository.requeue_calls)
        finally:
            coordinator.stop_background()

    def test_start_refuses_to_create_a_queued_study_without_a_step_executor(self) -> None:
        repository = _Repository(claimed=None)
        coordinator = V4ResearchCoordinator(repository)

        with self.assertRaisesRegex(ValueError, "execution is unavailable"):
            coordinator.start_v4_research(manifest_hash="a" * 64, idempotency_key="study-1")
        self.assertEqual([], repository.created)

    def test_transient_claim_error_does_not_kill_the_background_worker(self) -> None:
        repository = _TransientClaimRepository()
        coordinator = V4ResearchCoordinator(
            repository,
            step_executor=_Executor({"step": {"sequence": 1}, "complete": False}),
            allowed=lambda _now: True,
        )
        coordinator.start_background()
        try:
            self.assertTrue(repository.recovered.wait(timeout=1))
            self.assertIsNotNone(coordinator._thread)
            self.assertTrue(coordinator._thread.is_alive())
        finally:
            coordinator.stop_background()

    def test_persistent_storage_error_uses_backoff_and_remains_observable(self) -> None:
        repository = _PersistentClaimErrorRepository()
        coordinator = V4ResearchCoordinator(
            repository,
            step_executor=_Executor({"step": {"sequence": 1}, "complete": False}),
            allowed=lambda _now: True,
        )
        coordinator.start_background()
        try:
            self.assertTrue(repository.called.wait(timeout=1))
            threading.Event().wait(1.05)
            self.assertLessEqual(repository.claims, 3)
            self.assertEqual("OperationalError", coordinator.last_background_error)
        finally:
            coordinator.stop_background()

    def test_transient_step_persistence_error_requeues_instead_of_terminal_failure(self) -> None:
        repository = _TransientSaveRepository()
        coordinator = V4ResearchCoordinator(
            repository,
            step_executor=_Executor({"step": {"sequence": 1}, "complete": False}),
            allowed=lambda _now: True,
        )
        with self.assertRaises(sqlite3.OperationalError):
            coordinator.run_next_step()
        self.assertEqual([], repository.failed)
        self.assertEqual(1, repository.requeue_calls)


class _Repository:
    def __init__(self, *, claimed: dict[str, object] | None) -> None:
        self.claimed = claimed
        self.claims = 0
        self.saved_steps: list[tuple[str, dict[str, object]]] = []
        self.completed: list[tuple[str, dict[str, object]]] = []
        self.failed: list[tuple[str, str]] = []
        self.requeue_calls = 0
        self.requeued: threading.Event | None = None
        self.created: list[dict[str, object]] = []

    def create_v4_study_run(self, **arguments: Any) -> dict[str, object]:
        self.created.append(arguments)
        return {"study_id": "study-1", "status": "queued"}

    def claim_next_v4_study(self) -> dict[str, object] | None:
        self.claims += 1
        claimed, self.claimed = self.claimed, None
        return claimed

    def save_v4_study_step(self, *, study_id: str, step: dict[str, object]) -> None:
        self.saved_steps.append((study_id, step))

    def complete_v4_study(self, *, study_id: str, report: dict[str, object]) -> None:
        self.completed.append((study_id, report))

    def fail_v4_study(self, *, study_id: str, error: str) -> None:
        self.failed.append((study_id, error))

    def requeue_interrupted_v4_studies(self) -> int:
        self.requeue_calls += 1
        if self.requeued is not None:
            self.requeued.set()
        return 0


class _Executor:
    def __init__(self, result: dict[str, object]) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    def __call__(self, study: dict[str, object]) -> dict[str, object]:
        self.calls.append(study)
        return self._result


class _RaisingExecutor:
    def __call__(self, study: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("outcome unavailable")


class _TransientClaimRepository(_Repository):
    def __init__(self) -> None:
        super().__init__(claimed=None)
        self.recovered = threading.Event()

    def claim_next_v4_study(self) -> dict[str, object] | None:
        self.claims += 1
        if self.claims == 1:
            raise sqlite3.OperationalError("database is locked")
        self.recovered.set()
        return None


class _PersistentClaimErrorRepository(_Repository):
    def __init__(self) -> None:
        super().__init__(claimed=None)
        self.called = threading.Event()

    def claim_next_v4_study(self) -> dict[str, object] | None:
        self.claims += 1
        self.called.set()
        raise sqlite3.OperationalError("database is read-only")


class _TransientSaveRepository(_Repository):
    def __init__(self) -> None:
        super().__init__(claimed={"study_id": "study-1", "status": "running"})

    def save_v4_study_step(self, *, study_id: str, step: dict[str, object]) -> None:
        raise sqlite3.OperationalError("database is locked")
