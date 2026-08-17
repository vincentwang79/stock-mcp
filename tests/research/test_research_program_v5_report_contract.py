"""Contracts for deterministic, evidence-only forward research reports."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from stock_mcp import research_program

_AS_OF = datetime(2026, 10, 31, tzinfo=UTC)


def _hypothesis(hypothesis_id: str = "no-recent-limit-up-v1") -> dict[str, object]:
    return {
        "hypothesis_id": hypothesis_id,
        "definition_hash": "f" * 64,
        "frozen_after": "2026-08-07",
    }


def _observation(
    day: str,
    symbol: str,
    *,
    result_hash: str,
    passes: bool | None = None,
) -> dict[str, object]:
    facts: dict[str, object]
    if passes is None:
        facts = {"overnight_return_bps": 100, "intraday_return_bps": -20}
    else:
        facts = {
            "recent_limit_up_days": 0 if passes else 1,
            "passes_no_recent_limit_up": passes,
        }
    hypothesis_id = (
        "no-recent-limit-up-v1" if passes is not None else "overnight-intraday-separation-v1"
    )
    return {
        "hypothesis_id": hypothesis_id,
        "trade_date": day,
        "symbol": symbol,
        "input_hash": result_hash[::-1],
        "result_hash": result_hash,
        "observation": facts,
        "recorded_at": datetime.fromisoformat(f"{day}T10:00:00+00:00"),
    }


def _outcome(
    observation: dict[str, object],
    excess_bps: int,
    *,
    recorded_at: datetime = datetime(2026, 10, 1, tzinfo=UTC),
) -> dict[str, object]:
    return {
        "hypothesis_id": observation["hypothesis_id"],
        "signal_date": observation["trade_date"],
        "symbol": observation["symbol"],
        "horizon_sessions": 20,
        "observation_result_hash": observation["result_hash"],
        "outcome": {
            "path": "signal-close-diagnostic",
            "gross_return_bps": excess_bps + 10,
            "benchmark_return_bps": 10,
            "excess_return_bps": excess_bps,
        },
        "outcome_hash": str(observation["result_hash"])[0] * 64,
        "recorded_at": recorded_at,
    }


class ResearchProgramV5ReportContractTest(unittest.TestCase):
    def test_no_recent_limit_up_report_equal_weights_dates_and_paired_cohorts(self) -> None:
        build = getattr(research_program, "build_forward_research_report", None)
        self.assertTrue(callable(build), "mature evidence needs a deterministic report builder")
        observations = (
            _observation("2026-08-10", "600001.SH", result_hash="a" * 64, passes=True),
            _observation("2026-08-10", "600002.SH", result_hash="b" * 64, passes=True),
            _observation("2026-08-10", "600003.SH", result_hash="c" * 64, passes=False),
            _observation("2026-08-11", "600001.SH", result_hash="d" * 64, passes=True),
            _observation("2026-08-11", "600003.SH", result_hash="e" * 64, passes=False),
            _observation("2026-08-11", "600004.SH", result_hash="1" * 64, passes=False),
        )
        outcomes = tuple(
            _outcome(observation, excess)
            for observation, excess in zip(
                observations,
                (100, 200, 0, 50, -100, 100),
                strict=True,
            )
        )
        arguments = {
            "hypothesis": _hypothesis(),
            "observations": observations,
            "outcomes": outcomes,
            "horizon_sessions": 20,
            "as_of": _AS_OF,
            "block_sessions": 1,
            "bootstrap_samples": 200,
        }
        report = build(**arguments)

        self.assertEqual("research-forward-report-v1", report["schema"])
        self.assertEqual("paired-cohort", report["analysis_mode"])
        self.assertEqual(2, report["evidence"]["signal_date_count"])
        self.assertEqual(2, report["evidence"]["paired_signal_date_count"])
        self.assertEqual(59, report["summary"]["daily_equal_weight_excess_mean_bps"])
        self.assertEqual(100, report["summary"]["treatment_daily_mean_excess_bps"])
        self.assertEqual(0, report["summary"]["control_daily_mean_excess_bps"])
        self.assertEqual(100, report["summary"]["paired_delta_mean_bps"])
        self.assertEqual(10_000, report["summary"]["positive_paired_day_ratio_bps"])
        self.assertFalse(report["decision"]["promotion_eligible"])
        self.assertEqual("evidence_only", report["decision"]["status"])
        self.assertRegex(report["manifest_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(report["result_hash"], r"^[0-9a-f]{64}$")

        reordered = build(
            **{
                **arguments,
                "observations": tuple(reversed(observations)),
                "outcomes": tuple(reversed(outcomes)),
            }
        )
        self.assertEqual(report, reordered)

        remapped_observations = tuple(
            {**item, "symbol": "600000.SH"} if index == 0 else item
            for index, item in enumerate(observations)
        )
        remapped_outcomes = tuple(
            {**item, "symbol": "600000.SH"} if index == 0 else item
            for index, item in enumerate(outcomes)
        )
        remapped = build(
            **{
                **arguments,
                "observations": remapped_observations,
                "outcomes": remapped_outcomes,
            }
        )
        self.assertNotEqual(report["manifest_hash"], remapped["manifest_hash"])

    def test_report_excludes_future_and_legacy_evidence_and_rejects_hash_conflicts(
        self,
    ) -> None:
        build = getattr(research_program, "build_forward_research_report", None)
        self.assertTrue(callable(build))
        visible = _observation("2026-08-10", "600001.SH", result_hash="a" * 64, passes=True)
        pending = _observation("2026-08-11", "600002.SH", result_hash="b" * 64, passes=False)
        legacy = _observation("2026-08-12", "legacy-unspecified", result_hash="c" * 64, passes=True)
        report = build(
            hypothesis=_hypothesis(),
            observations=(visible, pending, legacy),
            outcomes=(
                _outcome(visible, 25),
                _outcome(pending, 50, recorded_at=datetime(2026, 11, 1, tzinfo=UTC)),
            ),
            horizon_sessions=20,
            as_of=_AS_OF,
            block_sessions=1,
            bootstrap_samples=50,
        )
        self.assertEqual(1, report["evidence"]["mature_observation_count"])
        self.assertEqual(1, report["evidence"]["pending_observation_count"])
        self.assertEqual(1, report["evidence"]["excluded_legacy_observation_count"])
        self.assertEqual(0, report["evidence"]["paired_signal_date_count"])

        conflict = {**_outcome(visible, 25), "observation_result_hash": "9" * 64}
        with self.assertRaisesRegex(ValueError, "observation hash"):
            build(
                hypothesis=_hypothesis(),
                observations=(visible,),
                outcomes=(conflict,),
                horizon_sessions=20,
                as_of=_AS_OF,
            )

    def test_unfrozen_continuous_hypothesis_remains_descriptive_only(self) -> None:
        build = getattr(research_program, "build_forward_research_report", None)
        self.assertTrue(callable(build))
        observation = _observation("2026-08-10", "600001.SH", result_hash="a" * 64, passes=None)
        report = build(
            hypothesis=_hypothesis("overnight-intraday-separation-v1"),
            observations=(observation,),
            outcomes=(_outcome(observation, 40),),
            horizon_sessions=20,
            as_of=_AS_OF,
        )
        self.assertEqual("descriptive-only", report["analysis_mode"])
        self.assertIsNone(report["summary"]["paired_delta_mean_bps"])
        self.assertFalse(report["decision"]["promotion_eligible"])
        self.assertIn("contrast_not_frozen", report["decision"]["failed_gates"])


if __name__ == "__main__":
    unittest.main()
