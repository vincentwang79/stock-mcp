"""Offline contract for a candidate's structured personal-review timeline."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime

from stock_mcp.application import StockMcpApplication
from stock_mcp.domain import Candidate, DailyReview, Evidence, MarketRegime, SetupType

TRADE_DATE = date(2026, 8, 6)
EVENT_DATE = date(2026, 8, 7)


def _candidate() -> Candidate:
    return Candidate(
        candidate_id="candidate-structured-1",
        symbol="600000.SH",
        name="浦发银行",
        rank=1,
        score=87,
        setup_type=SetupType.STRONG_PULLBACK,
        strategy_version="v0.1-active",
        evidence=(Evidence("relative_strength_bps", 1_250, 800, True, 45),),
        confirmation_condition="close >= 120000",
        invalidation_condition="close < 110000",
    )


class CandidateTimelineRepository:
    def __init__(self) -> None:
        candidate = _candidate()
        self.candidate = candidate
        self.review = DailyReview(
            status="published",
            trade_date=TRADE_DATE,
            source="tushare",
            source_timestamp=datetime(2026, 8, 6, 16, 30, tzinfo=UTC),
            strategy_version="v0.1-active",
            market_regime=MarketRegime.OFFENSIVE,
            candidates=(candidate,),
        )
        self.events: list[dict[str, object]] = []

    def record_candidate_event(
        self,
        *,
        candidate_id: str,
        status: str,
        event_date: date,
        price_1e4: int | None,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, object] | None:
        if candidate_id != self.candidate.candidate_id:
            return None
        event = {
            "candidate_id": candidate_id,
            "status": status,
            "event_date": event_date.isoformat(),
            "price_1e4": price_1e4,
            "reason": reason,
        }
        self.events.append(event)
        return event

    def list_candidate_events(self, candidate_id: str) -> tuple[dict[str, object], ...]:
        return tuple(event for event in self.events if event["candidate_id"] == candidate_id)

    def list_review_history(self) -> tuple[DailyReview, ...]:
        return (self.review,)

    def list_review_notes(self, _trade_date: date) -> tuple[dict[str, object], ...]:
        return ()


class CandidateEventWorkflowContractTest(unittest.TestCase):
    def test_structured_candidate_event_is_returned_by_candidate_history(self) -> None:
        repository = CandidateTimelineRepository()
        application = StockMcpApplication(repository, object(), object())

        recorded = application.record_candidate_event(
            candidate_id="candidate-structured-1",
            status="watched",
            event_date=EVENT_DATE,
            price_1e4=123_400,
            reason="收盘仍高于确认线，继续观察",
            idempotency_key="candidate-event-1",
        )
        history = application.get_review_history(candidate_id="candidate-structured-1")

        self.assertTrue(recorded["ok"])
        self.assertEqual("watched", recorded["data"]["status"])
        self.assertEqual(EVENT_DATE.isoformat(), recorded["data"]["event_date"])
        self.assertEqual(123_400, recorded["data"]["price_1e4"])
        self.assertEqual("收盘仍高于确认线，继续观察", recorded["data"]["reason"])
        self.assertTrue(history["ok"])
        self.assertEqual([recorded["data"]], history["data"]["events"])


if __name__ == "__main__":
    unittest.main()
