"""Offline RED contracts for bounded, recoverable Sina shadow qualification."""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from stock_mcp import production

SHANGHAI = ZoneInfo("Asia/Shanghai")


class SinaShadowServiceContractTest(unittest.TestCase):
    def test_shadow_coordinator_resumes_a_checkpoint_without_refetching_completed_pages(
        self,
    ) -> None:
        coordinator_type = getattr(production, "SinaShadowCoordinator", None)
        self.assertTrue(
            callable(coordinator_type),
            "Sina shadow needs a durable coordinator that can resume an interrupted run",
        )
        if not callable(coordinator_type):
            return

        store = _CheckpointStore(completed_pages={1: "a" * 64})
        fetcher = _Fetcher()
        coordinator = coordinator_type(store=store, fetch_page=fetcher, page_count=lambda: 2)
        result = coordinator.run(trade_date="2026-08-07")

        self.assertEqual([2], fetcher.pages)
        self.assertEqual("completed", result["status"])
        self.assertEqual({1, 2}, set(store.completed_pages))
        self.assertRegex(str(result["dataset_hash"]), r"^[0-9a-f]{64}$")

    def test_qualification_requires_20_complete_shadow_days_and_never_auto_activates_sina(
        self,
    ) -> None:
        qualify = getattr(production, "evaluate_sina_provider_qualification", None)
        self.assertTrue(
            callable(qualify),
            "Sina requires an explicit offline qualification gate before any manual approval",
        )
        if not callable(qualify):
            return

        result = qualify(
            [
                {
                    "trade_date": f"2026-07-{day:02d}",
                    "status": "completed",
                    "dataset_hash": f"{day:064x}",
                    "fetch_evidence_complete": True,
                    "same_source_history": True,
                    "status_coverage_bps": 10_000,
                    "manual_difference_reviewed": True,
                }
                for day in range(1, 21)
            ]
        )

        self.assertEqual("qualified_for_manual_approval", result["status"])
        self.assertFalse(result["source_active"])
        self.assertTrue(result["manual_approval_required"])

    def test_v4_research_yields_through_the_1620_to_1810_publication_window(self) -> None:
        allowed = getattr(production, "is_v4_research_allowed", None)
        self.assertTrue(
            callable(allowed),
            "v4 research must yield to the bounded post-market publication window",
        )
        if not callable(allowed):
            return

        self.assertFalse(allowed(datetime(2026, 8, 7, 16, 20, tzinfo=SHANGHAI)))
        self.assertFalse(allowed(datetime(2026, 8, 7, 18, 10, tzinfo=SHANGHAI)))
        self.assertTrue(allowed(datetime(2026, 8, 7, 18, 11, tzinfo=SHANGHAI)))


class _CheckpointStore:
    def __init__(self, *, completed_pages: dict[int, str]) -> None:
        self.completed_pages = dict(completed_pages)


class _Fetcher:
    def __init__(self) -> None:
        self.pages: list[int] = []

    def __call__(self, page: int) -> bytes:
        self.pages.append(page)
        return f"offline-page-{page}".encode()


if __name__ == "__main__":
    unittest.main()
