"""Durable public orchestration boundary for preregistered v4 studies."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any, Protocol

from .strategy import build_v4_research_arms


class V4ResearchRepository(Protocol):
    """Persistence seam owned by the v4 research job repository adapter.

    Claiming must atomically transition a queued study to running.  The executor
    records exactly one durable step after each claim, then optionally completes
    the study with its final report.  A startup recovery operation must make an
    queued and interrupted running studies claimable again without discarding
    prior steps.
    """

    def create_v4_study_run(
        self, *, manifest_hash: str, idempotency_key: str, arms: Any
    ) -> dict[str, object]: ...

    def claim_next_v4_study(self) -> dict[str, object] | None: ...

    def save_v4_study_step(self, *, study_id: str, step: dict[str, object]) -> None: ...

    def complete_v4_study(self, *, study_id: str, report: dict[str, object]) -> None: ...

    def fail_v4_study(self, *, study_id: str, error: str) -> None: ...

    def requeue_interrupted_v4_studies(self) -> int: ...


V4StudyStepExecutor = Callable[[dict[str, object]], Mapping[str, object]]
V4ResearchAllowed = Callable[[datetime], bool]


class V4ResearchCoordinator:
    """Coordinate durable v4 study work without knowing its storage schema.

    ``step_executor`` returns ``{"step": mapping, "complete": bool}`` for
    each claimed study.  A completed result additionally supplies ``report``.
    Repository operations are deliberately narrow so the storage adapter owns
    all transaction and schema decisions.
    """

    def __init__(
        self,
        database: V4ResearchRepository | Any,
        *,
        step_executor: V4StudyStepExecutor | None = None,
        allowed: V4ResearchAllowed | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._step_executor = step_executor
        self._allowed = allowed or (lambda _now: False)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._thread_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start_v4_research(self, *, manifest_hash: str, idempotency_key: str) -> dict[str, object]:
        self._ensure_execution_ready()
        arms = build_v4_research_arms(created_at=datetime.now().astimezone().isoformat())
        study = self._database.create_v4_study_run(
            manifest_hash=manifest_hash, idempotency_key=idempotency_key, arms=arms
        )
        self.start_background()
        self._wake.set()
        return study

    def requeue_interrupted(self) -> int:
        """Recover queued/running study work through the repository adapter."""

        self._ensure_execution_ready()
        return self._database.requeue_interrupted_v4_studies()

    def start_background(self) -> None:
        """Start one daemon worker after recovering interrupted work."""

        self._ensure_execution_ready()
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._database.requeue_interrupted_v4_studies()
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="stock-mcp-v4-research",
                daemon=True,
            )
            self._thread.start()

    def stop_background(self) -> None:
        """Request a clean worker stop; intended for runtime shutdown and tests."""

        self._stop.set()
        self._wake.set()
        with self._thread_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)

    def run_next_step(self) -> bool:
        """Claim and persist at most one v4 study step.

        The coordinator intentionally delegates all schedule boundaries to
        ``allowed(clock())``; it contains no hard-coded market-time window.
        """

        self._ensure_execution_ready()
        if not self._allowed(self._clock()):
            return False
        study = self._database.claim_next_v4_study()
        if study is None:
            return False
        study_id = str(study.get("study_id", ""))
        if not study_id:
            raise ValueError("claimed v4 study is missing study_id")
        try:
            result = self._step_executor(study)
            step = result.get("step")
            complete = result.get("complete")
            if not isinstance(step, Mapping) or not isinstance(complete, bool):
                raise ValueError("v4 study step result is invalid")
            self._database.save_v4_study_step(study_id=study_id, step=dict(step))
            if complete:
                report = result.get("report")
                if not isinstance(report, Mapping):
                    raise ValueError("completed v4 study requires a report")
                self._database.complete_v4_study(study_id=study_id, report=dict(report))
        except Exception as error:
            self._database.fail_v4_study(
                study_id=study_id,
                error=f"v4 research step failed ({type(error).__name__})",
            )
        return True

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            if self.run_next_step():
                continue
            self._wake.wait(timeout=0.25)
            self._wake.clear()

    def _ensure_execution_ready(self) -> None:
        if self._step_executor is None:
            raise ValueError("v4 research execution is unavailable without a step executor")
        required = (
            "claim_next_v4_study",
            "save_v4_study_step",
            "complete_v4_study",
            "fail_v4_study",
            "requeue_interrupted_v4_studies",
        )
        missing = tuple(
            name for name in required if not callable(getattr(self._database, name, None))
        )
        if missing:
            raise TypeError("v4 research repository is missing: " + ", ".join(missing))

    def get_v4_research(self, *, study_id: str) -> dict[str, object] | None:
        return self._database.get_v4_study_run(study_id)

    def get_v4_research_arms(self, *, study_id: str) -> tuple[dict[str, object], ...]:
        return self._database.list_v4_study_arms(study_id)

    def get_v4_research_days(
        self, *, study_id: str, arm_id: str, after_signal_date: date | None, limit: int
    ) -> tuple[dict[str, object], ...]:
        return self._database.list_v4_study_days(
            study_id=study_id,
            arm_id=arm_id,
            after_signal_date=after_signal_date,
            limit=limit,
        )

    def get_v4_research_report(self, *, study_id: str) -> dict[str, object] | None:
        run = self._database.get_v4_study_run(study_id)
        if run is None or run.get("report") is None:
            return None
        return dict(run["report"])
