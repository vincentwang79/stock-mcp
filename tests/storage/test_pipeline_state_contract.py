from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from stock_mcp.domain import (
    Candidate,
    DailyReview,
    Evidence,
    MarketRegime,
    SetupType,
    StrategyVersion,
)
from stock_mcp.pipeline import PipelineRun
from stock_mcp.production import SQLitePipelineRepository, SQLiteScheduleState
from stock_mcp.scheduler import ScheduleOutcome
from stock_mcp.storage import Database

TRADE_DATE = date(2026, 8, 7)
PIPELINE_VERSION = "pipeline-v0.1"
STRATEGY_VERSION = "v0.1"
AS_OF = datetime(2026, 8, 7, 8, 30, tzinfo=UTC)


def _parameters() -> dict[str, int]:
    return {
        "rule_engine_version": 1,
        "offensive_min_bps": 5_500,
        "defensive_max_bps": 4_000,
        "neutral_limit": 3,
        "offensive_limit": 5,
        "min_liquidity_amount_fen": 100_000_000,
        "max_consecutive_limit_up_days": 2,
        "strong_pullback_min_prior_gain_bps": 1_000,
        "strong_pullback_max_pullback_bps": 500,
        "volume_breakout_min_volume_ratio_bps": 15_000,
    }


def _review() -> DailyReview:
    return DailyReview(
        status="published",
        trade_date=TRADE_DATE,
        source="fixed-fixture",
        source_timestamp=AS_OF,
        strategy_version=STRATEGY_VERSION,
        market_regime=MarketRegime.DEFENSIVE,
        candidates=(),
    )


def _candidate_review() -> DailyReview:
    candidate = Candidate(
        candidate_id=f"{TRADE_DATE.isoformat()}:{STRATEGY_VERSION}:600001.SH",
        symbol="600001.SH",
        name="观察样本",
        rank=1,
        score=20,
        setup_type=SetupType.STRONG_PULLBACK,
        strategy_version=STRATEGY_VERSION,
        evidence=(Evidence("base_score", 20, 20, True, 20),),
        confirmation_condition="close >= 100000",
        invalidation_condition="close < 90000",
    )
    return DailyReview(
        status="ready",
        trade_date=TRADE_DATE,
        source="fixed-fixture",
        source_timestamp=AS_OF,
        strategy_version=STRATEGY_VERSION,
        market_regime=MarketRegime.OFFENSIVE,
        candidates=(candidate,),
    )


class SQLitePipelineStateContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.database = Database(self.root / "data" / "stock.sqlite3")
        self.database.initialize()
        self.database.save_strategy_version(
            StrategyVersion(
                version=STRATEGY_VERSION,
                status="active",
                parameters=_parameters(),
            )
        )

    def test_published_review_without_run_state_is_recovered_as_idempotent_ready_run(self) -> None:
        self.database.save_daily_review(_review())

        run = SQLitePipelineRepository(self.database).get_run(TRADE_DATE, PIPELINE_VERSION)

        self.assertIsNotNone(run)
        self.assertEqual("ready", run.status)
        self.assertEqual(0, run.attempts)
        self.assertEqual(_review(), run.review)
        self.assertIsNone(
            SQLitePipelineRepository(self.database).get_run(TRADE_DATE, "pipeline-v0.2"),
            "a legacy review must not be relabelled as a pipeline version that never ran",
        )

    def test_backup_restore_recovers_sqlite_schedule_state_and_ignores_newer_json_terminal_state(
        self,
    ) -> None:
        repository = SQLitePipelineRepository(self.database)
        state = SQLiteScheduleState(self.database, repository)
        run = PipelineRun(
            trade_date=TRADE_DATE,
            pipeline_version=PIPELINE_VERSION,
            status="failed",
            attempts=1,
            snapshot=None,
            review=None,
            error="fixture upstream failure",
        )
        repository.save_run(run)
        expected = ScheduleOutcome(
            trade_date=TRADE_DATE,
            status="retry_scheduled",
            next_at=AS_OF + timedelta(hours=9),
            run=run,
            error=run.error,
        )
        state.save(expected)
        backup_path = self.root / "backup.sqlite3"
        self.database.backup_to(backup_path)

        restored = Database(self.root / "restored" / "stock.sqlite3")
        restored.initialize()
        backup = Database(backup_path)
        backup.initialize()
        backup.backup_to(restored.path)
        legacy_state = self.root / "restored" / "state" / "schedule-state.json"
        legacy_state.parent.mkdir(parents=True)
        legacy_state.write_text(
            json.dumps(
                {
                    TRADE_DATE.isoformat(): {
                        "status": "ready",
                        "next_at": None,
                        "pipeline_version": PIPELINE_VERSION,
                        "error": None,
                    }
                }
            ),
            encoding="utf-8",
        )

        restored_repository = SQLitePipelineRepository(restored)
        restored_state = SQLiteScheduleState(restored, restored_repository)

        self.assertEqual(expected, restored_state.get(TRADE_DATE))
        self.assertIsNone(restored_state.get(TRADE_DATE + timedelta(days=1)))

    def test_ready_run_requires_a_review_that_can_be_loaded_from_sqlite(self) -> None:
        repository = SQLitePipelineRepository(self.database)
        invalid = PipelineRun(
            trade_date=TRADE_DATE,
            pipeline_version=PIPELINE_VERSION,
            status="ready",
            attempts=1,
            snapshot=None,
            review=None,
        )

        with self.assertRaisesRegex(ValueError, "ready.*review"):
            repository.save_run(invalid)

        self.assertIsNone(repository.get_run(TRADE_DATE, PIPELINE_VERSION))

    def test_expected_trading_calendar_is_durable_and_included_in_online_backup(self) -> None:
        days = (TRADE_DATE - timedelta(days=1), TRADE_DATE)
        self.database.save_expected_trading_days("tushare", days)
        backup_path = self.root / "calendar-backup.sqlite3"
        self.database.backup_to(backup_path)
        restored = Database(backup_path)
        restored.initialize()

        self.assertEqual(
            days,
            restored.load_expected_trading_days(days[0], days[-1], source="tushare"),
        )

    def test_pipeline_versions_have_distinct_run_identity_for_one_deterministic_review(
        self,
    ) -> None:
        repository = SQLitePipelineRepository(self.database)
        for pipeline_version in ("pipeline-v0.1", "pipeline-v0.2"):
            repository.save_run(
                PipelineRun(
                    trade_date=TRADE_DATE,
                    pipeline_version=pipeline_version,
                    status="ready",
                    attempts=1,
                    snapshot=None,
                    review=_review(),
                )
            )

        self.assertEqual(_review(), repository.get_run(TRADE_DATE, "pipeline-v0.1").review)
        self.assertEqual(_review(), repository.get_run(TRADE_DATE, "pipeline-v0.2").review)

    def test_auditable_live_observation_sessions_are_counted_in_sqlite(self) -> None:
        repository = SQLitePipelineRepository(self.database)
        repository.save_run(
            PipelineRun(
                trade_date=TRADE_DATE,
                pipeline_version=PIPELINE_VERSION,
                status="degraded_observation",
                attempts=1,
                snapshot=None,
                review=_review(),
                error="live observation period is not yet complete",
            )
        )

        self.assertEqual(1, self.database.count_live_observation_sessions(PIPELINE_VERSION))
        restored = repository.get_run(TRADE_DATE, PIPELINE_VERSION)
        self.assertEqual("degraded_observation", restored.status)
        self.assertEqual(replace(_review(), status="observation"), restored.review)

    def test_live_observation_gate_counts_only_the_active_strategy_version(self) -> None:
        repository = SQLitePipelineRepository(self.database)
        repository.save_run(
            PipelineRun(
                trade_date=TRADE_DATE,
                pipeline_version=PIPELINE_VERSION,
                status="degraded_observation",
                attempts=1,
                snapshot=None,
                review=_review(),
                error="live observation period is not yet complete",
            )
        )
        other_version = "v0.2"
        self.database.save_strategy_version(
            StrategyVersion(version=other_version, status="active", parameters=_parameters())
        )
        other_review = replace(
            _review(),
            trade_date=TRADE_DATE + timedelta(days=1),
            strategy_version=other_version,
        )
        repository.save_run(
            PipelineRun(
                trade_date=other_review.trade_date,
                pipeline_version=PIPELINE_VERSION,
                status="degraded_observation",
                attempts=1,
                snapshot=None,
                review=other_review,
                error="live observation period is not yet complete",
            )
        )

        self.assertEqual(
            1,
            self.database.count_live_observation_sessions(
                PIPELINE_VERSION, strategy_version=STRATEGY_VERSION
            ),
        )
        self.assertEqual(
            1,
            self.database.count_live_observation_sessions(
                PIPELINE_VERSION, strategy_version=other_version
            ),
        )

    def test_observation_candidates_are_auditable_but_not_public_or_writable(self) -> None:
        repository = SQLitePipelineRepository(self.database)
        review = _candidate_review()
        candidate_id = review.candidates[0].candidate_id
        repository.save_run(
            PipelineRun(
                trade_date=TRADE_DATE,
                pipeline_version=PIPELINE_VERSION,
                status="degraded_observation",
                attempts=1,
                snapshot=None,
                review=review,
                error="live observation period is not yet complete",
            )
        )

        restored = repository.get_run(TRADE_DATE, PIPELINE_VERSION)
        self.assertEqual("observation", restored.review.status)
        self.assertEqual((candidate_id,), tuple(c.candidate_id for c in restored.review.candidates))
        self.assertIsNone(self.database.get_daily_review(TRADE_DATE))
        self.assertIsNone(self.database.get_candidate(candidate_id))
        self.assertEqual((), self.database.list_review_history())
        self.assertIsNone(
            self.database.record_candidate_event(
                candidate_id=candidate_id,
                status="watched",
                event_date=TRADE_DATE,
                price_1e4=None,
                reason="must not annotate an unpublished candidate",
                idempotency_key="observation-event",
            )
        )
        self.assertIsNone(
            self.database.record_review_note(
                trade_date=TRADE_DATE,
                note="must not annotate an unpublished review",
                idempotency_key="observation-note",
            )
        )


if __name__ == "__main__":
    unittest.main()
