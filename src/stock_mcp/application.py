"""Application service behind the public MCP tools.

The transport adapter deliberately knows nothing about persistence models or
market-data providers.  This module is the narrow, deterministic boundary
between them: expected business conditions become structured results while
programming errors remain visible to the service operator.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from .domain import Candidate, DailyReview, Evidence, StrategyVersion
from .research_program import build_forward_research_report
from .strategy import validate_strategy_parameters

Result = dict[str, Any]
_THRESHOLD = re.compile(
    r"^\s*close\s*(>=|>)\s*(\d+)\s*$|^\s*close\s*(<|<=)\s*(\d+)\s*$", re.IGNORECASE
)


def _ok(data: Mapping[str, Any]) -> Result:
    return {"ok": True, "data": dict(data)}


def _error(code: str, message: str) -> Result:
    return {"ok": False, "error": {"code": code, "message": message}}


def _evidence(evidence: Evidence) -> dict[str, Any]:
    return {
        "metric": evidence.metric,
        "value": evidence.value,
        "threshold": evidence.threshold,
        "passed": evidence.passed,
        "score_contribution": evidence.score_contribution,
    }


def _candidate(candidate: Candidate, *, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    data = {
        "candidate_id": candidate.candidate_id,
        "symbol": candidate.symbol,
        "name": candidate.name,
        "rank": candidate.rank,
        "score": candidate.score,
        "setup_type": str(candidate.setup_type),
        "strategy_version": candidate.strategy_version,
        "evidence": [_evidence(item) for item in candidate.evidence],
        "confirmation_condition": candidate.confirmation_condition,
        "invalidation_condition": candidate.invalidation_condition,
    }
    if context is not None:
        review = context.get("review")
        if isinstance(review, DailyReview):
            data.update(
                {
                    "source": review.source,
                    "source_timestamp": review.source_timestamp,
                    "market_regime": str(review.market_regime),
                }
            )
        industry_context = context.get("industry_context")
        if isinstance(industry_context, Mapping):
            data["industry_context"] = dict(industry_context)
    return data


def _review(
    review: DailyReview,
    *,
    notes: tuple[Mapping[str, Any], ...] = (),
    candidate_contexts: Mapping[str, Mapping[str, Any]] | None = None,
    pipeline_version: str | None = None,
) -> dict[str, Any]:
    return {
        "status": review.status,
        "trade_date": review.trade_date.isoformat(),
        "source": review.source,
        "source_timestamp": review.source_timestamp,
        "strategy_version": review.strategy_version,
        "market_regime": str(review.market_regime),
        "pipeline_version": pipeline_version,
        "candidates": [
            _candidate(
                candidate,
                context=None
                if candidate_contexts is None
                else candidate_contexts.get(candidate.candidate_id),
            )
            for candidate in review.candidates
        ],
        "notes": [dict(note) for note in notes],
    }


def _strategy(strategy: StrategyVersion) -> dict[str, Any]:
    result = {
        "version": strategy.version,
        "status": strategy.status,
        "parameters": dict(strategy.parameters),
    }
    lifecycle = getattr(strategy, "lifecycle", None)
    superseded_by = getattr(strategy, "superseded_by", None)
    if lifecycle is not None:
        result["lifecycle"] = lifecycle
    if superseded_by is not None:
        result["superseded_by"] = superseded_by
    return result


def _condition_threshold(condition: str, *, expected: str) -> tuple[str, int] | None:
    """Return a fixed close operator/threshold, rejecting a malformed rule."""

    match = _THRESHOLD.fullmatch(condition)
    if match is None:
        return None
    operator = match.group(1) or match.group(3)
    value = match.group(2) or match.group(4)
    if operator is None or value is None:
        return None
    if expected == "confirmation" and operator not in {">=", ">"}:
        return None
    if expected == "invalidation" and operator not in {"<", "<="}:
        return None
    return operator, int(value)


def _compare(value: int, operator: str, threshold: int) -> bool:
    return {
        ">": value > threshold,
        ">=": value >= threshold,
        "<": value < threshold,
        "<=": value <= threshold,
    }[operator]


class StockMcpApplication:
    """Use-case service for the fixed, non-trading MCP tool set.

    ``repository``, ``quote_provider``, ``strategy_registry`` and ``replay``
    are duck-typed deliberately.  The production SQLite repository and the
    contract-test fakes share only the operations used in each method.
    """

    def __init__(
        self,
        repository: Any,
        quote_provider: Any,
        strategy_registry: Any,
        *,
        replay: Any | None = None,
        v4_research: Any | None = None,
    ) -> None:
        self._repository = repository
        self._quote_provider = quote_provider
        self._strategy_registry = strategy_registry
        self._replay = replay
        self._v4_research = v4_research
        self._strategy_write_results: dict[tuple[str, str], Result] = {}
        self._strategy_write_requests: dict[tuple[str, str], Mapping[str, Any]] = {}
        # Repository sets deliberately do not prescribe presentation order.
        # Preserve the user's explicit order in this process while the durable
        # repository remains responsible for membership and idempotency.
        self._watchlist_order: dict[str, list[str]] = {}

    def list_research_hypotheses(
        self, *, family: str | None = None, status: str | None = None
    ) -> Result:
        hypotheses = self._repository.list_research_hypotheses(family=family, status=status)
        trials = self._repository.list_research_trials()
        return _ok(
            {
                "hypotheses": [dict(item) for item in hypotheses],
                "lifetime_trial_count": len(trials),
            }
        )

    def get_research_hypothesis(self, *, hypothesis_id: str) -> Result:
        hypothesis = self._repository.get_research_hypothesis(hypothesis_id)
        if hypothesis is None:
            return _error("research_hypothesis_not_found", "research hypothesis does not exist")
        trials = self._repository.list_research_trials(hypothesis_id=hypothesis_id)
        observations = self._repository.list_research_forward_observations(
            hypothesis_id=hypothesis_id
        )
        outcomes = self._repository.list_research_forward_outcomes(hypothesis_id=hypothesis_id)
        return _ok(
            {
                "hypothesis": dict(hypothesis),
                "trials": [dict(item) for item in trials],
                "forward_observations": [dict(item) for item in observations],
                "forward_outcomes": [dict(item) for item in outcomes],
            }
        )

    def get_research_forward_report(
        self, *, hypothesis_id: str, horizon_sessions: int = 20
    ) -> Result:
        hypothesis = self._repository.get_research_hypothesis(hypothesis_id)
        if hypothesis is None:
            return _error("research_hypothesis_not_found", "research hypothesis does not exist")
        observations = self._repository.list_research_forward_observations(
            hypothesis_id=hypothesis_id
        )
        outcomes = self._repository.list_research_forward_outcomes(hypothesis_id=hypothesis_id)
        try:
            report = build_forward_research_report(
                hypothesis=hypothesis,
                observations=observations,
                outcomes=outcomes,
                horizon_sessions=horizon_sessions,
                as_of=datetime.now(UTC),
            )
        except ValueError as error:
            return _error("research_forward_report_invalid", str(error))
        return _ok(report)

    def start_v4_research(self, *, manifest_hash: str, idempotency_key: str) -> Result:
        coordinator = getattr(self, "_v4_research", None)
        if coordinator is None:
            return _error("v4_research_unavailable", "v4 research is unavailable")
        try:
            run = coordinator.start_v4_research(
                manifest_hash=manifest_hash, idempotency_key=idempotency_key
            )
        except ValueError as error:
            return _error("v4_research_rejected", str(error))
        return _ok(dict(run))

    def get_v4_research(self, *, study_id: str) -> Result:
        coordinator = getattr(self, "_v4_research", None)
        if coordinator is None:
            return _error("v4_research_unavailable", "v4 research is unavailable")
        run = coordinator.get_v4_research(study_id=study_id)
        return (
            _error("v4_research_not_found", "v4 research does not exist")
            if run is None
            else _ok(dict(run))
        )

    def get_v4_research_arms(self, *, study_id: str) -> Result:
        coordinator = getattr(self, "_v4_research", None)
        if coordinator is None:
            return _error("v4_research_unavailable", "v4 research is unavailable")
        return _ok(
            {
                "study_id": study_id,
                "arms": [
                    dict(item) for item in coordinator.get_v4_research_arms(study_id=study_id)
                ],
            }
        )

    def get_v4_research_days(
        self,
        *,
        study_id: str,
        arm_id: str,
        after_signal_date: date | None = None,
        limit: int = 20,
    ) -> Result:
        coordinator = getattr(self, "_v4_research", None)
        if coordinator is None:
            return _error("v4_research_unavailable", "v4 research is unavailable")
        days = coordinator.get_v4_research_days(
            study_id=study_id,
            arm_id=arm_id,
            after_signal_date=after_signal_date,
            limit=limit,
        )
        return _ok({"study_id": study_id, "arm_id": arm_id, "days": [dict(item) for item in days]})

    def get_v4_research_report(self, *, study_id: str) -> Result:
        coordinator = getattr(self, "_v4_research", None)
        if coordinator is None:
            return _error("v4_research_unavailable", "v4 research is unavailable")
        report = coordinator.get_v4_research_report(study_id=study_id)
        if report is not None:
            return _ok(dict(report))
        run = coordinator.get_v4_research(study_id=study_id)
        if run is None:
            return _error("v4_research_not_found", "v4 research does not exist")
        return _error(
            "v4_research_not_ready",
            f"v4 research report is not ready (status={run.get('status', 'unknown')})",
        )

    def get_v4_research_diagnostics(self, *, study_id: str) -> Result:
        coordinator = getattr(self, "_v4_research", None)
        if coordinator is None:
            return _error("v4_research_unavailable", "v4 research is unavailable")
        run = coordinator.get_v4_research(study_id=study_id)
        if run is None:
            return _error("v4_research_not_found", "v4 research does not exist")
        if run.get("status") != "completed":
            return _error(
                "v4_research_not_ready",
                f"v4 research diagnostics are not ready (status={run.get('status', 'unknown')})",
            )
        from .v4_research import derive_v4_study_diagnostics

        try:
            diagnostic = derive_v4_study_diagnostics(
                self._repository,
                source_study_id=study_id,
            )
        except ValueError as error:
            return _error("v4_research_diagnostic_rejected", str(error))
        return _ok(diagnostic)

    def get_provider_qualification(self, *, source: str) -> Result:
        qualification = self._repository.get_provider_qualification(source)
        if qualification is None:
            return _error(
                "provider_qualification_not_found", "provider qualification does not exist"
            )
        return _ok(dict(qualification))

    def activate_provider_source(
        self,
        *,
        source: str,
        qualification_id: str,
        capabilities: list[str],
        confirmed: bool,
        idempotency_key: str,
    ) -> Result:
        if not confirmed:
            return _error("confirmation_required", "explicit confirmation is required")
        if tuple(sorted(set(capabilities))) != ("backup_price", "enrichment"):
            return _error(
                "provider_activation_rejected", "both frozen provider capabilities are required"
            )
        activate = getattr(self._repository, "activate_provider_source", None)
        if not callable(activate):
            return _error("provider_activation_unavailable", "provider activation is unavailable")
        try:
            result = activate(
                source=source,
                qualification_id=qualification_id,
                capabilities=tuple(capabilities),
                idempotency_key=idempotency_key,
            )
        except ValueError as error:
            return _error("provider_activation_rejected", str(error))
        return _ok(dict(result))

    def get_daily_review(self, *, trade_date: date) -> Result:
        get_status = getattr(self._repository, "get_publication_status", None)
        publication = get_status(trade_date) if callable(get_status) else None
        if publication is not None and publication.get("status") != "ready":
            data = dict(publication)
            recorded_date = data.get("trade_date")
            if isinstance(recorded_date, date):
                data["trade_date"] = recorded_date.isoformat()
            return _ok(data)
        review = self._repository.get_daily_review(trade_date)
        if review is None or review.status not in {"published", "ready"}:
            if publication is not None:
                data = dict(publication)
                recorded_date = data.get("trade_date")
                if isinstance(recorded_date, date):
                    data["trade_date"] = recorded_date.isoformat()
                return _ok(data)
            return _error("daily_review_not_found", "no published review")
        notes = self._repository.list_review_notes(trade_date)
        return _ok(self._review_with_context(review, notes=notes))

    def get_candidate(self, *, candidate_id: str) -> Result:
        candidate = self._repository.get_candidate(candidate_id)
        if candidate is None:
            return _error("candidate_not_found", "candidate does not exist")
        get_context = getattr(self._repository, "get_candidate_context", None)
        context = get_context(candidate_id) if callable(get_context) else None
        return _ok(_candidate(candidate, context=context))

    def check_next_day(self, *, candidate_id: str) -> Result:
        """Fetch exactly one current quote; reads never call this provider."""

        candidate = self._repository.get_candidate(candidate_id)
        if candidate is None:
            return _error("candidate_not_found", "candidate does not exist")

        confirmation = _condition_threshold(
            candidate.confirmation_condition, expected="confirmation"
        )
        invalidation = _condition_threshold(
            candidate.invalidation_condition, expected="invalidation"
        )
        if confirmation is None or invalidation is None:
            return _error(
                "candidate_conditions_invalid",
                "candidate conditions are not fixed close thresholds",
            )

        try:
            quote = self._quote_provider.fetch_quote(candidate.symbol)
            close = quote["close_1e4"]
            source = quote["source"]
            as_of = quote["as_of"]
        except (AttributeError, KeyError, TypeError, RuntimeError, ValueError):
            return _error("next_day_quote_unavailable", "current quote is unavailable")
        if (
            not isinstance(close, int)
            or isinstance(close, bool)
            or not isinstance(source, str)
            or not source
        ):
            return _error("next_day_quote_unavailable", "current quote is unavailable")

        confirmation_operator, confirmation_threshold = confirmation
        invalidation_operator, invalidation_threshold = invalidation
        if _compare(close, confirmation_operator, confirmation_threshold):
            status = "confirmed"
        elif _compare(close, invalidation_operator, invalidation_threshold):
            status = "invalidated"
        else:
            status = "pending"
        return _ok(
            {
                "candidate_id": candidate.candidate_id,
                "symbol": candidate.symbol,
                "close_1e4": close,
                "source": source,
                "as_of": as_of,
                "status": status,
            }
        )

    def list_watchlists(self) -> Result:
        return _ok({"names": list(self._repository.list_watchlists())})

    def get_watchlist(self, *, name: str) -> Result:
        symbols = self._repository.get_watchlist(name)
        if symbols is None:
            return _error("watchlist_not_found", "watchlist does not exist")
        ordered = self._watchlist_order.setdefault(name, list(symbols))
        return _ok({"name": name, "symbols": list(ordered)})

    def create_watchlist(
        self,
        *,
        name: str,
        idempotency_key: str,
        description: str | None = None,
    ) -> Result:
        symbols = self._repository.create_watchlist(name=name, idempotency_key=idempotency_key)
        self._watchlist_order.setdefault(name, list(symbols))
        return _ok({"name": name, "symbols": list(self._watchlist_order[name])})

    def add_watchlist_items(
        self, *, name: str, symbols: tuple[str, ...], idempotency_key: str
    ) -> Result:
        persisted = self._repository.add_watchlist_items(
            name=name, symbols=tuple(symbols), idempotency_key=idempotency_key
        )
        if persisted is None:
            return _error("watchlist_not_found", "watchlist does not exist")
        ordered = self._watchlist_order.get(name)
        if ordered is None:
            # A set-backed repository may sort its result.  For a newly
            # touched list, the requested order is the only user intent we
            # have; retain it and then include any older members.
            ordered = list(dict.fromkeys(symbols))
            ordered.extend(symbol for symbol in persisted if symbol not in ordered)
            self._watchlist_order[name] = ordered
        for symbol in symbols:
            if symbol not in ordered:
                ordered.append(symbol)
        result = _ok({"name": name, "symbols": list(ordered)})
        return result

    def remove_watchlist_items(
        self, *, name: str, symbols: tuple[str, ...], idempotency_key: str
    ) -> Result:
        persisted = self._repository.remove_watchlist_items(
            name=name, symbols=tuple(symbols), idempotency_key=idempotency_key
        )
        if persisted is None:
            return _error("watchlist_not_found", "watchlist does not exist")
        ordered = self._watchlist_order.setdefault(name, list(persisted))
        removed = set(symbols)
        ordered[:] = [symbol for symbol in ordered if symbol not in removed]
        result = _ok({"name": name, "symbols": list(ordered)})
        return result

    def record_candidate_event(
        self,
        *,
        candidate_id: str,
        status: str,
        event_date: date,
        price_1e4: int | None,
        reason: str,
        idempotency_key: str,
    ) -> Result:
        event = self._repository.record_candidate_event(
            candidate_id=candidate_id,
            status=status,
            event_date=event_date,
            price_1e4=price_1e4,
            reason=reason,
            idempotency_key=idempotency_key,
        )
        if event is None:
            return _error("candidate_not_found", "candidate does not exist")
        return _ok(dict(event))

    def record_review_note(self, *, trade_date: date, note: str, idempotency_key: str) -> Result:
        recorded = self._repository.record_review_note(
            trade_date=trade_date, note=note, idempotency_key=idempotency_key
        )
        if recorded is None:
            return _error("daily_review_not_found", "no published review")
        return _ok(dict(recorded))

    def get_review_history(self, *, candidate_id: str | None = None, limit: int = 50) -> Result:
        reviews = self._repository.list_review_history()
        if candidate_id is not None:
            reviews = tuple(
                review
                for review in reviews
                if any(candidate.candidate_id == candidate_id for candidate in review.candidates)
            )
        reviews = reviews[:limit]
        events: tuple[object, ...] = ()
        if candidate_id is not None:
            list_events = getattr(self._repository, "list_candidate_review_events", None)
            if not callable(list_events):
                list_events = getattr(self._repository, "list_candidate_events", None)
            if callable(list_events):
                events = tuple(list_events(candidate_id))
        return _ok(
            {
                "reviews": [
                    self._review_with_context(
                        review,
                        notes=self._repository.list_review_notes(review.trade_date),
                    )
                    for review in reviews
                    if review.status == "published"
                ],
                "events": [dict(event) for event in events if isinstance(event, Mapping)],
            }
        )

    def _review_with_context(
        self,
        review: DailyReview,
        *,
        notes: tuple[Mapping[str, Any], ...] = (),
    ) -> dict[str, Any]:
        get_context = getattr(self._repository, "get_candidate_context", None)
        contexts: dict[str, Mapping[str, Any]] = {}
        if callable(get_context):
            for candidate in review.candidates:
                context = get_context(candidate.candidate_id)
                if isinstance(context, Mapping):
                    contexts[candidate.candidate_id] = context
        pipeline_version = None
        get_status = getattr(self._repository, "get_publication_status", None)
        if callable(get_status):
            publication = get_status(review.trade_date)
            if isinstance(publication, Mapping):
                value = publication.get("pipeline_version")
                if isinstance(value, str):
                    pipeline_version = value
        return _review(
            review,
            notes=notes,
            candidate_contexts=contexts,
            pipeline_version=pipeline_version,
        )

    def list_strategy_versions(self) -> Result:
        versions = self._strategy_registry.list_versions()
        return _ok({"versions": [_strategy(version) for version in versions]})

    def compare_strategy_versions(
        self, *, left_version: str, right_version: str, start: date, end: date
    ) -> Result:
        if left_version == right_version:
            return _error(
                "strategy_comparison_invalid",
                "strategy comparison requires distinct strategy versions",
            )
        left_strategy = self._get_strategy(left_version)
        right_strategy = self._get_strategy(right_version)
        if left_strategy is None:
            return _error("strategy_version_not_found", "left strategy version does not exist")
        if right_strategy is None:
            return _error("strategy_version_not_found", "right strategy version does not exist")
        if self._replay is None:
            return _error("replay_unavailable", "strategy replay is unavailable")
        try:
            persisted_compare = getattr(self._replay, "compare_completed_replays", None)
            if callable(persisted_compare) and (
                left_version.startswith("v3")
                or left_strategy.parameters.get("rule_engine_version") == 3
                or right_version.startswith("v3")
                or right_strategy.parameters.get("rule_engine_version") == 3
            ):
                comparison = persisted_compare(left_version, right_version, start, end)
            else:
                comparison = self._replay.compare(left_version, right_version, start, end)
        except ValueError as error:
            return _error("strategy_comparison_invalid", str(error))
        return _ok(dict(comparison))

    def start_strategy_replay(
        self,
        *,
        version: str,
        start_date: date,
        end_date: date,
        idempotency_key: str,
    ) -> Result:
        strategy = self._get_strategy(version)
        if strategy is None:
            return _error("strategy_version_not_found", "strategy version does not exist")
        if strategy.status != "proposed":
            return _error(
                "strategy_replay_rejected",
                "only a proposed strategy version can start a governance replay",
            )
        if self._replay is None:
            return _error("replay_unavailable", "strategy replay is unavailable")
        try:
            replay = self._replay.start_strategy_replay(
                version=version,
                start_date=start_date,
                end_date=end_date,
                idempotency_key=idempotency_key,
            )
        except ValueError as error:
            code = (
                "idempotency_conflict"
                if "idempotency" in str(error).lower()
                else "strategy_replay_rejected"
            )
            result = _error(code, str(error))
        else:
            result = _ok(dict(replay))
        return result

    def get_strategy_replay(self, *, replay_id: str) -> Result:
        if self._replay is None:
            return _error("replay_unavailable", "strategy replay is unavailable")
        replay = self._replay.get_strategy_replay(replay_id=replay_id)
        if replay is None:
            return _error("strategy_replay_not_found", "strategy replay does not exist")
        return _ok(dict(replay))

    def list_strategy_replays(self, *, version: str | None = None, limit: int = 20) -> Result:
        if self._replay is None:
            return _error("replay_unavailable", "strategy replay is unavailable")
        replays = self._replay.list_strategy_replays(version=version, limit=limit)
        return _ok({"replays": [dict(replay) for replay in replays]})

    def get_strategy_replay_days(
        self,
        *,
        replay_id: str,
        after_trade_date: date | None = None,
        limit: int = 20,
    ) -> Result:
        if self._replay is None:
            return _error("replay_unavailable", "strategy replay is unavailable")
        days = self._replay.get_strategy_replay_days(
            replay_id=replay_id,
            after_trade_date=after_trade_date,
            limit=limit,
        )
        if days is None:
            return _error("strategy_replay_not_found", "strategy replay does not exist")
        return _ok({"replay_id": replay_id, "days": [dict(day) for day in days]})

    def certify_strategy_replay(
        self, *, replay_id: str, confirmed: bool, idempotency_key: str
    ) -> Result:
        if not confirmed:
            return _error("confirmation_required", "explicit confirmation is required")
        if self._replay is None:
            return _error("replay_unavailable", "strategy replay is unavailable")
        try:
            replay = self._replay.certify_strategy_replay(
                replay_id=replay_id,
                confirmed=True,
                idempotency_key=idempotency_key,
            )
        except ValueError as error:
            code = (
                "idempotency_conflict"
                if "idempotency" in str(error).lower()
                else "strategy_replay_certification_rejected"
            )
            result = _error(code, str(error))
        else:
            result = (
                _error("strategy_replay_not_found", "strategy replay does not exist")
                if replay is None
                else _ok(dict(replay))
            )
        return result

    def create_strategy_proposal(
        self,
        *,
        version: str,
        parameters: Mapping[str, Any],
        idempotency_key: str,
        rationale: str | None = None,
        supersedes_version: str | None = None,
    ) -> Result:
        operation = "create_strategy_proposal"
        request = {
            "version": version,
            "parameters": dict(parameters),
            "rationale": rationale,
            "supersedes_version": supersedes_version,
        }
        persistent = self._load_persistent_write(operation, idempotency_key, request)
        if persistent is not None:
            return persistent
        cache_key = (operation, idempotency_key)
        if cache_key in self._strategy_write_results:
            if self._strategy_write_requests[cache_key] != request:
                return _error(
                    "idempotency_conflict",
                    "idempotency key cannot be reused for a different request",
                )
            return self._strategy_write_results[cache_key]
        try:
            validated = validate_strategy_parameters(parameters)
            if version in {"v0.3-policy-1", "v0.3-policy-2"}:
                from .v3 import v3_proposal_parameters

                expected_policy = 1 if version.endswith("-1") else 2
                if dict(validated) != v3_proposal_parameters(expected_policy):
                    raise ValueError(f"{version} is reserved for the frozen v3 policy template")
            strategy = StrategyVersion(version=version, status="proposed", parameters=validated)
            if supersedes_version is not None:
                if self._get_strategy(supersedes_version) is None:
                    raise ValueError("superseded strategy version does not exist")
                atomic_propose = getattr(self._strategy_registry, "propose_with_relation", None)
                if callable(atomic_propose):
                    persisted = atomic_propose(strategy, supersedes_version=supersedes_version)
                else:
                    relation_writer = getattr(
                        self._repository, "save_strategy_version_relation", None
                    )
                    if not callable(relation_writer):
                        raise ValueError("strategy version relations are unavailable")
                    persisted = self._strategy_registry.propose(strategy)
                    relation_writer(
                        predecessor=supersedes_version,
                        successor=version,
                        relation="supersedes",
                    )
            else:
                persisted = self._strategy_registry.propose(strategy)
        except ValueError as error:
            result = _error("strategy_proposal_rejected", str(error))
        else:
            result = _ok(_strategy(persisted))
        result = self._save_persistent_write(operation, idempotency_key, request, result)
        self._strategy_write_requests[cache_key] = request
        self._strategy_write_results[cache_key] = result
        return result

    def activate_strategy_version(
        self, *, version: str, confirmed: bool, idempotency_key: str
    ) -> Result:
        if not confirmed:
            return _error("confirmation_required", "explicit confirmation is required")
        operation = "activate_strategy_version"
        request = {"version": version, "confirmed": confirmed}
        persistent = self._load_persistent_write(operation, idempotency_key, request)
        if persistent is not None:
            return persistent
        cache_key = (operation, idempotency_key)
        if cache_key in self._strategy_write_results:
            if self._strategy_write_requests[cache_key] != request:
                return _error(
                    "idempotency_conflict",
                    "idempotency key cannot be reused for a different request",
                )
            return self._strategy_write_results[cache_key]
        if self._get_strategy(version) is None:
            return _error("strategy_version_not_found", "strategy version does not exist")
        try:
            active = self._strategy_registry.activate(version, confirmed=True)
        except ValueError as error:
            result = _error("strategy_activation_rejected", str(error))
        else:
            result = _ok(_strategy(active))
        result = self._save_persistent_write(operation, idempotency_key, request, result)
        self._strategy_write_requests[cache_key] = request
        self._strategy_write_results[cache_key] = result
        return result

    def _load_persistent_write(
        self, operation: str, key: str, request: Mapping[str, Any]
    ) -> Result | None:
        loader = getattr(self._repository, "load_idempotent_write", None)
        if not callable(loader):
            return None
        try:
            value = loader("strategy:" + operation, key, request)
        except ValueError as error:
            return _error("idempotency_conflict", str(error))
        return None if value is None else dict(value)

    def _save_persistent_write(
        self,
        operation: str,
        key: str,
        request: Mapping[str, Any],
        result: Result,
    ) -> Result:
        saver = getattr(self._repository, "save_idempotent_write", None)
        if not callable(saver):
            return result
        try:
            return dict(saver("strategy:" + operation, key, request, result))
        except ValueError as error:
            return _error("idempotency_conflict", str(error))

    def _get_strategy(self, version: str) -> StrategyVersion | None:
        try:
            return self._strategy_registry.get(version)
        except KeyError:
            return None
