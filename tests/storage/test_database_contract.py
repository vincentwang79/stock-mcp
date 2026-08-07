from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from stock_mcp.domain import (
    Candidate,
    DailyBar,
    DailyReview,
    Evidence,
    MarketRegime,
    SetupType,
    StrategyVersion,
)
from stock_mcp.storage import Database

AS_OF = datetime(2026, 8, 7, 8, 30, tzinfo=UTC)
TRADE_DATE = date(2026, 8, 7)
SOURCE = "fixed-fixture"
STRATEGY_VERSION = "v0.1"


def _bars() -> tuple[DailyBar, ...]:
    return (
        DailyBar(
            symbol="600000.SH",
            trade_date=TRADE_DATE,
            open_1e4=100_000,
            high_1e4=105_000,
            low_1e4=99_000,
            close_1e4=103_000,
            pre_close_1e4=100_000,
            volume_shares=1_000_000,
            amount_fen=103_000_000_00,
            source=SOURCE,
            source_timestamp=AS_OF,
        ),
        DailyBar(
            symbol="600001.SH",
            trade_date=TRADE_DATE,
            open_1e4=200_000,
            high_1e4=209_000,
            low_1e4=198_000,
            close_1e4=207_000,
            pre_close_1e4=200_000,
            volume_shares=2_000_000,
            amount_fen=414_000_000_00,
            source=SOURCE,
            source_timestamp=AS_OF,
        ),
    )


def _strategy() -> StrategyVersion:
    return StrategyVersion(
        version=STRATEGY_VERSION,
        status="published",
        parameters={"offensive_min_bps": 5_500, "offensive_limit": 3},
    )


def _review() -> DailyReview:
    candidate = Candidate(
        candidate_id=f"{TRADE_DATE.isoformat()}:{STRATEGY_VERSION}:600000.SH",
        symbol="600000.SH",
        name="浦发银行",
        rank=1,
        score=87,
        setup_type=SetupType.VOLUME_BREAKOUT,
        strategy_version=STRATEGY_VERSION,
        evidence=(
            Evidence(
                metric="close_above_pre_close_bps",
                value=300,
                threshold=0,
                passed=True,
                score_contribution=20,
            ),
        ),
        confirmation_condition="next close remains above 10.30",
        invalidation_condition="close below 9.90",
    )
    return DailyReview(
        status="ready",
        trade_date=TRADE_DATE,
        source=SOURCE,
        source_timestamp=AS_OF,
        strategy_version=STRATEGY_VERSION,
        market_regime=MarketRegime.OFFENSIVE,
        candidates=(candidate,),
    )


class DatabaseContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "stock.sqlite3"
        self.database = Database(self.database_path)
        self.database.initialize()

    def test_initialize_enables_foreign_keys_and_wal_for_each_connection(self) -> None:
        connection = self.database.connect()
        self.addCleanup(connection.close)

        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_initialize_migrates_legacy_idempotency_writes_with_a_schema_version(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.execute(
                """
                CREATE TABLE idempotent_writes (
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    PRIMARY KEY (operation, idempotency_key)
                )
                """
            )
            connection.execute(
                "INSERT INTO idempotent_writes VALUES ('create_watchlist', 'legacy-key', '[]')"
            )

        migrated = Database(legacy_path)
        migrated.initialize()
        migrated.initialize()

        with sqlite3.connect(legacy_path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(idempotent_writes)")}
            self.assertIn("request_hash", columns)
            self.assertEqual(4, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(
                "[]",
                connection.execute(
                    "SELECT result_json FROM idempotent_writes "
                    "WHERE operation = 'create_watchlist' AND idempotency_key = 'legacy-key'"
                ).fetchone()[0],
            )

    def test_save_and_load_daily_bars_are_unique_per_symbol_date_and_source(self) -> None:
        bars = _bars()

        self.database.save_daily_bars(bars)

        self.assertEqual(self.database.load_daily_bars(TRADE_DATE, SOURCE), bars)

    def test_saving_the_same_daily_bar_batch_for_trade_date_and_source_is_idempotent(self) -> None:
        bars = _bars()

        self.database.save_daily_bars(bars)
        self.database.save_daily_bars(bars)

        self.assertEqual(self.database.load_daily_bars(TRADE_DATE, SOURCE), bars)

    def test_symbol_history_is_cut_off_at_the_requested_date(self) -> None:
        bars = _bars()
        prior = replace(bars[0], trade_date=date(2026, 8, 6))
        future = replace(bars[0], trade_date=date(2026, 8, 8))
        self.database.save_daily_bars((prior, bars[0], future))

        history = self.database.load_symbol_history(
            bars[0].symbol,
            end_date=TRADE_DATE,
            source=SOURCE,
            limit=20,
        )

        self.assertEqual((prior, bars[0]), history)

    def test_strategy_version_cannot_be_changed_after_it_is_saved(self) -> None:
        strategy = _strategy()
        self.database.save_strategy_version(strategy)

        self.assertEqual(self.database.load_strategy_version(strategy.version), strategy)
        changed = replace(strategy, parameters={"offensive_min_bps": 6_000, "offensive_limit": 3})
        with self.assertRaises(ValueError):
            self.database.save_strategy_version(changed)

    def test_daily_review_and_candidates_round_trip_with_their_strategy_version(self) -> None:
        strategy = _strategy()
        review = _review()
        self.database.save_strategy_version(strategy)

        self.database.save_daily_review(review)

        stored = self.database.load_daily_review(TRADE_DATE, STRATEGY_VERSION)
        self.assertEqual(stored, review)
        self.assertEqual(stored.strategy_version, strategy.version)
        self.assertTrue(
            all(candidate.strategy_version == strategy.version for candidate in stored.candidates)
        )

    def test_watchlist_events_are_appended_instead_of_overwriting_history(self) -> None:
        self.database.append_watchlist_event(
            symbol="600000.SH",
            event_type="added",
            occurred_at=AS_OF,
            detail="volume breakout",
        )
        self.database.append_watchlist_event(
            symbol="600000.SH",
            event_type="removed",
            occurred_at=datetime(2026, 8, 8, 8, 30, tzinfo=UTC),
            detail="invalidation hit",
        )

        self.assertEqual(
            self.database.list_watchlist_events("600000.SH"),
            (
                ("added", AS_OF, "volume breakout"),
                ("removed", datetime(2026, 8, 8, 8, 30, tzinfo=UTC), "invalidation hit"),
            ),
        )

    def test_candidate_events_are_appended_instead_of_overwriting_history(self) -> None:
        candidate_id = _review().candidates[0].candidate_id
        self.database.append_candidate_event(
            candidate_id=candidate_id,
            event_type="reviewed",
            occurred_at=AS_OF,
            detail="evidence checked",
        )
        self.database.append_candidate_event(
            candidate_id=candidate_id,
            event_type="invalidated",
            occurred_at=datetime(2026, 8, 8, 8, 30, tzinfo=UTC),
            detail="close crossed invalidation",
        )

        self.assertEqual(
            self.database.list_candidate_events(candidate_id),
            (
                ("reviewed", AS_OF, "evidence checked"),
                (
                    "invalidated",
                    datetime(2026, 8, 8, 8, 30, tzinfo=UTC),
                    "close crossed invalidation",
                ),
            ),
        )

    def test_online_backup_can_be_opened_and_recovered(self) -> None:
        bars = _bars()
        self.database.save_daily_bars(bars)
        backup_path = Path(self.temp_dir.name) / "backup.sqlite3"

        self.database.backup_to(backup_path)

        with sqlite3.connect(backup_path) as backup_connection:
            self.assertEqual(
                backup_connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
        recovered = Database(backup_path)
        recovered.initialize()
        self.assertEqual(recovered.load_daily_bars(TRADE_DATE, SOURCE), bars)

    def test_doctor_reports_integrity_without_exposing_application_data(self) -> None:
        report = self.database.doctor()

        self.assertEqual("ok", report["integrity"])
        self.assertEqual("wal", report["journal_mode"])
        self.assertNotIn("rows", report)


if __name__ == "__main__":
    unittest.main()
