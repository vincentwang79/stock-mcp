from __future__ import annotations

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
    MarketSnapshot,
    Security,
    SetupType,
    StrategyVersion,
)
from stock_mcp.storage import Database

TRADE_DATE = date(2026, 9, 3)
AS_OF = datetime(2026, 9, 3, 9, 30, tzinfo=UTC)
RECONCILED_AT = datetime(2026, 9, 4, 1, 34, tzinfo=UTC)
STRATEGY_VERSION = "v0.3-policy-1"


def _strategy() -> StrategyVersion:
    return StrategyVersion(
        version=STRATEGY_VERSION,
        status="active",
        parameters={"rule_engine_version": 3},
    )


def _snapshot() -> MarketSnapshot:
    security = Security(
        symbol="600001.SH",
        name="测试证券",
        exchange="SSE",
        board="MAIN",
        list_date=date(2020, 1, 1),
        industry="测试行业",
        is_st=False,
    )
    bar = DailyBar(
        symbol=security.symbol,
        trade_date=TRADE_DATE,
        open_1e4=100_000,
        high_1e4=105_000,
        low_1e4=99_000,
        close_1e4=103_000,
        pre_close_1e4=100_000,
        volume_shares=1_000_000,
        amount_fen=10_300_000_000,
        source="tushare",
        source_timestamp=AS_OF,
    )
    return MarketSnapshot(
        trade_date=TRADE_DATE,
        source="tushare",
        source_timestamp=AS_OF,
        securities=(security,),
        bars=(bar,),
        advance_ratio_bps=5_000,
        above_ma20_ratio_bps=5_000,
    )


def _review() -> DailyReview:
    candidate = Candidate(
        candidate_id=f"{TRADE_DATE.isoformat()}:{STRATEGY_VERSION}:600001.SH",
        symbol="600001.SH",
        name="测试证券",
        rank=1,
        score=80,
        setup_type=SetupType.STRONG_PULLBACK,
        strategy_version=STRATEGY_VERSION,
        evidence=(Evidence("base_score", 80, 80, True, 80),),
        confirmation_condition="close > 105000",
        invalidation_condition="close < 99000",
    )
    return DailyReview(
        status="ready",
        trade_date=TRADE_DATE,
        source="tushare",
        source_timestamp=AS_OF,
        strategy_version=STRATEGY_VERSION,
        market_regime=MarketRegime.NEUTRAL,
        candidates=(candidate,),
    )


class LateReconciledPublicationContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "stock.sqlite3")
        self.database.initialize()
        self.database.save_strategy_version(_strategy())
        self.database.save_schedule_outcome_record(
            trade_date=TRADE_DATE,
            status="retry_scheduled",
            next_at=datetime(2026, 9, 3, 17, 10, tzinfo=UTC),
            pipeline_version="pipeline-v0.1",
            error="fixture upstream evidence was incomplete",
        )

    def test_late_reconciled_publication_appends_a_formal_review_without_rewriting_retry_audit(
        self,
    ) -> None:
        publisher = getattr(self.database, "publish_late_reconciled_daily_review", None)
        self.assertTrue(
            callable(publisher),
            "late reconciled publication must be an explicit append-only storage operation",
        )
        if not callable(publisher):
            return

        result = publisher(
            review=_review(),
            snapshot=_snapshot(),
            snapshot_features={"600001.SH": {"fixture": True}},
            input_hash="1" * 64,
            result_hash="2" * 64,
            reconciled_at=RECONCILED_AT,
            idempotency_key="publish-2026-09-03-fixture-1",
        )

        self.assertEqual("late_reconciled", result["publication_class"])
        self.assertEqual("retry_scheduled", result["original_schedule_status"])
        self.assertEqual(1, result["candidate_count"])
        self.assertEqual(64, len(result["publication_hash"]))
        publication_status = self.database.get_publication_status(TRADE_DATE)
        self.assertEqual("retry_scheduled", publication_status["status"])
        published = self.database.get_daily_review(TRADE_DATE)
        self.assertIsNotNone(published)
        self.assertEqual("published", published.status)
        self.assertEqual(1, len(published.candidates))
        self.assertEqual(
            "late_reconciled",
            self.database.get_late_reconciled_publication(TRADE_DATE)["publication_class"],
        )

        replay = publisher(
            review=_review(),
            snapshot=_snapshot(),
            snapshot_features={"600001.SH": {"fixture": True}},
            input_hash="1" * 64,
            result_hash="2" * 64,
            reconciled_at=RECONCILED_AT,
            idempotency_key="publish-2026-09-03-fixture-1",
        )
        self.assertEqual(result, replay)

    def test_late_reconciled_publication_refuses_to_shadow_an_existing_normal_review(self) -> None:
        existing_strategy = StrategyVersion(
            version="v0.2-policy-1",
            status="active",
            parameters={"rule_engine_version": 2},
        )
        self.database.save_strategy_version(existing_strategy)
        candidate = replace(
            _review().candidates[0],
            candidate_id=f"{TRADE_DATE.isoformat()}:v0.2-policy-1:600001.SH",
            strategy_version=existing_strategy.version,
        )
        self.database.save_daily_review(
            replace(_review(), strategy_version=existing_strategy.version, candidates=(candidate,))
        )
        publisher = self.database.publish_late_reconciled_daily_review

        with self.assertRaisesRegex(ValueError, "normal daily review"):
            publisher(
                review=_review(),
                snapshot=_snapshot(),
                snapshot_features={"600001.SH": {"fixture": True}},
                input_hash="3" * 64,
                result_hash="4" * 64,
                reconciled_at=RECONCILED_AT,
                idempotency_key="publish-2026-09-03-fixture-2",
            )
