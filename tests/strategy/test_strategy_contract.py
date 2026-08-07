from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta

from stock_mcp.domain import (
    DailyBar,
    MarketRegime,
    MarketSnapshot,
    Security,
    SetupType,
    StrategyVersion,
)
from stock_mcp.review import generate_daily_review

TRADE_DATE = date(2026, 8, 7)
AS_OF = datetime(2026, 8, 7, 8, 30, tzinfo=UTC)
SOURCE = "recorded-tushare"


def _security(
    symbol: str,
    *,
    name: str | None = None,
    board: str = "MAIN",
    list_date: date = date(2020, 1, 1),
    industry: str = "银行",
    is_st: bool = False,
) -> Security:
    return Security(
        symbol=symbol,
        name=name or symbol,
        exchange="SSE",
        board=board,
        list_date=list_date,
        industry=industry,
        is_st=is_st,
    )


def _bar(
    symbol: str,
    trade_date: date,
    *,
    close: int,
    pre_close: int | None = None,
    high: int | None = None,
    low: int | None = None,
    volume: int = 1_000_000,
    amount: int = 8_000_000_000,
) -> DailyBar:
    pre_close = pre_close if pre_close is not None else close - 1_000
    return DailyBar(
        symbol=symbol,
        trade_date=trade_date,
        open_1e4=pre_close,
        high_1e4=high if high is not None else max(close, pre_close) + 500,
        low_1e4=low if low is not None else min(close, pre_close) - 500,
        close_1e4=close,
        pre_close_1e4=pre_close,
        volume_shares=volume,
        amount_fen=amount,
        source=SOURCE,
        source_timestamp=AS_OF,
    )


