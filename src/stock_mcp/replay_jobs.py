"""Durable, bounded background orchestration for strategy-governance replays."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .replay import (
    HistoricalReplayService,
    _dataset_hash,
    _result_hash,
    _review_result,
    _validate_point_in_time_snapshots,
    walk_forward,
)
from .strategy import canonical_strategy_parameters_hash

_SOURCE = "tushare"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_LOGGER = logging.getLogger(__name__)


class StrategyReplayCoordinator:
    """Expose persistent replay use cases and process at most one session per step."""

    def __init__(
        self,
        database: Any,
        strategy_registry: Any,
        *,
        allowed: Callable[[datetime], bool] | None = None,
        poll_seconds: float = 1.0,
    ) -> None:
        self._database = database
        self._strategies = strategy_registry
        self._comparison = HistoricalReplayService(database, strategy_registry)
        self._allowed = allowed or (lambda _now: True)
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    def compare(self, left: str, right: str, start: date, end: date) -> dict[str, object]:
        return self._comparison.compare(left, right, start, end)

    def start_strategy_replay(
        self,
        *,
        version: str,
        start_date: date,
        end_date: date,
        idempotency_key: str,
    ) -> dict[str, object]:
        strategy = self._strategies.get(version)
        if strategy.status != "proposed":
            raise ValueError("only a proposed strategy version can start a governance replay")
        if not 1_095 <= (end_date - start_date).days <= 1_100:
            raise ValueError("governance replay must cover 1095 to 1100 calendar days")
        expected = tuple(
            self._database.load_expected_trading_days(start_date, end_date, source=_SOURCE)
        )
        if len(expected) < 600:
            raise ValueError("governance replay requires at least 600 expected trading days")
        snapshots = tuple(
            self._database.load_market_snapshot_dates(start_date, end_date, source=_SOURCE)
        )
        if snapshots != expected:
            raise ValueError("governance replay snapshots must exactly match the trading calendar")
        parameters_hash = canonical_strategy_parameters_hash(strategy.parameters)
        for existing in self._database.list_strategy_replay_jobs(version=version, limit=200):
            if (
                existing["parameters_hash"] == parameters_hash
                and existing["source"] == _SOURCE
                and existing["start_date"] == start_date
                and existing["end_date"] == end_date
                and existing["status"] != "failed"
            ):
                bind = getattr(
                    self._database, "bind_strategy_replay_start_idempotency", None
                )
                if callable(bind):
                    existing = bind(
                        str(existing["job_id"]),
                        strategy_version=version,
                        start_date=start_date,
                        end_date=end_date,
                        idempotency_key=idempotency_key,
                    )
                return self._public_job(existing)
        job = self._database.create_strategy_replay_job(
            strategy_version=version,
            parameters_hash=parameters_hash,
            source=_SOURCE,
            start_date=start_date,
            end_date=end_date,
            expected_sessions=expected,
            idempotency_key=idempotency_key,
        )
        return self._public_job(job)

    def get_strategy_replay(self, *, replay_id: str) -> dict[str, object] | None:
        job = self._database.get_strategy_replay_job(replay_id)
        return None if job is None else self._public_job(job)

    def list_strategy_replays(
        self, *, version: str | None = None, limit: int = 20
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            self._public_job(job)
            for job in self._database.list_strategy_replay_jobs(version=version, limit=limit)
        )

    def get_strategy_replay_days(
        self,
        *,
        replay_id: str,
        after_trade_date: date | None = None,
        limit: int = 20,
    ) -> tuple[dict[str, object], ...] | None:
        if self._database.get_strategy_replay_job(replay_id) is None:
            return None
        days = self._database.list_strategy_replay_days(
            replay_id,
            after_trade_date=after_trade_date,
            limit=limit,
        )
        return tuple(self._public_day(day) for day in days)

    def certify_strategy_replay(
        self, *, replay_id: str, confirmed: bool, idempotency_key: str
    ) -> dict[str, object] | None:
        if not confirmed:
            raise ValueError("explicit confirmation is required")
        if self._database.get_strategy_replay_job(replay_id) is None:
            return None
        self._database.certify_strategy_replay(
            replay_id, idempotency_key=idempotency_key
        )
        refreshed = self._database.get_strategy_replay_job(replay_id)
        return None if refreshed is None else self._public_job(refreshed)

    def requeue_interrupted(self) -> None:
        self._database.requeue_interrupted_strategy_replays()

    def start_background(self) -> None:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run_loop,
                name="stock-mcp-strategy-replay",
                daemon=True,
            )
            self._thread.start()

    def run_next_session(self) -> bool:
        get_next = getattr(self._database, "get_next_runnable_strategy_replay_job", None)
        if callable(get_next):
            job = get_next()
        else:
            jobs = self._database.list_strategy_replay_jobs(limit=200)
            job = next(
                (
                    item
                    for item in reversed(jobs)
                    if item["status"] in {"queued", "running"}
                ),
                None,
            )
        if job is None:
            return False
        job_id = str(job["job_id"])
        try:
            next_trade_date = job["next_trade_date"]
            if not isinstance(next_trade_date, date):
                self._finish(job)
                return True
            expected = tuple(job["expected_sessions"])
            index = expected.index(next_trade_date)
            snapshot = self._database.load_market_snapshot(
                next_trade_date,
                source=str(job["source"]),
            )
            if snapshot.source != job["source"]:
                raise ValueError("recorded market snapshot source does not match replay job")
            _validate_point_in_time_snapshots((snapshot,))
            target_bars = tuple(bar for bar in snapshot.bars if bar.trade_date == next_trade_date)
            if (
                snapshot.trade_date != next_trade_date
                or not snapshot.securities
                or not target_bars
                or any(bar.source != snapshot.source for bar in snapshot.bars)
            ):
                raise ValueError("recorded market snapshot is incomplete")
            input_hash = _dataset_hash((snapshot,))
            if index < 20:
                result: dict[str, object] = {
                    "warmup": True,
                    "market_regime": None,
                    "candidates": [],
                }
            else:
                review = walk_forward((snapshot,), self._strategies.get(str(job["version"])))[0]
                result = {"warmup": False, **_review_result(review)}
            output_hash = _result_hash(
                [{"trade_date": next_trade_date.isoformat(), **result}]
            )
            self._database.save_strategy_replay_day(
                job_id,
                trade_date=next_trade_date,
                input_hash=input_hash,
                output_hash=output_hash,
                result=result,
            )
            refreshed = self._database.get_strategy_replay_job(job_id)
            if refreshed is not None and refreshed["next_trade_date"] is None:
                self._finish(refreshed)
        except Exception as error:  # the persisted failure is the operator-visible boundary
            current = self._database.get_strategy_replay_job(job_id)
            if current is not None and current["status"] not in {"completed", "failed"}:
                message = f"{type(error).__name__}: {error}"[:1_000]
                self._database.fail_strategy_replay(job_id, error=message)
        return True

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = datetime.now(_SHANGHAI)
                worked = self._allowed(now) and self.run_next_session()
            except Exception as error:
                _LOGGER.warning(
                    "strategy replay worker recovered from %s", type(error).__name__
                )
                worked = False
            if not worked:
                self._stop.wait(self._poll_seconds)

    def _finish(self, job: Mapping[str, object]) -> None:
        job_id = str(job["job_id"])
        days = self._database.list_strategy_replay_days(job_id)
        dataset_hash = _ordered_hash(days, "input_hash")
        result_hash = _ordered_hash(days, "output_hash")
        reviewed = tuple(day for day in days if not bool(day["result"].get("warmup")))
        candidate_counts = tuple(len(day["result"].get("candidates", ())) for day in reviewed)
        summary = {
            "sessions": len(days),
            "reviewed_sessions": len(reviewed),
            "total_candidates": sum(candidate_counts),
            "zero_candidate_days": sum(count == 0 for count in candidate_counts),
            "max_candidates_per_day": max(candidate_counts, default=0),
        }
        self._database.complete_strategy_replay(
            job_id,
            dataset_hash=dataset_hash,
            result_hash=result_hash,
            summary=summary,
        )

    @staticmethod
    def _public_job(job: Mapping[str, object]) -> dict[str, object]:
        return {
            key: job.get(key)
            for key in (
                "replay_id",
                "version",
                "source",
                "start_date",
                "end_date",
                "status",
                "certified",
                "parameters_hash",
                "dataset_hash",
                "result_hash",
                "expected_session_count",
                "processed_sessions",
                "next_trade_date",
                "summary",
                "error",
                "created_at",
                "started_at",
                "completed_at",
            )
        }

    @staticmethod
    def _public_day(day: Mapping[str, object]) -> dict[str, object]:
        result = day.get("result")
        facts = dict(result) if isinstance(result, Mapping) else {}
        return {
            "trade_date": day["trade_date"],
            "status": "completed",
            "input_hash": day.get("input_hash"),
            "output_hash": day.get("output_hash"),
            **facts,
        }


def _ordered_hash(days: tuple[dict[str, object], ...], field: str) -> str:
    payload = [
        {"trade_date": day["trade_date"].isoformat(), field: day[field]}
        for day in days
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
