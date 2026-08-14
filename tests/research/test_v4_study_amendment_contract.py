"""Contracts for deriving a corrected report without rewriting immutable study rows."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from datetime import date, timedelta

from stock_mcp import v4_research

_ARMS = (
    "v0.3-policy-1",
    "v4-trend-quality",
    "v4-breakout-overextension-cap",
    "v4-no-recent-limit-up",
    "v4-breadth-five-day-median",
    "v4-size-bottom-30pct-filter",
    "v4-signal-quality-rank",
)


class V4StudyAmendmentContractTest(unittest.TestCase):
    def test_derives_a_complete_report_without_mutating_the_source_study(self) -> None:
        repository = _CompletedStudyRepository()
        original = copy.deepcopy(repository.days)
        derive = getattr(v4_research, "derive_v4_study_amendment", None)
        self.assertTrue(callable(derive), "v4 needs a read-only amendment derivation")
        if not callable(derive):
            return

        amendment = derive(repository, source_study_id="study-source")

        self.assertEqual(original, repository.days)
        self.assertEqual("v4-study-amendment-v1", amendment["schema"])
        self.assertEqual("study-source", amendment["source_study_id"])
        self.assertEqual(1, amendment["corrected_day_count"])
        self.assertEqual(1, amendment["corrected_outcome_count"])
        self.assertEqual("complete", amendment["report"]["completeness_status"])
        self.assertEqual(10_000, amendment["report"]["outcome_completeness_rate_bps"])
        self.assertFalse(amendment["report"]["sina_replication_complete"])
        self.assertEqual([], amendment["report"]["proposals"])
        self.assertEqual(
            "v0.3-policy-1",
            amendment["statistics"]["baseline"]["arm_id"],
        )
        self.assertEqual(
            "paired_delta_vs_v0.3-policy-1",
            amendment["statistics"]["arm_statistic_semantics"],
        )
        self.assertRegex(str(amendment["source_days_hash"]), r"^[0-9a-f]{64}$")
        self.assertRegex(str(amendment["amendment_hash"]), r"^[0-9a-f]{64}$")

    def test_derives_read_only_absolute_risk_and_eligibility_diagnostics(self) -> None:
        repository = _CompletedStudyRepository()
        original = copy.deepcopy(repository.days)
        derive = getattr(v4_research, "derive_v4_study_diagnostics", None)
        self.assertTrue(callable(derive), "v4 needs read-only study diagnostics")
        if not callable(derive):
            return

        diagnostics = derive(repository, source_study_id="study-source")

        self.assertEqual(original, repository.days)
        self.assertEqual("v4-study-diagnostic-v1", diagnostics["schema"])
        self.assertEqual("study-source", diagnostics["source_study_id"])
        self.assertEqual("v0.3-policy-1", diagnostics["decision"]["retain_version"])
        self.assertEqual([], diagnostics["decision"]["proposals"])
        baseline = diagnostics["arms"]["v0.3-policy-1"]
        self.assertEqual(-25, baseline["absolute_primary_metric"]["mean_bps"])
        self.assertEqual([-25.0, -25.0], baseline["absolute_primary_metric"]["ci95_bps"])
        self.assertEqual(0, baseline["risk"]["positive_signal_day_rate_bps"])
        self.assertEqual(-25, baseline["risk"]["daily_p05_bps"])
        self.assertEqual("unavailable", baseline["risk"]["worst_20_session_block"]["status"])
        gates = diagnostics["arms"]["v4-no-recent-limit-up"]["eligibility"]
        self.assertFalse(gates["eligible"])
        self.assertIn("minimum_signal_days", gates["failed_gates"])
        self.assertIn("sina_replication_complete", gates["failed_gates"])
        self.assertEqual(
            {
                "status": "unavailable",
                "reason": "market_regime_not_persisted_in_v4_study_days",
            },
            diagnostics["dimensions"]["market_regime"],
        )
        self.assertRegex(str(diagnostics["diagnostic_hash"]), r"^[0-9a-f]{64}$")

    def test_diagnostics_break_down_persisted_setup_rank_year_and_cost_evidence(self) -> None:
        repository = _DetailedCompletedStudyRepository()

        diagnostics = v4_research.derive_v4_study_diagnostics(
            repository,
            source_study_id="study-detailed",
        )

        baseline = diagnostics["arms"]["v0.3-policy-1"]
        self.assertEqual(
            "all_signal_days_zero_when_group_has_no_candidate",
            diagnostics["breakdown_semantics"],
        )
        self.assertEqual(55, baseline["breakdowns"]["by_year"]["2026"]["mean_bps"])
        self.assertEqual(
            27.5,
            baseline["breakdowns"]["by_setup_type"]["strong_pullback"]["mean_bps"],
        )
        self.assertEqual(
            27.5,
            baseline["breakdowns"]["by_rank"]["1"]["mean_bps"],
        )
        self.assertEqual(
            {"10": 70, "25": 55, "50": 30},
            {
                cost: item["mean_bps"]
                for cost, item in baseline["breakdowns"]["by_cost_bps"].items()
            },
        )
        self.assertEqual(
            "available",
            baseline["risk"]["worst_20_session_block"]["status"],
        )


class _CompletedStudyRepository:
    def __init__(self) -> None:
        start = date(2026, 1, 2)
        self.sessions = tuple(start + timedelta(days=index) for index in range(86))
        self.signal_date = self.sessions[60]
        self.manifest = {
            "schema": "v4-manifest-v1",
            "source": "tushare",
            "sessions": [day.isoformat() for day in self.sessions],
            "bar_start": self.sessions[0].isoformat(),
            "signal_start": self.sessions[60].isoformat(),
            "signal_end": self.sessions[-26].isoformat(),
            "outcome_through": self.sessions[-1].isoformat(),
            "prices_hash": "1" * 64,
            "statuses_hash": "2" * 64,
            "share_capital_hash": "3" * 64,
            "industry_mapping_hash": "4" * 64,
            "included_symbols": ["600001.SH"],
        }
        manifest_hash = hashlib.sha256(
            json.dumps(self.manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.manifest["manifest_hash"] = manifest_hash
        self.run = {
            "study_id": "study-source",
            "manifest_hash": manifest_hash,
            "status": "completed",
            "result_hash": "b" * 64,
        }
        self.days = {
            arm_id: (
                {
                    "signal_date": self.signal_date.isoformat(),
                    "result_hash": (str(index) * 64)[:64],
                    "result": _incomplete_day() if index == 1 else _complete_day(),
                },
            )
            for index, arm_id in enumerate(_ARMS, start=1)
        }

    def get_v4_study_run(self, study_id: str) -> dict[str, object] | None:
        return dict(self.run) if study_id == "study-source" else None

    def get_v4_dataset_manifest(self, manifest_hash: str) -> dict[str, object] | None:
        return dict(self.manifest) if manifest_hash == self.run["manifest_hash"] else None

    def list_v4_study_arms(self, study_id: str) -> tuple[dict[str, object], ...]:
        assert study_id == "study-source"
        return tuple({"arm_id": arm_id} for arm_id in _ARMS)

    def list_v4_study_days(self, **kwargs: object) -> tuple[dict[str, object], ...]:
        assert kwargs["study_id"] == "study-source"
        return self.days[str(kwargs["arm_id"])]


class _DetailedCompletedStudyRepository(_CompletedStudyRepository):
    def __init__(self) -> None:
        start = date(2025, 11, 1)
        self.sessions = tuple(start + timedelta(days=index) for index in range(125))
        self.signal_dates = self.sessions[60:-25]
        self.manifest = {
            "schema": "v4-manifest-v1",
            "source": "tushare",
            "sessions": [day.isoformat() for day in self.sessions],
            "bar_start": self.sessions[0].isoformat(),
            "signal_start": self.signal_dates[0].isoformat(),
            "signal_end": self.signal_dates[-1].isoformat(),
            "outcome_through": self.sessions[-1].isoformat(),
            "prices_hash": "1" * 64,
            "statuses_hash": "2" * 64,
            "share_capital_hash": "3" * 64,
            "industry_mapping_hash": "4" * 64,
            "included_symbols": ["600001.SH"],
        }
        manifest_hash = hashlib.sha256(
            json.dumps(self.manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.manifest["manifest_hash"] = manifest_hash
        self.run = {
            "study_id": "study-detailed",
            "manifest_hash": manifest_hash,
            "status": "completed",
            "result_hash": "c" * 64,
        }
        self.days = {
            arm_id: tuple(
                {
                    "signal_date": signal_date.isoformat(),
                    "result_hash": hashlib.sha256(
                        f"{arm_id}|{signal_date.isoformat()}".encode()
                    ).hexdigest(),
                    "result": _detailed_day(signal_date, index),
                }
                for index, signal_date in enumerate(self.signal_dates)
            )
            for arm_id in _ARMS
        }

    def get_v4_study_run(self, study_id: str) -> dict[str, object] | None:
        return dict(self.run) if study_id == "study-detailed" else None

    def list_v4_study_arms(self, study_id: str) -> tuple[dict[str, object], ...]:
        assert study_id == "study-detailed"
        return tuple({"arm_id": arm_id} for arm_id in _ARMS)

    def list_v4_study_days(self, **kwargs: object) -> tuple[dict[str, object], ...]:
        assert kwargs["study_id"] == "study-detailed"
        return self.days[str(kwargs["arm_id"])]


def _detailed_day(signal_date: date, index: int) -> dict[str, object]:
    candidate_id = f"candidate-{index}"
    gross = 100
    peer = 20
    return {
        "candidates": [
            {
                "candidate_id": candidate_id,
                "symbol": "600001.SH",
                "setup_type": "strong_pullback" if index % 2 == 0 else "volume_breakout",
                "rank": index % 2 + 1,
            }
        ],
        "candidate_outcomes": {
            candidate_id: {
                "schema": "v4-outcome-v2",
                "source": "tushare",
                "calendar_complete": True,
                "completeness_status": "complete",
                "signal_close_path": {"status": "available"},
                "next_open_path": {
                    "status": "available",
                    "gross_return_20d_bps": gross,
                    "net_return_bps_by_cost": {
                        10: {20: gross - 10},
                        25: {20: gross - 25},
                        50: {20: gross - 50},
                    },
                    "benchmark": {
                        "completeness_rate_bps": 10_000,
                        "market_cap_decile_return_bps": {20: peer},
                        "market_cap_matched_excess_bps": {20: gross - peer},
                    },
                },
                "confirmed_next_open_path": {"status": "expired"},
            }
        },
        "daily_primary_metric_bps": gross - 25 - peer,
        "completeness_status": "complete",
        "replication_evidence": None,
    }


def _complete_day() -> dict[str, object]:
    return {
        "candidates": [],
        "candidate_outcomes": {},
        "daily_primary_metric_bps": 0,
        "completeness_status": "complete",
        "replication_evidence": None,
    }


def _incomplete_day() -> dict[str, object]:
    return {
        "candidates": [
            {
                "candidate_id": "late-entry",
                "symbol": "600001.SH",
                "setup_type": "strong_pullback",
                "rank": 1,
            }
        ],
        "candidate_outcomes": {
            "late-entry": {
                "schema": "v4-outcome-v2",
                "source": "tushare",
                "calendar_complete": True,
                "completeness_status": "incomplete",
                "signal_close_path": {"status": "available"},
                "next_open_path": {
                    "status": "available",
                    "benchmark": {
                        "completeness_rate_bps": 10_000,
                        "market_cap_decile_return_bps": {20: 0},
                        "market_cap_matched_excess_bps": {20: 0},
                    },
                    "gross_return_20d_bps": 0,
                    "net_return_bps_by_cost": {
                        10: {20: -10},
                        25: {20: -25},
                        50: {20: -50},
                    },
                },
                "confirmed_next_open_path": {
                    "status": "confirmed",
                    "execution_status": "partial",
                    "event_date": "2026-03-06",
                    "entry_date": "2026-03-09",
                    "gross_return_5d_bps": 10,
                    "gross_return_10d_bps": 20,
                    "gross_return_20d_bps": None,
                },
            }
        },
        "daily_primary_metric_bps": -25,
        "completeness_status": "incomplete",
        "replication_evidence": None,
    }


if __name__ == "__main__":
    unittest.main()
