"""Offline RED contracts for the v3 durable-replay lifecycle."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta

from stock_mcp.domain import DailyBar, MarketSnapshot, Security, StrategyVersion
from stock_mcp.replay_jobs import StrategyReplayCoordinator

START = date(2023, 8, 8)
END = date(2026, 8, 7)
SESSIONS = tuple(START + timedelta(days=round(index * 1_095 / 726)) for index in range(727))


class StrategyReplayV3ContractTest(unittest.TestCase):
    def test_v3_governance_replay_requires_the_fixed_three_year_dataset(self) -> None:
        coordinator = StrategyReplayCoordinator(_V3ReplayRepository(), _V3Registry())

        with self.assertRaisesRegex(ValueError, "2023-08-08|fixed"):
            coordinator.start_strategy_replay(
                version="v3-proposed",
                start_date=date(2023, 8, 9),
                end_date=END,
                idempotency_key="wrong-v3-range",
            )

    def test_v3_job_records_pipeline_hash_schema_and_sixty_session_warmup(self) -> None:
        """A v3 job binds its immutable pipeline and hashing contract before work starts."""
        repository = _V3ReplayRepository()
        coordinator = StrategyReplayCoordinator(repository, _V3Registry())

        replay = coordinator.start_strategy_replay(
            version="v3-proposed",
            start_date=START,
            end_date=END,
            idempotency_key="v3-start",
        )

        self.assertEqual("pipeline-v0.2", replay.get("pipeline_version"))
        self.assertEqual("v3-input-v1", replay.get("input_hash_schema"))
        self.assertEqual("v3-result-v1", replay.get("result_hash_schema"))
        self.assertEqual("v3-outcome-v1", replay.get("outcome_hash_schema"))
        self.assertEqual(60, replay.get("warmup_sessions"))
        self.assertEqual("新浪财经行业分类", replay.get("industry_classification_standard"))
        self.assertEqual(
            "retrospective_current_mapping", replay.get("industry_classification_mode")
        )
        self.assertIsInstance(replay.get("industry_mapping_sha256"), str)
        self.assertRegex(str(replay.get("industry_mapping_sha256")), r"^[0-9a-f]{64}$")

    def test_v3_worker_marks_exactly_the_first_sixty_sessions_as_warmup(self) -> None:
        """The 61st recorded session is the first that may emit candidate evidence."""
        repository = _V3ReplayRepository()
        coordinator = StrategyReplayCoordinator(repository, _V3Registry())
        coordinator.start_strategy_replay(
            version="v3-proposed",
            start_date=START,
            end_date=END,
            idempotency_key="v3-warmup",
        )

        for _ in range(61):
            self.assertTrue(coordinator.run_next_session())

        actual_warmup = [day["result"]["warmup"] for day in repository.days]
        self.assertEqual([True] * 60 + [False], actual_warmup)

    def test_v3_certification_requires_completed_outcomes(self) -> None:
        """Candidate proof may finish first, but v3 governance proof requires outcome proof too."""
        repository = _V3ReplayRepository()
        coordinator = StrategyReplayCoordinator(repository, _V3Registry())
        replay = coordinator.start_strategy_replay(
            version="v3-proposed",
            start_date=START,
            end_date=END,
            idempotency_key="v3-certification",
        )
        assert repository.job is not None
        repository.job.update(
            status="completed",
            processed_sessions=len(SESSIONS),
            next_trade_date=None,
            outcome_status="queued",
        )

        with self.assertRaisesRegex(ValueError, "outcome"):
            coordinator.certify_strategy_replay(
                replay_id=str(replay["replay_id"]),
                confirmed=True,
                idempotency_key="v3-certification",
            )

        self.assertEqual(0, repository.certification_calls)

    def test_v3_worker_resumes_outcome_work_after_candidate_replay_restart(self) -> None:
        repository = _V3ReplayRepository()
        coordinator = StrategyReplayCoordinator(repository, _V3Registry())
        coordinator.start_strategy_replay(
            version="v3-proposed",
            start_date=START,
            end_date=END,
            idempotency_key="v3-outcome-resume",
        )
        assert repository.job is not None
        repository.job.update(
            status="completed",
            processed_sessions=len(SESSIONS),
            next_trade_date=None,
            input_hash_schema="v3-input-v1",
            outcome_hash_schema="v3-outcome-v1",
            outcome_status="queued",
        )
        calls: list[str] = []
        coordinator._finish_v3_outcomes = (  # type: ignore[method-assign]
            lambda job, days: calls.append(str(job["job_id"]))
        )

        self.assertTrue(coordinator.run_next_session())

        self.assertEqual(["replay-v3"], calls)

    def test_v1_and_v2_completed_replays_remain_certifiable_without_outcome_evidence(self) -> None:
        """The v3 outcome gate must not retroactively invalidate legacy proof."""
        for version in ("v1-proposed", "v2-proposed"):
            with self.subTest(version=version):
                repository = _V3ReplayRepository()
                coordinator = StrategyReplayCoordinator(repository, _V3Registry())
                replay = coordinator.start_strategy_replay(
                    version=version,
                    start_date=START,
                    end_date=END,
                    idempotency_key=f"{version}-certification",
                )
                assert repository.job is not None
                repository.job.update(
                    status="completed",
                    processed_sessions=len(SESSIONS),
                    next_trade_date=None,
                )
                for field in (
                    "outcome_status",
                    "outcome_hash",
                    "outcome_hash_schema",
                ):
                    repository.job.pop(field, None)

                certified = coordinator.certify_strategy_replay(
                    replay_id=str(replay["replay_id"]),
                    confirmed=True,
                    idempotency_key=f"{version}-certification",
                )

                self.assertTrue(certified["certified"])
                self.assertEqual(1, repository.certification_calls)


class _V3Registry:
    def get(self, version: str) -> StrategyVersion:
        if version not in {"v1-proposed", "v2-proposed", "v3-proposed"}:
            raise KeyError(version)
        return StrategyVersion(
            version=version,
            status="proposed",
            parameters={
                "rule_engine_version": 2,
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


class _V3ReplayRepository:
    """In-memory, fixed fixture: it cannot fetch live market data."""

    def __init__(self) -> None:
        self.days: list[dict[str, object]] = []
        self.job: dict[str, object] | None = None
        self.certification_calls = 0

    def load_expected_trading_days(self, start: date, end: date, *, source: str):
        return SESSIONS

    def load_market_snapshot_dates(self, start: date, end: date, *, source: str):
        return SESSIONS

    def create_strategy_replay_job(self, **values: object) -> dict[str, object]:
        self.job = {
            "job_id": "replay-v3",
            "replay_id": "replay-v3",
            "version": values["strategy_version"],
            "strategy_version": values["strategy_version"],
            "parameters_hash": values["parameters_hash"],
            "source": values["source"],
            "start_date": values["start_date"],
            "end_date": values["end_date"],
            "expected_sessions": tuple(values["expected_sessions"]),
            "expected_session_count": len(SESSIONS),
            "processed_sessions": 0,
            "next_trade_date": SESSIONS[0],
            "status": "queued",
            "certified": False,
            "pipeline_version": None,
            "input_hash_schema": None,
            "result_hash_schema": None,
            "outcome_hash_schema": None,
            "warmup_sessions": None,
            "outcome_status": None,
            "outcome_hash": None,
            "industry_classification_standard": None,
            "industry_classification_mode": None,
            "industry_classification_as_of": None,
            "industry_mapping_sha256": None,
        }
        return dict(self.job)

    def list_strategy_replay_jobs(self, *, version: str | None = None, limit: int = 20):
        return () if self.job is None else (dict(self.job),)

    def get_strategy_replay_job(self, job_id: str):
        return None if self.job is None or job_id != self.job["job_id"] else dict(self.job)

    def get_next_pending_strategy_replay_outcome_job(self):
        if self.job is None:
            return None
        if self.job.get("status") != "completed":
            return None
        if self.job.get("outcome_status") not in {"queued", "running"}:
            return None
        return dict(self.job)

    def list_strategy_replay_days(self, job_id: str):
        return tuple(self.days)

    def load_market_snapshot(self, target: date, *, source: str) -> MarketSnapshot:
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
            source=source,
            source_timestamp=datetime(2026, 8, 7, tzinfo=UTC),
        )
        return MarketSnapshot(
            trade_date=target,
            source=source,
            source_timestamp=bar.source_timestamp,
            securities=(security,),
            bars=(bar,),
            advance_ratio_bps=3_000,
            above_ma20_ratio_bps=3_000,
        )

    def save_strategy_replay_day(self, job_id: str, **values: object) -> dict[str, object]:
        self.days.append(dict(values))
        assert self.job is not None
        completed = len(self.days)
        self.job["status"] = "running"
        self.job["processed_sessions"] = completed
        self.job["next_trade_date"] = None if completed == len(SESSIONS) else SESSIONS[completed]
        return dict(values)

    def fail_strategy_replay(self, job_id: str, *, error: str) -> dict[str, object]:
        assert self.job is not None
        self.job.update(status="failed", error=error)
        return dict(self.job)

    def certify_strategy_replay(self, job_id: str, *, idempotency_key: str) -> dict[str, object]:
        self.certification_calls += 1
        assert self.job is not None
        self.job["certified"] = True
        return {"job_id": job_id}


if __name__ == "__main__":
    unittest.main()
