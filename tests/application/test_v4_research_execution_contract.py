"""Offline fail-closed contracts for v4 research starts."""

from __future__ import annotations

import unittest
from datetime import datetime

from stock_mcp.application import StockMcpApplication
from stock_mcp.v4_research import V4ResearchCoordinator


class V4ResearchExecutionApplicationContractTest(unittest.TestCase):
    def test_unknown_manifest_is_rejected_before_a_study_is_queued(self) -> None:
        repository = _ManifestRepository(manifests={})
        result = self._application(repository).start_v4_research(
            manifest_hash="a" * 64,
            idempotency_key="unknown-manifest",
        )

        self.assertFalse(result["ok"])
        self.assertEqual("v4_research_rejected", result["error"]["code"])
        self.assertEqual(0, repository.created)

    def test_incomplete_manifest_is_rejected_before_a_study_is_queued(self) -> None:
        manifest_hash = "b" * 64
        repository = _ManifestRepository(
            manifests={
                manifest_hash: {
                    "schema": "v4-manifest-v1",
                    "manifest_hash": manifest_hash,
                }
            }
        )
        result = self._application(repository).start_v4_research(
            manifest_hash=manifest_hash,
            idempotency_key="incomplete-manifest",
        )

        self.assertFalse(result["ok"])
        self.assertEqual("v4_research_rejected", result["error"]["code"])
        self.assertEqual(0, repository.created)

    def test_known_running_study_report_is_not_ready_not_not_found(self) -> None:
        repository = _ManifestRepository(manifests={})
        repository.runs["study-running"] = {
            "study_id": "study-running",
            "status": "running",
            "report": None,
        }
        result = self._application(repository).get_v4_research_report(study_id="study-running")

        self.assertFalse(result["ok"])
        self.assertEqual("v4_research_not_ready", result["error"]["code"])

    def _application(self, repository: _ManifestRepository) -> StockMcpApplication:
        coordinator = V4ResearchCoordinator(
            repository,
            step_executor=lambda _study: {"step": {}, "complete": False},
            allowed=lambda _now: True,
            clock=lambda: datetime(2026, 8, 12, 9, 0).astimezone(),
        )
        self.addCleanup(coordinator.stop_background)
        return StockMcpApplication(
            repository,
            quote_provider=object(),
            strategy_registry=object(),
            v4_research=coordinator,
        )


class _ManifestRepository:
    def __init__(self, *, manifests: dict[str, dict[str, object]]) -> None:
        self.manifests = manifests
        self.created = 0
        self.runs: dict[str, dict[str, object]] = {}

    def get_v4_dataset_manifest(self, manifest_hash: str) -> dict[str, object] | None:
        manifest = self.manifests.get(manifest_hash)
        return None if manifest is None else dict(manifest)

    def create_v4_study_run(
        self, *, manifest_hash: str, idempotency_key: str, arms: object
    ) -> dict[str, object]:
        self.created += 1
        return {
            "study_id": "v4-study-1",
            "manifest_hash": manifest_hash,
            "idempotency_key": idempotency_key,
            "arms": arms,
            "status": "queued",
        }

    def requeue_interrupted_v4_studies(self) -> int:
        return 0

    def claim_next_v4_study(self) -> None:
        return None

    def save_v4_study_step(self, *, study_id: str, step: dict[str, object]) -> None:
        raise AssertionError("invalid studies must not execute")

    def complete_v4_study(self, *, study_id: str, report: dict[str, object]) -> None:
        raise AssertionError("invalid studies must not execute")

    def fail_v4_study(self, *, study_id: str, error: str) -> None:
        raise AssertionError("invalid studies must not execute")

    def get_v4_study_run(self, study_id: str) -> dict[str, object] | None:
        run = self.runs.get(study_id)
        return None if run is None else dict(run)

    def list_v4_study_arms(self, study_id: str) -> tuple[dict[str, object], ...]:
        return ()

    def list_v4_study_days(self, **_kwargs: object) -> tuple[dict[str, object], ...]:
        return ()


if __name__ == "__main__":
    unittest.main()
