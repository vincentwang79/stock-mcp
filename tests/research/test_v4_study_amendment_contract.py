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
        "candidates": [{"candidate_id": "late-entry", "symbol": "600001.SH"}],
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
