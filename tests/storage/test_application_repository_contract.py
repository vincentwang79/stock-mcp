"""Durable application-repository contract for :class:`Database`.

The application service is intentionally duck typed.  This test fixes the
small set of repository operations it needs against a real SQLite file, rather
than the in-memory fake used by the application-only contract tests.  Each
write is repeated through a fresh ``Database`` instance to prove that an
idempotency key is a persistent property, not a process-local cache.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path

from stock_mcp.application import StockMcpApplication
from stock_mcp.domain import (
    Candidate,
    DailyReview,
    Evidence,
    MarketRegime,
    SetupType,
    StrategyVersion,
)
from stock_mcp.storage import Database, IdempotencyKeyReuseError

AS_OF = datetime(2026, 8, 7, 8, 30, tzinfo=UTC)
TRADE_DATE = date(2026, 8, 7)


def _strategy(version: str, *, status: str = "proposed") -> StrategyVersion:
    return StrategyVersion(
        version=version,
        status=status,
        parameters={"offensive_limit": 3, "offensive_min_bps": 5_500},
    )


def _review(version: str, *, status: str = "published", score: int = 87) -> DailyReview:
    candidate = Candidate(
        candidate_id=f"{TRADE_DATE.isoformat()}:{version}:600000.SH",
        symbol="600000.SH",
        name="浦发银行",
        rank=1,
        score=score,
        setup_type=SetupType.STRONG_PULLBACK,
        strategy_version=version,
        evidence=(
            Evidence(
                metric="relative_strength_bps",
                value=1_250,
                threshold=800,
                passed=True,
                score_contribution=45,
            ),
        ),
        confirmation_condition="close >= 120000",
        invalidation_condition="close < 110000",
    )
    return DailyReview(
        status=status,
        trade_date=TRADE_DATE,
        source="tushare",
        source_timestamp=AS_OF,
        strategy_version=version,
        market_regime=MarketRegime.OFFENSIVE,
        candidates=(candidate,),
    )


class ApplicationRepositoryContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "stock.sqlite3"
        self.database = Database(self.path)
        self.database.initialize()

    def reopen(self) -> Database:
        reopened = Database(self.path)
        reopened.initialize()
        return reopened

    def _save_active_review(self, version: str = "v0.1") -> DailyReview:
        strategy = _strategy(version)
        review = _review(version)
        self.database.save_strategy_version(strategy)
        self.database.save_daily_review(review)
        self.database.set_active_strategy_version(version)
        return review

    def test_daily_review_lookup_preserves_history(
        self,
    ) -> None:
        older_strategy = _strategy("v0.1")
        older_review = _review("v0.1", score=70)
        newer_strategy = _strategy("v0.2")
        newer_review = _review("v0.2", score=90)
        for strategy, review in ((older_strategy, older_review), (newer_strategy, newer_review)):
            self.database.save_strategy_version(strategy)
            self.database.save_daily_review(review)

        self.database.set_active_strategy_version("v0.1")
        reopened = self.reopen()

        self.assertEqual(older_strategy, reopened.get_active_strategy_version())
        self.assertEqual(newer_review, reopened.get_daily_review(TRADE_DATE))
        self.assertEqual(older_review, reopened.load_daily_review(TRADE_DATE, "v0.1"))
        self.assertEqual(newer_review, reopened.load_daily_review(TRADE_DATE, "v0.2"))

    def test_daily_review_lookup_uses_the_published_record_not_the_active_strategy(self) -> None:
        ready_strategy = _strategy("v0.1")
        ready_review = _review("v0.1", status="ready", score=70)
        published_strategy = _strategy("v0.2")
        published_review = _review("v0.2", status="published", score=90)
        for strategy, review in (
            (ready_strategy, ready_review),
            (published_strategy, published_review),
        ):
            self.database.save_strategy_version(strategy)
            self.database.save_daily_review(review)

        self.database.set_active_strategy_version(ready_strategy.version)

        self.assertEqual(published_review, self.database.get_daily_review(TRADE_DATE))

    def test_latest_publication_status_uses_the_newest_recorded_pipeline_date(self) -> None:
        older = TRADE_DATE.replace(day=6)
        self.database.save_schedule_outcome_record(
            trade_date=older,
            status="ready",
            next_at=None,
            pipeline_version="pipeline-v0.1",
            error=None,
        )
        self.database.save_schedule_outcome_record(
            trade_date=TRADE_DATE,
            status="failed",
            next_at=None,
            pipeline_version="pipeline-v0.1",
            error="fixture evidence gap",
        )

        self.assertEqual(
            {
                "trade_date": TRADE_DATE,
                "status": "failed",
                "next_at": None,
                "pipeline_version": "pipeline-v0.1",
                "error": "fixture evidence gap",
            },
            self.database.get_latest_publication_status(),
        )

    def test_concurrent_identical_idempotent_writes_share_one_result(self) -> None:
        self.database.create_watchlist(name="focus", idempotency_key="create-focus")
        workers = 12
        barrier = threading.Barrier(workers)

        def add_items() -> tuple[str, ...] | None:
            barrier.wait()
            return Database(self.path).add_watchlist_items(
                name="focus",
                symbols=("600000.SH", "000001.SZ"),
                idempotency_key="concurrent-add",
            )

        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = tuple(executor.map(lambda _: add_items(), range(workers)))

        self.assertEqual((("600000.SH", "000001.SZ"),) * workers, results)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM idempotent_writes "
                    "WHERE operation = 'add_watchlist_items' AND idempotency_key = 'concurrent-add'"
                ).fetchone()[0],
            )

    def test_reusing_an_idempotency_key_for_a_different_request_is_rejected(self) -> None:
        self.database.create_watchlist(name="focus", idempotency_key="create-focus")
        self.database.add_watchlist_items(
            name="focus",
            symbols=("600000.SH",),
            idempotency_key="add-focus",
        )

        with self.assertRaisesRegex(IdempotencyKeyReuseError, "idempotency key.*different request"):
            self.database.add_watchlist_items(
                name="focus",
                symbols=("000001.SZ",),
                idempotency_key="add-focus",
            )

    def test_application_does_not_cache_away_watchlist_idempotency_conflicts(self) -> None:
        application = StockMcpApplication(self.database, object(), object())
        application.create_watchlist(name="focus", idempotency_key="create-focus")
        application.add_watchlist_items(
            name="focus", symbols=("600000.SH",), idempotency_key="same-key"
        )

        with self.assertRaises(IdempotencyKeyReuseError):
            application.add_watchlist_items(
                name="focus", symbols=("000001.SZ",), idempotency_key="same-key"
            )

    def test_candidate_and_review_history_are_durable_and_unknown_records_are_none(self) -> None:
        review = self._save_active_review()
        reopened = self.reopen()

        self.assertEqual(
            review.candidates[0], reopened.get_candidate(review.candidates[0].candidate_id)
        )
        self.assertEqual((review,), reopened.list_review_history())
        self.assertIsNone(reopened.get_daily_review(date(2099, 1, 1)))
        self.assertIsNone(reopened.get_candidate("missing"))

    def test_watchlist_membership_preserves_user_order_and_idempotency_across_restart(self) -> None:
        created = self.database.create_watchlist(name="focus", idempotency_key="create-focus")
        self.assertEqual((), created)
        first_add = self.database.add_watchlist_items(
            name="focus",
            symbols=("600000.SH", "000001.SZ", "600000.SH"),
            idempotency_key="add-focus-1",
        )
        self.assertEqual(("600000.SH", "000001.SZ"), first_add)

        reopened = self.reopen()
        repeated_add = reopened.add_watchlist_items(
            name="focus",
            symbols=("600000.SH", "000001.SZ", "600000.SH"),
            idempotency_key="add-focus-1",
        )
        self.assertEqual(first_add, repeated_add)
        self.assertEqual(("focus",), reopened.list_watchlists())
        self.assertEqual(("600000.SH", "000001.SZ"), reopened.get_watchlist("focus"))
        self.assertIsNone(reopened.get_watchlist("missing"))

        removed = reopened.remove_watchlist_items(
            name="focus", symbols=("600000.SH",), idempotency_key="remove-focus-1"
        )
        self.assertEqual(("000001.SZ",), removed)
        self.assertEqual(
            removed,
            self.reopen().remove_watchlist_items(
                name="focus", symbols=("600000.SH",), idempotency_key="remove-focus-1"
            ),
        )
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM watchlist_items WHERE watchlist_name = ?", ("focus",)
                ).fetchone()[0],
            )

    def test_candidate_events_and_review_notes_are_idempotent_and_durable(self) -> None:
        review = self._save_active_review()
        candidate = review.candidates[0]
        event = self.database.record_candidate_event(
            candidate_id=candidate.candidate_id,
            status="watched",
            event_date=TRADE_DATE,
            price_1e4=120_000,
            reason="close held above confirmation",
            idempotency_key="event-1",
        )
        note = self.database.record_review_note(
            trade_date=TRADE_DATE,
            note="wait for volume",
            idempotency_key="note-1",
        )
        reopened = self.reopen()

        self.assertEqual(
            event,
            reopened.record_candidate_event(
                candidate_id=candidate.candidate_id,
                status="watched",
                event_date=TRADE_DATE,
                price_1e4=120_000,
                reason="close held above confirmation",
                idempotency_key="event-1",
            ),
        )
        self.assertEqual(
            note,
            reopened.record_review_note(
                trade_date=TRADE_DATE, note="wait for volume", idempotency_key="note-1"
            ),
        )
        self.assertEqual((note,), reopened.list_review_notes(TRADE_DATE))
        self.assertEqual((event,), reopened.list_candidate_review_events(candidate.candidate_id))
        self.assertIsNone(
            reopened.record_candidate_event(
                candidate_id="missing",
                status="skipped",
                event_date=TRADE_DATE,
                price_1e4=None,
                reason="ignored",
                idempotency_key="missing-event",
            )
        )
        self.assertIsNone(
            reopened.record_review_note(
                trade_date=date(2099, 1, 1), note="ignored", idempotency_key="missing-note"
            )
        )
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                1, connection.execute("SELECT COUNT(*) FROM candidate_events").fetchone()[0]
            )
            self.assertEqual(
                1, connection.execute("SELECT COUNT(*) FROM review_notes").fetchone()[0]
            )

    def test_strategy_versions_and_active_pointer_survive_restart(self) -> None:
        first = _strategy("v0.1")
        second = _strategy("v0.2")
        self.database.save_strategy_version(first)
        self.database.save_strategy_version(second)
        self.database.set_active_strategy_version(second.version)

        reopened = self.reopen()

        self.assertEqual((first, second), reopened.list_strategy_versions())
        self.assertEqual(second, reopened.get_active_strategy_version())


if __name__ == "__main__":
    unittest.main()
