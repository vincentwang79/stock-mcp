"""Offline contract for a single-version, governance-grade strategy replay."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta

from stock_mcp.domain import DailyBar, MarketSnapshot, Security, StrategyVersion
from stock_mcp.replay import HistoricalReplayService

SOURCE = "recorded-tushare"
SOURCE_TIMESTAMP = datetime(2025, 12, 31, 8, 30, tzinfo=UTC)
START = date(2023, 1, 1)
END = date(2026, 1, 1)


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
            "volume_breakout_min_volume_ratio_bps": 15_000,
        },
    )


def _calendar() -> tuple[date, ...]:
    # Fixed offline calendar: 600 increasing dates over exactly 1,096 calendar days.
    return tuple(START + timedelta(days=round(offset * 1_096 / 599)) for offset in range(600))


def _snapshot(trade_date: date, *, future_bar: bool = False) -> MarketSnapshot:
    security = Security(
        symbol="600001.SH",
        name="脱敏样本",
        exchange="SSE",
        board="MAIN",
        list_date=date(2020, 1, 1),
        industry="银行",
        is_st=False,
    )
    bars = [
        DailyBar(
            symbol=security.symbol,
            trade_date=trade_date,
            open_1e4=100_000,
            high_1e4=101_000,
            low_1e4=99_000,
            close_1e4=100_000,
            pre_close_1e4=100_000,
            volume_shares=1_000_000,
            amount_fen=8_000_000_000,
            source=SOURCE,
            source_timestamp=SOURCE_TIMESTAMP,
        )
    ]
    if future_bar:
        bars.append(
            DailyBar(
                symbol=security.symbol,
                trade_date=trade_date + timedelta(days=1),
                open_1e4=100_000,
                high_1e4=101_000,
                low_1e4=99_000,
                close_1e4=100_000,
                pre_close_1e4=100_000,
                volume_shares=1_000_000,
                amount_fen=8_000_000_000,
                source=SOURCE,
                source_timestamp=SOURCE_TIMESTAMP,
            )
        )
    return MarketSnapshot(
        trade_date=trade_date,
        source=SOURCE,
        source_timestamp=SOURCE_TIMESTAMP,
        securities=(security,),
        bars=tuple(bars),
        advance_ratio_bps=3_000,
        above_ma20_ratio_bps=3_000,
    )


class GovernanceReplayContractTest(unittest.TestCase):
    def test_single_version_governance_replay_is_complete_deterministic_and_evidenced(self) -> None:
        calendar = _calendar()
        self.assertEqual(1_096, (END - START).days)
        self.assertEqual(600, len(calendar))
        repository = _RecordedGovernanceRepository(
            tuple(_snapshot(day) for day in calendar), calendar
        )
        service = HistoricalReplayService(repository, _StrategyRegistry(_strategy()))

        replay = getattr(service, "replay_for_governance", None)
        self.assertTrue(
            callable(replay),
            "governance requires a dedicated single-version replay entry point",
        )
        first = replay("v0.1-proposed", START, END)
        second = replay("v0.1-proposed", START, END)

        self.assertEqual(first, second, "identical immutable inputs must replay byte-for-byte")
        self.assertEqual("v0.1-proposed", first["strategy_version"])
        self.assertEqual(
            tuple(date.fromisoformat(day) for day in first["snapshot_dates"]),
            calendar,
        )
        self.assertEqual(580, first["days_replayed"], "the first 20 sessions are warm-up")
        self.assertEqual(580, len(first["daily"]))
        self.assertTrue(
            all(day["candidates"] == [] for day in first["daily"]),
            "a defensive market with zero candidates is a valid replay result",
        )
        for name in ("parameters_hash", "dataset_hash", "result_hash"):
            with self.subTest(name=name):
                self.assertRegex(first[name], r"^[0-9a-f]{64}$")

    def test_single_version_governance_replay_rejects_a_future_bar_on_its_own_day(self) -> None:
        calendar = _calendar()
        snapshots = tuple(
            _snapshot(day, future_bar=(index == 200)) for index, day in enumerate(calendar)
        )
        service = HistoricalReplayService(
            _RecordedGovernanceRepository(snapshots, calendar), _StrategyRegistry(_strategy())
        )

        replay = getattr(service, "replay_for_governance", None)
        self.assertTrue(callable(replay), "governance replay entry point is required")
        with self.assertRaisesRegex(ValueError, "future bars"):
            replay("v0.1-proposed", START, END)

    def test_comparing_a_version_to_itself_is_rejected_without_recording_a_proof(self) -> None:
        calendar = _calendar()
        repository = _RecordedGovernanceRepository(
            tuple(_snapshot(day) for day in calendar), calendar
        )
        service = HistoricalReplayService(repository, _StrategyRegistry(_strategy()))

        with self.assertRaisesRegex(ValueError, "distinct strategy versions"):
            service.compare("v0.1-proposed", "v0.1-proposed", START, END)

        self.assertEqual([], repository.attestations)


class _RecordedGovernanceRepository:
    """Fixed, in-memory recorded market facts; it never uses a live endpoint."""

    def __init__(self, snapshots: tuple[MarketSnapshot, ...], calendar: tuple[date, ...]) -> None:
        self._snapshots = snapshots
        self._calendar = calendar
        self.attestations: list[tuple[object, ...]] = []

    def load_market_snapshots(
        self, start: date, end: date, *, source: str
    ) -> tuple[MarketSnapshot, ...]:
        return tuple(
            snapshot for snapshot in self._snapshots if start <= snapshot.trade_date <= end
        )

    def load_expected_trading_days(
        self, start: date, end: date, *, source: str
    ) -> tuple[date, ...]:
        return tuple(day for day in self._calendar if start <= day <= end)

    def record_governance_replay_attestation(self, *values: object) -> None:
        self.attestations.append(values)


class _StrategyRegistry:
    def __init__(self, strategy: StrategyVersion) -> None:
        self._strategy = strategy

    def get(self, version: str) -> StrategyVersion:
        if version != self._strategy.version:
            raise KeyError(version)
        return self._strategy
