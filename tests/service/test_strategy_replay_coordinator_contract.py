"""Offline contract for one-session-at-a-time durable replay execution."""

from __future__ import annotations

import time
import unittest
from datetime import UTC, date, datetime, timedelta

from stock_mcp.domain import DailyBar, MarketSnapshot, Security, StrategyVersion

try:
    from stock_mcp.replay_jobs import StrategyReplayCoordinator
except ImportError:
    StrategyReplayCoordinator = None  # type: ignore[assignment,misc]


START = date(2023, 1, 1)
END = date(2026, 1, 1)
SESSIONS = tuple(START + timedelta(days=round(index * 1_096 / 599)) for index in range(600))


class StrategyReplayCoordinatorContractTest(unittest.TestCase):
    def test_only_proposed_strategies_can_start_governance_replay(self) -> None:
        repository = _Repository()
        coordinator = StrategyReplayCoordinator(repository, _Registry(status="active"))

        with self.assertRaisesRegex(ValueError, "proposed"):
            coordinator.start_strategy_replay(
                version="v0.1-proposed",
                start_date=START,
                end_date=END,
                idempotency_key="active-version",
            )

        self.assertEqual(0, repository.created)

    def test_start_is_deduplicated_and_worker_persists_only_one_session_per_step(self) -> None:
        self.assertIsNotNone(StrategyReplayCoordinator, "durable replay coordinator is required")
        if StrategyReplayCoordinator is None:
            return
        repository = _Repository()
        coordinator = StrategyReplayCoordinator(repository, _Registry())

        first = coordinator.start_strategy_replay(
            version="v0.1-proposed",
            start_date=START,
            end_date=END,
            idempotency_key="start-1",
        )
        repeated = coordinator.start_strategy_replay(
            version="v0.1-proposed",
            start_date=START,
            end_date=END,
            idempotency_key="start-1",
        )

        self.assertEqual(first["replay_id"], repeated["replay_id"])
        self.assertEqual(1, repository.created)
        self.assertTrue(coordinator.run_next_session())
        self.assertEqual([SESSIONS[0]], [day["trade_date"] for day in repository.days])
        self.assertTrue(repository.days[0]["result"]["warmup"])
        self.assertEqual("running", repository.job["status"])

        with self.assertRaisesRegex(ValueError, "idempotency"):
            coordinator.start_strategy_replay(
                version="v0.1-proposed",
                start_date=START,
                end_date=END + timedelta(days=1),
                idempotency_key="start-1",
            )

        aliased = coordinator.start_strategy_replay(
            version="v0.1-proposed",
            start_date=START,
            end_date=END,
            idempotency_key="start-alias",
        )
        self.assertEqual(first["replay_id"], aliased["replay_id"])
        with self.assertRaisesRegex(ValueError, "idempotency"):
            coordinator.start_strategy_replay(
                version="v0.1-proposed",
                start_date=START,
                end_date=END + timedelta(days=1),
                idempotency_key="start-alias",
            )

    def test_warmup_future_fact_and_empty_review_snapshot_fail_explicitly(self) -> None:
        self.assertIsNotNone(StrategyReplayCoordinator)
        if StrategyReplayCoordinator is None:
            return
        future_repository = _Repository(future_bar=True)
        future = StrategyReplayCoordinator(future_repository, _Registry())
        future.start_strategy_replay(
            version="v0.1-proposed",
            start_date=START,
            end_date=END,
            idempotency_key="future",
        )

        self.assertTrue(future.run_next_session())
        self.assertEqual("failed", future_repository.job["status"])
        self.assertEqual([], future_repository.days)

        empty_repository = _Repository(empty_snapshot=True, start_index=20)
        empty = StrategyReplayCoordinator(empty_repository, _Registry())
        empty.start_strategy_replay(
            version="v0.1-proposed",
            start_date=START,
            end_date=END,
            idempotency_key="empty",
        )

        self.assertTrue(empty.run_next_session())
        self.assertEqual("failed", empty_repository.job["status"])
        self.assertEqual([], empty_repository.days)

    def test_snapshot_source_must_match_the_replay_job_source(self) -> None:
        repository = _Repository(snapshot_source="other-provider")
        coordinator = StrategyReplayCoordinator(repository, _Registry())
        coordinator.start_strategy_replay(
            version="v0.1-proposed",
            start_date=START,
            end_date=END,
            idempotency_key="wrong-source",
        )

        self.assertTrue(coordinator.run_next_session())
        self.assertEqual("failed", repository.job["status"])
        self.assertEqual([], repository.days)

    def test_background_worker_survives_transient_policy_errors(self) -> None:
        calls = 0

        def temporarily_broken(_now: datetime) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("clock failure")
            return False

        coordinator = StrategyReplayCoordinator(
            _Repository(), _Registry(), allowed=temporarily_broken, poll_seconds=0.01
        )
        coordinator.start_background()
        time.sleep(0.05)

        self.assertIsNotNone(coordinator._thread)
        self.assertTrue(coordinator._thread.is_alive())
        coordinator._stop.set()


