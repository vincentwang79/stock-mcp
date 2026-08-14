"""Read-only MCP contracts for the Research Program v5 ledger."""

from __future__ import annotations

import unittest

from stock_mcp.mcp_tools import build_tool_catalog


class _Service:
    def list_research_hypotheses(self, **_arguments):
        return {"ok": True, "data": {"hypotheses": []}}

    def get_research_hypothesis(self, **_arguments):
        return {"ok": False, "error": {"code": "not_found", "message": "missing"}}

    def __getattr__(self, _name):
        return lambda **_arguments: {"ok": True, "data": {}}


class ResearchProgramV5McpContractTest(unittest.TestCase):
    def test_catalog_exposes_only_read_only_research_registry_queries(self) -> None:
        catalog = {item.name: item for item in build_tool_catalog(_Service())}
        self.assertIn("list_research_hypotheses", catalog)
        self.assertIn("get_research_hypothesis", catalog)
        self.assertTrue(catalog["list_research_hypotheses"].annotations["readOnlyHint"])
        self.assertTrue(catalog["get_research_hypothesis"].annotations["readOnlyHint"])
        self.assertFalse(catalog["list_research_hypotheses"].annotations["openWorldHint"])
        self.assertNotIn("register_research_hypothesis", catalog)
        self.assertNotIn("promote_research_hypothesis", catalog)


if __name__ == "__main__":
    unittest.main()
