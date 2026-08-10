"""Offline RED contracts for v3 replay dispatch at the application boundary."""

from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace

from stock_mcp.application import StockMcpApplication
from tests.application.test_application_contract import FakeQuoteProvider, FakeRepository


class StrategyReplayV3ApplicationContractTest(unittest.TestCase):
    def test_v3_compare_reads_persisted_replays_without_recomputation(self) -> None:
        """Comparison is an audit read over v3 evidence, never a fresh market replay."""
        gateway = _V3ReplayGateway()
        application = StockMcpApplication(
            FakeRepository(), FakeQuoteProvider(), _V3Registry(), replay=gateway
        )

        result = application.compare_strategy_versions(
            left_version="v3-left",
            right_version="v3-right",
            start=date(2023, 1, 1),
            end=date(2026, 1, 1),
        )

        self.assertTrue(result["ok"])
        self.assertEqual(1, len(gateway.persisted_compare_calls))
        self.assertEqual(0, gateway.recompute_calls)
        self.assertEqual(0, gateway.write_calls)
        self.assertEqual("v3-result-v1", result["data"]["result_hash_schema"])

    def test_list_versions_preserves_lifecycle_and_supersession_evidence(self) -> None:
        application = StockMcpApplication(
            FakeRepository(), FakeQuoteProvider(), _V3Registry(), replay=_V3ReplayGateway()
        )

        result = application.list_strategy_versions()

        self.assertTrue(result["ok"])
        first = result["data"]["versions"][0]
        self.assertEqual(
            {
                "version": "v1-active",
                "lifecycle": "superseded",
                "superseded_by": "v3-left",
            },
            {key: first.get(key) for key in ("version", "lifecycle", "superseded_by")},
        )


class _V3Registry:
    def __init__(self) -> None:
        self._versions = (
            SimpleNamespace(
                version="v1-active",
                status="active",
                parameters={"offensive_limit": 2},
                lifecycle="superseded",
                superseded_by="v3-left",
            ),
            SimpleNamespace(
                version="v3-left",
                status="proposed",
                parameters={"offensive_limit": 3},
                lifecycle="proposed",
                superseded_by=None,
            ),
            SimpleNamespace(
                version="v3-right",
                status="proposed",
                parameters={"offensive_limit": 4},
                lifecycle="proposed",
                superseded_by=None,
            ),
        )

    def get(self, version: str):
        return next((item for item in self._versions if item.version == version), None)

    def list_versions(self):
        return self._versions


class _V3ReplayGateway:
    def __init__(self) -> None:
        self.persisted_compare_calls: list[tuple[str, str, date, date]] = []
        self.recompute_calls = 0
        self.write_calls = 0

    def compare_completed_replays(
        self, left: str, right: str, start: date, end: date
    ) -> dict[str, object]:
        self.persisted_compare_calls.append((left, right, start, end))
        return {
            "left_version": left,
            "right_version": right,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days_compared": 540,
            "left_candidate_count": 11,
            "right_candidate_count": 12,
            "result_hash_schema": "v3-result-v1",
            "daily": [],
        }

    def compare(self, left: str, right: str, start: date, end: date) -> dict[str, object]:
        self.recompute_calls += 1
        raise AssertionError("v3 comparison must not recompute market replay")


if __name__ == "__main__":
    unittest.main()
