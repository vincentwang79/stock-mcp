from __future__ import annotations

import json
import unittest
from pathlib import Path

from stock_mcp.mcp_tools import build_tool_catalog


class _MetadataService:
    def __getattr__(self, _name: str):
        return lambda **_arguments: {"ok": True, "data": {}}


class ToolMetadataContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = build_tool_catalog(_MetadataService())

    def test_every_tool_explains_user_intent_and_disallowed_use(self) -> None:
        for tool in self.catalog:
            with self.subTest(tool=tool.name):
                self.assertTrue(tool.title.strip())
                self.assertTrue(tool.description.startswith("Use this when"))
                self.assertIn("Do not use", tool.description)
                self.assertNotIn(f"tool: {tool.name}", tool.description.lower())

    def test_transport_registers_the_rich_description_and_human_title(self) -> None:
        from stock_mcp.mcp_server import _register_tool

        captured: dict[str, object] = {}

        class _Annotations:
            def __init__(self, **values: object) -> None:
                captured["annotations"] = values

        class _Server:
            def tool(self, **values: object):
                captured["registration"] = values

                def register(function: object) -> object:
                    captured["function"] = function
                    return function

                return register

        tool = next(item for item in self.catalog if item.name == "get_latest_daily_review")
        _register_tool(_Server(), tool, _Annotations)

        function = captured["function"]
        self.assertEqual(tool.description, function.__doc__)
        self.assertEqual(tool.title, captured["annotations"]["title"])

    def test_every_public_input_parameter_has_plain_language_docs_and_examples(self) -> None:
        for tool in self.catalog:
            properties = tool.input_model.model_json_schema().get("properties", {})
            for name, schema in properties.items():
                with self.subTest(tool=tool.name, parameter=name):
                    self.assertTrue(schema.get("description"))
                    self.assertTrue(schema.get("examples"))

    def test_golden_prompt_set_covers_direct_indirect_negative_and_confirmation_cases(self) -> None:
        fixture = Path(__file__).with_name("fixtures") / "natural_language_routing_prompts.json"
        prompts = json.loads(fixture.read_text(encoding="utf-8"))
        tool_names = {tool.name for tool in self.catalog}

        self.assertGreaterEqual(len(prompts), 12)
        self.assertEqual({"direct", "indirect", "negative"}, {item["category"] for item in prompts})
        for item in prompts:
            with self.subTest(prompt=item["prompt"]):
                expected = item["expected_tool"]
                self.assertTrue(expected is None or expected in tool_names)
                self.assertIsInstance(item["requires_confirmation"], bool)
                if item["requires_confirmation"]:
                    self.assertIsNotNone(expected)
                    tool = next(tool for tool in self.catalog if tool.name == expected)
                    self.assertFalse(tool.annotations["readOnlyHint"])

    def test_every_write_tool_requires_an_explicit_confirmed_user_request(self) -> None:
        for tool in self.catalog:
            if tool.annotations["readOnlyHint"]:
                continue
            with self.subTest(tool=tool.name):
                description = tool.description.lower()
                self.assertIn("explicit", description)
                self.assertIn("confirm", description)


if __name__ == "__main__":
    unittest.main()
