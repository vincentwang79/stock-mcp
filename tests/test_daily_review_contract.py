import unittest
from datetime import UTC, date, datetime, timedelta

from stock_mcp.domain import (
    DailyBar,
    MarketRegime,
    MarketSnapshot,
    Security,
    StrategyVersion,
)
from stock_mcp.review import MixedSourceSnapshotError, generate_daily_review


def _snapshot(*, source: str = "fixture", breadth: int = 60) -> MarketSnapshot:
    as_of = datetime(2026, 8, 7, 8, 30, tzinfo=UTC)
    securities = tuple(
        Security(
            symbol=f"600{i:03d}.SH",
            name=f"样本{i}",
            exchange="SSE",
            board="MAIN",
            list_date=date(2020, 1, 1),
            industry="测试行业",
            is_st=False,
        )
        for i in range(5)
    )
    target_bars = tuple(
        DailyBar(
            symbol=security.symbol,
            trade_date=date(2026, 8, 7),
            open_1e4=100_000 + i * 1_000,
            high_1e4=108_000 + i * 1_000,
            low_1e4=99_000 + i * 1_000,
            close_1e4=106_000 + i * 1_000,
            pre_close_1e4=100_000 + i * 1_000,
            volume_shares=1_000_000 + i * 100_000,
            amount_fen=120_000_000_00 + i * 10_000_000_00,
            source=source,
            source_timestamp=as_of,
        )
        for i, security in enumerate(securities)
    )
    history = tuple(
        DailyBar(
            symbol=security.symbol,
            trade_date=date(2026, 8, 7) - timedelta(days=offset),
            open_1e4=94_000 + (3 - offset) * 2_000 + i * 1_000,
            high_1e4=96_000 + (3 - offset) * 2_000 + i * 1_000,
            low_1e4=93_000 + (3 - offset) * 2_000 + i * 1_000,
            close_1e4=95_000 + (3 - offset) * 2_000 + i * 1_000,
            pre_close_1e4=94_000 + (3 - offset) * 2_000 + i * 1_000,
            volume_shares=500_000,
            amount_fen=6_000_000_000,
            source=source,
            source_timestamp=as_of,
        )
        for i, security in enumerate(securities)
        for offset in (3, 2, 1)
    )
    bars = (*history, *target_bars)
    return MarketSnapshot(
        trade_date=date(2026, 8, 7),
        source=source,
        source_timestamp=as_of,
        securities=securities,
        bars=bars,
        advance_ratio_bps=breadth * 100,
        above_ma20_ratio_bps=breadth * 100,
    )


def _strategy() -> StrategyVersion:
    return StrategyVersion(
        version="v0.1-proposed",
        status="proposed",
        parameters={
            "offensive_min_bps": 5_500,
            "defensive_max_bps": 4_000,
            "neutral_limit": 2,
            "offensive_limit": 3,
        },
    )


class DailyReviewContractTest(unittest.TestCase):
    def test_daily_review_is_deterministic_and_respects_offensive_limit(self) -> None:
        first = generate_daily_review(_snapshot(), _strategy())
        second = generate_daily_review(_snapshot(), _strategy())

        self.assertEqual(first, second)
        self.assertIs(first.market_regime, MarketRegime.OFFENSIVE)
        self.assertEqual(len(first.candidates), 3)
        self.assertTrue(
            all(candidate.strategy_version == "v0.1-proposed" for candidate in first.candidates)
        )
        self.assertTrue(all(candidate.evidence for candidate in first.candidates))

    def test_defensive_market_returns_a_ready_review_with_no_candidates(self) -> None:
        review = generate_daily_review(_snapshot(breadth=35), _strategy())

        self.assertIs(review.market_regime, MarketRegime.DEFENSIVE)
        self.assertEqual(review.candidates, ())
        self.assertEqual(review.status, "ready")

    def test_snapshot_rejects_mixed_price_sources(self) -> None:
        snapshot = _snapshot()
        mixed = MarketSnapshot(
            trade_date=snapshot.trade_date,
            source=snapshot.source,
            source_timestamp=snapshot.source_timestamp,
            securities=snapshot.securities,
            bars=(*snapshot.bars[:-1], snapshot.bars[-1].with_source("other")),
            advance_ratio_bps=snapshot.advance_ratio_bps,
            above_ma20_ratio_bps=snapshot.above_ma20_ratio_bps,
        )

        with self.assertRaises(MixedSourceSnapshotError):
            generate_daily_review(mixed, _strategy())

    def test_target_day_only_data_does_not_guess_a_historical_setup(self) -> None:
        snapshot = _snapshot()
        target_only = tuple(bar for bar in snapshot.bars if bar.trade_date == snapshot.trade_date)

        review = generate_daily_review(
            MarketSnapshot(
                trade_date=snapshot.trade_date,
                source=snapshot.source,
                source_timestamp=snapshot.source_timestamp,
                securities=snapshot.securities,
                bars=target_only,
                advance_ratio_bps=snapshot.advance_ratio_bps,
                above_ma20_ratio_bps=snapshot.above_ma20_ratio_bps,
            ),
            _strategy(),
        )

        self.assertEqual((), review.candidates)

    def test_candidate_conditions_use_the_public_close_grammar(self) -> None:
        review = generate_daily_review(_snapshot(), _strategy())

        self.assertTrue(review.candidates)
        self.assertTrue(review.candidates[0].confirmation_condition.startswith("close >"))
        self.assertTrue(review.candidates[0].invalidation_condition.startswith("close <"))


if __name__ == "__main__":
    unittest.main()
