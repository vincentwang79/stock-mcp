from __future__ import annotations

import unittest
from datetime import datetime

from stock_mcp.application import StockMcpApplication
from stock_mcp.mcp_tools import build_tool_catalog
from tests.application.test_application_contract import (
    AS_OF,
    TRADE_DATE,
    FakeQuoteProvider,
    FakeReplay,
    FakeRepository,
    FakeStrategyRegistry,
)


class McpApplicationDispatchIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = FakeRepository()
        self.replay = FakeReplay()
        application = StockMcpApplication(
            self.repository,
            FakeQuoteProvider(),
            FakeStrategyRegistry(),
            replay=self.replay,
        )
        self.tools = {tool.name: tool for tool in build_tool_catalog(application)}

    def test_json_date_is_converted_to_the_application_date_type(self) -> None:
        result = self.tools["get_daily_review"].handler(trade_date=TRADE_DATE.isoformat())

        self.assertTrue(result["ok"])
        self.assertEqual(TRADE_DATE.isoformat(), result["data"]["trade_date"])

    def test_watchlist_and_event_field_names_dispatch_without_adapter_drift(self) -> None:
        self.tools["create_watchlist"].handler(name="focus", idempotency_key="create-1")
        added = self.tools["add_watchlist_items"].handler(
            name="focus", symbols=["600000.SH"], idempotency_key="add-1"
        )
        fetched = self.tools["get_watchlist"].handler(name="focus")
        event = self.tools["record_candidate_event"].handler(
            candidate_id="candidate-1",
            event_type="observed",
            detail="held above confirmation",
            idempotency_key="event-1",
        )

        self.assertTrue(added["ok"])
        self.assertEqual(["600000.SH"], fetched["data"]["symbols"])
        self.assertTrue(event["ok"])

    def test_strategy_comparison_dispatches_explicit_walk_forward_range(self) -> None:
        result = self.tools["compare_strategy_versions"].handler(
            left_version="v0.0-active",
            right_version="v0.1-proposed",
            start="2026-08-03",
            end=TRADE_DATE.isoformat(),
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            [("v0.0-active", "v0.1-proposed", TRADE_DATE.replace(day=3), TRADE_DATE)],
            self.replay.calls,
        )

    def test_next_day_response_is_json_serializable_at_the_tool_boundary(self) -> None:
        result = self.tools["check_next_day"].handler(candidate_id="candidate-1")

        self.assertTrue(result["ok"])
        self.assertEqual(
            AS_OF,
            datetime.fromisoformat(result["data"]["as_of"].replace("Z", "+00:00")),
        )


if __name__ == "__main__":
    unittest.main()
