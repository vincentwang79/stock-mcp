"""MCP boundary contract for the fixed strategy-replay tool set."""

from __future__ import annotations

import unittest
from collections.abc import Mapping
from typing import Any

from stock_mcp.mcp_tools import build_tool_catalog

REPLAY_TOOLS = frozenset(
    {
        "start_strategy_replay",
        "get_strategy_replay",
        "list_strategy_replays",
        "get_strategy_replay_days",
        "certify_strategy_replay",
    }
)
READ_TOOLS = frozenset({"get_strategy_replay", "list_strategy_replays", "get_strategy_replay_days"})


class _ReplayApplication:
    """Network-free MCP dispatch fixture with recorded, immutable evidence."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def start_strategy_replay(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(("start_strategy_replay", arguments))
        return {"ok": True, "data": _replay("queued")}

    def get_strategy_replay(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(("get_strategy_replay", arguments))
        return {"ok": True, "data": _replay("running")}

    def list_strategy_replays(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(("list_strategy_replays", arguments))
        return {"ok": True, "data": {"replays": [_replay("completed")]}}

    def get_strategy_replay_days(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(("get_strategy_replay_days", arguments))
        return {
            "ok": True,
            "data": {
                "replay_id": arguments["replay_id"],
                "days": [{"trade_date": "2023-01-03", "status": "completed"}],
            },
        }

    def certify_strategy_replay(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(("certify_strategy_replay", arguments))
        if not arguments["confirmed"]:
            return {
                "ok": False,
                "error": {
                    "code": "confirmation_required",
                    "message": "explicit confirmation is required",
                },
            }
        return {"ok": True, "data": _replay("completed", certified=True)}


def _replay(status: str, *, certified: bool = False) -> dict[str, Any]:
    return {
        "replay_id": "replay-1",
        "version": "v0.1-proposed",
        "start_date": "2023-01-03",
        "end_date": "2026-01-02",
        "status": status,
        "certified": certified,
    }


def _value(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value[name]
    return getattr(value, name)


def _assert_closed_object_schemas(test: unittest.TestCase, schema: Any) -> None:
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            test.assertIs(
                False,
                schema.get("additionalProperties"),
                "object schema must reject undeclared fields: "
                f"{schema.get('title', '<anonymous>')}",
            )
        for value in schema.values():
            _assert_closed_object_schemas(test, value)
    elif isinstance(schema, list):
        for value in schema:
            _assert_closed_object_schemas(test, value)


class StrategyReplayToolContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = _ReplayApplication()
        self.tools = {tool.name: tool for tool in build_tool_catalog(self.service)}

    def test_five_replay_tools_are_frozen_with_closed_pydantic_schemas(self) -> None:
        self.assertTrue(self.tools.keys() >= REPLAY_TOOLS, REPLAY_TOOLS - self.tools.keys())
        for name in REPLAY_TOOLS:
            with self.subTest(name=name):
                tool = self.tools[name]
                self.assertEqual(name in READ_TOOLS, _value(tool.annotations, "readOnlyHint"))
                self.assertFalse(_value(tool.annotations, "destructiveHint"))
                self.assertTrue(_value(tool.annotations, "idempotentHint"))
                self.assertFalse(_value(tool.annotations, "openWorldHint"))
                _assert_closed_object_schemas(self, tool.input_model.model_json_schema())
                _assert_closed_object_schemas(self, tool.output_model.model_json_schema())

    def test_replay_inputs_preserve_dates_paging_limits_and_explicit_confirmation(self) -> None:
        self.assertTrue(self.tools.keys() >= REPLAY_TOOLS, REPLAY_TOOLS - self.tools.keys())
        start = self.tools["start_strategy_replay"].handler(
            version="v0.1-proposed",
            start_date="2023-01-03",
            end_date="2026-01-02",
            idempotency_key="start-1",
        )
        days = self.tools["get_strategy_replay_days"].handler(
            replay_id="replay-1", after_trade_date="2023-01-02", limit=20
        )
        invalid_page = self.tools["get_strategy_replay_days"].handler(
            replay_id="replay-1", limit=51
        )
        refused = self.tools["certify_strategy_replay"].handler(
            replay_id="replay-1", confirmed=False, idempotency_key="certify-1"
        )

        self.assertTrue(start["ok"])
        self.assertTrue(days["ok"])
        self.assertEqual("2023-01-03", self.service.calls[0][1]["start_date"].isoformat())
        self.assertEqual("2023-01-02", self.service.calls[1][1]["after_trade_date"].isoformat())
        self.assertFalse(invalid_page["ok"])
        self.assertEqual("invalid_input", invalid_page["error"]["code"])
        self.assertFalse(refused["ok"])
        self.assertEqual("confirmation_required", refused["error"]["code"])

    def test_replay_output_status_schema_is_limited_to_lifecycle_states(self) -> None:
        self.assertTrue(self.tools.keys() >= REPLAY_TOOLS, REPLAY_TOOLS - self.tools.keys())
        schema = self.tools["get_strategy_replay"].output_model.model_json_schema()
        serialized = str(schema)
        for status in ("queued", "running", "completed", "failed"):
            self.assertIn(status, serialized)


if __name__ == "__main__":
    unittest.main()
