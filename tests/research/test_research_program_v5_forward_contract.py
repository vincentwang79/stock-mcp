"""Contracts for deterministic forward observations and point-in-time batches."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime

from stock_mcp import research_program


class _Repository:
    def __init__(self) -> None:
        self.saved_batches: list[tuple[dict[str, object], ...]] = []
        self.facts: tuple[dict[str, object], ...] = ()

    def save_point_in_time_fundamentals(self, facts):
        materialized = tuple(dict(item) for item in facts)
        self.saved_batches.append(materialized)
        self.facts = (*self.facts, *materialized)
        return len(materialized)

    def load_point_in_time_fundamentals(self, **_arguments):
        return self.facts


class ResearchProgramV5ForwardContractTest(unittest.TestCase):
    def test_forward_observation_is_deterministic_and_hypothesis_specific(self) -> None:
        build = getattr(research_program, "build_research_forward_observation", None)
        self.assertTrue(callable(build), "forward evidence needs a deterministic builder")
        arguments = {
            "hypothesis_id": "no-recent-limit-up-v1",
            "trade_date": date(2026, 8, 10),
            "source_timestamp": datetime(2026, 8, 10, 10, tzinfo=UTC),
            "raw_inputs": {"prior_limit_up_touched": (False, True, False, False, False)},
        }
        first = build(**arguments)
        second = build(**arguments)
        self.assertEqual(first, second)
        self.assertEqual(1, first["observation"]["recent_limit_up_days"])
        self.assertFalse(first["observation"]["passes_no_recent_limit_up"])
        self.assertRegex(first["input_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(first["result_hash"], r"^[0-9a-f]{64}$")
        with self.assertRaisesRegex(ValueError, "five"):
            build(
                **{
                    **arguments,
                    "raw_inputs": {"prior_limit_up_touched": (False,) * 4},
                }
            )
        with self.assertRaisesRegex(ValueError, "boolean"):
            build(
                **{
                    **arguments,
                    "raw_inputs": {"prior_limit_up_touched": (False, False, False, False, "false")},
                }
            )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            build(**{**arguments, "source_timestamp": datetime(2026, 8, 10, 10)})

        salience = build(
            hypothesis_id="extreme-return-abnormal-turnover-v1",
            trade_date=date(2026, 8, 10),
            source_timestamp=arguments["source_timestamp"],
            raw_inputs={
                "current_return_bps": 1_200,
                "industry_return_bps": 300,
                "prior_turnover_bps": (100, 200, 300),
                "current_turnover_bps": 400,
            },
        )
        self.assertEqual(900, salience["observation"]["industry_relative_return_bps"])
        risk = build(
            hypothesis_id="downside-tail-liquidity-v1",
            trade_date=date(2026, 8, 10),
            source_timestamp=arguments["source_timestamp"],
            raw_inputs={
                "prior_returns_bps": (-400, -300, 100, 200),
                "overnight_gaps_bps": (-250, 50, -100),
                "turnover_bps": (100, 100, 100),
            },
        )
        self.assertEqual(-400, risk["observation"]["worst_return_bps"])
        separation = build(
            hypothesis_id="overnight-intraday-separation-v1",
            trade_date=date(2026, 8, 10),
            source_timestamp=arguments["source_timestamp"],
            raw_inputs={
                "pre_close_1e4": 100_000,
                "open_1e4": 105_000,
                "close_1e4": 115_500,
            },
        )
        self.assertEqual(500, separation["observation"]["overnight_return_bps"])

    def test_point_in_time_batch_rejects_future_rows_before_any_write(self) -> None:
        ingest = getattr(research_program, "ingest_point_in_time_research_batch", None)
        self.assertTrue(callable(ingest), "point-in-time rows need an atomic offline ingest seam")
        repository = _Repository()
        timestamp = datetime(2026, 3, 31, 10, tzinfo=UTC)
        with self.assertRaisesRegex(ValueError, "future"):
            ingest(
                repository,
                as_of=date(2026, 3, 31),
                source_timestamp=timestamp,
                daily_basic_rows=({"ts_code": "600001.SH", "trade_date": "20260331", "pb": 1.1},),
                fina_indicator_rows=(
                    {
                        "ts_code": "600001.SH",
                        "ann_date": "20260410",
                        "end_date": "20251231",
                        "roe": 11.8,
                        "update_flag": "1",
                    },
                ),
            )
        self.assertEqual([], repository.saved_batches)

    def test_point_in_time_feature_view_exposes_values_and_visibility(self) -> None:
        ingest = getattr(research_program, "ingest_point_in_time_research_batch", None)
        build = getattr(research_program, "point_in_time_research_facts", None)
        self.assertTrue(callable(ingest))
        self.assertTrue(callable(build))
        repository = _Repository()
        timestamp = datetime(2026, 3, 31, 10, tzinfo=UTC)
        report = ingest(
            repository,
            as_of=date(2026, 3, 31),
            source_timestamp=timestamp,
            daily_basic_rows=(
                {
                    "ts_code": "600001.SH",
                    "trade_date": "20260331",
                    "turnover_rate_f": 1.25,
                    "pe_ttm": 8.5,
                    "pb": 1.1,
                    "float_share": 123.45,
                    "circ_mv": 987.65,
                },
            ),
            fina_indicator_rows=(
                {
                    "ts_code": "600001.SH",
                    "ann_date": "20260320",
                    "end_date": "20251231",
                    "roe": 12.5,
                    "roa": 5.25,
                    "gross_margin": 30,
                    "update_flag": "1",
                },
            ),
        )
        self.assertEqual({"daily_basic": 1, "fina_indicator": 1, "saved": 2}, report)
        facts = build(repository, symbol="600001.SH", as_of=date(2026, 3, 31))
        self.assertEqual("complete", facts["coverage_status"])
        self.assertEqual("8.5", facts["valuation"]["pe_ttm"])
        self.assertEqual("12.5", facts["profitability"]["roe"])
        self.assertEqual("2026-03-20", facts["profitability_visible_date"])
        self.assertRegex(facts["facts_hash"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
