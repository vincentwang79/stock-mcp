"""SQLite contracts for durable v4 research execution."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from stock_mcp.storage import Database


class V4StudyExecutionRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "stock.sqlite3")
        self.database.initialize()
        symbol_hash = lambda values: hashlib.sha256(  # noqa: E731
            json.dumps(tuple(values), separators=(",", ":")).encode()
        ).hexdigest()
        manifest = {
            "schema": "v4-manifest-v1",
            "source": "tushare",
            "share_capital_source": "sina",
            "status_source": "baostock",
            "created_at": "2026-08-11T00:00:00+00:00",
            "universe_symbols": ["600001.SH", "600002.SH"],
            "included_symbols": ["600001.SH"],
            "excluded_symbols": ["600002.SH"],
            "universe_symbol_count": 2,
            "included_symbol_count": 1,
            "excluded_symbol_count": 1,
            "capital_coverage_bps": 5000,
            "exclusion_reason": "sina_share_capital_unavailable",
            "universe_symbols_hash": symbol_hash(("600001.SH", "600002.SH")),
            "included_symbols_hash": symbol_hash(("600001.SH",)),
            "excluded_symbols_hash": symbol_hash(("600002.SH",)),
            "universe_source": "sina-backfill-manifest-v1",
            "universe_source_manifest_hash": "d" * 64,
        }
        self.manifest_hash = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        self.database.save_v4_dataset_manifest({**manifest, "manifest_hash": self.manifest_hash})
        self.study = self.database.create_v4_study_run(
            manifest_hash=self.manifest_hash,
            idempotency_key="study-1",
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

    def test_manifest_rejects_tampered_symbol_coverage_even_with_a_new_outer_hash(self) -> None:
        manifest = self.database.get_v4_dataset_manifest(self.manifest_hash)
        assert manifest is not None
        manifest["included_symbol_count"] = 2
        manifest.pop("manifest_hash")
        changed_hash = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.assertRaisesRegex(ValueError, "coverage|symbols|inconsistent"):
            self.database.save_v4_dataset_manifest({**manifest, "manifest_hash": changed_hash})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_claim_requeue_and_terminal_transitions_are_durable(self) -> None:
        claimed = self.database.claim_next_v4_study()
        self.assertEqual(self.study["study_id"], claimed["study_id"])
        self.assertEqual("running", claimed["status"])
        self.assertIsNotNone(claimed["started_at"])

        resumed_without_restart = self.database.claim_next_v4_study()
        self.assertEqual(self.study["study_id"], resumed_without_restart["study_id"])
        self.assertEqual("running", resumed_without_restart["status"])

        self.assertEqual(1, self.database.requeue_interrupted_v4_studies())
        self.assertEqual("queued", self.database.get_v4_study_run(self.study["study_id"])["status"])

        claimed_again = self.database.claim_next_v4_study()
        self.assertEqual(self.study["study_id"], claimed_again["study_id"])
        self.database.fail_v4_study(study_id=self.study["study_id"], error="fixture failed")
        failed = self.database.get_v4_study_run(self.study["study_id"])
        self.assertEqual("failed", failed["status"])
        self.assertEqual("fixture failed", failed["error"])
        self.assertIsNone(self.database.claim_next_v4_study())

    def test_day_step_is_immutable_and_completion_hashes_report(self) -> None:
        study_id = self.study["study_id"]
        self.database.claim_next_v4_study()
        step = {
            "kind": "day",
            "arm_id": "baseline",
            "signal_date": "2023-10-31",
            "result": {"candidates": [], "signal_return_20d_bps": 0},
        }
        self.database.save_v4_study_step(study_id=study_id, step=step)
        self.database.save_v4_study_step(study_id=study_id, step=step)

        changed = dict(step)
        changed["result"] = {"candidates": ["600000.SH"]}
        with self.assertRaisesRegex(ValueError, "immutable|conflict"):
            self.database.save_v4_study_step(study_id=study_id, step=changed)

        report = {"winner": None, "retain_version": "v0.3-policy-1"}
        self.database.complete_v4_study(study_id=study_id, report=report)
        completed = self.database.get_v4_study_run(study_id)
        self.assertEqual("completed", completed["status"])
        self.assertEqual(report, completed["report"])
        self.assertEqual(64, len(completed["result_hash"]))

        self.database.complete_v4_study(study_id=study_id, report=report)
        with self.assertRaisesRegex(ValueError, "immutable|conflict"):
            self.database.complete_v4_study(
                study_id=study_id,
                report={"winner": "v4-trend-quality"},
            )


if __name__ == "__main__":
    unittest.main()
