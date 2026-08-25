"""Offline persistence contracts for durable strategy-governance replays."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from stock_mcp.domain import StrategyVersion
from stock_mcp.storage import Database
from stock_mcp.strategy import canonical_strategy_parameters_hash

SOURCE = "recorded-fixture-2026-08-07"
STRATEGY_VERSION = "v0.2-proposed"
START_DATE = date(2023, 8, 7)
END_DATE = date(2026, 8, 7)
TRADING_DAYS = (date(2026, 8, 6), date(2026, 8, 7))
GOVERNANCE_START = date(2023, 8, 7)
GOVERNANCE_END = date(2026, 8, 7)
GOVERNANCE_SESSIONS = tuple(
    GOVERNANCE_START + timedelta(days=(GOVERNANCE_END - GOVERNANCE_START).days * ordinal // 599)
    for ordinal in range(600)
)
DATASET_HASH = "b" * 64
RESULT_HASH = "c" * 64
INPUT_HASH = "d" * 64
OUTPUT_HASH = "e" * 64


def _proposal() -> StrategyVersion:
    return StrategyVersion(
        version=STRATEGY_VERSION,
        status="proposed",
        parameters={
            "rule_engine_version": 1,
            "offensive_min_bps": 5_500,
            "defensive_max_bps": 4_000,
            "neutral_limit": 2,
            "offensive_limit": 3,
            "min_liquidity_amount_fen": 2_000_000_000,
            "max_consecutive_limit_up_days": 2,
            "strong_pullback_min_prior_gain_bps": 1_000,
            "strong_pullback_max_pullback_bps": 800,
            "volume_breakout_min_volume_ratio_bps": 15_000,
        },
    )


PARAMETERS_HASH = canonical_strategy_parameters_hash(_proposal().parameters)


class StrategyReplayPersistenceContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "stock.sqlite3"
        self.database = Database(self.database_path)
        self.database.initialize()
        self.database.save_strategy_version(_proposal())

    def test_v8_migration_creates_a_queued_durable_replay_job(self) -> None:
        """A persistent replay starts queued and retains immutable governance inputs."""
        legacy_path = Path(self.temp_dir.name) / "v8.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.execute("PRAGMA user_version = 8")

        migrated = Database(legacy_path)
        migrated.initialize()
        migrated.save_strategy_version(_proposal())

        create_job = getattr(migrated, "create_strategy_replay_job", None)
        self.assertTrue(callable(create_job), "v9 must expose durable replay-job creation")
        job = create_job(
            strategy_version=STRATEGY_VERSION,
            parameters_hash=PARAMETERS_HASH,
            source=SOURCE,
            start_date=START_DATE,
            end_date=END_DATE,
            expected_sessions=TRADING_DAYS,
        )

        self.assertEqual("queued", job["status"])
        self.assertEqual(STRATEGY_VERSION, job["strategy_version"])
        self.assertEqual(PARAMETERS_HASH, job["parameters_hash"])
        self.assertEqual(SOURCE, job["source"])
        self.assertEqual(TRADING_DAYS, job["expected_sessions"])
        list_jobs = self._require(migrated, "list_strategy_replay_jobs")
        self.assertEqual((job,), list_jobs())
        with sqlite3.connect(legacy_path) as connection:
            self.assertEqual(14, connection.execute("PRAGMA user_version").fetchone()[0])

    def test_replay_day_is_idempotent_only_for_the_same_immutable_input_and_output(self) -> None:
        job = self._create_job()
        result = {"market_regime": "defensive", "candidates": []}

        save_day = getattr(self.database, "save_strategy_replay_day", None)
        self.assertTrue(callable(save_day), "v9 must persist replay days through a public API")
        first = save_day(
            job["job_id"],
            trade_date=TRADING_DAYS[0],
            input_hash=INPUT_HASH,
            output_hash=OUTPUT_HASH,
            result=result,
        )
        repeated = save_day(
            job["job_id"],
            trade_date=TRADING_DAYS[0],
            input_hash=INPUT_HASH,
            output_hash=OUTPUT_HASH,
            result=result,
        )

        self.assertEqual(first, repeated)
        list_days = self._require(self.database, "list_strategy_replay_days")
        self.assertEqual(
            (first,),
            list_days(job["job_id"]),
        )
        with self.assertRaisesRegex(ValueError, "immutable|conflict"):
            save_day(
                job["job_id"],
                trade_date=TRADING_DAYS[0],
                input_hash=INPUT_HASH,
                output_hash="f" * 64,
                result=result,
            )

    def test_replay_days_must_be_saved_in_expected_calendar_order(self) -> None:
        job = self._create_job()

        with self.assertRaisesRegex(ValueError, "next|order"):
            self.database.save_strategy_replay_day(
                job["job_id"],
                trade_date=TRADING_DAYS[1],
                input_hash=INPUT_HASH,
                output_hash=OUTPUT_HASH,
                result={"warmup": True, "candidates": []},
            )

        self.assertEqual((), self.database.list_strategy_replay_days(job["job_id"]))

    def test_start_and_certification_keys_are_bound_atomically_to_their_requests(self) -> None:
        create = self._require(self.database, "create_strategy_replay_job")
        first = create(
            strategy_version=STRATEGY_VERSION,
            parameters_hash=PARAMETERS_HASH,
            source=SOURCE,
            start_date=START_DATE,
            end_date=END_DATE,
            expected_sessions=TRADING_DAYS,
            idempotency_key="start-key",
        )
        repeated = create(
            strategy_version=STRATEGY_VERSION,
            parameters_hash=PARAMETERS_HASH,
            source=SOURCE,
            start_date=START_DATE,
            end_date=END_DATE,
            expected_sessions=TRADING_DAYS,
            idempotency_key="start-key",
        )
        self.assertEqual(first["job_id"], repeated["job_id"])
        with self.assertRaisesRegex(ValueError, "idempotency"):
            create(
                strategy_version=STRATEGY_VERSION,
                parameters_hash=PARAMETERS_HASH,
                source=SOURCE,
                start_date=START_DATE + timedelta(days=1),
                end_date=END_DATE,
                expected_sessions=TRADING_DAYS,
                idempotency_key="start-key",
            )

        completed = self._completed_job(governance_grade=True)
        certify = self._require(self.database, "certify_strategy_replay")
        proof = certify(completed["job_id"], idempotency_key="certify-key")
        self.assertEqual(proof, certify(completed["job_id"], idempotency_key="certify-key"))
        other = self._completed_job(
            governance_grade=True, dataset_hash="f" * 64, result_hash="0" * 64
        )
        with self.assertRaisesRegex(ValueError, "idempotency"):
            certify(other["job_id"], idempotency_key="certify-key")

    def test_requeue_interrupted_job_keeps_days_and_reports_the_first_missing_session(self) -> None:
        job = self._create_job()
        self._save_day(job, TRADING_DAYS[0])
        self.assertEqual(
            "running",
            self._require(self.database, "get_strategy_replay_job")(job["job_id"])["status"],
        )

        reopened = Database(self.database_path)
        reopened.initialize()
        requeue = getattr(reopened, "requeue_interrupted_strategy_replays", None)
        self.assertTrue(callable(requeue), "v9 must recover interrupted replay jobs")
        self.assertEqual(1, requeue())

        get_job = self._require(reopened, "get_strategy_replay_job")
        recovered = get_job(job["job_id"])
        self.assertEqual("queued", recovered["status"])
        self.assertEqual(TRADING_DAYS[1], recovered["next_trade_date"])
        self.assertEqual(
            (TRADING_DAYS[0],),
            tuple(
                day["trade_date"]
                for day in self._require(reopened, "list_strategy_replay_days")(job["job_id"])
            ),
        )

    def test_oldest_runnable_job_is_not_hidden_by_two_hundred_terminal_jobs(self) -> None:
        queued = self._create_job()
        fail = self._require(self.database, "fail_strategy_replay")
        for index in range(200):
            terminal = self._create_job()
            fail(terminal["job_id"], error=f"recorded terminal {index}")

        get_next = getattr(self.database, "get_next_runnable_strategy_replay_job", None)
        self.assertTrue(callable(get_next), "worker needs a status-filtered oldest-job query")
        if not callable(get_next):
            return

        self.assertEqual(queued["job_id"], get_next()["job_id"])

    def test_completion_requires_every_expected_session_then_records_final_hashes(self) -> None:
        job = self._create_job()
        self._save_day(job, TRADING_DAYS[0])
        complete = getattr(self.database, "complete_strategy_replay", None)
        self.assertTrue(callable(complete), "v9 must finish a durable replay job")

        with self.assertRaisesRegex(ValueError, "missing|incomplete"):
            complete(
                job["job_id"],
                dataset_hash=DATASET_HASH,
                result_hash=RESULT_HASH,
                summary={"sessions": 2},
            )

        self._save_day(job, TRADING_DAYS[1])
        completed = complete(
            job["job_id"],
            dataset_hash=DATASET_HASH,
            result_hash=RESULT_HASH,
            summary={"sessions": 2},
        )

        self.assertEqual("completed", completed["status"])
        self.assertEqual(DATASET_HASH, completed["dataset_hash"])
        self.assertEqual(RESULT_HASH, completed["result_hash"])
        self.assertEqual({"sessions": 2}, completed["summary"])

    def test_failed_job_retains_its_failure_and_cannot_overwrite_completed_evidence(self) -> None:
        job = self._create_job()
        fail = getattr(self.database, "fail_strategy_replay", None)
        self.assertTrue(callable(fail), "v9 must retain a terminal replay failure")

        failed = fail(job["job_id"], error="recorded fixture is incomplete")

        self.assertEqual("failed", failed["status"])
        self.assertEqual("recorded fixture is incomplete", failed["error"])
        self.assertEqual(
            failed,
            self._require(self.database, "get_strategy_replay_job")(job["job_id"]),
        )

        completed = self._completed_job()
        with self.assertRaisesRegex(ValueError, "completed|terminal"):
            fail(completed["job_id"], error="must not rewrite completed evidence")

    def test_only_a_completed_governance_grade_job_can_create_permanent_proof(self) -> None:
        short_job = self._completed_job()
        certify = getattr(self.database, "certify_strategy_replay", None)
        self.assertTrue(callable(certify), "v9 must certify completed governance replays")
        with self.assertRaisesRegex(ValueError, "governance|coverage|session|span"):
            certify(short_job["job_id"])

        first = self._completed_job(governance_grade=True)

        attestation = certify(first["job_id"])
        self.assertEqual(STRATEGY_VERSION, attestation["strategy_version"])
        self.assertEqual(PARAMETERS_HASH, attestation["parameters_hash"])
        self.assertEqual(DATASET_HASH, attestation["dataset_hash"])
        self.assertEqual(RESULT_HASH, attestation["result_hash"])
        self.assertEqual(
            attestation,
            self._require(self.database, "get_strategy_replay_attestation")(STRATEGY_VERSION),
        )
        self.assertEqual(attestation, certify(first["job_id"]), "certification is retry-safe")

        conflicting = self._completed_job(
            governance_grade=True,
            dataset_hash="f" * 64,
            result_hash="0" * 64,
        )
        with self.assertRaisesRegex(ValueError, "conflict|immutable"):
            certify(conflicting["job_id"])
        self.assertEqual(
            attestation,
            self._require(self.database, "get_strategy_replay_attestation")(STRATEGY_VERSION),
        )

    def test_v8_legacy_attestation_is_retained_but_cannot_authorize_activation(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy-attestation-v8.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(
                """
                CREATE TABLE strategy_versions (
                    version TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    parameters_json TEXT NOT NULL
                );
                CREATE TABLE strategy_approvals (
                    version TEXT PRIMARY KEY,
                    parameters_hash TEXT NOT NULL,
                    approved_at TEXT NOT NULL
                );
                CREATE TABLE replay_attestations (
                    version TEXT PRIMARY KEY,
                    parameters_hash TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    dataset_hash TEXT,
                    start_date TEXT,
                    end_date TEXT,
                    session_count INTEGER
                );
                PRAGMA user_version = 8;
                """
            )
            connection.execute(
                "INSERT INTO strategy_versions VALUES (?, ?, ?)",
                (
                    STRATEGY_VERSION,
                    "proposed",
                    json.dumps(dict(_proposal().parameters), sort_keys=True, separators=(",", ":")),
                ),
            )
            connection.execute(
                "INSERT INTO strategy_approvals VALUES (?, ?, ?)",
                (STRATEGY_VERSION, PARAMETERS_HASH, "2026-08-07T08:30:00+00:00"),
            )
            connection.execute(
                "INSERT INTO replay_attestations VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    STRATEGY_VERSION,
                    PARAMETERS_HASH,
                    "2026-08-07T08:30:00+00:00",
                    DATASET_HASH,
                    START_DATE.isoformat(),
                    END_DATE.isoformat(),
                    400,
                ),
            )

        migrated = Database(legacy_path)
        migrated.initialize()

        self.assertEqual(
            "replay_attestation_required",
            migrated.activate_strategy_version_with_grants(STRATEGY_VERSION, PARAMETERS_HASH),
        )
        with migrated.connect() as connection:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM replay_attestations WHERE version = ?", (STRATEGY_VERSION,)
                ).fetchone(),
                "v8 evidence is retained for audit even though activation does not consume it",
            )

    def test_activation_consumes_only_approval_and_keeps_permanent_proof_on_failure(self) -> None:
        completed = self._completed_job(governance_grade=True)
        attestation = self._require(self.database, "certify_strategy_replay")(completed["job_id"])
        self.database.approve_strategy_version(STRATEGY_VERSION)
        with self.database.connect() as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_active_strategy BEFORE INSERT ON active_strategy
                BEGIN SELECT RAISE(ABORT, 'simulated active pointer failure'); END
                """
            )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "simulated active pointer failure"):
            self.database.activate_strategy_version_with_grants(STRATEGY_VERSION, PARAMETERS_HASH)

        self.assertIsNone(self.database.get_active_strategy_version())
        self.assertEqual(
            attestation,
            self._require(self.database, "get_strategy_replay_attestation")(STRATEGY_VERSION),
        )
        with self.database.connect() as connection:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM strategy_approvals WHERE version = ?", (STRATEGY_VERSION,)
                ).fetchone(),
                "failed activation must retain the operator approval for a safe retry",
            )
            connection.execute("DROP TRIGGER fail_active_strategy")

        self.assertEqual(
            "ok",
            self.database.activate_strategy_version_with_grants(STRATEGY_VERSION, PARAMETERS_HASH),
        )
        self.assertEqual(
            attestation,
            self._require(self.database, "get_strategy_replay_attestation")(STRATEGY_VERSION),
            "activation consumes approval, never the permanent replay proof",
        )
        with self.database.connect() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM strategy_approvals WHERE version = ?", (STRATEGY_VERSION,)
                ).fetchone()
            )

    def _create_job(self, *, governance_grade: bool = False) -> dict[str, object]:
        start_date, end_date, expected_sessions = (
            (GOVERNANCE_START, GOVERNANCE_END, GOVERNANCE_SESSIONS)
            if governance_grade
            else (START_DATE, END_DATE, TRADING_DAYS)
        )
        create_job = self._require(self.database, "create_strategy_replay_job")
        return create_job(
            strategy_version=STRATEGY_VERSION,
            parameters_hash=PARAMETERS_HASH,
            source=SOURCE,
            start_date=start_date,
            end_date=end_date,
            expected_sessions=expected_sessions,
        )

    def _save_day(self, job: dict[str, object], trade_date: date) -> None:
        save_day = self._require(self.database, "save_strategy_replay_day")
        save_day(
            job["job_id"],
            trade_date=trade_date,
            input_hash=INPUT_HASH,
            output_hash=OUTPUT_HASH,
            result={"market_regime": "defensive", "candidates": []},
        )

    def _completed_job(
        self,
        *,
        governance_grade: bool = False,
        dataset_hash: str = DATASET_HASH,
        result_hash: str = RESULT_HASH,
    ) -> dict[str, object]:
        job = self._create_job(governance_grade=governance_grade)
        expected_sessions = GOVERNANCE_SESSIONS if governance_grade else TRADING_DAYS
        for trade_date in expected_sessions:
            self._save_day(job, trade_date)
        complete = self._require(self.database, "complete_strategy_replay")
        return complete(
            job["job_id"],
            dataset_hash=dataset_hash,
            result_hash=result_hash,
            summary={"sessions": len(expected_sessions)},
        )

    def _require(self, target: object, name: str) -> object:
        method = getattr(target, name, None)
        self.assertTrue(callable(method), f"v9 must expose {name}")
        return method


if __name__ == "__main__":
    unittest.main()
