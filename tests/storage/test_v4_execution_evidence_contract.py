"""Offline persistence contracts for resumable, evidence-backed v4 execution."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from stock_mcp.storage import Database


class V4ExecutionEvidenceContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "stock.sqlite3")
        self.database.initialize()
        self.manifest_hash = self._save_manifest()
        self.study = self.database.create_v4_study_run(
            manifest_hash=self.manifest_hash,
            idempotency_key="v4-execution-evidence",
            arms=(
                {
                    "arm_id": "baseline",
                    "parameters": {"rule_engine_version": 3},
                    "parameters_hash": "b" * 64,
                    "parent_version": "v0.3-policy-1",
                    "change": "none",
                },
            ),
        )
        self.database.claim_next_v4_study()

    def test_candidate_outcomes_are_durable_and_immutable(self) -> None:
        save = getattr(self.database, "save_v4_study_candidate_outcomes", None)
        load = getattr(self.database, "list_v4_study_candidate_outcomes", None)
        self.assertTrue(callable(save), "v4 execution must persist candidate outcomes")
        self.assertTrue(callable(load), "v4 execution must read persisted candidate outcomes")
        if not callable(save) or not callable(load):
            return

        outcomes = {
            "600001.SH@2026-08-03": {
                "schema": "v4-outcome-v2",
                "completeness_status": "complete",
                "benchmark": {"completeness_rate_bps": 10_000},
            }
        }
        save(study_id=self.study["study_id"], arm_id="baseline", outcomes=outcomes)
        save(study_id=self.study["study_id"], arm_id="baseline", outcomes=outcomes)
        self.assertEqual(
            outcomes,
            load(study_id=self.study["study_id"], arm_id="baseline"),
        )
        with self.assertRaisesRegex(ValueError, "immutable|conflict"):
            save(
                study_id=self.study["study_id"],
                arm_id="baseline",
                outcomes={
                    "600001.SH@2026-08-03": {
                        "schema": "v4-outcome-v2",
                        "completeness_status": "partial",
                        "benchmark": {"completeness_rate_bps": 5_000},
                    }
                },
            )

    def test_day_and_candidate_outcomes_are_one_atomic_write(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "CREATE TRIGGER reject_v4_outcome BEFORE INSERT ON "
                "v4_study_candidate_outcomes BEGIN SELECT RAISE(ABORT, 'fixture'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.save_v4_study_step(
                study_id=self.study["study_id"],
                step={
                    "kind": "day",
                    "arm_id": "baseline",
                    "signal_date": "2026-08-03",
                    "result": {
                        "candidates": [{"candidate_id": "fixed-candidate"}],
                        "candidate_outcomes": {
                            "fixed-candidate": {
                                "schema": "v4-outcome-v2",
                                "completeness_status": "complete",
                            }
                        },
                    },
                },
            )
        self.assertEqual(
            (),
            self.database.list_v4_study_days(
                study_id=self.study["study_id"],
                arm_id="baseline",
                after_signal_date=None,
                limit=10,
            ),
        )

    def test_same_manifest_arm_day_and_evidence_have_identical_hashes_across_studies(self) -> None:
        step = {
            "kind": "day",
            "arm_id": "baseline",
            "signal_date": "2026-08-03",
            "result": {
                "candidates": [{"candidate_id": "fixed-candidate"}],
                "candidate_outcomes": {
                    "fixed-candidate": {
                        "schema": "v4-outcome-v2",
                        "completeness_status": "complete",
                    }
                },
            },
        }
        self.database.save_v4_study_step(study_id=self.study["study_id"], step=step)
        first_day = self.database.list_v4_study_days(
            study_id=self.study["study_id"], arm_id="baseline", after_signal_date=None, limit=1
        )[0]
        with self.database.connect() as connection:
            first_outcome_hash = str(
                connection.execute(
                    "SELECT outcome_hash FROM v4_study_candidate_outcomes "
                    "WHERE study_id=? AND arm_id='baseline' AND candidate_id='fixed-candidate'",
                    (self.study["study_id"],),
                ).fetchone()[0]
            )
        self.database.fail_v4_study(study_id=self.study["study_id"], error="fixture")
        second = self.database.create_v4_study_run(
            manifest_hash=self.manifest_hash,
            idempotency_key="v4-hash-repeat",
            arms=(
                {
                    "arm_id": "baseline",
                    "parameters": {"rule_engine_version": 3},
                    "parameters_hash": "b" * 64,
                    "parent_version": "v0.3-policy-1",
                    "change": "none",
                },
            ),
        )
        self.database.claim_next_v4_study()
        self.database.save_v4_study_step(study_id=second["study_id"], step=step)
        second_day = self.database.list_v4_study_days(
            study_id=second["study_id"], arm_id="baseline", after_signal_date=None, limit=1
        )[0]
        with self.database.connect() as connection:
            second_outcome_hash = str(
                connection.execute(
                    "SELECT outcome_hash FROM v4_study_candidate_outcomes "
                    "WHERE study_id=? AND arm_id='baseline' AND candidate_id='fixed-candidate'",
                    (second["study_id"],),
                ).fetchone()[0]
            )
        self.assertEqual(first_day["result_hash"], second_day["result_hash"])
        self.assertEqual(first_outcome_hash, second_outcome_hash)

    def test_execution_state_reads_manifest_arms_and_completed_dates(self) -> None:
        state = getattr(self.database, "get_v4_study_execution_state", None)
        self.assertTrue(callable(state), "v4 execution needs one durable read seam")
        if not callable(state):
            return

        self.database.save_v4_study_step(
            study_id=self.study["study_id"],
            step={
                "kind": "day",
                "arm_id": "baseline",
                "signal_date": "2026-08-03",
                "result": {"candidates": []},
            },
        )
        execution = state(study_id=self.study["study_id"])
        self.assertEqual(self.manifest_hash, execution["manifest"]["manifest_hash"])
        self.assertEqual(("baseline",), tuple(arm["arm_id"] for arm in execution["arms"]))
        self.assertEqual({"baseline": ("2026-08-03",)}, execution["completed_dates"])
        self.assertEqual({}, execution["statistics"])
        self.assertIsNone(execution["sina_replication"])

    def test_statistics_are_durable_and_immutable(self) -> None:
        save = getattr(self.database, "save_v4_study_statistics", None)
        state = getattr(self.database, "get_v4_study_execution_state", None)
        self.assertTrue(callable(save), "v4 execution must persist frozen statistics")
        self.assertTrue(callable(state), "v4 statistics require durable readback")
        if not callable(save) or not callable(state):
            return

        statistics = {
            "schema": "v4-statistics-v1",
            "manifest_hash": self.manifest_hash,
            "winner": {"eligible": False, "arm_id": None, "decision": "retain_baseline"},
            "arms": {"baseline": {"completeness_rate_bps": 10_000}},
        }
        save(study_id=self.study["study_id"], statistics=statistics)
        save(study_id=self.study["study_id"], statistics=statistics)
        self.assertEqual(statistics, state(study_id=self.study["study_id"])["statistics"])
        with self.assertRaisesRegex(ValueError, "immutable|conflict"):
            save(
                study_id=self.study["study_id"],
                statistics={**statistics, "winner": {"eligible": True, "arm_id": "baseline"}},
            )

    def test_completion_rejects_a_report_that_only_claims_complete_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "seven|calendar|outcome|benchmark|evidence"):
            self.database.complete_v4_study(
                study_id=self.study["study_id"],
                report={
                    "winner": None,
                    "completeness_status": "complete",
                    "outcome_completeness_rate_bps": 10_000,
                    "benchmark_completeness_rate_bps": 10_000,
                },
            )
        run = self.database.get_v4_study_run(self.study["study_id"])
        assert run is not None
        self.assertEqual("running", run["status"])

    def test_completion_rejects_seven_arms_with_an_incomplete_persisted_calendar(self) -> None:
        arms = tuple(
            {
                "arm_id": f"arm-{index}",
                "parameters": {"rule_engine_version": 3},
                "parameters_hash": f"{index + 1:064x}",
                "parent_version": "v0.3-policy-1",
                "change": f"change-{index}",
            }
            for index in range(7)
        )
        study = self.database.create_v4_study_run(
            manifest_hash=self.manifest_hash,
            idempotency_key="v4-incomplete-calendar",
            arms=arms,
        )
        self.database.fail_v4_study(study_id=self.study["study_id"], error="fixture complete")
        self.database.claim_next_v4_study()
        for arm in arms:
            self.database.save_v4_study_step(
                study_id=study["study_id"],
                step={
                    "kind": "day",
                    "arm_id": arm["arm_id"],
                    "signal_date": "2026-08-03",
                    "result": {
                        "candidates": [],
                        "candidate_outcomes": {},
                        "completeness_status": "complete",
                    },
                },
            )
        self.database.save_v4_study_statistics(
            study_id=study["study_id"],
            statistics={
                "schema": "v4-statistics-v1",
                "manifest_hash": self.manifest_hash,
                "winner": {"eligible": False, "arm_id": None, "decision": "retain_baseline"},
            },
        )
        with self.assertRaisesRegex(ValueError, "calendar|evidence|manifest"):
            self.database.complete_v4_study(
                study_id=study["study_id"],
                report={
                    "schema": "v4-statistics-v1",
                    "completeness_status": "complete",
                    "outcome_completeness_rate_bps": 10_000,
                    "benchmark_completeness_rate_bps": 10_000,
                },
            )

    def test_no_winner_creates_no_proposal_artifact(self) -> None:
        state = getattr(self.database, "get_v4_study_execution_state", None)
        self.assertTrue(
            callable(state), "proposal artifacts must be observable through the repository"
        )
        if not callable(state):
            return

        execution = state(study_id=self.study["study_id"])
        self.assertEqual((), execution["proposal_artifacts"])

    def test_winner_statistics_require_a_persisted_complete_sina_replication_artifact(self) -> None:
        save = getattr(self.database, "save_v4_study_statistics", None)
        self.assertTrue(callable(save), "winner gating belongs at durable statistics persistence")
        if not callable(save):
            return

        with self.assertRaisesRegex(ValueError, "sina|replication|artifact"):
            save(
                study_id=self.study["study_id"],
                statistics={
                    "schema": "v4-statistics-v1",
                    "manifest_hash": self.manifest_hash,
                    "winner": {
                        "eligible": True,
                        "arm_id": "baseline",
                        "decision": "propose",
                    },
                    "sina_replication": {
                        "source": "sina",
                        "status": "complete",
                        "completeness_rate_bps": 10_000,
                    },
                },
            )

    def test_restart_claims_the_earliest_unfinished_study(self) -> None:
        later = self.database.create_v4_study_run(
            manifest_hash=self.manifest_hash,
            idempotency_key="v4-execution-evidence-later",
            arms=(
                {
                    "arm_id": "baseline",
                    "parameters": {"rule_engine_version": 3},
                    "parameters_hash": "c" * 64,
                    "parent_version": "v0.3-policy-1",
                    "change": "none",
                },
            ),
        )

        self.assertEqual(1, self.database.requeue_interrupted_v4_studies())
        resumed = self.database.claim_next_v4_study()

        self.assertIsNotNone(resumed)
        assert resumed is not None
        self.assertEqual(self.study["study_id"], resumed["study_id"])
        self.assertNotEqual(later["study_id"], resumed["study_id"])

    def _save_manifest(self) -> str:
        universe = ("600001.SH",)
        symbol_hash = hashlib.sha256(
            json.dumps(universe, separators=(",", ":")).encode()
        ).hexdigest()
        manifest = {
            "schema": "v4-manifest-v1",
            "source": "tushare",
            "share_capital_source": "sina",
            "status_source": "baostock",
            "created_at": "2026-08-11T00:00:00+00:00",
            "universe_symbols": list(universe),
            "included_symbols": list(universe),
            "excluded_symbols": [],
            "universe_symbol_count": 1,
            "included_symbol_count": 1,
            "excluded_symbol_count": 0,
            "capital_coverage_bps": 10_000,
            "exclusion_reason": None,
            "universe_symbols_hash": symbol_hash,
            "included_symbols_hash": symbol_hash,
            "excluded_symbols_hash": hashlib.sha256(b"[]").hexdigest(),
            "universe_source": "sina-backfill-manifest-v1",
            "universe_source_manifest_hash": "d" * 64,
        }
        manifest_hash = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.database.save_v4_dataset_manifest({**manifest, "manifest_hash": manifest_hash})
        return manifest_hash


if __name__ == "__main__":
    unittest.main()
