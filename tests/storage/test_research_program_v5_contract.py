"""Schema v13 contracts for the immutable research-program ledger."""

from __future__ import annotations

import sqlite3
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

    def test_schema_v13_persists_security_observations_and_outcomes_immutably(self) -> None:
        self.assertEqual(13, storage.SCHEMA_VERSION)
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
            "symbol": "600001.SH",
            "input_hash": "d" * 64,
            "result_hash": "e" * 64,
            "observation": {"candidate_count": 1, "status": "observed"},
            "recorded_at": "2026-08-10T10:00:00+00:00",
        }
        save_observation(observation)
        save_observation(observation)
        save_observation({**observation, "symbol": "600002.SH"})
        with self.assertRaisesRegex(ValueError, "conflict"):
            save_observation({**observation, "result_hash": "f" * 64})
        observations = self.database.list_research_forward_observations(
            hypothesis_id="no-recent-limit-up-v1"
        )
        self.assertEqual(["600001.SH", "600002.SH"], [item["symbol"] for item in observations])

        outcome = {
            "hypothesis_id": "no-recent-limit-up-v1",
            "signal_date": "2026-08-10",
            "symbol": "600001.SH",
            "horizon_sessions": 5,
            "observation_result_hash": "e" * 64,
            "outcome": {
                "entry_date": "2026-08-11",
                "exit_date": "2026-08-18",
                "gross_return_bps": 120,
                "benchmark_return_bps": 80,
                "excess_return_bps": 40,
            },
            "outcome_hash": "1" * 64,
            "recorded_at": "2026-08-18T10:00:00+00:00",
        }
        self.database.save_research_forward_outcome(outcome)
        self.database.save_research_forward_outcome(outcome)
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.database.save_research_forward_outcome({**outcome, "outcome_hash": "2" * 64})
        saved_outcomes = self.database.list_research_forward_outcomes(
            hypothesis_id="no-recent-limit-up-v1", symbol="600001.SH"
        )
        self.assertEqual(40, saved_outcomes[0]["outcome"]["excess_return_bps"])

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

    def test_forward_bundle_is_atomic_before_any_observation_is_visible(self) -> None:
        self.database.register_research_hypotheses(
            (
                {
                    "hypothesis_id": "overnight-intraday-separation-v1",
                    "family": "market-microstructure",
                    "title": "Overnight and intraday",
                    "mechanism": "separate returns",
                    "formula": {"version": 1},
                    "data_requirements": ["daily_bars"],
                    "status": "exploratory",
                    "sample_role": "new_discovery",
                    "frozen_after": None,
                    "registered_at": "2026-08-14T00:00:00+00:00",
                },
            )
        )
        observation = {
            "hypothesis_id": "overnight-intraday-separation-v1",
            "trade_date": "2025-01-02",
            "symbol": "600001.SH",
            "input_hash": "a" * 64,
            "result_hash": "b" * 64,
            "observation": {"overnight_return_bps": 100},
            "recorded_at": "2025-01-02T10:00:00+00:00",
        }
        invalid_outcome = {
            "hypothesis_id": "overnight-intraday-separation-v1",
            "signal_date": "2025-01-02",
            "symbol": "600001.SH",
            "horizon_sessions": 5,
            "observation_result_hash": "c" * 64,
            "outcome": {"gross_return_bps": 100},
            "outcome_hash": "d" * 64,
            "recorded_at": "2025-01-10T10:00:00+00:00",
        }
        save_bundle = getattr(self.database, "save_research_forward_bundle", None)
        self.assertTrue(callable(save_bundle), "forward evidence needs one atomic write")
        with self.assertRaisesRegex(ValueError, "observation hash"):
            save_bundle(observations=(observation,), outcomes=(invalid_outcome,))
        self.assertEqual(
            (),
            self.database.list_research_forward_observations(
                hypothesis_id="overnight-intraday-separation-v1"
            ),
        )

    def test_v12_observation_migrates_with_an_explicit_legacy_symbol(self) -> None:
        legacy_path = Path(self.temporary.name) / "legacy-v12.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(
                """
                CREATE TABLE research_hypotheses (
                    hypothesis_id TEXT PRIMARY KEY, family TEXT NOT NULL,
                    title TEXT NOT NULL, mechanism TEXT NOT NULL,
                    formula_json TEXT NOT NULL, data_requirements_json TEXT NOT NULL,
                    status TEXT NOT NULL, sample_role TEXT NOT NULL, frozen_after TEXT,
                    definition_hash TEXT NOT NULL, registered_at TEXT NOT NULL
                );
                CREATE TABLE research_forward_observations (
                    hypothesis_id TEXT NOT NULL, trade_date TEXT NOT NULL,
                    input_hash TEXT NOT NULL, result_hash TEXT NOT NULL,
                    observation_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
                    PRIMARY KEY(hypothesis_id, trade_date)
                );
                INSERT INTO research_hypotheses VALUES(
                    'legacy-v1','legacy','Legacy','retained','{}','[]','exploratory',
                    'new_discovery',NULL,
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    '2026-08-14T00:00:00+00:00'
                );
                INSERT INTO research_forward_observations VALUES(
                    'legacy-v1','2025-01-02',
                    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                    'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                    '{"retained":true}','2025-01-02T10:00:00+00:00'
                );
                PRAGMA user_version = 12;
                """
            )
        migrated = storage.Database(legacy_path)
        migrated.initialize()
        rows = migrated.list_research_forward_observations(hypothesis_id="legacy-v1")
        self.assertEqual(13, migrated.schema_version())
        self.assertEqual("legacy-unspecified", rows[0]["symbol"])
        self.assertEqual({"retained": True}, rows[0]["observation"])


if __name__ == "__main__":
    unittest.main()
