from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta

from stock_mcp.domain import DailyBar, MarketSnapshot, Security, StrategyVersion
from stock_mcp.replay import HistoricalReplayService, walk_forward

SOURCE = "recorded-tushare"
AS_OF = datetime(2026, 8, 7, 8, 30, tzinfo=UTC)


def _strategy() -> StrategyVersion:
    return StrategyVersion(
        version="v0.1-proposed",
        status="proposed",
        parameters={
            "rule_engine_version": 1,
            "offensive_min_bps": 5_500,
            "defensive_max_bps": 4_000,
            "neutral_limit": 2,
            "offensive_limit": 3,
            "min_liquidity_amount_fen": 2_000_000_000,
            "max_consecutive_limit_up_days": 2,
            "strong_pullback_min_prior_gain_bps": 1_000,
            "strong_pullback_max_pullback_bps": 800,
            "volume_breakout_min_volume_ratio_bps": 10_000,
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


class HistoricalReplayComparisonContractTest(unittest.TestCase):
    def test_comparison_preserves_daily_regime_and_fully_evidenced_candidates(self) -> None:
        trade_date = date(2026, 8, 7)
        left = _strategy()
        right = StrategyVersion(
            version="v0.2-proposed", status="proposed", parameters=left.parameters
        )
        service = HistoricalReplayService(
            _RecordedSnapshotRepository((_snapshot(trade_date),)),
            _StrategiesByVersion((left, right)),
        )

        comparison = service.compare(left.version, right.version, trade_date, trade_date)

        day = comparison["daily"][0]
        self.assertTrue({"left", "right"} <= day.keys())
        for side, version in (("left", left.version), ("right", right.version)):
            with self.subTest(side=side):
                review = day[side]
                self.assertEqual("offensive", review["market_regime"])
                candidate = review["candidates"][0]
                self.assertEqual(
                    f"{trade_date.isoformat()}:{version}:600001.SH",
                    candidate["candidate_id"],
                )
                self.assertEqual("600001.SH", candidate["symbol"])
                self.assertIsInstance(candidate["score"], int)
                self.assertTrue(candidate["evidence"])
                self.assertTrue(
                    {"metric", "value", "threshold", "passed", "score_contribution"}
                    <= candidate["evidence"][0].keys()
                )

    def test_sparse_calendar_cannot_create_an_activation_attestation(self) -> None:
        start = date(2023, 1, 1)
        expected_dates = tuple(
            start + timedelta(days=round(offset * 1_095 / 599)) for offset in range(600)
        )
        snapshots = tuple(_snapshot(day) for day in expected_dates if day != expected_dates[200])
        repository = _GovernanceRepository(snapshots, expected_dates)
        strategy = _strategy()

        HistoricalReplayService(repository, _StrategiesByVersion((strategy,))).compare(
            strategy.version,
            strategy.version,
            start,
            expected_dates[-1],
        )

        self.assertEqual([], repository.attestations)

    def test_complete_calendar_binds_attestation_to_the_replayed_dataset(self) -> None:
        start = date(2023, 1, 1)
        expected_dates = tuple(
            start + timedelta(days=round(offset * 1_095 / 599)) for offset in range(600)
        )
        snapshots = tuple(_snapshot(day) for day in expected_dates)
        repository = _GovernanceRepository(snapshots, expected_dates)
        strategy = _strategy()

        comparison = HistoricalReplayService(repository, _StrategiesByVersion((strategy,))).compare(
            strategy.version, strategy.version, start, expected_dates[-1]
        )

        self.assertEqual(580, comparison["days_compared"], "first 20 sessions are warm-up")
        self.assertEqual(2, len(repository.attestations))
        for attestation in repository.attestations:
            self.assertEqual(strategy.version, attestation[0])
            self.assertEqual(64, len(attestation[2]), "attestation must bind a dataset hash")
            self.assertEqual(start, attestation[3])
            self.assertEqual(expected_dates[-1], attestation[4])
            self.assertEqual(580, attestation[5])


class _RecordedSnapshotRepository:
    def __init__(self, snapshots: tuple[MarketSnapshot, ...]) -> None:
        self._snapshots = snapshots

    def load_market_snapshots(
        self, start: date, end: date, *, source: str
    ) -> tuple[MarketSnapshot, ...]:
        return self._snapshots


class _GovernanceRepository(_RecordedSnapshotRepository):
    def __init__(
        self,
        snapshots: tuple[MarketSnapshot, ...],
        expected_dates: tuple[date, ...],
    ) -> None:
        super().__init__(snapshots)
        self.expected_dates = expected_dates
        self.attestations: list[tuple[object, ...]] = []

    def load_expected_trading_days(
        self, start: date, end: date, *, source: str
    ) -> tuple[date, ...]:
        return tuple(day for day in self.expected_dates if start <= day <= end)

    def record_governance_replay_attestation(self, *values: object) -> None:
        self.attestations.append(values)


class _StrategiesByVersion:
    def __init__(self, strategies: tuple[StrategyVersion, ...]) -> None:
        self._strategies = {strategy.version: strategy for strategy in strategies}

    def get(self, version: str) -> StrategyVersion:
        return self._strategies[version]
