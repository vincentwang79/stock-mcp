"""Strict public MCP schema for private candidate-review events."""

from __future__ import annotations

import unittest
from datetime import date
from typing import Any

from stock_mcp.mcp_tools import build_tool_catalog


class EventService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def record_candidate_event(self, **arguments: object) -> dict[str, object]:
        self.calls.append(dict(arguments))
        return {"ok": True, "data": dict(arguments)}


class CandidateEventInputContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = EventService()
        self.tool = next(
            tool
            for tool in build_tool_catalog(self.service)
            if tool.name == "record_candidate_event"
        )

    def test_event_uses_closed_status_date_optional_price_and_reason(self) -> None:
        result = self.tool.handler(
            candidate_id="candidate-1",
            status="bought",
            event_date="2026-08-07",
            price_1e4=123_400,
            reason="人工记录复盘结果",
            idempotency_key="candidate-event-1",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(1, len(self.service.calls))
        dispatched = self.service.calls[0]
        self.assertEqual("bought", dispatched["status"])
        self.assertEqual(date(2026, 8, 7), dispatched["event_date"])
        self.assertEqual(123_400, dispatched["price_1e4"])
        self.assertEqual("人工记录复盘结果", dispatched["reason"])

    def test_rejects_unknown_status_missing_reason_and_nonpositive_price(self) -> None:
        valid: dict[str, Any] = {
            "candidate_id": "candidate-1",
            "status": "watched",
            "event_date": "2026-08-07",
            "reason": "观察",
            "idempotency_key": "candidate-event-invalid",
        }
        cases = (
            {**valid, "status": "observed"},
            {key: value for key, value in valid.items() if key != "reason"},
            {**valid, "reason": "", "idempotency_key": "candidate-event-empty"},
            {**valid, "price_1e4": 0, "idempotency_key": "candidate-event-zero"},
            {**valid, "price_1e4": -1, "idempotency_key": "candidate-event-negative"},
        )

        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.tool.handler(**arguments)
                self.assertEqual("invalid_input", result["error"]["code"])
        self.assertEqual([], self.service.calls)

    def test_rejects_position_quantity_and_account_fields(self) -> None:
        valid = {
            "candidate_id": "candidate-1",
            "status": "skipped",
            "event_date": "2026-08-07",
            "reason": "不符合个人计划",
            "idempotency_key": "candidate-event-forbidden",
        }
        for forbidden in ("position", "quantity", "account_id"):
            with self.subTest(forbidden=forbidden):
                result = self.tool.handler(**{**valid, forbidden: "must-not-accept"})
                self.assertEqual("invalid_input", result["error"]["code"])
        self.assertEqual([], self.service.calls)


if __name__ == "__main__":
    unittest.main()
