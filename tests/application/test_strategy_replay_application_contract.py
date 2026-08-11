"""Offline contracts for strategy-governance replay use cases."""

from __future__ import annotations

import unittest
from datetime import date

from stock_mcp.application import StockMcpApplication
from tests.application.test_application_contract import (
    FakeQuoteProvider,
    FakeRepository,
    FakeStrategyRegistry,
)


class _ReplayGateway:
    """Fixed, in-memory evidence fixture; it never reads market data."""

    def __init__(self) -> None:
        self.start_calls: list[dict[str, object]] = []
        self.compare_calls: list[tuple[str, str, date, date]] = []
        self._requests: dict[str, tuple[str, date, date]] = {}
        self._replays = {
            "replay-1": {
                "replay_id": "replay-1",
                "version": "v0.1-proposed",
                "start_date": "2023-01-03",
                "end_date": "2026-01-02",
                "status": "queued",
                "certified": False,
            }
        }

    def start_strategy_replay(
        self, *, version: str, start_date: date, end_date: date, idempotency_key: str
    ) -> dict[str, object]:
        request = (version, start_date, end_date)
        existing = self._requests.get(idempotency_key)
        if existing is not None and existing != request:
            raise ValueError("idempotency key cannot be reused for a different request")
        self._requests[idempotency_key] = request
        self.start_calls.append(
            {
                "version": version,
                "start_date": start_date,
                "end_date": end_date,
                "idempotency_key": idempotency_key,
            }
        )
        return dict(self._replays["replay-1"])

    def get_strategy_replay(self, *, replay_id: str) -> dict[str, object] | None:
        replay = self._replays.get(replay_id)
        return None if replay is None else dict(replay)

    def list_strategy_replays(
        self, *, version: str | None = None, limit: int = 20
    ) -> tuple[dict[str, object], ...]:
        values = tuple(self._replays.values())
        if version is not None:
            values = tuple(replay for replay in values if replay["version"] == version)
        return tuple(dict(replay) for replay in values[:limit])

    def get_strategy_replay_days(
        self, *, replay_id: str, after_trade_date: date | None = None, limit: int = 20
    ) -> tuple[dict[str, object], ...] | None:
        if replay_id not in self._replays:
            return None
        days = (
            {"trade_date": "2023-01-03", "status": "completed"},
            {"trade_date": "2023-01-04", "status": "completed"},
        )
        if after_trade_date is not None:
            days = tuple(day for day in days if day["trade_date"] > after_trade_date.isoformat())
        return days[:limit]

    def certify_strategy_replay(
        self, *, replay_id: str, confirmed: bool, idempotency_key: str
    ) -> dict[str, object] | None:
        if not confirmed:
            raise ValueError("confirmation required")
        replay = self._replays.get(replay_id)
        if replay is None:
            return None
        replay["certified"] = True
        return dict(replay)

    def compare(self, left: str, right: str, start: date, end: date) -> dict[str, object]:
        self.compare_calls.append((left, right, start, end))
        return {"left_version": left, "right_version": right, "days_compared": 0}


class StrategyReplayApplicationContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = _ReplayGateway()
        self.application = StockMcpApplication(
            FakeRepository(),
            FakeQuoteProvider(),
            FakeStrategyRegistry(),
            replay=self.gateway,
        )

    def test_same_version_comparison_is_rejected_without_replaying(self) -> None:
        result = self.application.compare_strategy_versions(
            left_version="v0.1-proposed",
            right_version="v0.1-proposed",
            start=date(2023, 1, 3),
            end=date(2026, 1, 2),
        )

        self.assertFalse(result["ok"])
        self.assertEqual("strategy_comparison_invalid", result["error"]["code"])
        self.assertEqual([], self.gateway.compare_calls)

    def test_start_replay_returns_the_original_job_for_the_same_idempotency_request(self) -> None:
        start = getattr(self.application, "start_strategy_replay", None)
        self.assertTrue(callable(start), "application must expose start_strategy_replay")
        if not callable(start):
            return

        first = start(
            version="v0.1-proposed",
            start_date=date(2023, 1, 3),
            end_date=date(2026, 1, 2),
            idempotency_key="replay-start-1",
        )
        second = start(
            version="v0.1-proposed",
            start_date=date(2023, 1, 3),
            end_date=date(2026, 1, 2),
            idempotency_key="replay-start-1",
        )

        self.assertEqual(first, second)
        self.assertTrue(first["ok"])
        self.assertEqual("queued", first["data"]["status"])
        self.assertEqual("replay-1", first["data"]["replay_id"])

        conflict = start(
            version="v0.1-proposed",
            start_date=date(2023, 1, 4),
            end_date=date(2026, 1, 2),
            idempotency_key="replay-start-1",
        )
        self.assertFalse(conflict["ok"])
        self.assertEqual("idempotency_conflict", conflict["error"]["code"])

    def test_start_replay_rejects_an_active_strategy_before_dispatch(self) -> None:
        result = self.application.start_strategy_replay(
            version="v0.0-active",
            start_date=date(2023, 1, 3),
            end_date=date(2026, 1, 2),
            idempotency_key="active-version",
        )

        self.assertFalse(result["ok"])
        self.assertEqual("strategy_replay_rejected", result["error"]["code"])
        self.assertEqual([], self.gateway.start_calls)

    def test_replay_reads_are_paged_and_certification_requires_confirmation(self) -> None:
        get_replay = getattr(self.application, "get_strategy_replay", None)
        list_replays = getattr(self.application, "list_strategy_replays", None)
        get_days = getattr(self.application, "get_strategy_replay_days", None)
        certify = getattr(self.application, "certify_strategy_replay", None)
        for name, method in {
            "get_strategy_replay": get_replay,
            "list_strategy_replays": list_replays,
            "get_strategy_replay_days": get_days,
            "certify_strategy_replay": certify,
        }.items():
            self.assertTrue(callable(method), f"application must expose {name}")
        if not all(callable(method) for method in (get_replay, list_replays, get_days, certify)):
            return

        replay = get_replay(replay_id="replay-1")
        listed = list_replays(version="v0.1-proposed", limit=20)
        days = get_days(replay_id="replay-1", after_trade_date=date(2023, 1, 3), limit=20)
        refused = certify(replay_id="replay-1", confirmed=False, idempotency_key="replay-certify-1")

        self.assertTrue(replay["ok"])
        self.assertEqual(["replay-1"], [item["replay_id"] for item in listed["data"]["replays"]])
        self.assertEqual(["2023-01-04"], [item["trade_date"] for item in days["data"]["days"]])
        self.assertFalse(refused["ok"])
        self.assertEqual("confirmation_required", refused["error"]["code"])

        certified = certify(
            replay_id="replay-1", confirmed=True, idempotency_key="replay-certify-2"
        )
        same_certification = certify(
            replay_id="replay-1", confirmed=True, idempotency_key="replay-certify-2"
        )
        self.assertTrue(certified["ok"])
        self.assertTrue(certified["data"]["certified"])
        self.assertEqual(certified, same_certification)


if __name__ == "__main__":
    unittest.main()
