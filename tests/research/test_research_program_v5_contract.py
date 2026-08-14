"""Offline contracts for first-batch Research Program v5 behavior."""

from __future__ import annotations

import importlib
import importlib.util
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace


def _research_module():
    spec = importlib.util.find_spec("stock_mcp.research_program")
    return importlib.import_module("stock_mcp.research_program") if spec else SimpleNamespace()


class ResearchProgramV5ContractTest(unittest.TestCase):
    def test_first_batch_definitions_freeze_existing_and_cross_mechanism_hypotheses(self) -> None:
        module = _research_module()
        build = getattr(module, "first_batch_hypotheses", None)
        self.assertTrue(callable(build), "first-batch hypotheses must be explicit and versioned")
        hypotheses = build(registered_at=datetime(2026, 8, 14, tzinfo=UTC))
        by_id = {item["hypothesis_id"]: item for item in hypotheses}
        self.assertEqual("frozen_forward", by_id["no-recent-limit-up-v1"]["status"])
        self.assertEqual("2026-08-07", by_id["no-recent-limit-up-v1"]["frozen_after"])
        self.assertEqual(
            {
                "extreme-return-abnormal-turnover-v1",
                "downside-tail-liquidity-v1",
                "overnight-intraday-separation-v1",
            },
            {
                hypothesis_id
                for hypothesis_id, item in by_id.items()
                if item["status"] == "exploratory"
            },
        )
        self.assertIn("earnings-price-point-in-time-v1", by_id)
        self.assertIn("profitability-quality-point-in-time-v1", by_id)
        self.assertEqual(
            {
                "breadth-five-day-median-v1",
                "breakout-overextension-cap-v1",
                "no-recent-limit-up-v1",
                "signal-quality-rank-v1",
                "size-bottom-30pct-filter-v1",
                "trend-quality-v1",
            },
            {
                hypothesis_id
                for hypothesis_id, item in by_id.items()
                if item["status"] in {"frozen_forward", "discovery_exhausted"}
            },
        )

    def test_v4_diagnostic_becomes_six_permanent_discovery_trials(self) -> None:
        module = _research_module()
        build = getattr(module, "v4_discovery_trials_from_diagnostic", None)
        self.assertTrue(callable(build), "existing v4 attempts must enter the lifetime ledger")
        arms = {
            f"v4-{name}": {
                "absolute_primary_metric": {"mean_bps": index},
                "paired_delta_vs_baseline": {"mean_bps": index - 3},
                "eligibility": {"eligible": False, "failed_gates": ["paired_ci"]},
            }
            for index, name in enumerate(
                (
                    "breadth-five-day-median",
                    "breakout-overextension-cap",
                    "no-recent-limit-up",
                    "signal-quality-rank",
                    "size-bottom-30pct-filter",
                    "trend-quality",
                ),
                1,
            )
        }
        trials = build(
            {
                "schema": "v4-study-diagnostic-v1",
                "source_study_id": "v4-study-fixed",
                "manifest_hash": "a" * 64,
                "source_result_hash": "b" * 64,
                "diagnostic_hash": "c" * 64,
                "arms": {"v0.3-policy-1": {}, **arms},
            },
            recorded_at=datetime(2026, 8, 14, tzinfo=UTC),
        )
        self.assertEqual(6, len(trials))
        self.assertEqual(6, len({trial["result_hash"] for trial in trials}))
        self.assertEqual(
            "no-recent-limit-up-v1",
            next(
                trial["hypothesis_id"]
                for trial in trials
                if trial["result"]["arm_id"] == "v4-no-recent-limit-up"
            ),
        )

    def test_exploratory_signals_are_deterministic_continuous_facts_not_tuned_rules(self) -> None:
        module = _research_module()
        salience = getattr(module, "extreme_return_abnormal_turnover_facts", None)
        downside = getattr(module, "downside_tail_liquidity_facts", None)
        overnight = getattr(module, "overnight_intraday_facts", None)
        self.assertTrue(callable(salience))
        self.assertTrue(callable(downside))
        self.assertTrue(callable(overnight))

        self.assertEqual(
            {
                "industry_relative_return_bps": 900,
                "absolute_salience_bps": 900,
                "turnover_ratio_bps": 20_000,
            },
            salience(
                current_return_bps=1_200,
                industry_return_bps=300,
                prior_turnover_bps=(100, 200, 300),
                current_turnover_bps=400,
            ),
        )
        risk = downside(
            prior_returns_bps=(-400, -300, 100, 200),
            overnight_gaps_bps=(-250, 50, -100),
            turnover_bps=(100, 100, 100),
        )
        self.assertEqual(-400, risk["worst_return_bps"])
        self.assertEqual(-250, risk["worst_overnight_gap_bps"])
        self.assertGreater(risk["downside_semideviation_bps"], 0)
        self.assertEqual(0, risk["turnover_dispersion_bps"])
        self.assertEqual(
            {"overnight_return_bps": 500, "intraday_return_bps": 1_000},
            overnight(pre_close_1e4=100_000, open_1e4=105_000, close_1e4=115_500),
        )

    def test_tushare_normalization_preserves_visibility_and_decimal_units(self) -> None:
        module = _research_module()
        normalize_daily = getattr(module, "normalize_tushare_daily_basic", None)
        normalize_financial = getattr(module, "normalize_tushare_fina_indicator", None)
        self.assertTrue(callable(normalize_daily))
        self.assertTrue(callable(normalize_financial))
        timestamp = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)
        daily = normalize_daily(
            {
                "ts_code": "600001.SH",
                "trade_date": "20260814",
                "turnover_rate_f": 1.25,
                "pe_ttm": 8.5,
                "pb": 1.1,
                "float_share": 123.45,
                "circ_mv": 987.65,
            },
            source_timestamp=timestamp,
        )
        self.assertEqual("2026-08-14", daily["visible_date"])
        self.assertEqual("1.25", daily["payload"]["turnover_rate_f"])
        self.assertEqual("daily_basic", daily["interface"])
        financial = normalize_financial(
            {
                "ts_code": "600001.SH",
                "ann_date": "20260320",
                "end_date": "20251231",
                "roe": 12.5,
                "roa": 5.25,
                "gross_margin": 30.0,
                "update_flag": "1",
            },
            source_timestamp=timestamp,
        )
        self.assertEqual("2026-03-20", financial["visible_date"])
        self.assertEqual("2025-12-31", financial["report_period"])
        self.assertEqual("12.5", financial["payload"]["roe"])

    def test_lifetime_statistics_report_white_and_romano_wolf_without_resetting_trials(
        self,
    ) -> None:
        module = _research_module()
        evaluate = getattr(module, "evaluate_lifetime_research_statistics", None)
        self.assertTrue(
            callable(evaluate), "lifetime tests need structured multiple-testing evidence"
        )
        result = evaluate(
            manifest_hash="a" * 64,
            baseline=(0, 0, 0, 0, 0, 0, 0, 0),
            challengers={
                "attention": (10, 20, 10, 20, 10, 20, 10, 20),
                "risk": (-5, 0, -5, 0, -5, 0, -5, 0),
            },
            lifetime_trial_count=8,
            block_sessions=2,
            bootstrap_samples=500,
        )
        self.assertEqual(8, result["lifetime_trial_count"])
        self.assertEqual("white_reality_check", result["family_test"]["method"])
        self.assertEqual("romano_wolf_stepdown", result["stepdown_test"]["method"])
        self.assertEqual({"attention", "risk"}, set(result["stepdown_test"]["adjusted_p_values"]))
        self.assertLessEqual(
            result["stepdown_test"]["adjusted_p_values"]["attention"],
            result["stepdown_test"]["adjusted_p_values"]["risk"],
        )


if __name__ == "__main__":
    unittest.main()
