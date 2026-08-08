from __future__ import annotations

import unittest
from datetime import UTC, date, datetime

from stock_mcp.application import StockMcpApplication
from stock_mcp.domain import Candidate, DailyReview, Evidence, MarketRegime, SetupType
from stock_mcp.mcp_tools import GetCandidateResult, GetDailyReviewResult

TRADE_DATE = date(2026, 8, 7)
AS_OF = datetime(2026, 8, 7, 8, 30, tzinfo=UTC)


class _PublicationRepository:
    def __init__(self, status: str, review: DailyReview | None = None) -> None:
        self.status = status
        self.review = review

    def get_daily_review(self, _trade_date: date) -> DailyReview | None:
        return self.review

    def get_publication_status(self, trade_date: date) -> dict[str, object]:
        return {
            "trade_date": trade_date,
            "status": self.status,
            "next_at": AS_OF,
            "error": "fixture provider unavailable",
        }


class _CandidateRepository:
    def __init__(self) -> None:
        self.candidate = Candidate(
            candidate_id="candidate-1",
            symbol="600000.SH",
            name="浦发银行",
            rank=1,
            score=87,
            setup_type=SetupType.STRONG_PULLBACK,
            strategy_version="v0.1",
            evidence=(
                Evidence(
                    metric="industry_strength_bps",
                    value=920,
                    threshold=500,
                    passed=True,
                    score_contribution=10,
                ),
            ),
            confirmation_condition="close >= 120000",
            invalidation_condition="close < 110000",
        )
        self.review = DailyReview(
            status="published",
            trade_date=TRADE_DATE,
            source="tushare",
            source_timestamp=AS_OF,
            strategy_version="v0.1",
            market_regime=MarketRegime.OFFENSIVE,
            candidates=(self.candidate,),
        )

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        return self.candidate if candidate_id == self.candidate.candidate_id else None

    def get_candidate_context(self, candidate_id: str) -> dict[str, object] | None:
        if candidate_id != self.candidate.candidate_id:
            return None
        return {
            "review": self.review,
            "industry_context": {
                "industry": "银行",
                "industry_strength_bps": 920,
                "eligible_peer_count": 42,
            },
        }

    def get_publication_status(self, trade_date: date) -> dict[str, object]:
        return {
            "trade_date": trade_date,
            "status": "ready",
            "pipeline_version": "pipeline-v0.1",
        }

    def get_daily_review(self, trade_date: date) -> DailyReview | None:
        return self.review if trade_date == TRADE_DATE else None

    def list_review_notes(self, _trade_date: date) -> tuple[object, ...]:
        return ()


class ApplicationPublicationStatusContractTest(unittest.TestCase):
    def test_unpublished_pipeline_outcomes_are_visible_as_explicit_structured_statuses(
        self,
    ) -> None:
        for status in (
            "retry_scheduled",
            "degraded_observation",
            "degraded_no_screen",
            "failed",
        ):
            with self.subTest(status=status):
                result = StockMcpApplication(
                    _PublicationRepository(status), object(), object()
                ).get_daily_review(trade_date=TRADE_DATE)

                self.assertTrue(result["ok"])
                self.assertEqual(status, result["data"]["status"])
                self.assertEqual(TRADE_DATE.isoformat(), result["data"]["trade_date"])
                self.assertEqual("fixture provider unavailable", result["data"]["error"])
                self.assertEqual(AS_OF, result["data"]["next_at"])

        observation_with_review = StockMcpApplication(
            _PublicationRepository("degraded_observation", _CandidateRepository().review),
            object(),
            object(),
        ).get_daily_review(trade_date=TRADE_DATE)
        self.assertEqual("degraded_observation", observation_with_review["data"]["status"])

    def test_candidate_includes_its_review_provenance_and_industry_context(self) -> None:
        result = StockMcpApplication(_CandidateRepository(), object(), object()).get_candidate(
            candidate_id="candidate-1"
        )

        self.assertTrue(result["ok"])
        self.assertEqual("tushare", result["data"]["source"])
        self.assertEqual(AS_OF, result["data"]["source_timestamp"])
        self.assertEqual("offensive", result["data"]["market_regime"])
        self.assertEqual(
            {
                "industry": "银行",
                "industry_strength_bps": 920,
                "eligible_peer_count": 42,
            },
            result["data"]["industry_context"],
        )

    def test_daily_review_candidates_include_industry_context(self) -> None:
        result = StockMcpApplication(_CandidateRepository(), object(), object()).get_daily_review(
            trade_date=TRADE_DATE
        )

        candidate = result["data"]["candidates"][0]
        self.assertEqual("pipeline-v0.1", result["data"]["pipeline_version"])
        self.assertEqual("银行", candidate["industry_context"]["industry"])
        self.assertEqual(42, candidate["industry_context"]["eligible_peer_count"])
        GetDailyReviewResult.model_validate(result)

        candidate_result = StockMcpApplication(
            _CandidateRepository(), object(), object()
        ).get_candidate(candidate_id="candidate-1")
        GetCandidateResult.model_validate(candidate_result)


if __name__ == "__main__":
    unittest.main()
