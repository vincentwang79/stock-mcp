"""Schema v12 contracts for the immutable research-program ledger."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from stock_mcp import storage


class ResearchProgramV5StorageContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = storage.Database(Path(self.temporary.name) / "research.sqlite3")
        self.database.initialize()

    def test_schema_v12_persists_hypotheses_trials_and_forward_observations_immutably(self) -> None:
        self.assertEqual(12, storage.SCHEMA_VERSION)
        register = getattr(self.database, "register_research_hypotheses", None)
        save_trial = getattr(self.database, "save_research_trial", None)
        save_observation = getattr(self.database, "save_research_forward_observation", None)
        self.assertTrue(callable(register), "research hypotheses need a durable registry")
        self.assertTrue(callable(save_trial), "research trials need a lifetime ledger")
        self.assertTrue(callable(save_observation), "frozen hypotheses need forward evidence")

        hypothesis = {
            "hypothesis_id": "no-recent-limit-up-v1",
            "family": "attention-overreaction",
            "title": "No recent limit-up",
            "mechanism": "attention-driven overreaction",
            "formula": {"prior_sessions": 20, "max_touched_up": 0},
            "data_requirements": ["daily_price_limits"],
            "status": "frozen_forward",
            "sample_role": "discovery_exhausted",
            "frozen_after": "2026-08-07",
            "registered_at": "2026-08-14T00:00:00+00:00",
        }
        register((hypothesis,))
        register((hypothesis,))
        recorded = self.database.get_research_hypothesis("no-recent-limit-up-v1")
        self.assertEqual(hypothesis["formula"], recorded["formula"])
        with self.assertRaisesRegex(ValueError, "conflict"):
            register(({**hypothesis, "mechanism": "changed after registration"},))

        trial = {
            "trial_id": "trial-v4-no-recent-limit-up",
            "hypothesis_id": "no-recent-limit-up-v1",
            "manifest_hash": "a" * 64,
            "sample_role": "discovery_exhausted",
            "status": "completed",
            "result": {"eligible": False, "mean_paired_delta_bps": 61.27},
            "result_hash": "b" * 64,
            "created_at": "2026-08-14T00:00:00+00:00",
            "completed_at": "2026-08-14T00:01:00+00:00",
        }
        save_trial(trial)
        save_trial(trial)
        with self.assertRaisesRegex(ValueError, "conflict"):
            save_trial({**trial, "result_hash": "c" * 64})
        self.assertEqual(1, len(self.database.list_research_trials()))

        observation = {
            "hypothesis_id": "no-recent-limit-up-v1",
            "trade_date": "2026-08-10",
            "input_hash": "d" * 64,
            "result_hash": "e" * 64,
            "observation": {"candidate_count": 1, "status": "observed"},
            "recorded_at": "2026-08-10T10:00:00+00:00",
        }
        save_observation(observation)
        save_observation(observation)
        with self.assertRaisesRegex(ValueError, "conflict"):
            save_observation({**observation, "result_hash": "f" * 64})
        self.assertEqual(
            "2026-08-10",
            self.database.list_research_forward_observations(hypothesis_id="no-recent-limit-up-v1")[
                0
            ]["trade_date"],
        )

    def test_point_in_time_fundamentals_never_expose_later_revisions(self) -> None:
        save = getattr(self.database, "save_point_in_time_fundamentals", None)
        load = getattr(self.database, "load_point_in_time_fundamentals", None)
        self.assertTrue(callable(save), "point-in-time fundamentals need immutable persistence")
        self.assertTrue(callable(load), "point-in-time reads must honor visible dates")
        facts = (
            {
                "symbol": "600001.SH",
                "interface": "fina_indicator",
                "report_period": "2025-12-31",
                "visible_date": "2026-03-20",
                "revision_key": "20260320|1",
                "source": "tushare",
                "payload": {"roe": "12.50", "update_flag": "1"},
                "payload_hash": "1" * 64,
                "source_timestamp": "2026-03-20T10:00:00+00:00",
            },
            {
                "symbol": "600001.SH",
                "interface": "fina_indicator",
                "report_period": "2025-12-31",
                "visible_date": "2026-04-10",
                "revision_key": "20260410|1",
                "source": "tushare",
                "payload": {"roe": "11.80", "update_flag": "1"},
                "payload_hash": "2" * 64,
                "source_timestamp": "2026-04-10T10:00:00+00:00",
            },
        )
        save(facts)
        march = load(symbol="600001.SH", as_of=date(2026, 3, 31))
        april = load(symbol="600001.SH", as_of=date(2026, 4, 30))
        self.assertEqual("12.50", march[0]["payload"]["roe"])
        self.assertEqual("11.80", april[0]["payload"]["roe"])
        self.assertEqual(date(2026, 4, 10), april[0]["visible_date"])
        with self.assertRaisesRegex(ValueError, "conflict"):
            save(({**facts[0], "payload_hash": "3" * 64},))


if __name__ == "__main__":
    unittest.main()
