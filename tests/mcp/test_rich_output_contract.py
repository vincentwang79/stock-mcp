from __future__ import annotations

import json
import unittest

from stock_mcp.mcp_tools import build_tool_catalog


class McpRichOutputContractTest(unittest.TestCase):
    def test_core_read_tools_publish_distinct_closed_data_schemas(self) -> None:
        catalog = {definition.name: definition for definition in build_tool_catalog(object())}
        expected_fields = {
            "get_daily_review": ("trade_date", "source", "market_regime", "candidates"),
            "get_candidate": (
                "candidate_id",
                "source",
                "source_timestamp",
                "market_regime",
                "industry_context",
            ),
            "get_review_history": ("reviews",),
            "list_strategy_versions": ("versions",),
        }

        output_models = {catalog[name].output_model for name in expected_fields}
        self.assertEqual(len(expected_fields), len(output_models))
        for name, fields in expected_fields.items():
            with self.subTest(tool=name):
                output_schema = catalog[name].output_model.model_json_schema()
                serialized = json.dumps(output_schema, sort_keys=True)
                self.assertNotIn('"additionalProperties": true', serialized)
                for field in fields:
                    self.assertIn(field, serialized)


if __name__ == "__main__":
    unittest.main()
