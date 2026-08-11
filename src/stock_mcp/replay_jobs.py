"""Durable, bounded background orchestration for strategy-governance replays."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable, Mapping
from datetime import date, datetime
from fractions import Fraction
from typing import Any
from zoneinfo import ZoneInfo

from .outcomes import attach_v3_outcome_hash, evaluate_v3_candidate_outcomes
from .replay import (
    HistoricalReplayService,
    _dataset_hash,
    _result_hash,
    _review_result,
    _validate_point_in_time_snapshots,
    walk_forward,
)
from .strategy import canonical_strategy_parameters_hash
from .v3 import (
    INPUT_HASH_SCHEMA,
    OUTCOME_HASH_SCHEMA,
    PIPELINE_VERSION,
    REQUIRED_WARMUP_SESSIONS,
    RESULT_HASH_SCHEMA,
    canonical_v3_market_input_hash,
    canonical_v3_result_hash,
    generate_v3_daily_review,
)

_SOURCE = "tushare"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_V3_GOVERNANCE_START = date(2023, 8, 8)
_V3_GOVERNANCE_END = date(2026, 8, 7)
_V3_GOVERNANCE_SESSIONS = 727


def _is_v3_strategy(version: object, parameters: Mapping[str, object]) -> bool:
    name = str(version)
    return (
        parameters.get("rule_engine_version") == 3
        or name.startswith("v3")
        or name.startswith("v0.3-")
    )


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

    def compare_completed_replays(
        self, left: str, right: str, start: date, end: date
    ) -> dict[str, object]:
        """Compare persisted v3 replay evidence without recomputing or writing facts."""

        def completed(version: str) -> Mapping[str, object]:
            strategy = self._strategies.get(version)
            requires_v3_schema = _is_v3_strategy(version, strategy.parameters)
            jobs = self._database.list_strategy_replay_jobs(version=version, limit=200)
            match = next(
                (
                    job
                    for job in jobs
                    if job.get("status") == "completed"
                    and job.get("start_date") == start
                    and job.get("end_date") == end
                    and (
                        not requires_v3_schema
                        or job.get("result_hash_schema") == RESULT_HASH_SCHEMA
                    )
                ),
                None,
            )
            if match is None:
                raise ValueError(f"no completed persisted v3 replay for {version}")
            return match

        left_job = completed(left)
        right_job = completed(right)
        left_days = self._database.list_strategy_replay_days(str(left_job["job_id"]))
        right_days = self._database.list_strategy_replay_days(str(right_job["job_id"]))
        if tuple(day["trade_date"] for day in left_days) != tuple(
            day["trade_date"] for day in right_days
        ):
            raise ValueError("persisted v3 replay calendars do not match")
        daily = []
        left_count = 0
        right_count = 0
        for left_day, right_day in zip(left_days, right_days, strict=True):
            left_result = dict(left_day["result"])
            right_result = dict(right_day["result"])
            left_count += len(left_result.get("candidates", ()))
            right_count += len(right_result.get("candidates", ()))
            daily.append(
                {
                    "trade_date": left_day["trade_date"].isoformat(),
                    "left": left_result,
                    "right": right_result,
                }
            )
        return {
            "left_version": left,
            "right_version": right,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days_compared": len(daily),
            "left_candidate_count": left_count,
            "right_candidate_count": right_count,
            "result_hash_schema": RESULT_HASH_SCHEMA,
            "daily": daily,
        }

    def start_strategy_replay(
        self,
        *,
        version: str,
        start_date: date,
        end_date: date,
        idempotency_key: str,
    ) -> dict[str, object]:
        strategy = self._strategies.get(version)
        if strategy.status != "proposed" or getattr(strategy, "lifecycle", None) == "superseded":
            raise ValueError("only a proposed strategy version can start a governance replay")
        is_v3 = _is_v3_strategy(version, strategy.parameters)
        if is_v3 and (start_date, end_date) != (
            _V3_GOVERNANCE_START,
            _V3_GOVERNANCE_END,
        ):
            raise ValueError(
                "v3 governance replay requires the fixed 2023-08-08 to 2026-08-07 range"
            )
        if not 1_095 <= (end_date - start_date).days <= 1_100:
            raise ValueError("governance replay must cover 1095 to 1100 calendar days")
        expected = tuple(
            self._database.load_expected_trading_days(start_date, end_date, source=_SOURCE)
        )
        if len(expected) < 600:
            raise ValueError("governance replay requires at least 600 expected trading days")
        if is_v3 and len(expected) != _V3_GOVERNANCE_SESSIONS:
            raise ValueError("v3 governance replay requires exactly 727 expected trading days")
        snapshots = tuple(
            self._database.load_market_snapshot_dates(start_date, end_date, source=_SOURCE)
        )
        if snapshots != expected:
            raise ValueError("governance replay snapshots must exactly match the trading calendar")
        parameters_hash = canonical_strategy_parameters_hash(strategy.parameters)
        v3_metadata: dict[str, object] = {}
        if is_v3:
            standard = "新浪财经行业分类"
            mode = "retrospective_current_mapping"
            classification_as_of = date(2026, 8, 10)
            mapping_hash = "829fb6481d3269a59a2f679b09c2d2d93ada2ffd0db54931f2ec61b646ac1c1a"
            if strategy.parameters.get("rule_engine_version") == 3:
                loader = getattr(self._database, "load_v3_snapshot_features", None)
                if not callable(loader):
                    raise ValueError("v3 replay requires locally built v3 facts")
                features = loader(expected[REQUIRED_WARMUP_SESSIONS], source=_SOURCE)
                if not features:
                    raise ValueError("v3 replay requires locally built v3 facts")
                sample = next(iter(features.values()))
                if not isinstance(sample, Mapping):
                    raise ValueError("v3 industry reference metadata is invalid")
                standard = str(sample["industry_standard"])
                mode = str(sample["industry_mode"])
                classification_as_of = date.fromisoformat(str(sample["industry_as_of"]))
                mapping_hash = str(sample["industry_mapping_sha256"])
            replay_input_hash = hashlib.sha256(
                json.dumps(
                    {
                        "schema": INPUT_HASH_SCHEMA,
                        "pipeline": PIPELINE_VERSION,
                        "source": _SOURCE,
                        "dates": [value.isoformat() for value in expected],
                        "industry_mapping_sha256": mapping_hash,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            v3_metadata = {
                "pipeline_version": PIPELINE_VERSION,
                "input_hash": replay_input_hash,
                "input_hash_schema": INPUT_HASH_SCHEMA,
                "result_hash_schema": RESULT_HASH_SCHEMA,
                "outcome_hash_schema": OUTCOME_HASH_SCHEMA,
                "warmup_sessions": REQUIRED_WARMUP_SESSIONS,
                "industry_classification_standard": standard,
                "industry_classification_mode": mode,
                "industry_classification_as_of": classification_as_of,
                "industry_mapping_sha256": mapping_hash,
            }
        for existing in self._database.list_strategy_replay_jobs(version=version, limit=200):
            if (
                existing["parameters_hash"] == parameters_hash
                and existing["source"] == _SOURCE
                and existing["start_date"] == start_date
                and existing["end_date"] == end_date
                and existing["status"] != "failed"
            ):
                bind = getattr(self._database, "bind_strategy_replay_start_idempotency", None)
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
            **v3_metadata,
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
        job = self._database.get_strategy_replay_job(replay_id)
        return tuple(self._public_day(day, job=job) for day in days)

    def certify_strategy_replay(
        self, *, replay_id: str, confirmed: bool, idempotency_key: str
    ) -> dict[str, object] | None:
        if not confirmed:
            raise ValueError("explicit confirmation is required")
        if self._database.get_strategy_replay_job(replay_id) is None:
            return None
        job = self._database.get_strategy_replay_job(replay_id)
        if job is not None and (
            (
                job.get("input_hash_schema") == INPUT_HASH_SCHEMA
                or str(job.get("version", "")).startswith(("v3", "v0.3-"))
            )
            and (job.get("outcome_status") != "completed" or job.get("outcome_hash") is None)
        ):
            raise ValueError("v3 outcome evidence must complete before certification")
        self._database.certify_strategy_replay(replay_id, idempotency_key=idempotency_key)
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
                (item for item in reversed(jobs) if item["status"] in {"queued", "running"}),
                None,
            )
        if job is None:
            return self._resume_pending_v3_outcome()
        job_id = str(job["job_id"])
        try:
            next_trade_date = job["next_trade_date"]
            if not isinstance(next_trade_date, date):
                self._finish(job)
                return True
            expected = tuple(job["expected_sessions"])
            index = expected.index(next_trade_date)
            warmup_sessions = (
                REQUIRED_WARMUP_SESSIONS
                if str(job.get("version", "")).startswith(("v3", "v0.3-"))
                else int(job.get("warmup_sessions") or 20)
            )
            strategy = self._strategies.get(str(job["version"]))
            if job.get("input_hash_schema") == INPUT_HASH_SCHEMA and index >= warmup_sessions:
                from .v3_facts import load_v3_market_input

                market = load_v3_market_input(
                    self._database,
                    next_trade_date,
                    source=str(job["source"]),
                )
                reference = market.industry_reference
                recorded_reference = (
                    reference.classification_standard,
                    reference.classification_mode,
                    reference.classification_as_of,
                    reference.classification_mapping_sha256,
                )
                job_reference = (
                    job.get("industry_classification_standard"),
                    job.get("industry_classification_mode"),
                    job.get("industry_classification_as_of"),
                    job.get("industry_mapping_sha256"),
                )
                if recorded_reference != job_reference:
                    raise ValueError("v3 industry classification reference changed during replay")
                input_hash = canonical_v3_market_input_hash(market)
                review = generate_v3_daily_review(market, strategy)
                result: dict[str, object] = {
                    "warmup": False,
                    **_review_result(review),
                }
                output_hash = canonical_v3_result_hash(market, strategy, review)
            else:
                snapshot = self._database.load_market_snapshot(
                    next_trade_date,
                    source=str(job["source"]),
                )
                if snapshot.source != job["source"]:
                    raise ValueError("recorded market snapshot source does not match replay job")
                _validate_point_in_time_snapshots((snapshot,))
                target_bars = tuple(
                    bar for bar in snapshot.bars if bar.trade_date == next_trade_date
                )
                if (
                    snapshot.trade_date != next_trade_date
                    or not snapshot.securities
                    or not target_bars
                    or any(bar.source != snapshot.source for bar in snapshot.bars)
                ):
                    raise ValueError("recorded market snapshot is incomplete")
                input_hash = _dataset_hash((snapshot,))
                if index < warmup_sessions:
                    result = {
                        "warmup": True,
                        "market_regime": None,
                        "candidates": [],
                    }
                else:
                    review = walk_forward((snapshot,), strategy)[0]
                    result = {"warmup": False, **_review_result(review)}
                output_hash = _result_hash([{"trade_date": next_trade_date.isoformat(), **result}])
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

    def _resume_pending_v3_outcome(self) -> bool:
        get_pending = getattr(self._database, "get_next_pending_strategy_replay_outcome_job", None)
        if not callable(get_pending):
            return False
        job = get_pending()
        if job is None:
            return False
        job_id = str(job["job_id"])
        try:
            days = self._database.list_strategy_replay_days(job_id)
            self._finish_v3_outcomes(job, days)
        except Exception as error:
            fail_outcome = getattr(self._database, "fail_strategy_replay_outcome", None)
            if callable(fail_outcome):
                fail_outcome(job_id, error=f"{type(error).__name__}: {error}"[:1_000])
            else:
                raise
        return True

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = datetime.now(_SHANGHAI)
                worked = self._allowed(now) and self.run_next_session()
            except Exception as error:
                _LOGGER.warning("strategy replay worker recovered from %s", type(error).__name__)
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
        completed = self._database.complete_strategy_replay(
            job_id,
            dataset_hash=dataset_hash,
            result_hash=result_hash,
            summary=summary,
        )
        if completed.get("input_hash_schema") == INPUT_HASH_SCHEMA:
            try:
                self._finish_v3_outcomes(completed, days)
            except Exception as error:
                fail_outcome = getattr(self._database, "fail_strategy_replay_outcome", None)
                if callable(fail_outcome):
                    fail_outcome(job_id, error=f"{type(error).__name__}: {error}"[:1_000])
                else:
                    raise

    def _finish_v3_outcomes(
        self, job: Mapping[str, object], days: tuple[dict[str, object], ...]
    ) -> None:
        candidates: list[dict[str, object]] = []
        for day in days:
            result = day.get("result")
            if not isinstance(result, Mapping):
                continue
            for raw in result.get("candidates", ()):  # type: ignore[union-attr]
                if isinstance(raw, Mapping):
                    candidates.append({"trade_date": day["trade_date"], **dict(raw)})
        source = str(job["source"])
        end = job["end_date"]
        if not isinstance(end, date):
            raise ValueError("v3 replay end date is invalid")
        bars_by_symbol: dict[str, tuple[dict[str, object], ...]] = {}
        expected_count = int(job.get("expected_session_count") or 1)
        for symbol in sorted({str(candidate["symbol"]) for candidate in candidates}):
            history = self._database.load_symbol_history(
                symbol, end_date=end, source=source, limit=expected_count
            )
            bars_by_symbol[symbol] = tuple(_outcome_bar(bar) for bar in history)
        benchmark = self._equal_weight_benchmark(job)
        outcomes = evaluate_v3_candidate_outcomes(
            candidates=candidates,
            bars_by_symbol=bars_by_symbol,
            equal_weight_mainboard_bars=benchmark,
            as_of=end,
        )
        hashed = attach_v3_outcome_hash(job, outcomes)
        self._database.attach_strategy_replay_outcome(
            str(job["job_id"]), outcome=outcomes, outcome_hash=str(hashed["outcome_hash"])
        )

    def _equal_weight_benchmark(self, job: Mapping[str, object]) -> tuple[dict[str, object], ...]:
        source = str(job["source"])
        level = 100_000
        result: list[dict[str, object]] = []
        for session in job["expected_sessions"]:  # type: ignore[union-attr]
            bars = tuple(self._database.load_daily_bars(session, source))
            returns = tuple(
                Fraction(bar.close_1e4 - bar.pre_close_1e4, bar.pre_close_1e4)
                for bar in bars
                if bar.pre_close_1e4 > 0
            )
            if not returns:
                continue
            daily_return = sum(returns, Fraction(0)) / len(returns)
            close = max(1, int(Fraction(level) * (1 + daily_return)))
            result.append(
                {
                    "trade_date": session,
                    "pre_close_1e4": level,
                    "open_1e4": level,
                    "high_1e4": max(level, close),
                    "low_1e4": min(level, close),
                    "close_1e4": close,
                }
            )
            level = close
        return tuple(result)

    @staticmethod
    def _public_job(job: Mapping[str, object]) -> dict[str, object]:
        result = {
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
                "pipeline_version",
                "input_hash",
                "input_hash_schema",
                "result_hash_schema",
                "outcome_hash_schema",
                "warmup_sessions",
                "outcome_status",
                "outcome",
                "outcome_hash",
                "industry_classification_standard",
                "industry_classification_mode",
                "industry_classification_as_of",
                "industry_mapping_sha256",
            )
        }
        raw_outcome = result.get("outcome")
        if isinstance(raw_outcome, Mapping):
            result["outcome"] = {
                "candidates": [
                    {"candidate_id": str(candidate_id), **dict(evidence)}
                    for candidate_id, evidence in sorted(raw_outcome.items())
                    if isinstance(evidence, Mapping)
                ]
            }
        if str(job.get("version", "")).startswith(("v3", "v0.3-")):
            result.update(
                pipeline_version=result.get("pipeline_version") or PIPELINE_VERSION,
                input_hash_schema=result.get("input_hash_schema") or INPUT_HASH_SCHEMA,
                result_hash_schema=result.get("result_hash_schema") or RESULT_HASH_SCHEMA,
                outcome_hash_schema=result.get("outcome_hash_schema") or OUTCOME_HASH_SCHEMA,
                warmup_sessions=result.get("warmup_sessions") or REQUIRED_WARMUP_SESSIONS,
                industry_classification_standard=(
                    result.get("industry_classification_standard") or "新浪财经行业分类"
                ),
                industry_classification_mode=(
                    result.get("industry_classification_mode") or "retrospective_current_mapping"
                ),
                industry_classification_as_of=(
                    result.get("industry_classification_as_of") or date(2026, 8, 10)
                ),
                industry_mapping_sha256=(
                    result.get("industry_mapping_sha256")
                    or "829fb6481d3269a59a2f679b09c2d2d93ada2ffd0db54931f2ec61b646ac1c1a"
                ),
            )
        return result

    @staticmethod
    def _public_day(
        day: Mapping[str, object], *, job: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        result = day.get("result")
        facts = dict(result) if isinstance(result, Mapping) else {}
        candidates = facts.get("candidates")
        if (
            isinstance(candidates, list)
            and job is not None
            and isinstance(job.get("outcome"), Mapping)
        ):
            outcomes = job["outcome"]
            facts["candidates"] = [
                {
                    **dict(candidate),
                    "outcome": outcomes.get(str(candidate.get("candidate_id"))),
                }
                for candidate in candidates
                if isinstance(candidate, Mapping)
            ]
        return {
            "trade_date": day["trade_date"],
            "status": "completed",
            "input_hash": day.get("input_hash"),
            "output_hash": day.get("output_hash"),
            "pipeline_version": None if job is None else job.get("pipeline_version"),
            "input_hash_schema": None if job is None else job.get("input_hash_schema"),
            "result_hash_schema": None if job is None else job.get("result_hash_schema"),
            "industry_classification_standard": (
                None if job is None else job.get("industry_classification_standard")
            ),
            "industry_classification_mode": (
                None if job is None else job.get("industry_classification_mode")
            ),
            "industry_classification_as_of": (
                None if job is None else job.get("industry_classification_as_of")
            ),
            "industry_mapping_sha256": (
                None if job is None else job.get("industry_mapping_sha256")
            ),
            **facts,
        }


def _ordered_hash(days: tuple[dict[str, object], ...], field: str) -> str:
    payload = [{"trade_date": day["trade_date"].isoformat(), field: day[field]} for day in days]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _outcome_bar(bar: Any) -> dict[str, object]:
    return {
        "trade_date": bar.trade_date,
        "open_1e4": bar.open_1e4,
        "high_1e4": bar.high_1e4,
        "low_1e4": bar.low_1e4,
        "close_1e4": bar.close_1e4,
        "pre_close_1e4": bar.pre_close_1e4,
    }
