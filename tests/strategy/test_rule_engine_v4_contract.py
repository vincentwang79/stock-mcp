"""Offline RED contracts for preregistered v4 research arms and statistics."""

from __future__ import annotations

import unittest

from stock_mcp import strategy


class V4ResearchStrategyContractTest(unittest.TestCase):
    def test_research_plan_has_frozen_baseline_and_exactly_six_single_factor_challengers(
        self,
    ) -> None:
        build_arms = getattr(strategy, "build_v4_research_arms", None)
        self.assertTrue(
            callable(build_arms),
            "v4 research needs a preregistered seven-arm plan rather than a parameter search",
        )
        if not callable(build_arms):
            return

        arms = build_arms(created_at="2026-08-11T00:00:00+08:00")
        self.assertEqual(7, len(arms))
        self.assertEqual("v0.3-policy-1", arms[0]["version"])
        self.assertEqual("baseline", arms[0]["role"])
        self.assertEqual(
            {
                "trend-quality",
                "breakout-overextension-cap",
                "no-recent-limit-up",
                "breadth-five-day-median",
                "size-bottom-30pct-filter",
                "signal-quality-rank",
            },
            {arm["change"] for arm in arms[1:]},
        )
        for arm in arms:
            self.assertEqual("proposed", arm["status"])
            self.assertRegex(str(arm["parameters_hash"]), r"^[0-9a-f]{64}$")
            self.assertEqual("tushare", arm["source"])

    def test_statistics_use_circular_20_session_blocks_white_reality_check_and_strict_gate(
        self,
    ) -> None:
        evaluate = getattr(strategy, "evaluate_v4_research_statistics", None)
        self.assertTrue(
            callable(evaluate),
            "v4 research needs fixed circular block-bootstrap and White Reality Check evidence",
        )
        if not callable(evaluate):
            return

        result = evaluate(
            manifest_hash="a" * 64,
            primary_metric="20d_25bps_market_cap_matched_excess_bps",
            daily_candidate_returns={
                "v0.3-policy-1": (0, 0, 0, 0),
                "v4-trend-quality": (10, 15, 20, 25),
            },
            block_sessions=20,
            bootstrap_samples=10_000,
        )

        self.assertEqual("circular_block_bootstrap", result["bootstrap_method"])
        self.assertEqual(20, result["block_sessions"])
        self.assertEqual("white_reality_check", result["multiple_testing_method"])
        self.assertFalse(
            result["winner"]["eligible"], "tiny samples must never manufacture a winner"
        )
        self.assertEqual("retain_baseline", result["winner"]["decision"])

    def test_statistics_expose_baseline_and_explicit_paired_differences(self) -> None:
        result = strategy.evaluate_v4_research_statistics(
            manifest_hash="a" * 64,
            primary_metric="20d_25bps_market_cap_matched_excess_bps",
            daily_candidate_returns={
                "v0.3-policy-1": (100, 200, 300, 400),
                "v4-trend-quality": (110, 215, 320, 425),
            },
        )

        self.assertEqual(
            {
                "arm_id": "v0.3-policy-1",
                "mean_absolute_primary_bps": 250.0,
                "signal_day_count": 4,
            },
            result["baseline"],
        )
        challenger = result["arms"]["v4-trend-quality"]
        self.assertEqual(267.5, challenger["mean_absolute_primary_bps"])
        self.assertEqual(17.5, challenger["mean_paired_delta_bps"])
        self.assertEqual(challenger["ci95"], challenger["paired_delta_ci95"])
        self.assertEqual(
            "paired_delta_vs_v0.3-policy-1",
            result["arm_statistic_semantics"],
        )


if __name__ == "__main__":
    unittest.main()