def _strategy(*, version: str = "v0.1-proposed") -> StrategyVersion:
    """One public, deliberately conservative v0.1 parameter set."""
    return StrategyVersion(
        version=version,
        status="proposed",
        parameters={
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


def _snapshot(
    securities: tuple[Security, ...],
    bars: tuple[DailyBar, ...],
    *,
    trade_date: date = TRADE_DATE,
    breadth_bps: int = 6_500,
) -> MarketSnapshot:
    return MarketSnapshot(
        trade_date=trade_date,
        source=SOURCE,
        source_timestamp=AS_OF,
        securities=securities,
        bars=bars,
        advance_ratio_bps=breadth_bps,
        above_ma20_ratio_bps=breadth_bps,
    )


class StrategyScreeningContractTest(unittest.TestCase):
    def test_future_bars_do_not_change_the_target_day_review(self) -> None:
        security = _security("600001.SH")
        target_bar = _bar(security.symbol, TRADE_DATE, close=103_000, pre_close=100_000)
        snapshot = _snapshot((security,), (target_bar,))
        future_winner = _bar(
            "600999.SH",
            TRADE_DATE + timedelta(days=1),
            close=150_000,
            pre_close=100_000,
            amount=100_000_000_000,
        )
        polluted = _snapshot((security, _security("600999.SH")), (target_bar, future_winner))

        self.assertEqual(
            generate_daily_review(polluted, _strategy()),
            generate_daily_review(snapshot, _strategy()),
        )

    def test_same_historical_snapshot_is_deterministic(self) -> None:
        security = _security("600001.SH")
        bars = (
            _bar(security.symbol, TRADE_DATE - timedelta(days=2), close=100_000),
            _bar(security.symbol, TRADE_DATE - timedelta(days=1), close=105_000),
            _bar(security.symbol, TRADE_DATE, close=103_000, pre_close=105_000),
        )
        snapshot = _snapshot((security,), bars)

        self.assertEqual(
            generate_daily_review(snapshot, _strategy()),
            generate_daily_review(snapshot, _strategy()),
        )

    def test_filters_ineligible_or_abnormal_target_day_securities(self) -> None:
        eligible = _security("600001.SH")
        st_security = _security("600002.SH", is_st=True)
        recent_listing = _security("600003.SH", list_date=TRADE_DATE - timedelta(days=179))
        low_liquidity = _security("600004.SH")
        abnormal_limit_up = _security("600005.SH")
        suspended = _security("600006.SH")
        securities = (
            eligible,
            st_security,
            recent_listing,
            low_liquidity,
            abnormal_limit_up,
            suspended,
        )
        bars = (
            _bar(eligible.symbol, TRADE_DATE - timedelta(days=2), close=98_000, volume=500_000),
            _bar(eligible.symbol, TRADE_DATE - timedelta(days=1), close=100_000, volume=500_000),
            _bar(eligible.symbol, TRADE_DATE, close=104_000, pre_close=100_000),
            _bar(st_security.symbol, TRADE_DATE, close=110_000, pre_close=100_000),
            _bar(recent_listing.symbol, TRADE_DATE, close=110_000, pre_close=100_000),
            _bar(
                low_liquidity.symbol,
                TRADE_DATE,
                close=110_000,
                pre_close=100_000,
                amount=100_000_000,
            ),
            _bar(
                abnormal_limit_up.symbol,
                TRADE_DATE - timedelta(days=2),
                close=110_000,
                pre_close=100_000,
            ),
            _bar(
                abnormal_limit_up.symbol,
                TRADE_DATE - timedelta(days=1),
                close=121_000,
                pre_close=110_000,
            ),
            _bar(abnormal_limit_up.symbol, TRADE_DATE, close=133_100, pre_close=121_000),
        )

        review = generate_daily_review(_snapshot(securities, bars), _strategy())

        self.assertEqual(
            tuple(candidate.symbol for candidate in review.candidates), (eligible.symbol,)
        )

    def test_distinguishes_strong_pullback_from_volume_breakout(self) -> None:
        pullback = _security("600010.SH", industry="半导体")
        breakout = _security("600011.SH", industry="半导体")
        bars = (
            _bar(pullback.symbol, TRADE_DATE - timedelta(days=4), close=100_000, volume=1_000_000),
            _bar(pullback.symbol, TRADE_DATE - timedelta(days=3), close=108_000, pre_close=100_000),
            _bar(pullback.symbol, TRADE_DATE - timedelta(days=2), close=115_000, pre_close=108_000),
            _bar(pullback.symbol, TRADE_DATE - timedelta(days=1), close=120_000, pre_close=115_000),
            _bar(pullback.symbol, TRADE_DATE, close=114_000, pre_close=120_000, low=112_000),
            _bar(breakout.symbol, TRADE_DATE - timedelta(days=4), close=100_000, volume=1_000_000),
            _bar(breakout.symbol, TRADE_DATE - timedelta(days=3), close=100_200, volume=1_000_000),
            _bar(breakout.symbol, TRADE_DATE - timedelta(days=2), close=99_900, volume=1_000_000),
            _bar(breakout.symbol, TRADE_DATE - timedelta(days=1), close=100_100, volume=1_000_000),
            _bar(
                breakout.symbol,
                TRADE_DATE,
                close=106_000,
                pre_close=100_100,
                high=106_500,
                volume=2_000_000,
            ),
        )

        review = generate_daily_review(_snapshot((pullback, breakout), bars), _strategy())
        setup_by_symbol = {
            candidate.symbol: candidate.setup_type for candidate in review.candidates
        }

        self.assertEqual(setup_by_symbol[pullback.symbol], SetupType.STRONG_PULLBACK)
        self.assertEqual(setup_by_symbol[breakout.symbol], SetupType.VOLUME_BREAKOUT)

    def test_candidate_contains_structured_industry_context_evidence(self) -> None:
        security = _security("600021.SH", industry="电力设备")
        industry_peer = _security("600022.SH", industry="电力设备")
        bars = (
            _bar(security.symbol, TRADE_DATE - timedelta(days=2), close=98_000, volume=500_000),
            _bar(security.symbol, TRADE_DATE - timedelta(days=1), close=100_000, volume=500_000),
            _bar(security.symbol, TRADE_DATE, close=105_000, pre_close=100_000),
            _bar(
                industry_peer.symbol, TRADE_DATE - timedelta(days=2), close=98_000, volume=500_000
            ),
            _bar(
                industry_peer.symbol, TRADE_DATE - timedelta(days=1), close=100_000, volume=500_000
            ),
            _bar(industry_peer.symbol, TRADE_DATE, close=104_000, pre_close=100_000),
        )

        review = generate_daily_review(_snapshot((security, industry_peer), bars), _strategy())
        candidate = next(item for item in review.candidates if item.symbol == security.symbol)

        self.assertIn("industry_strength_bps", {evidence.metric for evidence in candidate.evidence})

    def test_defensive_market_is_ready_with_zero_candidates(self) -> None:
        security = _security("600001.SH")
        snapshot = _snapshot(
            (security,),
            (_bar(security.symbol, TRADE_DATE, close=106_000, pre_close=100_000),),
            breadth_bps=3_000,
        )

        review = generate_daily_review(snapshot, _strategy())

        self.assertEqual(review.status, "ready")
        self.assertIs(review.market_regime, MarketRegime.DEFENSIVE)
        self.assertEqual(review.candidates, ())
