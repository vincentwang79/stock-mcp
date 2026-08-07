from __future__ import annotations

import unittest
from collections.abc import Mapping
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

EXPECTED_TOOL_NAMES = frozenset(
    {
        "get_daily_review",
        "get_candidate",
        "check_next_day",
        "list_watchlists",
        "get_watchlist",
        "create_watchlist",
        "add_watchlist_items",
        "remove_watchlist_items",
        "record_candidate_event",
        "record_review_note",
        "get_review_history",
        "list_strategy_versions",
        "compare_strategy_versions",
        "create_strategy_proposal",
        "activate_strategy_version",
    }
)

READ_TOOL_NAMES = frozenset(
    {
        "get_daily_review",
        "get_candidate",
        "check_next_day",
        "list_watchlists",
        "get_watchlist",
        "get_review_history",
        "list_strategy_versions",
        "compare_strategy_versions",
    }
)
WRITE_TOOL_NAMES = EXPECTED_TOOL_NAMES - READ_TOOL_NAMES
FORBIDDEN_INPUT_TERMS = ("balance", "position", "broker", "order", "credential")


class FakeApplicationService:
    """A network-free service spy used to validate catalog dispatch only."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._idempotent_results: dict[tuple[str, str], dict[str, Any]] = {}
        self.persisted_write_counts: dict[str, int] = {}

    def __getattr__(self, tool_name: str):
        if tool_name not in EXPECTED_TOOL_NAMES:
            raise AttributeError(tool_name)

        def dispatch(**arguments: Any) -> dict[str, Any]:
            self.calls.append((tool_name, arguments))
            if tool_name == "get_candidate" and arguments.get("candidate_id") == "missing":
                return {
                    "ok": False,
                    "error": {
                        "code": "candidate_not_found",
                        "message": "candidate does not exist",
                    },
                }
            if tool_name == "get_daily_review" and str(arguments.get("trade_date")) == "2099-01-01":
                return {
                    "ok": False,
                    "error": {"code": "daily_review_not_found", "message": "no published review"},
                }
            if tool_name == "activate_strategy_version" and not arguments.get("confirmed"):
                return {
                    "ok": False,
                    "error": {
                        "code": "confirmation_required",
                        "message": "explicit confirmation is required",
                    },
                }
            idempotency_key = arguments.get("idempotency_key")
            if idempotency_key:
                key = (tool_name, idempotency_key)
                if key not in self._idempotent_results:
                    self.persisted_write_counts[tool_name] = (
                        self.persisted_write_counts.get(tool_name, 0) + 1
                    )
                    self._idempotent_results[key] = {
                        "ok": True,
                        "data": {"write_count": 1, "tool": tool_name},
                    }
                return self._idempotent_results[key]
            if tool_name == "get_daily_review":
                return {"ok": True, "data": {"candidates": []}}
            if tool_name == "check_next_day":
                return {
                    "ok": True,
                    "data": {
                        "candidate_id": arguments["candidate_id"],
                        "source": "akshare",
                        "as_of": "2026-08-07T09:31:00+08:00",
                    },
                }
            return {"ok": True, "data": {"tool": tool_name}}

        return dispatch


def _value(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value[name]
    return getattr(value, name)


def _schema_properties(model: Any) -> dict[str, Any]:
    """Accept Pydantic v2 models while keeping this contract SDK-independent."""
    schema = model.model_json_schema()
    return schema.get("properties", {})


class ToolCatalogContractTests(unittest.TestCase):
    def setUp(self) -> None:
        from stock_mcp.mcp_tools import build_tool_catalog

        self.service = FakeApplicationService()
        self.catalog = build_tool_catalog(self.service)
        self.tools = {tool.name: tool for tool in self.catalog}

    def test_catalog_contains_exactly_the_public_tools_with_pydantic_dtos(self) -> None:
        self.assertEqual(EXPECTED_TOOL_NAMES, frozenset(self.tools))
        self.assertEqual(len(EXPECTED_TOOL_NAMES), len(self.catalog))
        for tool in self.catalog:
            self.assertTrue(callable(tool.handler), tool.name)
            self.assertIsNotNone(tool.input_model, tool.name)
            self.assertIsNotNone(tool.output_model, tool.name)
            self.assertIsInstance(_schema_properties(tool.input_model), dict, tool.name)
            self.assertIsInstance(_schema_properties(tool.output_model), dict, tool.name)

    def test_annotations_mark_read_write_and_external_quote_boundaries(self) -> None:
        for name, tool in self.tools.items():
            annotations = tool.annotations
            self.assertEqual(name in READ_TOOL_NAMES, _value(annotations, "readOnlyHint"), name)
            self.assertEqual(
                name in {"remove_watchlist_items", "activate_strategy_version"},
                _value(annotations, "destructiveHint"),
                name,
            )
            self.assertTrue(_value(annotations, "idempotentHint"), name)
            self.assertEqual(name == "check_next_day", _value(annotations, "openWorldHint"), name)

    def test_write_input_schemas_exclude_trading_and_credential_parameters(self) -> None:
        for name in WRITE_TOOL_NAMES:
            properties = _schema_properties(self.tools[name].input_model)
            combined = " ".join(properties).lower()
            for forbidden in FORBIDDEN_INPUT_TERMS:
                self.assertNotIn(forbidden, combined, f"{name} must not accept {forbidden}")
            self.assertIn("idempotency_key", properties, name)

    def test_business_errors_and_zero_candidates_are_structured_results(self) -> None:
        missing_candidate = self.tools["get_candidate"].handler(candidate_id="missing")
        self.assertFalse(_value(missing_candidate, "ok"))
        self.assertEqual("candidate_not_found", _value(_value(missing_candidate, "error"), "code"))

        missing_review = self.tools["get_daily_review"].handler(trade_date="2099-01-01")
        self.assertFalse(_value(missing_review, "ok"))
        self.assertEqual("daily_review_not_found", _value(_value(missing_review, "error"), "code"))

        empty_review = self.tools["get_daily_review"].handler(trade_date="2026-08-07")
        self.assertTrue(_value(empty_review, "ok"))
        self.assertEqual([], _value(_value(empty_review, "data"), "candidates"))

    def test_confirmation_idempotency_and_explicit_next_day_quote_fetch(self) -> None:
        catalog_build_calls = list(self.service.calls)
        self.assertEqual([], catalog_build_calls, "building the catalog must not fetch a quote")

        unconfirmed = self.tools["activate_strategy_version"].handler(
            version="v0.1-proposed",
            confirmed=False,
            idempotency_key="activation-1",
        )
        self.assertFalse(_value(unconfirmed, "ok"))
        self.assertEqual("confirmation_required", _value(_value(unconfirmed, "error"), "code"))

        created_once = self.tools["create_watchlist"].handler(
            name="focus",
            idempotency_key="watchlist-1",
        )
        created_twice = self.tools["create_watchlist"].handler(
            name="focus",
            idempotency_key="watchlist-1",
        )
        self.assertEqual(_value(created_once, "data"), _value(created_twice, "data"))
        create_calls = [call for call in self.service.calls if call[0] == "create_watchlist"]
        self.assertEqual(
            2, len(create_calls), "the application service owns idempotent persistence"
        )
        self.assertEqual(1, self.service.persisted_write_counts["create_watchlist"])

        before_quote = len([call for call in self.service.calls if call[0] == "check_next_day"])
        quote = self.tools["check_next_day"].handler(candidate_id="candidate-1")
        after_quote = len([call for call in self.service.calls if call[0] == "check_next_day"])
        self.assertEqual(before_quote + 1, after_quote)
        self.assertTrue(_value(quote, "ok"))
        self.assertEqual("akshare", _value(_value(quote, "data"), "source"))
        self.assertIn("as_of", _value(quote, "data"))

    def test_server_factory_accepts_the_catalog_service(self) -> None:
        from stock_mcp.mcp_server import create_server

        server = create_server(self.service)
        self.assertIsNotNone(server)

    def test_real_sdk_registration_preserves_strict_input_schema(self) -> None:
        from stock_mcp.mcp_server import create_server

        server = create_server(self.service)
        manager = getattr(server, "_tool_manager", None)
        if manager is None:
            self.skipTest("MCP SDK runtime is not installed")

        tool = manager._tools["create_watchlist"]
        self.assertFalse(tool.parameters["additionalProperties"])
        self.assertEqual(80, tool.parameters["properties"]["name"]["maxLength"])
        with self.assertRaisesRegex(ToolError, "Extra inputs are not permitted"):
            import asyncio

            asyncio.run(
                server.call_tool(
                    "create_watchlist",
                    {
                        "name": "focus",
                        "idempotency_key": "strict-extra",
                        "broker_credential": "must-be-rejected",
                    },
                )
            )


if __name__ == "__main__":
    unittest.main()
