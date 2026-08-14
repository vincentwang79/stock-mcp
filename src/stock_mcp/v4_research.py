"""Durable public orchestration boundary for preregistered v4 studies."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from datetime import date, datetime
from fractions import Fraction
from typing import Any, Protocol

from .domain import StrategyVersion, V3BreadthFacts, V3MarketInput
from .outcomes_v2 import evaluate_v4_candidate_outcomes, validate_v4_outcome_batch
from .strategy import (
    build_v4_research_arms,
    evaluate_v4_research_statistics,
)
from .v3 import _v3_pool_percentiles, adjusted_close_chain, v3_proposal_parameters
from .v3_facts import load_v3_market_input
from .v4 import generate_v4_daily_review


class V4ResearchRepository(Protocol):
    """Persistence seam owned by the v4 research job repository adapter.

    Claiming must atomically transition a queued study to running.  The executor
    records exactly one durable step after each claim, then optionally completes
    the study with its final report.  A startup recovery operation must make an
    queued and interrupted running studies claimable again without discarding
    prior steps.
    """

    def create_v4_study_run(
        self, *, manifest_hash: str, idempotency_key: str, arms: Any
    ) -> dict[str, object]: ...

    def claim_next_v4_study(self) -> dict[str, object] | None: ...

    def save_v4_study_step(self, *, study_id: str, step: dict[str, object]) -> None: ...

    def save_v4_study_steps(
        self, *, study_id: str, steps: tuple[dict[str, object], ...]
    ) -> None: ...

    def complete_v4_study(self, *, study_id: str, report: dict[str, object]) -> None: ...

    def fail_v4_study(self, *, study_id: str, error: str) -> None: ...

    def requeue_interrupted_v4_studies(self) -> int: ...


V4StudyStepExecutor = Callable[[dict[str, object]], Mapping[str, object]]
V4ResearchAllowed = Callable[[datetime], bool]

_V4_ARM_IDS = (
    "v0.3-policy-1",
    "v4-trend-quality",
    "v4-breakout-overextension-cap",
    "v4-no-recent-limit-up",
    "v4-breadth-five-day-median",
    "v4-size-bottom-30pct-filter",
    "v4-signal-quality-rank",
)
_PRIMARY_METRIC = "20d_25bps_market_cap_matched_excess_bps"


class V4StudyExecutor:
    """Execute one immutable v4 day (or the terminal statistics step) per call.

    The cursor is derived exclusively from persisted study days.  Therefore a
    process restart can repeat the call without skipping or overwriting work.
    ``data_loader`` is injectable so all standard tests remain offline; the
    default loader reads only facts already frozen in the local SQLite file.
    """

    def __init__(self, repository: Any, *, data_loader: Any | None = None) -> None:
        self._repository = repository
        self._injected_loader = data_loader is not None
        self._data_loader = data_loader or SQLiteV4StudyDataLoader(repository)

    def __call__(self, study: dict[str, object]) -> dict[str, object]:
        study_id = str(study.get("study_id", ""))
        manifest_hash = str(study.get("manifest_hash", ""))
        if not study_id or not manifest_hash:
            raise ValueError("v4 study identity is incomplete")
        manifest = self._repository.get_v4_dataset_manifest(manifest_hash)
        _validate_study_manifest(
            manifest,
            manifest_hash,
            require_frozen_evidence=not self._injected_loader,
        )
        assert manifest is not None
        sessions = tuple(date.fromisoformat(str(item)) for item in manifest["sessions"])
        signal_dates = sessions[60:-25]
        arms = tuple(str(item["arm_id"]) for item in self._repository.list_v4_study_arms(study_id))
        if set(arms) != set(_V4_ARM_IDS) or len(arms) != len(_V4_ARM_IDS):
            raise ValueError("v4 study requires the frozen seven research arms")
        progress = self._repository.get_v4_study_progress(study_id=study_id)
        for arm_id in arms:
            item = progress.get(arm_id)
            if not isinstance(item, Mapping):
                raise ValueError("v4 study progress is incomplete")
            count = item.get("completed_count")
            last = item.get("last_signal_date")
            if not isinstance(count, int) or count < 0 or count > len(signal_dates):
                raise ValueError("v4 study progress is invalid")
            expected_last = None if count == 0 else signal_dates[count - 1].isoformat()
            if last != expected_last:
                raise ValueError("v4 study progress contains a gap")
        for index, signal_date in enumerate(signal_dates):
            missing_arms = tuple(
                arm_id
                for arm_id in _V4_ARM_IDS
                if int(progress[arm_id]["completed_count"]) <= index
            )
            if not missing_arms:
                continue
            load_batch = getattr(self._data_loader, "load_v4_signal_evidence_batch", None)
            if callable(load_batch):
                evidence_by_arm = load_batch(
                    manifest_hash=manifest_hash,
                    signal_date=signal_date,
                    arm_ids=missing_arms,
                )
                if not isinstance(evidence_by_arm, Mapping) or set(evidence_by_arm) != set(
                    missing_arms
                ):
                    raise ValueError("v4 study day batch is incomplete")
                steps = tuple(
                    {
                        "kind": "day",
                        "signal_date": signal_date.isoformat(),
                        "arm_id": arm_id,
                        "result": _validated_day_result(
                            dict(evidence_by_arm[arm_id]), source=str(manifest["source"])
                        ),
                    }
                    for arm_id in missing_arms
                )
                return {"steps": steps, "complete": False}
            for arm_id in missing_arms:
                evidence = dict(
                    self._data_loader.load_v4_signal_evidence(
                        manifest_hash=manifest_hash,
                        signal_date=signal_date,
                        arm_id=arm_id,
                    )
                )
                result = _validated_day_result(evidence, source=str(manifest["source"]))
                step = {
                    "kind": "day",
                    "signal_date": signal_date.isoformat(),
                    "arm_id": arm_id,
                    "result": result,
                }
                return {"step": step, "complete": False}

        days_by_arm = {
            arm_id: self._repository.list_v4_study_days(
                study_id=study_id,
                arm_id=arm_id,
                after_signal_date=None,
                limit=len(signal_dates) + 1,
            )
            for arm_id in _V4_ARM_IDS
        }
        report, statistics = _terminal_report(
            manifest_hash=manifest_hash,
            signal_dates=signal_dates,
            days_by_arm=days_by_arm,
        )
        # The coordinator persists the returned step before committing the
        # report. Reusing the final immutable day makes this terminal action
        # idempotent without inventing a non-day cursor row.
        final_day = days_by_arm[_V4_ARM_IDS[-1]][-1]
        final_step = {
            "kind": "day",
            "signal_date": str(final_day["signal_date"]),
            "arm_id": _V4_ARM_IDS[-1],
            "result": dict(final_day["result"]),
        }
        return {
            "step": final_step,
            "complete": True,
            "statistics": statistics,
            "report": report,
        }


class SQLiteV4StudyDataLoader:
    """Build one v4 arm/day exclusively from the frozen local manifest facts."""

    def __init__(self, database: Any) -> None:
        self._database = database
        self._cache: dict[tuple[str, date], dict[str, object]] = {}
        self._verified_revisions: dict[str, int] = {}

    def load_v4_signal_evidence(
        self, *, manifest_hash: str, signal_date: date, arm_id: str
    ) -> dict[str, object]:
        return self.load_v4_signal_evidence_batch(
            manifest_hash=manifest_hash,
            signal_date=signal_date,
            arm_ids=(arm_id,),
        )[arm_id]

    def load_v4_signal_evidence_batch(
        self, *, manifest_hash: str, signal_date: date, arm_ids: tuple[str, ...]
    ) -> dict[str, dict[str, object]]:
        """Evaluate all requested arms from one frozen signal-day input.

        Candidate execution outcomes are arm-independent.  The batch therefore
        evaluates each unique candidate once and projects the immutable result
        back into every arm that selected it.
        """

        if not arm_ids or len(set(arm_ids)) != len(arm_ids):
            raise ValueError("v4 arm batch is empty or contains duplicates")
        self._verify_manifest_evidence(manifest_hash)
        key = (manifest_hash, signal_date)
        common = self._cache.get(key)
        if common is None:
            common = self._load_common(manifest_hash, signal_date)
            self._cache[key] = common
        candidates_by_arm: dict[str, tuple[dict[str, object], ...]] = {}
        unique_candidates: dict[str, dict[str, object]] = {}
        projections: dict[str, tuple[object, ...]] = {}
        for arm_id in arm_ids:
            review = generate_v4_daily_review(
                market=common["market"],
                strategy=common["strategy"],
                prior_four_breadth=common["prior_four_breadth"],
                arm_id=arm_id,
                features=common["features"],
            )
            candidates = tuple(
                _candidate_payload(
                    candidate,
                    signal_date,
                    common["features"].get(candidate.symbol, {}),
                )
                for candidate in review.candidates
            )
            candidates_by_arm[arm_id] = candidates
            for candidate in candidates:
                candidate_id = str(candidate["candidate_id"])
                projection = _outcome_candidate_projection(candidate)
                if candidate_id in projections and projections[candidate_id] != projection:
                    raise ValueError("v4 candidate outcome inputs conflict across arms")
                projections[candidate_id] = projection
                unique_candidates.setdefault(candidate_id, candidate)
        all_outcomes = evaluate_v4_candidate_outcomes(
            candidates=tuple(unique_candidates.values()),
            bars_by_symbol=common["bars_by_symbol"],
            status_by_symbol=common["status_by_symbol"],
            mainboard_bars=common["mainboard_bars"],
            source="tushare",
            as_of=common["outcome_through"],
        )
        evidence_by_arm: dict[str, dict[str, object]] = {}
        for arm_id, candidates in candidates_by_arm.items():
            outcomes = {
                str(candidate["candidate_id"]): all_outcomes[str(candidate["candidate_id"])]
                for candidate in candidates
            }
            try:
                validate_v4_outcome_batch(candidates=candidates, outcomes=outcomes)
                complete = True
            except ValueError:
                complete = False
            try:
                metric = v4_daily_primary_metric_bps(
                    outcomes=outcomes, candidate_count=len(candidates)
                )
            except ValueError:
                complete = False
                metric = 0
            evidence_by_arm[arm_id] = {
                "source": "tushare",
                "source_timestamp": common["source_timestamp"],
                "candidates": candidates,
                "candidate_outcomes": outcomes,
                "daily_primary_metric_bps": metric,
                "completeness_status": "complete" if complete else "incomplete",
                # The primary Tushare study cannot manufacture its own independent
                # Sina replication evidence. That remains a separate persisted gate.
                "replication_evidence": None,
            }
        return evidence_by_arm

    def _verify_manifest_evidence(self, manifest_hash: str) -> dict[str, object]:
        manifest = self._database.get_v4_dataset_manifest(manifest_hash)
        _validate_study_manifest(manifest, manifest_hash)
        assert manifest is not None
        manifest_sessions = tuple(date.fromisoformat(str(item)) for item in manifest["sessions"])
        provider_sessions = tuple(
            self._database.load_expected_trading_days(
                manifest_sessions[0], manifest_sessions[-1], source="tushare"
            )
        )
        expected_calendar_hash = hashlib.sha256(
            "|".join(day.isoformat() for day in manifest_sessions).encode()
        ).hexdigest()
        if (
            provider_sessions != manifest_sessions
            or manifest.get("calendar_hash") != expected_calendar_hash
        ):
            raise ValueError("v4 manifest does not match the recorded provider calendar")
        revision = int(self._database.get_v4_evidence_revision())
        if self._verified_revisions.get(manifest_hash) == revision:
            return manifest
        hashes = self._database.compute_v4_evidence_hashes(
            start=date.fromisoformat(str(manifest["bar_start"])),
            end=date.fromisoformat(str(manifest["outcome_through"])),
            included_symbols=tuple(str(item) for item in manifest["included_symbols"]),
        )
        if any(manifest.get(name) != value for name, value in hashes.items()):
            raise ValueError("v4 manifest evidence hashes do not match the local database")
        self._verified_revisions[manifest_hash] = revision
        return manifest

    def _load_common(self, manifest_hash: str, signal_date: date) -> dict[str, object]:
        self._cache.clear()
        manifest = self._verify_manifest_evidence(manifest_hash)
        included = tuple(str(item) for item in manifest["included_symbols"])
        included_set = set(included)
        market = _restrict_v4_market(
            load_v3_market_input(
                self._database,
                signal_date,
                source="tushare",
                included_symbols=frozenset(included),
            ),
            included_set,
        )
        sessions = tuple(date.fromisoformat(str(item)) for item in manifest["sessions"])
        outcome_through = date.fromisoformat(str(manifest["outcome_through"]))
        future_dates = tuple(day for day in sessions if signal_date < day <= outcome_through)[:25]
        capital_by_symbol_date = _load_share_capital_window(
            self._database,
            symbols=included,
            dates=(signal_date, *future_dates),
        )
        strategy = StrategyVersion(
            version="v0.3-policy-1", status="proposed", parameters=v3_proposal_parameters(1)
        )
        limit_facts_by_date = {
            day: self._database.load_daily_price_limits(day, source="tushare")
            for day in market.prior_dates[-20:]
        }
        features = _v4_features(
            market,
            included_set,
            capital_by_symbol_date=capital_by_symbol_date,
            limit_facts_by_date=limit_facts_by_date,
        )
        position = sessions.index(signal_date)
        if position < 4:
            raise ValueError("v4 signal date lacks four prior breadth sessions")
        prior_four_breadth = tuple(
            _load_v4_breadth(self._database, day, source="tushare", included=included_set)
            for day in sessions[position - 4 : position]
        )
        mainboard_rows, status_by_symbol = _load_outcome_rows(
            self._database,
            symbols=tuple(sorted(features)),
            dates=future_dates,
            capital_by_symbol_date=capital_by_symbol_date,
            signal_market_caps={
                symbol: facts.get("market_cap_fen") for symbol, facts in features.items()
            },
            signal_closes={
                item.security.symbol: item.target_bar.close_1e4 for item in market.securities
            },
        )
        bars_by_symbol: dict[str, list[dict[str, object]]] = {}
        for row in mainboard_rows:
            bars_by_symbol.setdefault(str(row["symbol"]), []).append(row)
        return {
            "source_timestamp": market.source_timestamp.isoformat(),
            "market": market,
            "strategy": strategy,
            "prior_four_breadth": prior_four_breadth,
            "features": features,
            "bars_by_symbol": bars_by_symbol,
            "status_by_symbol": status_by_symbol,
            "mainboard_bars": mainboard_rows,
            "outcome_through": outcome_through,
        }


def _validated_day_result(evidence: dict[str, object], *, source: str) -> dict[str, object]:
    if evidence.get("source") != source:
        raise ValueError("v4 study day cannot mix price sources")
    candidates = evidence.get("candidates")
    outcomes = evidence.get("candidate_outcomes")
    metric = evidence.get("daily_primary_metric_bps")
    completeness = evidence.get("completeness_status")
    if not isinstance(candidates, (tuple, list)) or not isinstance(outcomes, Mapping):
        raise ValueError("v4 study day evidence is incomplete")
    if not isinstance(metric, int) or isinstance(metric, bool):
        raise ValueError("v4 study day primary metric is invalid")
    candidate_ids = {
        str(item.get("candidate_id", "")) for item in candidates if isinstance(item, Mapping)
    }
    if candidate_ids != set(map(str, outcomes)):
        raise ValueError("v4 candidate outcomes do not match the selected candidates")
    if completeness not in {"complete", "incomplete"}:
        raise ValueError("v4 study day completeness is invalid")
    return {
        "source": source,
        "source_timestamp": evidence.get("source_timestamp"),
        "candidates": [dict(item) for item in candidates],
        "candidate_outcomes": {str(key): dict(value) for key, value in outcomes.items()},
        "daily_primary_metric_bps": metric,
        "completeness_status": completeness,
        "replication_evidence": evidence.get("replication_evidence"),
    }


def _terminal_report(
    *,
    manifest_hash: str,
    signal_dates: tuple[date, ...],
    days_by_arm: Mapping[str, tuple[dict[str, object], ...]],
) -> tuple[dict[str, object], dict[str, object]]:
    expected = tuple(day.isoformat() for day in signal_dates)
    complete = True
    returns: dict[str, tuple[int, ...]] = {}
    metadata: dict[str, dict[str, object]] = {}
    replications: list[object] = []
    rates: dict[str, tuple[int, int]] = {}
    completeness_by_arm: dict[str, bool] = {}
    completed_days_by_arm: dict[str, tuple[bool, ...]] = {}
    for arm_id in _V4_ARM_IDS:
        days = days_by_arm[arm_id]
        if tuple(str(day["signal_date"]) for day in days) != expected:
            raise ValueError("v4 study signal-day calendar is incomplete")
        results = tuple(dict(day["result"]) for day in days)
        completed_days = tuple(item.get("completeness_status") == "complete" for item in results)
        completeness = all(completed_days)
        completeness_by_arm[arm_id] = completeness
        completed_days_by_arm[arm_id] = completed_days
        complete = complete and completeness
        returns[arm_id] = tuple(int(item["daily_primary_metric_bps"]) for item in results)
        selected = tuple(
            outcome
            for result in results
            for outcome in (
                result.get("candidate_outcomes", {}).values()
                if isinstance(result.get("candidate_outcomes"), Mapping)
                else ()
            )
            if isinstance(outcome, Mapping)
        )
        executable = sum(
            1
            for outcome in selected
            if isinstance(outcome.get("next_open_path"), Mapping)
            and outcome["next_open_path"].get("status") == "available"
        )
        executable_rate = 10_000 if not selected else executable * 10_000 // len(selected)
        unexecutable_rate = 0 if not selected else 10_000 - executable_rate
        rates[arm_id] = (executable_rate, unexecutable_rate)
    baseline_executable, baseline_unexecutable = rates[_V4_ARM_IDS[0]]
    for arm_id in _V4_ARM_IDS:
        executable_rate, unexecutable_rate = rates[arm_id]
        completed_days = completed_days_by_arm[arm_id]
        metadata[arm_id] = {
            "completeness_rate_bps": _completion_rate_bps(completed_days),
            "executable_rate_bps": executable_rate,
            "executable_rate_delta_bps": executable_rate - baseline_executable,
            "unexecutable_rate_delta_bps": unexecutable_rate - baseline_unexecutable,
            "unexecutable_rate_bps": unexecutable_rate,
        }
    for arm_id in _V4_ARM_IDS:
        replications.extend(
            item.get("replication_evidence")
            for item in tuple(dict(day["result"]) for day in days_by_arm[arm_id])
        )
    replication = _consistent_replication(replications)
    statistics = evaluate_v4_research_statistics(
        manifest_hash=manifest_hash,
        primary_metric=_PRIMARY_METRIC,
        daily_candidate_returns=returns,
        arm_metadata=metadata,
        replication_evidence=replication,
    )
    if not complete:
        statistics = {
            **statistics,
            "winner": {"eligible": False, "arm_id": None, "decision": "retain_baseline"},
        }
    winner = dict(statistics["winner"])
    completed_signal_days = tuple(
        all(completed_days_by_arm[arm_id][index] for arm_id in _V4_ARM_IDS)
        for index in range(len(signal_dates))
    )
    completion_rate_bps = _completion_rate_bps(completed_signal_days)
    report = {
        "schema": "v4-statistics-v1",
        "manifest_hash": manifest_hash,
        "completeness_status": "complete" if complete else "incomplete",
        "outcome_completeness_rate_bps": completion_rate_bps,
        "benchmark_completeness_rate_bps": completion_rate_bps,
        "sina_replication_complete": bool(statistics.get("sina_replication_complete")),
        "winner": winner,
        "retain_version": "v0.3-policy-1" if not winner.get("eligible") else None,
        # Proposal material is intentionally absent until an independently
        # persisted Sina replication artifact is available.
        "proposals": [],
        "statistics_hash": _canonical_hash(statistics),
    }
    return report, statistics


def derive_v4_study_amendment(repository: Any, *, source_study_id: str) -> dict[str, object]:
    """Derive a corrected report from immutable persisted v4 day evidence.

    This is intentionally read-only.  It never rewrites the source study and
    only recognizes the narrowly frozen confirmed-entry expiry correction.
    Any other incomplete evidence remains a hard failure.
    """

    run = repository.get_v4_study_run(source_study_id)
    if not isinstance(run, Mapping) or run.get("status") != "completed":
        raise ValueError("v4 amendment requires a completed source study")
    manifest_hash = str(run.get("manifest_hash", ""))
    manifest = repository.get_v4_dataset_manifest(manifest_hash)
    _validate_study_manifest(manifest, manifest_hash)
    assert manifest is not None
    sessions = tuple(date.fromisoformat(str(item)) for item in manifest["sessions"])
    signal_dates = sessions[60:-25]
    arms = tuple(str(item["arm_id"]) for item in repository.list_v4_study_arms(source_study_id))
    if set(arms) != set(_V4_ARM_IDS) or len(arms) != len(_V4_ARM_IDS):
        raise ValueError("v4 amendment requires the frozen seven research arms")

    corrections: list[dict[str, object]] = []
    source_day_hashes: list[dict[str, str]] = []
    amended_days: dict[str, tuple[dict[str, object], ...]] = {}
    for arm_id in _V4_ARM_IDS:
        source_days = repository.list_v4_study_days(
            study_id=source_study_id,
            arm_id=arm_id,
            after_signal_date=None,
            limit=len(signal_dates) + 1,
        )
        values: list[dict[str, object]] = []
        for source_day in source_days:
            signal_date = str(source_day["signal_date"])
            source_day_hashes.append(
                {
                    "arm_id": arm_id,
                    "signal_date": signal_date,
                    "result_hash": str(source_day["result_hash"]),
                }
            )
            result = json.loads(
                json.dumps(source_day["result"], ensure_ascii=False, sort_keys=True)
            )
            day_corrections = _amend_v4_confirmed_entry_expiry(
                result,
                arm_id=arm_id,
                signal_date=signal_date,
            )
            corrections.extend(day_corrections)
            values.append({"signal_date": signal_date, "result": result})
        amended_days[arm_id] = tuple(values)

    unexpected = tuple(
        (arm_id, str(day["signal_date"]))
        for arm_id, days in amended_days.items()
        for day in days
        if day["result"].get("completeness_status") != "complete"  # type: ignore[union-attr]
    )
    if unexpected:
        raise ValueError("v4 amendment found unsupported incomplete evidence")
    report, statistics = _terminal_report(
        manifest_hash=manifest_hash,
        signal_dates=signal_dates,
        days_by_arm=amended_days,
    )
    amendment: dict[str, object] = {
        "schema": "v4-study-amendment-v1",
        "policy": "confirmed-entry-expiry-v1",
        "source_study_id": source_study_id,
        "source_result_hash": str(run.get("result_hash", "")),
        "source_days_hash": _canonical_hash(source_day_hashes),
        "manifest_hash": manifest_hash,
        "corrected_day_count": len({(item["arm_id"], item["signal_date"]) for item in corrections}),
        "corrected_outcome_count": len(corrections),
        "corrections": corrections,
        "report": report,
        "statistics": statistics,
    }
    amendment["amendment_hash"] = _canonical_hash(amendment)
    return amendment


def _amend_v4_confirmed_entry_expiry(
    result: dict[str, object], *, arm_id: str, signal_date: str
) -> tuple[dict[str, object], ...]:
    outcomes = result.get("candidate_outcomes")
    if not isinstance(outcomes, dict):
        raise ValueError("v4 amendment candidate outcomes are missing")
    corrections: list[dict[str, object]] = []
    for candidate_id, outcome in outcomes.items():
        if not isinstance(outcome, dict) or outcome.get("completeness_status") == "complete":
            continue
        signal_path = outcome.get("signal_close_path")
        next_path = outcome.get("next_open_path")
        confirmed = outcome.get("confirmed_next_open_path")
        benchmark = next_path.get("benchmark") if isinstance(next_path, dict) else None
        supported = (
            outcome.get("schema") == "v4-outcome-v2"
            and outcome.get("calendar_complete") is True
            and isinstance(signal_path, dict)
            and signal_path.get("status") == "available"
            and isinstance(next_path, dict)
            and next_path.get("status") in {"available", "unexecutable"}
            and isinstance(benchmark, dict)
            and benchmark.get("completeness_rate_bps") == 10_000
            and isinstance(confirmed, dict)
            and confirmed.get("status") == "confirmed"
            and confirmed.get("execution_status") in {"partial", "unavailable"}
            and isinstance(confirmed.get("entry_date"), str)
            and confirmed.get("gross_return_20d_bps") is None
        )
        if not supported:
            continue
        original_entry_date = confirmed.get("entry_date")
        original_execution_status = str(confirmed.get("execution_status"))
        confirmed.update(
            {
                "status": "confirmed",
                "execution_status": "unexecutable",
                "entry_date": None,
                "gross_return_5d_bps": 0,
                "gross_return_10d_bps": 0,
                "gross_return_20d_bps": 0,
                "net_return_bps_by_cost": {
                    str(cost): {"5": 0, "10": 0, "20": 0} for cost in (10, 25, 50)
                },
                "mfe_20d_bps": 0,
                "mae_20d_bps": 0,
                "execution_terminal_reason": "entry_expired_before_20_session_horizon",
                "first_late_executable_date": original_entry_date,
            }
        )
        outcome["completeness_status"] = "complete"
        corrections.append(
            {
                "arm_id": arm_id,
                "signal_date": signal_date,
                "candidate_id": str(candidate_id),
                "original_execution_status": original_execution_status,
                "terminal_status": "unexecutable",
            }
        )
    if all(
        isinstance(outcome, dict) and outcome.get("completeness_status") == "complete"
        for outcome in outcomes.values()
    ):
        result["completeness_status"] = "complete"
    return tuple(corrections)


def _completion_rate_bps(completed_days: tuple[bool, ...]) -> int:
    if not completed_days:
        raise ValueError("v4 study has no signal days")
    return sum(completed_days) * 10_000 // len(completed_days)


def _consistent_replication(values: list[object]) -> Mapping[str, object] | None:
    present = [dict(item) for item in values if isinstance(item, Mapping)]
    if not present:
        return None
    first = present[0]
    if any(item != first for item in present[1:]):
        raise ValueError("v4 Sina replication evidence conflicts across study days")
    return first


def _validate_study_manifest(
    manifest: object, manifest_hash: str, *, require_frozen_evidence: bool = True
) -> None:
    if not isinstance(manifest, Mapping):
        raise ValueError("unknown v4 dataset manifest")
    sessions = manifest.get("sessions")
    if (
        manifest.get("manifest_hash") != manifest_hash
        or manifest.get("source") != "tushare"
        or not isinstance(sessions, list)
        or len(sessions) < 86
    ):
        raise ValueError("v4 dataset manifest is incomplete")
    parsed = tuple(date.fromisoformat(str(item)) for item in sessions)
    if parsed != tuple(sorted(set(parsed))):
        raise ValueError("v4 dataset manifest calendar is invalid")
    if not require_frozen_evidence:
        return
    canonical = dict(manifest)
    recorded_hash = str(canonical.pop("manifest_hash", ""))
    calculated_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if recorded_hash != manifest_hash or calculated_hash != manifest_hash:
        raise ValueError("v4 dataset manifest hash is invalid")
    if manifest.get("schema") != "v4-manifest-v1":
        raise ValueError("v4 dataset manifest is incomplete")
    required = (
        "signal_start",
        "signal_end",
        "outcome_through",
        "prices_hash",
        "statuses_hash",
        "share_capital_hash",
        "industry_mapping_hash",
        "included_symbols",
    )
    if any(name not in manifest for name in required):
        raise ValueError("v4 dataset manifest is incomplete")
    if (
        str(manifest["signal_start"]) != parsed[60].isoformat()
        or str(manifest["signal_end"]) != parsed[-26].isoformat()
        or str(manifest["outcome_through"]) != parsed[-1].isoformat()
    ):
        raise ValueError("v4 dataset manifest boundaries are invalid")


def _candidate_payload(
    candidate: Any, signal_date: date, facts: Mapping[str, object]
) -> dict[str, object]:
    evidence = [
        {
            "metric": item.metric,
            "value": item.value,
            "threshold": item.threshold,
            "passed": item.passed,
            "score_contribution": item.score_contribution,
        }
        for item in candidate.evidence
    ]
    return {
        "candidate_id": candidate.candidate_id,
        "symbol": candidate.symbol,
        "name": candidate.name,
        "trade_date": signal_date.isoformat(),
        "rank": candidate.rank,
        "score": candidate.score,
        "setup_type": candidate.setup_type.value,
        "evidence": evidence,
        "confirmation_condition": candidate.confirmation_condition,
        "invalidation_condition": candidate.invalidation_condition,
        "market_cap_fen": facts.get("market_cap_fen"),
        "signal_close_1e4": facts.get("signal_close_1e4"),
    }


def _outcome_candidate_projection(candidate: Mapping[str, object]) -> tuple[object, ...]:
    """Return exactly the fields that can change v4 outcome evaluation."""

    return tuple(
        candidate.get(name)
        for name in (
            "candidate_id",
            "symbol",
            "trade_date",
            "confirmation_condition",
            "invalidation_condition",
            "market_cap_fen",
            "signal_close_1e4",
        )
    )


def _v4_features(
    market: Any,
    included: set[str],
    *,
    capital_by_symbol_date: Mapping[str, Mapping[date, int]],
    limit_facts_by_date: Mapping[date, Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    caps: dict[str, int] = {}
    for item in market.securities:
        if item.security.symbol not in included:
            continue
        bars = (*item.prior_bars, item.target_bar)
        adjusted = adjusted_close_chain(bars)
        factors = tuple(value / bar.close_1e4 for value, bar in zip(adjusted, bars, strict=True))
        prior_high = max(
            Fraction(bar.high_1e4) * factor
            for bar, factor in zip(item.prior_bars, factors[:-1], strict=True)
        )
        outstanding = capital_by_symbol_date.get(item.security.symbol, {}).get(market.trade_date)
        market_cap = None
        if outstanding is not None:
            market_cap = item.target_bar.close_1e4 * outstanding // 100
            caps[item.security.symbol] = market_cap
        rows[item.security.symbol] = {
            "return_20d_bps": _fraction_bps(adjusted[-1], adjusted[-21]),
            "ma20_rising_5d": sum(adjusted[-20:]) > sum(adjusted[-25:-5]),
            "breakout_overextension_bps": _fraction_bps(adjusted[-1], prior_high),
            "prior_20_touched_up_count": _prior_limit_up_count(item, limit_facts_by_date),
            "five_day_breadth_complete": True,
            "market_cap_fen": market_cap,
            "signal_close_1e4": item.target_bar.close_1e4,
        }
    sorted_caps = sorted(caps.values())
    for symbol, cap in caps.items():
        lower = sum(value < cap for value in sorted_caps)
        equal = sum(value == cap for value in sorted_caps)
        rows[symbol]["market_cap_percentile_bps"] = (
            10_000
            if len(sorted_caps) == 1
            else (2 * lower + equal - 1) * 5_000 // (len(sorted_caps) - 1)
        )
    percentile_facts = _v3_pool_percentiles(
        market,
        StrategyVersion("v0.3-policy-1", "proposed", v3_proposal_parameters(1)),
    )
    for symbol, facts in percentile_facts.items():
        rows[symbol].update(facts)
    return rows


def _prior_limit_up_count(
    item: Any, limit_facts_by_date: Mapping[date, Mapping[str, object]]
) -> int:
    count = 0
    for bar in item.prior_bars[-20:]:
        facts = limit_facts_by_date.get(bar.trade_date)
        if not isinstance(facts, Mapping):
            raise ValueError(
                "v4 prior price-limit evidence is incomplete for "
                f"{item.security.symbol} on {bar.trade_date.isoformat()}"
            )
        fact = facts.get(item.security.symbol)
        if not isinstance(fact, Mapping):
            raise ValueError(
                "v4 prior price-limit evidence is incomplete for "
                f"{item.security.symbol} on {bar.trade_date.isoformat()}"
            )
        if fact.get("touched_up"):
            count += 1
    return count


def _restrict_v4_market(market: V3MarketInput, included: set[str]) -> V3MarketInput:
    """Apply the frozen manifest universe before any v4 score or rank is computed."""

    securities = tuple(
        item
        for item in market.securities
        if item.security.symbol in included
        and tuple(bar.trade_date for bar in item.prior_bars) == market.prior_dates
    )
    if not securities:
        raise ValueError("v4 manifest screening universe is empty on the signal date")
    industries = {
        symbol: industry
        for symbol, industry in market.industry_reference.industries.items()
        if symbol in included
    }
    return V3MarketInput(
        trade_date=market.trade_date,
        source=market.source,
        source_timestamp=market.source_timestamp,
        prior_dates=market.prior_dates,
        securities=securities,
        breadth=_v4_breadth_from_securities(securities),
        industry_reference=type(market.industry_reference)(
            classification_standard=market.industry_reference.classification_standard,
            classification_mode=market.industry_reference.classification_mode,
            classification_as_of=market.industry_reference.classification_as_of,
            classification_mapping_sha256=market.industry_reference.classification_mapping_sha256,
            industries=industries,
        ),
        pipeline_version=market.pipeline_version,
        input_hash_schema=market.input_hash_schema,
    )


def _load_v4_breadth(
    database: Any, target: date, *, source: str, included: set[str]
) -> V3BreadthFacts:
    """Reconstruct one PIT breadth observation with only its required MA20 history."""

    snapshot = database.load_market_snapshot(target, source=source, history_limit=21)
    limits = database.load_daily_price_limits(target, source=source)
    bars_by_symbol: dict[str, list[Any]] = {}
    for bar in snapshot.bars:
        if bar.source != source or bar.trade_date > target:
            raise ValueError("v4 breadth contains mixed-source or future bars")
        bars_by_symbol.setdefault(bar.symbol, []).append(bar)
    eligible = advance = ma20_eligible = above_ma20 = 0
    for security in snapshot.securities:
        if security.symbol not in included:
            continue
        facts = limits.get(security.symbol)
        bars = sorted(bars_by_symbol.get(security.symbol, ()), key=lambda item: item.trade_date)
        target_bars = tuple(bar for bar in bars if bar.trade_date == target)
        prior = tuple(bar for bar in bars if bar.trade_date < target)
        if (
            security.board != "MAIN"
            or security.is_st
            or (target - security.list_date).days < 180
            or len(target_bars) != 1
            or not isinstance(facts, Mapping)
            or bool(facts.get("policy_exception"))
        ):
            continue
        eligible += 1
        target_bar = target_bars[0]
        if target_bar.close_1e4 > target_bar.pre_close_1e4:
            advance += 1
        if len(prior) >= 19:
            adjusted = adjusted_close_chain(prior[-19:], target_bar)
            ma20_eligible += 1
            if adjusted[-1] > sum(adjusted) / 20:
                above_ma20 += 1
    if eligible <= 0 or ma20_eligible * 10_000 // eligible < 9_700:
        raise ValueError("v4 breadth evidence is incomplete")
    return V3BreadthFacts(
        advance_count=advance,
        eligible_count=eligible,
        above_ma20_count=above_ma20,
        ma20_eligible_count=ma20_eligible,
        advance_ratio_bps=advance * 10_000 // eligible,
        above_ma20_ratio_bps=above_ma20 * 10_000 // ma20_eligible,
    )


def _v4_breadth_from_securities(securities: tuple[Any, ...]) -> V3BreadthFacts:
    eligible = advance = ma20_eligible = above_ma20 = 0
    for item in securities:
        security = item.security
        if (
            security.board != "MAIN"
            or security.is_st
            or (item.target_bar.trade_date - security.list_date).days < 180
            or item.price_limit.policy_exception
        ):
            continue
        eligible += 1
        if item.target_bar.close_1e4 > item.target_bar.pre_close_1e4:
            advance += 1
        adjusted = adjusted_close_chain(item.prior_bars[-19:], item.target_bar)
        if len(adjusted) == 20:
            ma20_eligible += 1
            if adjusted[-1] > sum(adjusted) / 20:
                above_ma20 += 1
    if eligible <= 0 or ma20_eligible * 10_000 // eligible < 9_700:
        raise ValueError("v4 breadth evidence is incomplete")
    return V3BreadthFacts(
        advance_count=advance,
        eligible_count=eligible,
        above_ma20_count=above_ma20,
        ma20_eligible_count=ma20_eligible,
        advance_ratio_bps=advance * 10_000 // eligible,
        above_ma20_ratio_bps=above_ma20 * 10_000 // ma20_eligible,
    )


def v4_daily_primary_metric_bps(*, outcomes: Mapping[str, object], candidate_count: int) -> int:
    """Compute the registered daily metric without charging nonexistent entries."""

    if candidate_count == 0:
        return 0
    if len(outcomes) != candidate_count:
        raise ValueError("v4 primary metric outcome set is incomplete")
    values: list[int] = []
    for item in outcomes.values():
        if not isinstance(item, Mapping):
            raise ValueError("v4 primary metric outcome is invalid")
        path = item.get("next_open_path")
        if not isinstance(path, Mapping):
            raise ValueError("v4 primary metric path is missing")
        benchmark = path.get("benchmark")
        matched = (
            benchmark.get("market_cap_decile_return_bps")
            if isinstance(benchmark, Mapping)
            else None
        )
        gross = path.get("gross_return_20d_bps")
        peer = matched.get(20) if isinstance(matched, Mapping) else None
        if not isinstance(gross, int) or not isinstance(peer, int):
            raise ValueError("v4 primary metric evidence is incomplete")
        cost = 0 if path.get("status") == "unexecutable" else 25
        values.append(gross - cost - peer)
    return _floor_average(values)


def _load_outcome_rows(
    database: Any,
    *,
    symbols: tuple[str, ...],
    dates: tuple[date, ...],
    capital_by_symbol_date: Mapping[str, Mapping[date, int]],
    signal_market_caps: Mapping[str, object],
    signal_closes: Mapping[str, int],
) -> tuple[list[dict[str, object]], dict[str, dict[str, int]]]:
    if len(dates) != 25:
        raise ValueError("v4 outcome requires exactly twenty-five reserved sessions")
    symbol_set = set(symbols)
    statuses: dict[str, dict[str, int]] = {}
    with database.connect() as connection:
        placeholders = ",".join("?" for _ in dates)
        query = (
            "SELECT symbol,trade_date,tradestatus FROM daily_security_status "
            f"WHERE source='baostock' AND trade_date IN ({placeholders})"
        )
        for symbol, trade_date, status in connection.execute(
            query, tuple(day.isoformat() for day in dates)
        ):
            if str(symbol) in symbol_set:
                parsed = int(status)
                if parsed not in {0, 1}:
                    raise ValueError("v4 outcome tradeStatus must be 0 or 1")
                statuses.setdefault(str(symbol), {})[str(trade_date)] = parsed
    rows: list[dict[str, object]] = []
    for day in dates:
        bars = database.load_daily_bars(day, "tushare")
        by_symbol = {bar.symbol: bar for bar in bars if bar.symbol in symbol_set}
        for symbol in symbols:
            outstanding = capital_by_symbol_date.get(symbol, {}).get(day)
            if outstanding is None:
                raise ValueError("v4 outcome share-capital evidence is incomplete")
            signal_cap = signal_market_caps.get(symbol)
            if not isinstance(signal_cap, int) or isinstance(signal_cap, bool) or signal_cap <= 0:
                raise ValueError("v4 signal-date market-cap evidence is incomplete")
            bar = by_symbol.get(symbol)
            day_key = day.isoformat()
            recorded_status = statuses.get(symbol, {}).get(day_key)
            if recorded_status is None:
                raise ValueError("v4 outcome security-status evidence is incomplete")
            trading = recorded_status == 1
            if bar is None and trading:
                raise ValueError("v4 tradable outcome price is missing")
            if bar is None:
                prior_close = signal_closes[symbol]
                prior_rows = tuple(row for row in rows if row["symbol"] == symbol)
                if prior_rows:
                    prior_close = int(prior_rows[-1]["close_1e4"])
                price_values = {
                    "open_1e4": prior_close,
                    "high_1e4": prior_close,
                    "low_1e4": prior_close,
                    "close_1e4": prior_close,
                    "pre_close_1e4": prior_close,
                }
            else:
                price_values = {
                    "open_1e4": bar.open_1e4,
                    "high_1e4": bar.high_1e4,
                    "low_1e4": bar.low_1e4,
                    "close_1e4": bar.close_1e4,
                    "pre_close_1e4": bar.pre_close_1e4,
                }
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": day,
                    **price_values,
                    "source": "tushare",
                    "market_cap_fen": int(price_values["close_1e4"]) * outstanding // 100,
                    "signal_market_cap_fen": signal_cap,
                    "signal_close_1e4": signal_closes[symbol],
                }
            )
    return rows, statuses


def _load_share_capital_window(
    database: Any, *, symbols: tuple[str, ...], dates: tuple[date, ...]
) -> dict[str, dict[date, int]]:
    if not symbols or not dates or dates != tuple(sorted(set(dates))):
        raise ValueError("v4 share-capital window is invalid")
    placeholders = ",".join("?" for _ in symbols)
    query = (
        "SELECT symbol,effective_date,outstanding_shares FROM share_capital_facts "
        f"WHERE source='sina' AND symbol IN ({placeholders}) AND effective_date<=? "
        "ORDER BY symbol,effective_date"
    )
    facts: dict[str, list[tuple[date, int]]] = {}
    with database.connect() as connection:
        for symbol, effective_date, outstanding in connection.execute(
            query, (*symbols, dates[-1].isoformat())
        ):
            facts.setdefault(str(symbol), []).append(
                (date.fromisoformat(str(effective_date)), int(outstanding))
            )
    result: dict[str, dict[date, int]] = {}
    for symbol in symbols:
        timeline = facts.get(symbol, ())
        position = 0
        current: int | None = None
        resolved: dict[date, int] = {}
        for day in dates:
            while position < len(timeline) and timeline[position][0] <= day:
                current = timeline[position][1]
                position += 1
            if current is not None:
                resolved[day] = current
        result[symbol] = resolved
    return result


def _fraction_bps(value: Fraction, base: Fraction) -> int:
    result = (value / base - 1) * 10_000
    return result.numerator // result.denominator


def _floor_average(values: list[int]) -> int:
    if not values:
        raise ValueError("v4 primary metric is unavailable")
    value = Fraction(sum(values), len(values))
    return value.numerator // value.denominator


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class V4ResearchCoordinator:
    """Coordinate durable v4 study work without knowing its storage schema.

    ``step_executor`` returns ``{"step": mapping, "complete": bool}`` for
    each claimed study.  A completed result additionally supplies ``report``.
    Repository operations are deliberately narrow so the storage adapter owns
    all transaction and schema decisions.
    """

    def __init__(
        self,
        database: V4ResearchRepository | Any,
        *,
        step_executor: V4StudyStepExecutor | None = None,
        allowed: V4ResearchAllowed | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._step_executor = step_executor
        self._allowed = allowed or (lambda _now: False)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._thread_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_background_error: str | None = None

    @property
    def last_background_error(self) -> str | None:
        """Expose the latest supervised worker fault to diagnostics."""

        return self._last_background_error

    def start_v4_research(self, *, manifest_hash: str, idempotency_key: str) -> dict[str, object]:
        self._ensure_execution_ready()
        manifest = self._database.get_v4_dataset_manifest(manifest_hash)
        _validate_study_manifest(manifest, manifest_hash)
        arms = build_v4_research_arms(created_at=datetime.now().astimezone().isoformat())
        study = self._database.create_v4_study_run(
            manifest_hash=manifest_hash, idempotency_key=idempotency_key, arms=arms
        )
        self.start_background()
        self._wake.set()
        return study

    def requeue_interrupted(self) -> int:
        """Recover queued/running study work through the repository adapter."""

        self._ensure_execution_ready()
        return self._database.requeue_interrupted_v4_studies()

    def start_background(self) -> None:
        """Start one daemon worker after recovering interrupted work."""

        self._ensure_execution_ready()
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._database.requeue_interrupted_v4_studies()
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="stock-mcp-v4-research",
                daemon=True,
            )
            self._thread.start()

    def stop_background(self) -> None:
        """Request a clean worker stop; intended for runtime shutdown and tests."""

        self._stop.set()
        self._wake.set()
        with self._thread_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)

    def run_next_step(self) -> bool:
        """Claim and persist at most one v4 study step.

        The coordinator intentionally delegates all schedule boundaries to
        ``allowed(clock())``; it contains no hard-coded market-time window.
        """

        self._ensure_execution_ready()
        if not self._allowed(self._clock()):
            return False
        study = self._database.claim_next_v4_study()
        if study is None:
            return False
        study_id = str(study.get("study_id", ""))
        if not study_id:
            raise ValueError("claimed v4 study is missing study_id")
        try:
            result = self._step_executor(study)
            step = result.get("step")
            steps = result.get("steps")
            complete = result.get("complete")
            has_step = isinstance(step, Mapping)
            has_steps = (
                isinstance(steps, (tuple, list))
                and bool(steps)
                and all(isinstance(item, Mapping) for item in steps)
            )
            if has_step == has_steps or not isinstance(complete, bool):
                raise ValueError("v4 study step result is invalid")
            if has_steps:
                batch = tuple(dict(item) for item in steps)
                save_batch = getattr(self._database, "save_v4_study_steps", None)
                if callable(save_batch):
                    save_batch(study_id=study_id, steps=batch)
                else:
                    for item in batch:
                        self._database.save_v4_study_step(study_id=study_id, step=item)
            else:
                assert isinstance(step, Mapping)
                self._database.save_v4_study_step(study_id=study_id, step=dict(step))
            if complete:
                statistics = result.get("statistics")
                save_statistics = getattr(self._database, "save_v4_study_statistics", None)
                if isinstance(statistics, Mapping) and callable(save_statistics):
                    save_statistics(study_id=study_id, statistics=dict(statistics))
                report = result.get("report")
                if not isinstance(report, Mapping):
                    raise ValueError("completed v4 study requires a report")
                self._database.complete_v4_study(study_id=study_id, report=dict(report))
        except (sqlite3.OperationalError, TimeoutError, OSError):
            self._database.requeue_interrupted_v4_studies()
            raise
        except Exception as error:
            message = str(error).strip()
            summary = f"v4 research step failed ({type(error).__name__})"
            if message:
                summary = f"{summary}: {message}"[:512]
            self._database.fail_v4_study(
                study_id=study_id,
                error=summary,
            )
        return True

    def _run_loop(self) -> None:
        storage_failures = 0
        while not self._stop.is_set():
            try:
                if self.run_next_step():
                    storage_failures = 0
                    self._last_background_error = None
                    continue
                storage_failures = 0
                self._last_background_error = None
            except (sqlite3.OperationalError, TimeoutError, OSError) as error:
                # Transient storage/OS faults must not silently terminate the
                # daemon. Bounded exponential backoff prevents a persistent
                # read-only/full/locked database from turning into a hot loop.
                storage_failures += 1
                self._last_background_error = type(error).__name__
            delay = min(5.0, 0.25 * (2 ** min(storage_failures, 5)))
            self._wake.wait(timeout=delay)
            self._wake.clear()

    def _ensure_execution_ready(self) -> None:
        if self._step_executor is None:
            raise ValueError("v4 research execution is unavailable without a step executor")
        required = (
            "claim_next_v4_study",
            "save_v4_study_step",
            "complete_v4_study",
            "fail_v4_study",
            "requeue_interrupted_v4_studies",
        )
        missing = tuple(
            name for name in required if not callable(getattr(self._database, name, None))
        )
        if missing:
            raise TypeError("v4 research repository is missing: " + ", ".join(missing))

    def get_v4_research(self, *, study_id: str) -> dict[str, object] | None:
        return self._database.get_v4_study_run(study_id)

    def get_v4_research_arms(self, *, study_id: str) -> tuple[dict[str, object], ...]:
        return self._database.list_v4_study_arms(study_id)

    def get_v4_research_days(
        self, *, study_id: str, arm_id: str, after_signal_date: date | None, limit: int
    ) -> tuple[dict[str, object], ...]:
        return self._database.list_v4_study_days(
            study_id=study_id,
            arm_id=arm_id,
            after_signal_date=after_signal_date,
            limit=limit,
        )

    def get_v4_research_report(self, *, study_id: str) -> dict[str, object] | None:
        run = self._database.get_v4_study_run(study_id)
        if run is None or run.get("report") is None:
            return None
        return dict(run["report"])
