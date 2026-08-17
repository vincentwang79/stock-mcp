"""Read-only MCP contracts for the Research Program v5 ledger."""

from __future__ import annotations

import unittest

from stock_mcp.mcp_tools import build_tool_catalog


class _Service:
    def list_research_hypotheses(self, **_arguments):
        return {"ok": True, "data": {"hypotheses": []}}

    def get_research_hypothesis(self, **_arguments):
        return {"ok": False, "error": {"code": "not_found", "message": "missing"}}

    def get_research_forward_report(self, **_arguments):
        return {"ok": True, "data": {"schema": "research-forward-report-v1"}}

    def __getattr__(self, _name):
        return lambda **_arguments: {"ok": True, "data": {}}


class ResearchProgramV5McpContractTest(unittest.TestCase):
    def test_catalog_exposes_only_read_only_research_registry_queries(self) -> None:
        catalog = {item.name: item for item in build_tool_catalog(_Service())}
        self.assertIn("list_research_hypotheses", catalog)
        self.assertIn("get_research_hypothesis", catalog)
        self.assertIn("get_research_forward_report", catalog)
        self.assertTrue(catalog["list_research_hypotheses"].annotations["readOnlyHint"])
        self.assertTrue(catalog["get_research_hypothesis"].annotations["readOnlyHint"])
        self.assertTrue(catalog["get_research_forward_report"].annotations["readOnlyHint"])
        self.assertFalse(catalog["list_research_hypotheses"].annotations["openWorldHint"])
        report = catalog["get_research_forward_report"].handler(
            hypothesis_id="no-recent-limit-up-v1", horizon_sessions=20
        )
        self.assertTrue(report["ok"])
        self.assertNotIn("register_research_hypothesis", catalog)
        self.assertNotIn("promote_research_hypothesis", catalog)


if __name__ == "__main__":
    unittest.main()
