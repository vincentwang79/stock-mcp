"""Offline RED contract for the v4 research application boundary."""

from __future__ import annotations

import unittest
from datetime import date

from stock_mcp.application import StockMcpApplication


class V4ResearchApplicationContractTest(unittest.TestCase):
    def test_application_starts_research_idempotently_and_never_activates_a_winner(self) -> None:
        method = getattr(StockMcpApplication, "start_v4_research", None)
        self.assertTrue(
            callable(method),
            "application needs a dedicated v4 research start operation",
        )
        if not callable(method):
            return

        application = object.__new__(StockMcpApplication)
        application._v4_research = _ResearchCoordinator()  # type: ignore[attr-defined]
        first = method(
            application,
            manifest_hash="a" * 64,
            idempotency_key="v4-research-1",
        )
        repeated = method(
            application,
            manifest_hash="a" * 64,
            idempotency_key="v4-research-1",
        )

        self.assertTrue(first["ok"])
        self.assertEqual(first, repeated)
        self.assertEqual("proposed", first["data"]["status"])
        self.assertFalse(first["data"]["certified"])
        self.assertFalse(first["data"]["active"])
        self.assertEqual(1, application._v4_research.starts)  # type: ignore[attr-defined]


class _ResearchCoordinator:
    def __init__(self) -> None:
        self.starts = 0
        self._results: dict[str, dict[str, object]] = {}

    def start_v4_research(self, *, manifest_hash: str, idempotency_key: str) -> dict[str, object]:
        if idempotency_key not in self._results:
            self.starts += 1
            self._results[idempotency_key] = {
                "replay_id": "v4-replay-1",
                "manifest_hash": manifest_hash,
                "status": "proposed",
                "certified": False,
                "active": False,
                "created_on": date(2026, 8, 11).isoformat(),
            }
        return self._results[idempotency_key]


if __name__ == "__main__":
    unittest.main()