class _Registry:
    def __init__(self, *, status: str = "proposed") -> None:
        self.strategy = StrategyVersion(
            version="v0.1-proposed",
            status=status,
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

    def get(self, version: str) -> StrategyVersion:
        if version != self.strategy.version:
            raise KeyError(version)
        return self.strategy


class _Repository:
    def __init__(
        self,
        *,
        future_bar: bool = False,
        empty_snapshot: bool = False,
        start_index: int = 0,
        snapshot_source: str = "tushare",
    ) -> None:
        self.created = 0
        self.days: list[dict[str, object]] = []
        self.job: dict[str, object] | None = None
        self.future_bar = future_bar
        self.empty_snapshot = empty_snapshot
        self.start_index = start_index
        self.snapshot_source = snapshot_source
        self.start_requests: dict[str, tuple[object, ...]] = {}

    def load_expected_trading_days(self, start: date, end: date, *, source: str):
        return SESSIONS

    def load_market_snapshot_dates(self, start: date, end: date, *, source: str):
        return SESSIONS

    def create_strategy_replay_job(self, **values):
        key = values["idempotency_key"]
        request = (
            values["strategy_version"],
            values["start_date"],
            values["end_date"],
        )
        prior = self.start_requests.get(key)
        if prior is not None:
            if prior != request:
                raise ValueError("idempotency key cannot be reused for a different request")
            assert self.job is not None
            return dict(self.job)
        self.start_requests[key] = request
        self.created += 1
        self.job = {
            "job_id": "replay-1",
            "replay_id": "replay-1",
            "strategy_version": values["strategy_version"],
            "version": values["strategy_version"],
            "parameters_hash": values["parameters_hash"],
            "source": values["source"],
            "start_date": values["start_date"],
            "end_date": values["end_date"],
            "expected_sessions": tuple(values["expected_sessions"]),
            "expected_session_count": len(SESSIONS),
            "processed_sessions": self.start_index,
            "next_trade_date": SESSIONS[self.start_index],
            "status": "queued",
            "dataset_hash": None,
            "result_hash": None,
            "summary": None,
            "error": None,
            "certified": False,
        }
        return dict(self.job)

    def list_strategy_replay_jobs(self, *, version=None, limit=20):
        if self.job is None:
            return ()
        if version is not None and version != self.job["version"]:
            return ()
        return (dict(self.job),)

    def bind_strategy_replay_start_idempotency(
        self, job_id: str, *, strategy_version, start_date, end_date, idempotency_key
    ):
        request = (strategy_version, start_date, end_date)
        prior = self.start_requests.get(idempotency_key)
        if prior is not None and prior != request:
            raise ValueError("idempotency key cannot be reused for a different request")
        self.start_requests[idempotency_key] = request
        assert self.job is not None and self.job["job_id"] == job_id
        return dict(self.job)

    def get_strategy_replay_job(self, job_id: str):
        return None if self.job is None else dict(self.job)

    def load_market_snapshot(self, target: date, *, source: str):
        if self.empty_snapshot:
            return MarketSnapshot(
                trade_date=target,
                source="tushare",
                source_timestamp=datetime(2026, 8, 7, tzinfo=UTC),
                securities=(),
                bars=(),
                advance_ratio_bps=3_000,
                above_ma20_ratio_bps=3_000,
            )
        security = Security(
            symbol="600001.SH",
            name="脱敏样本",
            exchange="SSE",
            board="MAIN",
            list_date=date(2020, 1, 1),
            industry="银行",
            is_st=False,
        )
        bar = DailyBar(
            symbol=security.symbol,
            trade_date=target,
            open_1e4=100_000,
            high_1e4=101_000,
            low_1e4=99_000,
            close_1e4=100_000,
            pre_close_1e4=100_000,
            volume_shares=1_000_000,
            amount_fen=8_000_000_000,
            source=self.snapshot_source,
            source_timestamp=datetime(2026, 8, 7, tzinfo=UTC),
        )
        bars = [bar]
        if self.future_bar:
            bars.append(
                DailyBar(
                    symbol=security.symbol,
                    trade_date=target + timedelta(days=1),
                    open_1e4=100_000,
                    high_1e4=101_000,
                    low_1e4=99_000,
                    close_1e4=100_000,
                    pre_close_1e4=100_000,
                    volume_shares=1_000_000,
                    amount_fen=8_000_000_000,
                    source=self.snapshot_source,
                    source_timestamp=bar.source_timestamp,
                )
            )
        return MarketSnapshot(
            trade_date=target,
            source=self.snapshot_source,
            source_timestamp=bar.source_timestamp,
            securities=(security,),
            bars=tuple(bars),
            advance_ratio_bps=3_000,
            above_ma20_ratio_bps=3_000,
        )

    def save_strategy_replay_day(self, job_id: str, **values):
        self.days.append(dict(values))
        assert self.job is not None
        self.job["status"] = "running"
        self.job["processed_sessions"] = len(self.days)
        self.job["next_trade_date"] = SESSIONS[len(self.days)]
        return dict(values)

    def fail_strategy_replay(self, job_id: str, *, error: str):
        assert self.job is not None
        self.job["status"] = "failed"
        self.job["error"] = error
        return dict(self.job)


if __name__ == "__main__":
    unittest.main()
