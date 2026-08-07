from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta

from stock_mcp.domain import DailyBar, MarketSnapshot, Security, StrategyVersion
from stock_mcp.replay import walk_forward

SOURCE = "recorded-tushare"
AS_OF = datetime(2026, 8, 7, 8, 30, tzinfo=UTC)


def _strategy() -> StrategyVersion:
    return StrategyVersion(
        version="v0.1-proposed",
        status="proposed",
        parameters={
            "offensive_min_bps": 5_500,
            "defensive_max_bps": 4_000,
            "neutral_limit": 2,
            "offensive_limit": 3,
            "min_liquidity_amount_fen": 2_000_000_000,
            "max_consecutive_limit_up_days": 2,
        },
    )


def _snapshot(trade_date: date, *, include_future_bar: bool = False) -> MarketSnapshot:
    security = Security(
        symbol="600001.SH",
        name="样本",
        exchange="SSE",
        board="MAIN",
        list_date=date(2020, 1, 1),
        industry="银行",
        is_st=False,
    )
    bars = []
    for offset in range(3):
        bar_date = trade_date - timedelta(days=2 - offset)
        close = 100_000 + offset * 2_000
        bars.append(
            DailyBar(
                symbol=security.symbol,
                trade_date=bar_date,
                open_1e4=close - 1_000,
                high_1e4=close + 500,
                low_1e4=close - 1_500,
                close_1e4=close,
                pre_close_1e4=close - 1_000,
                volume_shares=1_000_000,
                amount_fen=8_000_000_000,
                source=SOURCE,
                source_timestamp=AS_OF,
            )
        )
    if include_future_bar:
        bars.append(
            DailyBar(
                symbol=security.symbol,
                trade_date=trade_date + timedelta(days=1),
                open_1e4=1,
                high_1e4=2,
                low_1e4=1,
                close_1e4=2,
                pre_close_1e4=1,
                volume_shares=1,
                amount_fen=1,
                source=SOURCE,
                source_timestamp=AS_OF,
            )
        )
    return MarketSnapshot(
        trade_date=trade_date,
        source=SOURCE,
        source_timestamp=AS_OF,
        securities=(security,),
        bars=tuple(bars),
        advance_ratio_bps=6_500,
        above_ma20_ratio_bps=6_500,
    )


class WalkForwardContractTest(unittest.TestCase):
    def test_walk_forward_returns_one_versioned_review_per_increasing_snapshot(self) -> None:
        first_date = date(2026, 8, 6)
        second_date = date(2026, 8, 7)

        reviews = walk_forward((_snapshot(first_date), _snapshot(second_date)), _strategy())

        self.assertEqual(tuple(review.trade_date for review in reviews), (first_date, second_date))
        self.assertTrue(all(review.strategy_version == "v0.1-proposed" for review in reviews))

    def test_walk_forward_rejects_non_increasing_trade_dates(self) -> None:
        snapshot = _snapshot(date(2026, 8, 7))

        with self.assertRaises(ValueError):
            walk_forward((snapshot, snapshot), _strategy())

    def test_walk_forward_rejects_a_snapshot_that_contains_future_bars(self) -> None:
        snapshot = _snapshot(date(2026, 8, 7), include_future_bar=True)

        with self.assertRaises(ValueError):
            walk_forward((snapshot,), _strategy())
