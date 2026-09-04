"""Offline RED contracts for the v3 research-fact schema and governance seam."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from inspect import signature
from pathlib import Path

from stock_mcp.domain import DailyBar, MarketSnapshot, Security, StrategyVersion
from stock_mcp.storage import Database

AS_OF = datetime(2026, 8, 7, 8, 30, tzinfo=UTC)
TRADE_DATE = date(2026, 8, 7)
SOURCE = "recorded-tushare-2026-08-07"
V2 = "v2-observation"
V3 = "v3-proposed"
PIPELINE_VERSION = "facts-v3"
INPUT_HASH = "1" * 64
OUTPUT_HASH = "2" * 64
OUTCOME_HASH = "3" * 64


def _strategy(version: str) -> StrategyVersion:
    if version == V3:
        from stock_mcp.v3 import v3_proposal_parameters

        parameters = v3_proposal_parameters(1)
    else:
        parameters = {
            "rule_engine_version": 2,
            "offensive_min_bps": 5_500,
            "defensive_max_bps": 4_000,
            "neutral_limit": 2,
            "offensive_limit": 3,
            "min_liquidity_amount_fen": 2_000_000_000,
            "max_consecutive_limit_up_days": 2,
            "strong_pullback_min_prior_gain_bps": 1_000,
            "strong_pullback_max_pullback_bps": 800,
            "volume_breakout_min_volume_ratio_bps": 15_000,
        }
    return StrategyVersion(
        version=version,
        status="proposed",
        parameters=parameters,
    )


def _parameters_hash(strategy: StrategyVersion) -> str:
    import json

    return sha256(
        json.dumps(strategy.parameters, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class V3StorageContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "research.sqlite3"
        self.database = Database(self.path)
        self.database.initialize()

    def test_v9_database_migrates_through_v10_to_current_schema(self) -> None:
        legacy = Path(self.temporary.name) / "legacy-v9.sqlite3"
        with sqlite3.connect(legacy) as connection:
            connection.execute("CREATE TABLE retained_v9_evidence (value TEXT NOT NULL)")
            connection.execute("INSERT INTO retained_v9_evidence VALUES ('recorded')")
            connection.execute("PRAGMA user_version = 9")

        Database(legacy).initialize()

        with sqlite3.connect(legacy) as connection:
            self.assertEqual(15, connection.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            self.assertTrue(
                {
                    "daily_price_limits",
                    "v3_snapshot_features",
                    "strategy_version_relations",
                    "strategy_lifecycle_events",
                }.issubset(tables)
            )
            self.assertEqual(
                "recorded",
                connection.execute("SELECT value FROM retained_v9_evidence").fetchone()[0],
            )
            job_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(strategy_replay_jobs)")
            }
            self.assertTrue(
                {
                    "pipeline_version",
                    "input_hash",
                    "warmup_sessions",
                    "outcome_json",
                    "outcome_hash",
                }.issubset(job_columns)
            )
            attestation_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(strategy_replay_attestations)")
            }
            self.assertIn("outcome_hash", attestation_columns)

    def test_price_limits_are_idempotent_but_reject_conflicting_recorded_facts(self) -> None:
        save = self._require("save_daily_price_limits")
        load = self._require("load_daily_price_limits")
        original = {"600001.SH": {"limit_up_1e4": 110_000, "limit_down_1e4": 90_000}}

        save(trade_date=TRADE_DATE, source=SOURCE, limits=original)
        save(trade_date=TRADE_DATE, source=SOURCE, limits=original)

        self.assertEqual(original, load(TRADE_DATE, source=SOURCE))
        with self.assertRaisesRegex(ValueError, "immutable|conflict"):
            save(
                trade_date=TRADE_DATE,
                source=SOURCE,
                limits={"600001.SH": {"limit_up_1e4": 111_000, "limit_down_1e4": 90_000}},
            )
        self.assertEqual(original, load(TRADE_DATE, source=SOURCE))

    def test_complete_market_snapshot_requires_admitted_main_board_bar_coverage(self) -> None:
        securities = tuple(
            Security(
                f"60000{index}.SH",
                f"样本{index}",
                "SSE",
                "MAIN",
                date(2020, 1, 1),
                "银行",
                False,
            )
            for index in range(1, 3)
        )
        bars = tuple(
            DailyBar(
                security.symbol,
                TRADE_DATE,
                100_000,
                101_000,
                99_000,
                100_000,
                100_000,
                1_000_000,
                10_000_000_000,
                "tushare",
                AS_OF,
            )
            for security in securities
        )
        self.database.save_market_snapshot(
            MarketSnapshot(
                TRADE_DATE,
                "tushare",
                AS_OF,
                securities,
                bars[:1],
                5_000,
                5_000,
            )
        )

        self.assertFalse(
            self.database.has_complete_market_snapshot(
                TRADE_DATE,
                source="tushare",
                minimum_main_board_count=2,
            )
        )

        complete_day = TRADE_DATE + timedelta(days=1)
        complete_bars = tuple(
            DailyBar(
                bar.symbol,
                complete_day,
                bar.open_1e4,
                bar.high_1e4,
                bar.low_1e4,
                bar.close_1e4,
                bar.pre_close_1e4,
                bar.volume_shares,
                bar.amount_fen,
                bar.source,
                bar.source_timestamp,
            )
            for bar in bars
        )
        self.database.save_market_snapshot(
            MarketSnapshot(
                complete_day,
                "tushare",
                AS_OF,
                securities,
                complete_bars,
                5_000,
                5_000,
            )
        )
        self.assertTrue(
            self.database.has_complete_market_snapshot(
                complete_day,
                source="tushare",
                minimum_main_board_count=2,
            )
        )

    def test_daily_status_batch_is_complete_only_when_expected_universe_is_covered(self) -> None:
        complete = self._require("has_complete_daily_security_status")
        statuses = tuple(
            {
                "symbol": symbol,
                "trade_date": TRADE_DATE,
                "source": "baostock",
                "tradestatus": "1",
                "is_st": False,
                "source_timestamp": AS_OF,
                "batch_sha256": sha256(symbol.encode()).hexdigest(),
            }
            for symbol in ("600001.SH", "600002.SH")
        )
        self.database.save_baostock_status_batch(
            run_id="fixture-status-run",
            trade_date=TRADE_DATE,
            statuses=statuses,
            checkpoint={
                "schema": "baostock-daily-status-v1",
                "status": "complete",
                "trade_date": TRADE_DATE.isoformat(),
                "row_count": 2,
            },
        )

        self.assertTrue(
            complete(
                TRADE_DATE,
                source="baostock",
                expected_symbols=frozenset({"600001.SH", "600002.SH"}),
                minimum_count=2,
            )
        )
        self.assertFalse(
            complete(
                TRADE_DATE,
                source="baostock",
                expected_symbols=frozenset({"600001.SH", "600003.SH"}),
                minimum_count=2,
            )
        )

    def test_daily_status_rows_without_atomic_checkpoint_are_not_complete(self) -> None:
        self.database.save_daily_security_statuses(
            (
                {
                    "symbol": "600001.SH",
                    "trade_date": TRADE_DATE,
                    "source": "baostock",
                    "tradestatus": "1",
                    "is_st": False,
                    "source_timestamp": AS_OF,
                    "batch_sha256": "a" * 64,
                },
            )
        )

        self.assertFalse(
            self.database.has_complete_daily_security_status(
                TRADE_DATE,
                source="baostock",
                expected_symbols=frozenset({"600001.SH"}),
                minimum_count=1,
            )
        )

    def test_daily_status_eligibility_loader_preserves_point_in_time_st_fact(self) -> None:
        load = getattr(self.database, "load_daily_security_eligibility_statuses", None)
        self.assertTrue(
            callable(load),
            "live v3 status loading must preserve both tradeStatus and is_st",
        )
        if not callable(load):
            return
        self.database.save_daily_security_statuses(
            (
                {
                    "symbol": "600165.SH",
                    "trade_date": TRADE_DATE,
                    "source": "baostock",
                    "tradestatus": "1",
                    "is_st": True,
                    "source_timestamp": AS_OF,
                    "batch_sha256": "b" * 64,
                },
            )
        )

        self.assertEqual(
            {
                ("600165.SH", TRADE_DATE): {
                    "tradestatus": "1",
                    "is_st": True,
                }
            },
            load(TRADE_DATE, TRADE_DATE, source="baostock"),
        )

    def test_v3_snapshot_features_are_batch_atomic_and_immutable(self) -> None:
        save = self._require("save_v3_snapshot_features")
        load = self._require("load_v3_snapshot_features")
        original = {
            "600001.SH": {"industry": "银行", "price_limit_state": "none"},
            "600002.SH": {"industry": "电力设备", "price_limit_state": "limit_up"},
        }
        save(trade_date=TRADE_DATE, source=SOURCE, features=original)
        save(trade_date=TRADE_DATE, source=SOURCE, features=original)

        with self.assertRaisesRegex(ValueError, "immutable|conflict"):
            save(
                trade_date=TRADE_DATE,
                source=SOURCE,
                features={
                    **original,
                    "600002.SH": {"industry": "电力设备", "price_limit_state": "none"},
                },
            )

        self.assertEqual(original, load(TRADE_DATE, source=SOURCE))

    def test_replay_job_binds_pipeline_input_warmup_and_outcome_hashes(self) -> None:
        strategy = _strategy(V3)
        self.database.save_strategy_version(strategy)
        create = self._require("create_strategy_replay_job")
        complete = self._require("complete_strategy_replay")
        self.assertTrue(
            {"pipeline_version", "input_hash", "warmup_sessions"}.issubset(
                signature(create).parameters
            ),
            "v3 replay creation must bind its pipeline, input, and warmup evidence",
        )
        self.assertTrue(
            {"outcome", "outcome_hash"}.issubset(signature(complete).parameters),
            "v3 replay completion must persist a structured outcome and its hash",
        )
        job = create(
            strategy_version=V3,
            parameters_hash=_parameters_hash(strategy),
            source=SOURCE,
            start_date=TRADE_DATE,
            end_date=TRADE_DATE,
            expected_sessions=(TRADE_DATE,),
            pipeline_version=PIPELINE_VERSION,
            input_hash=INPUT_HASH,
            warmup_sessions=20,
        )
        save_day = self._require("save_strategy_replay_day")
        save_day(
            job["job_id"],
            trade_date=TRADE_DATE,
            input_hash=INPUT_HASH,
            output_hash=OUTPUT_HASH,
            result={"warmup": True, "candidates": []},
        )

        completed = complete(
            job["job_id"],
            dataset_hash=INPUT_HASH,
            result_hash=OUTPUT_HASH,
            outcome={"status": "no_candidates"},
            outcome_hash=OUTCOME_HASH,
            summary={"sessions": 1},
        )

        self.assertEqual(PIPELINE_VERSION, completed["pipeline_version"])
        self.assertEqual(INPUT_HASH, completed["input_hash"])
        self.assertEqual(20, completed["warmup_sessions"])
        self.assertEqual({"status": "no_candidates"}, completed["outcome"])
        self.assertEqual(OUTCOME_HASH, completed["outcome_hash"])

    def test_completed_v3_replay_with_pending_outcome_is_resumable(self) -> None:
        strategy = _strategy(V3)
        self.database.save_strategy_version(strategy)
        job = self.database.create_strategy_replay_job(
            strategy_version=V3,
            parameters_hash=_parameters_hash(strategy),
            source=SOURCE,
            start_date=TRADE_DATE,
            end_date=TRADE_DATE,
            expected_sessions=(TRADE_DATE,),
            pipeline_version="pipeline-v0.2",
            input_hash=INPUT_HASH,
            warmup_sessions=60,
            input_hash_schema="v3-input-v1",
            result_hash_schema="v3-result-v1",
            outcome_hash_schema="v3-outcome-v1",
        )
        self.database.save_strategy_replay_day(
            str(job["job_id"]),
            trade_date=TRADE_DATE,
            input_hash=INPUT_HASH,
            output_hash=OUTPUT_HASH,
            result={"warmup": True, "candidates": []},
        )
        completed = self.database.complete_strategy_replay(
            str(job["job_id"]),
            dataset_hash=INPUT_HASH,
            result_hash=OUTPUT_HASH,
            summary={"sessions": 1},
        )
        self.assertEqual("queued", completed["outcome_status"])

        pending = self.database.get_next_pending_strategy_replay_outcome_job()

        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(job["job_id"], pending["job_id"])

    def test_attestation_carries_the_immutable_replay_outcome_hash(self) -> None:
        strategy = _strategy(V3)
        self.database.save_strategy_version(strategy)
        record = self._require("record_verified_strategy_replay_attestation")

        first = record(
            strategy_version=V3,
            parameters_hash=_parameters_hash(strategy),
            dataset_hash=INPUT_HASH,
            result_hash=OUTPUT_HASH,
            outcome_hash=OUTCOME_HASH,
        )
        repeated = record(
            strategy_version=V3,
            parameters_hash=_parameters_hash(strategy),
            dataset_hash=INPUT_HASH,
            result_hash=OUTPUT_HASH,
            outcome_hash=OUTCOME_HASH,
        )

        self.assertEqual(first, repeated)
        self.assertEqual(OUTCOME_HASH, first["outcome_hash"])
        with self.assertRaisesRegex(ValueError, "immutable|conflict"):
            record(
                strategy_version=V3,
                parameters_hash=_parameters_hash(strategy),
                dataset_hash=INPUT_HASH,
                result_hash=OUTPUT_HASH,
                outcome_hash="4" * 64,
            )

    def test_zero_session_synthetic_v3_attestation_cannot_activate(self) -> None:
        strategy = _strategy(V3)
        self.database.save_strategy_version(strategy)
        self.database.record_verified_strategy_replay_attestation(
            strategy_version=V3,
            parameters_hash=_parameters_hash(strategy),
            dataset_hash=INPUT_HASH,
            result_hash=OUTPUT_HASH,
            outcome_hash=OUTCOME_HASH,
        )
        self.database.approve_strategy_version(V3)

        self.assertEqual(
            "replay_attestation_required",
            self.database.activate_strategy_version_with_grants(V3, _parameters_hash(strategy)),
        )

    def test_official_v03_name_cannot_bypass_v3_gates_with_legacy_parameters(self) -> None:
        strategy = _strategy("v0.3-policy-1")
        self.database.save_strategy_version(strategy)
        self.database.record_verified_strategy_replay_attestation(
            strategy_version=strategy.version,
            parameters_hash=_parameters_hash(strategy),
            dataset_hash=INPUT_HASH,
            result_hash=OUTPUT_HASH,
            outcome_hash=OUTCOME_HASH,
        )
        self.database.approve_strategy_version(strategy.version)

        self.assertNotEqual(
            "ok",
            self.database.activate_strategy_version_with_grants(
                strategy.version, _parameters_hash(strategy)
            ),
        )

    def test_strategy_relations_are_immutable_and_lifecycle_events_only_append(self) -> None:
        self.database.save_strategy_version(_strategy(V2))
        self.database.save_strategy_version(_strategy(V3))
        relate = self._require("save_strategy_version_relation")
        list_relations = self._require("list_strategy_version_relations")
        append_event = self._require("append_strategy_lifecycle_event")
        list_events = self._require("list_strategy_lifecycle_events")

        relation = {"predecessor": V2, "successor": V3, "relation": "supersedes"}
        relate(**relation)
        relate(**relation)
        self.assertEqual((relation,), list_relations(V3))
        with self.assertRaisesRegex(ValueError, "immutable|conflict"):
            relate(predecessor=V2, successor=V3, relation="derived_from")

        append_event(version=V3, event_type="proposed", occurred_at=AS_OF, detail="recorded")
        append_event(version=V3, event_type="certified", occurred_at=AS_OF, detail="recorded")
        self.assertEqual(
            ("proposed", "certified"),
            tuple(event["event_type"] for event in list_events(V3)),
        )

    def test_proposal_and_supersedes_relation_are_one_atomic_write(self) -> None:
        proposal = _strategy(V3)

        with self.assertRaisesRegex(ValueError, "predecessor|does not exist"):
            self.database.save_strategy_proposal_with_relation(proposal, predecessor="missing-v2")

        self.assertIsNone(self.database.load_strategy_version(V3))
        self.assertEqual((), self.database.list_strategy_version_relations(V3))

    def test_v3_activation_supersedes_v2_atomically_without_erasing_v2_history(self) -> None:
        v2 = _strategy(V2)
        v3 = _strategy(V3)
        self.database.save_strategy_version(v2)
        self.database.save_strategy_version(v3)
        self._seed_v3_proof(v3)
        self.database.set_active_strategy_version(V2)
        self.database.approve_strategy_version(V3)

        self.assertEqual(
            "ok",
            self.database.activate_strategy_version_with_grants(V3, _parameters_hash(v3)),
        )
        self.assertEqual(V3, self.database.get_active_strategy_version().version)
        self.assertEqual("superseded", self._require("get_strategy_lifecycle_state")(V2))
        self.assertEqual(v2, self.database.load_strategy_version(V2), "v2 facts remain auditable")
        with self.assertRaisesRegex(ValueError, "superseded|reactivat"):
            self.database.activate_strategy_version_with_grants(V2, _parameters_hash(v2))

    def test_activation_failure_rolls_back_v3_pointer_and_v2_supersession_together(self) -> None:
        v2 = _strategy(V2)
        v3 = _strategy(V3)
        self.database.save_strategy_version(v2)
        self.database.save_strategy_version(v3)
        self._seed_v3_proof(v3)
        self.database.set_active_strategy_version(V2)
        self.database.approve_strategy_version(V3)
        with self.database.connect() as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_v3_activation BEFORE UPDATE ON active_strategy
                BEGIN SELECT RAISE(ABORT, 'recorded activation fault'); END
                """
            )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "recorded activation fault"):
            self.database.activate_strategy_version_with_grants(V3, _parameters_hash(v3))

        self.assertEqual(V2, self.database.get_active_strategy_version().version)
        self.assertNotEqual("superseded", self._require("get_strategy_lifecycle_state")(V2))
        self.assertEqual(v2, self.database.load_strategy_version(V2))
        with self.database.connect() as connection:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM strategy_approvals WHERE version = ?", (V3,)
                ).fetchone(),
                "the one-time grant must remain after a failed atomic activation",
            )

    def _require(self, name: str):
        value = getattr(self.database, name, None)
        self.assertTrue(callable(value), f"Schema v10 requires Database.{name}()")
        return value

    def _seed_v3_proof(self, strategy: StrategyVersion) -> None:
        start = date(2023, 8, 8)
        end = date(2026, 8, 7)
        sessions = tuple(start + timedelta(days=round(index * 1_095 / 726)) for index in range(727))
        job = self.database.create_strategy_replay_job(
            strategy_version=strategy.version,
            parameters_hash=_parameters_hash(strategy),
            source=SOURCE,
            start_date=start,
            end_date=end,
            expected_sessions=sessions,
            pipeline_version="pipeline-v0.2",
            input_hash=INPUT_HASH,
            warmup_sessions=60,
            input_hash_schema="v3-input-v1",
            result_hash_schema="v3-result-v1",
            outcome_hash_schema="v3-outcome-v1",
        )
        for session in sessions:
            self.database.save_strategy_replay_day(
                str(job["job_id"]),
                trade_date=session,
                input_hash=INPUT_HASH,
                output_hash=OUTPUT_HASH,
                result={"warmup": True, "candidates": []},
            )
        self.database.complete_strategy_replay(
            str(job["job_id"]),
            dataset_hash=INPUT_HASH,
            result_hash=OUTPUT_HASH,
            summary={"sessions": 727},
            outcome={},
            outcome_hash=OUTCOME_HASH,
        )
        self.database.certify_strategy_replay(str(job["job_id"]))


if __name__ == "__main__":
    unittest.main()
