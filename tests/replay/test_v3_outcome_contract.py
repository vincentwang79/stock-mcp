"""Offline RED contract for v3 candidate-outcome evidence."""

from __future__ import annotations

import unittest
from datetime import date

from stock_mcp import outcomes


class ReplayV3OutcomeContractTest(unittest.TestCase):
    def test_completed_candidate_receives_deterministic_asynchronous_outcome_evidence(self) -> None:
        """Outcome evidence is generated after candidate replay, from fixed post-date bars only."""
        evaluate = getattr(outcomes, "evaluate_v3_candidate_outcomes", None)
        self.assertTrue(
            callable(evaluate), "v3 requires a dedicated asynchronous outcome evaluator"
        )
        if not callable(evaluate):
            return

        outcome = evaluate(
            candidates=(
                {
                    "candidate_id": "candidate-1",
                    "symbol": "600001.SH",
                    "trade_date": date(2026, 7, 1),
                    "confirmation_condition": "close >= 101000",
                    "invalidation_condition": "close < 98000",
                    "industry_classification": "银行",
                },
            ),
            bars_by_symbol={
                "600001.SH": (
                    _bar("2026-07-02", 101_000, 103_000, 100_000, pre_close_1e4=100_000),
                    _bar("2026-07-03", 102_000, 105_000, 99_000, pre_close_1e4=101_000),
                    _bar("2026-07-06", 97_000, 100_000, 96_000, pre_close_1e4=102_000),
                    _bar("2026-07-07", 104_000, 106_000, 103_000, pre_close_1e4=97_000),
                    _bar("2026-07-08", 105_000, 107_000, 104_000, pre_close_1e4=104_000),
                    _bar("2026-07-09", 106_000, 107_000, 105_000, pre_close_1e4=105_000),
                )
            },
            equal_weight_mainboard_bars=(
                _bar("2026-07-02", 101_000, 101_000, 101_000, pre_close_1e4=100_000),
                _bar("2026-07-03", 102_000, 102_000, 102_000, pre_close_1e4=101_000),
                _bar("2026-07-06", 103_000, 103_000, 103_000, pre_close_1e4=102_000),
                _bar("2026-07-07", 104_000, 104_000, 104_000, pre_close_1e4=103_000),
                _bar("2026-07-08", 105_000, 105_000, 105_000, pre_close_1e4=104_000),
                _bar("2026-07-09", 106_000, 106_000, 106_000, pre_close_1e4=105_000),
            ),
            as_of=date(2026, 7, 9),
        )

        evidence = outcome["candidate-1"]
        self.assertEqual("partial", evidence["availability"])
        self.assertEqual("invalidated", evidence["path_status"])
        self.assertEqual("2026-07-02", evidence["first_confirmation_date"])
        self.assertEqual("2026-07-06", evidence["first_invalidation_date"])
        self.assertEqual(500, evidence["return_5d_bps"])
        self.assertEqual(500, evidence["benchmark_return_5d_bps"])
        self.assertEqual(0, evidence["excess_return_5d_bps"])
        self.assertEqual(700, evidence["mfe_20d_bps"])
        self.assertEqual(-400, evidence["mae_20d_bps"])
        self.assertIsNone(evidence["return_10d_bps"])
        self.assertIsNone(evidence["return_20d_bps"])

    def test_outcomes_use_the_close_preclose_chain_across_corporate_actions(self) -> None:
        sessions = tuple(date(2026, 7, 2 + index) for index in range(5))
        candidate_bars = (
            _bar("2026-07-02", 100_000, 100_000, 100_000, pre_close_1e4=100_000),
            _bar("2026-07-03", 50_000, 50_000, 50_000, pre_close_1e4=50_000),
            *tuple(
                _bar(day.isoformat(), 50_000, 50_000, 50_000, pre_close_1e4=50_000)
                for day in sessions[2:]
            ),
        )
        benchmark = tuple(
            _bar(day.isoformat(), 100_000, 100_000, 100_000, pre_close_1e4=100_000)
            for day in sessions
        )

        result = outcomes.evaluate_v3_candidate_outcomes(
            candidates=(
                {
                    "candidate_id": "split",
                    "symbol": "600001.SH",
                    "trade_date": date(2026, 7, 1),
                    "confirmation_condition": "close >= 110000",
                    "invalidation_condition": "close < 90000",
                },
            ),
            bars_by_symbol={"600001.SH": candidate_bars},
            equal_weight_mainboard_bars=benchmark,
            as_of=sessions[-1],
        )["split"]

        self.assertEqual(0, result["return_5d_bps"])
        self.assertEqual(0, result["mfe_20d_bps"])
        self.assertEqual(0, result["mae_20d_bps"])
        self.assertEqual("pending", result["path_status"])

    def test_complete_benchmark_without_candidate_bars_is_unavailable(self) -> None:
        benchmark = tuple(
            _bar(
                date(2026, 7, 2).replace(day=2 + index).isoformat(),
                100_000,
                100_000,
                100_000,
                pre_close_1e4=100_000,
            )
            for index in range(20)
        )
        result = outcomes.evaluate_v3_candidate_outcomes(
            candidates=(
                {
                    "candidate_id": "missing",
                    "symbol": "600001.SH",
                    "trade_date": date(2026, 7, 1),
                    "confirmation_condition": "close >= 110000",
                    "invalidation_condition": "close < 90000",
                },
            ),
            bars_by_symbol={},
            equal_weight_mainboard_bars=benchmark,
            as_of=date(2026, 7, 21),
        )["missing"]

        self.assertEqual("unavailable", result["availability"])

    def test_outcome_hash_is_separate_from_candidate_input_and_result_hashes(self) -> None:
        """Appending outcome evidence must not rewrite immutable candidate replay proof."""
        attach = getattr(outcomes, "attach_v3_outcome_hash", None)
        self.assertTrue(callable(attach), "v3 requires an outcome-hash attachment operation")
        if not callable(attach):
            return

        job = {
            "input_hash": "a" * 64,
            "result_hash": "b" * 64,
            "input_hash_schema": "v3-input-v1",
            "result_hash_schema": "v3-result-v1",
            "outcome_hash_schema": "v3-outcome-v1",
            "outcome_hash": None,
        }
        updated = attach(job, {"candidate-1": {"status": "completed"}})

        self.assertEqual("a" * 64, updated["input_hash"])
        self.assertEqual("b" * 64, updated["result_hash"])
        self.assertEqual("v3-outcome-v1", updated["outcome_hash_schema"])
        self.assertRegex(updated["outcome_hash"], r"^[0-9a-f]{64}$")


def _bar(
    trade_date: str,
    close_1e4: int,
    high_1e4: int,
    low_1e4: int,
    *,
    pre_close_1e4: int | None = None,
) -> dict[str, object]:
    bar: dict[str, object] = {
        "trade_date": trade_date,
        "close_1e4": close_1e4,
        "high_1e4": high_1e4,
        "low_1e4": low_1e4,
    }
    if pre_close_1e4 is not None:
        bar["pre_close_1e4"] = pre_close_1e4
    return bar


if __name__ == "__main__":
    unittest.main()
