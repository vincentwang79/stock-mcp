"""Offline RED schemas for v4 research and Sina qualification MCP tools."""

from __future__ import annotations

import unittest
from typing import Any

from stock_mcp.mcp_tools import build_tool_catalog


class V4ResearchMcpDtoContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = _OfflineService()
        self.tools = {tool.name: tool for tool in build_tool_catalog(self.service)}

    def test_catalog_exposes_research_and_provider_qualification_tools_with_safe_annotations(
        self,
    ) -> None:
        required = {
            "start_v4_research": (False, True, False),
            "get_v4_research": (True, False, False),
            "get_v4_research_arms": (True, False, False),
            "get_v4_research_days": (True, False, False),
            "get_v4_research_report": (True, False, False),
            "get_provider_qualification": (True, False, False),
            "activate_provider_source": (False, True, True),
        }
        self.assertEqual([], [name for name in required if name not in self.tools])
        for name, (read_only, needs_idempotency, destructive) in required.items():
            tool = self.tools[name]
            self.assertEqual(read_only, _annotation(tool.annotations, "readOnlyHint"), name)
            self.assertEqual(destructive, _annotation(tool.annotations, "destructiveHint"), name)
            self.assertFalse(_annotation(tool.annotations, "openWorldHint"), name)
            self.assertTrue(_annotation(tool.annotations, "idempotentHint"), name)
            fields = tool.input_model.model_json_schema().get("properties", {})
            if needs_idempotency:
                self.assertIn("idempotency_key", fields)
            self.assertNotIn("activate", " ".join(fields).lower())

    def test_research_dto_preserves_manifest_outcome_and_statistics_provenance(self) -> None:
        tool = self.tools.get("get_v4_research")
        self.assertIsNotNone(tool, "v4 research replay must have an audit DTO")
        if tool is None:
            return
        schema = str(tool.output_model.model_json_schema())
        required = (
            "manifest_hash",
            "outcome_hash_schema",
            "outcome_through",
            "bootstrap_method",
            "multiple_testing_method",
            "winner",
            "completeness_status",
        )
        self.assertEqual([], [field for field in required if field not in schema])

    def test_start_tool_returns_the_same_persisted_research_run_for_same_idempotency_key(
        self,
    ) -> None:
        tool = self.tools.get("start_v4_research")
        self.assertIsNotNone(tool, "v4 research start must be public and durable")
        if tool is None:
            return
        first = tool.handler(manifest_hash="a" * 64, idempotency_key="research-1")
        repeated = tool.handler(manifest_hash="a" * 64, idempotency_key="research-1")
        self.assertTrue(first["ok"])
        self.assertEqual(first, repeated)
        self.assertEqual(1, self.service.writes)


def _annotation(value: Any, name: str) -> Any:
    return value[name] if isinstance(value, dict) else getattr(value, name)


class _OfflineService:
    def __init__(self) -> None:
        self.writes = 0
        self._runs: dict[str, dict[str, object]] = {}

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        def handler(**arguments: Any) -> dict[str, object]:
            if name == "start_v4_research":
                key = arguments["idempotency_key"]
                if key not in self._runs:
                    self.writes += 1
                    self._runs[key] = {"replay_id": "v4-1", "status": "proposed"}
                return {"ok": True, "data": self._runs[key]}
            return {"ok": True, "data": {}}

        return handler


if __name__ == "__main__":
    unittest.main()
