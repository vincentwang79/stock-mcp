"""Offline RED schemas for the public v3 replay evidence DTOs."""

from __future__ import annotations

import unittest
from typing import Any

from pydantic import ValidationError

from stock_mcp.mcp_tools import build_tool_catalog


class StrategyReplayV3DtoContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = {tool.name: tool for tool in build_tool_catalog(_Application())}

    def test_replay_and_day_dtos_publish_the_frozen_v3_evidence_fields(self) -> None:
        """MCP exposes all hashes, warmup, outcome and industry-classification provenance."""
        replay_schema = str(self.tools["get_strategy_replay"].output_model.model_json_schema())
        days_schema = str(self.tools["get_strategy_replay_days"].output_model.model_json_schema())

        replay_fields = (
            "pipeline_version",
            "input_hash_schema",
            "result_hash_schema",
            "outcome_hash_schema",
            "warmup_sessions",
            "outcome_status",
            "outcome_hash",
            "industry_classification_standard",
            "industry_classification_mode",
            "industry_classification_as_of",
            "industry_mapping_sha256",
        )
        self.assertEqual([], [field for field in replay_fields if field not in replay_schema])
        day_fields = (
            "warmup",
            "input_hash",
            "output_hash",
            "setup_type",
            "confirmation_condition",
            "invalidation_condition",
            "industry_evidence",
            "outcome",
        )
        self.assertEqual([], [field for field in day_fields if field not in days_schema])

    def test_v3_result_models_accept_complete_fixed_offline_evidence(self) -> None:
        """The response schema must not silently drop fields needed for an audit."""
        replay = _validate_or_fail(
            self,
            self.tools["get_strategy_replay"].output_model,
            {"ok": True, "data": _replay()},
        )
        days = _validate_or_fail(
            self,
            self.tools["get_strategy_replay_days"].output_model,
            {
                "ok": True,
                "data": {
                    "replay_id": "replay-v3",
                    "days": [
                        {
                            "trade_date": "2026-07-01",
                            "status": "completed",
                            "warmup": False,
                            "input_hash": "a" * 64,
                            "output_hash": "b" * 64,
                            "pipeline_version": "pipeline-v0.2",
                            "input_hash_schema": "v3-input-v1",
                            "result_hash_schema": "v3-result-v1",
                            "industry_classification_standard": "cn-mainboard-industry-v1",
                            "industry_classification_mode": "recorded",
                            "industry_classification_as_of": "2026-06-30",
                            "industry_mapping_sha256": "c" * 64,
                            "candidates": [_candidate()],
                        }
                    ],
                },
            },
        )

        self.assertEqual("pipeline-v0.2", replay.data.pipeline_version)
        self.assertEqual("v3-outcome-v1", replay.data.outcome_hash_schema)
        self.assertEqual("bank", days.data.days[0].candidates[0].industry_evidence.bucket)

    def test_list_versions_has_lifecycle_and_supersession_chain(self) -> None:
        schema = str(self.tools["list_strategy_versions"].output_model.model_json_schema())
        self.assertIn("lifecycle", schema)
        self.assertIn("superseded_by", schema)
        result = self.tools["list_strategy_versions"].output_model.model_validate(
            {
                "ok": True,
                "data": {
                    "versions": [
                        {
                            "version": "v1",
                            "status": "active",
                            "lifecycle": "superseded",
                            "superseded_by": "v3",
                            "parameters": {"offensive_limit": 3},
                        }
                    ]
                },
            }
        )
        self.assertEqual("superseded", result.data.versions[0].lifecycle)


class _Application:
    """Never reads data: output schemas are the unit under test."""

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        def handler(**arguments: Any) -> dict[str, Any]:
            return {"ok": True, "data": _replay()}

        return handler


def _replay() -> dict[str, object]:
    return {
        "replay_id": "replay-v3",
        "version": "v3-proposed",
        "start_date": "2023-01-01",
        "end_date": "2026-01-01",
        "status": "completed",
        "certified": False,
        "pipeline_version": "pipeline-v0.2",
        "input_hash_schema": "v3-input-v1",
        "result_hash_schema": "v3-result-v1",
        "outcome_hash_schema": "v3-outcome-v1",
        "warmup_sessions": 60,
        "outcome_status": "completed",
        "outcome": {"candidates": []},
        "outcome_hash": "d" * 64,
        "industry_classification_standard": "cn-mainboard-industry-v1",
        "industry_classification_mode": "recorded",
        "industry_classification_as_of": "2026-06-30",
        "industry_mapping_sha256": "e" * 64,
    }


def _candidate() -> dict[str, object]:
    return {
        "candidate_id": "candidate-1",
        "symbol": "600001.SH",
        "score": 87,
        "setup_type": "strong_pullback",
        "confirmation_condition": "close >= 101000",
        "invalidation_condition": "close < 98000",
        "industry_evidence": {"standard": "cn-mainboard-industry-v1", "bucket": "bank"},
        "outcome": {
            "availability": "partial",
            "path_status": "invalidated",
            "return_5d_bps": 500,
            "return_10d_bps": None,
            "return_20d_bps": None,
            "benchmark_return_5d_bps": 400,
            "benchmark_return_10d_bps": None,
            "benchmark_return_20d_bps": None,
            "excess_return_5d_bps": 100,
            "excess_return_10d_bps": None,
            "excess_return_20d_bps": None,
            "mfe_20d_bps": 700,
            "mae_20d_bps": -400,
            "first_confirmation_date": "2026-07-02",
            "first_invalidation_date": "2026-07-06",
        },
        "evidence": [],
    }


def _validate_or_fail(test: unittest.TestCase, model: Any, value: object):
    try:
        return model.model_validate(value)
    except ValidationError as error:
        test.fail(f"v3 evidence DTO rejected: {error}")


if __name__ == "__main__":
    unittest.main()
