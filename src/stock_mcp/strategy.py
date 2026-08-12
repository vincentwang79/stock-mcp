from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from types import MappingProxyType
from typing import Any

from .domain import StrategyVersion

_PARAMETER_RANGES: dict[str, tuple[int, int]] = {
    "rule_engine_version": (1, 2),
    "offensive_min_bps": (0, 10_000),
    "defensive_max_bps": (0, 10_000),
    "neutral_limit": (0, 50),
    "offensive_limit": (0, 50),
    "min_liquidity_amount_fen": (0, 10**16),
    "max_consecutive_limit_up_days": (0, 20),
    "strong_pullback_min_prior_gain_bps": (0, 20_000),
    "strong_pullback_max_pullback_bps": (0, 10_000),
    "volume_breakout_min_volume_ratio_bps": (10_000, 100_000),
}
_REQUIRED_PARAMETERS = frozenset(_PARAMETER_RANGES)


def canonical_strategy_parameters_hash(parameters: Mapping[str, Any]) -> str:
    """Bind approvals and replay evidence to one canonical immutable rule map."""

    validated = validate_strategy_parameters(parameters, require_complete=True)
    encoded = json.dumps(validated, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_strategy_parameters(
    parameters: Mapping[str, Any], *, require_complete: bool = False
) -> dict[str, int]:
    """Return a plain validated strategy map with a closed set of integer keys."""

    if parameters.get("rule_engine_version") == 3:
        from .v3 import validate_v3_parameters

        return validate_v3_parameters(parameters, require_complete=require_complete)

    unknown = sorted(set(parameters) - set(_PARAMETER_RANGES))
    if unknown:
        raise ValueError("unsupported strategy parameters: " + ", ".join(unknown))
    if require_complete:
        missing = sorted(_REQUIRED_PARAMETERS - set(parameters))
        if missing:
            raise ValueError("missing strategy parameters: " + ", ".join(missing))
    validated: dict[str, int] = {}
    for name, value in parameters.items():
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"strategy parameter {name} must be an integer")
        minimum, maximum = _PARAMETER_RANGES[name]
        if not minimum <= value <= maximum:
            raise ValueError(f"strategy parameter {name} is out of range")
        validated[name] = value
    if require_complete and validated["defensive_max_bps"] >= validated["offensive_min_bps"]:
        raise ValueError("defensive_max_bps must be lower than offensive_min_bps")
    return validated


class StrategyRegistry:
    """In-memory immutable strategy proposal and activation registry."""

    def __init__(self) -> None:
        self._versions: dict[str, StrategyVersion] = {}
        self._active_version: str | None = None

    @property
    def active_version(self) -> str | None:
        return self._active_version

    def propose(self, strategy: StrategyVersion) -> StrategyVersion:
        if strategy.status != "proposed":
            raise ValueError("only proposed strategy versions can be registered")
        if strategy.version in self._versions:
            raise ValueError(f"strategy version already exists: {strategy.version}")
        parameters = validate_strategy_parameters(strategy.parameters)
        stored = replace(strategy, parameters=_freeze_mapping(parameters))
        self._versions[stored.version] = stored
        return stored

    def activate(self, version: str, *, confirmed: bool) -> StrategyVersion:
        if not confirmed:
            raise ValueError("strategy activation requires explicit confirmation")
        proposal = self.get(version)
        validate_strategy_parameters(proposal.parameters, require_complete=True)
        if proposal.status != "proposed":
            raise ValueError("only a proposed strategy version can be activated")
        active = replace(proposal, status="active")
        self._versions[version] = active
        self._active_version = version
        return active

    def get(self, version: str) -> StrategyVersion:
        try:
            return self._versions[version]
        except KeyError as error:
            raise KeyError(f"unknown strategy version: {version}") from error

    def list_versions(self) -> tuple[StrategyVersion, ...]:
        return tuple(self._versions[version] for version in sorted(self._versions))


class DatabaseStrategyRegistry:
    """Durable registry that activates an immutable proposal through a pointer."""

    def __init__(self, database: Any) -> None:
        self._database = database

    @property
    def active_version(self) -> str | None:
        active = self._database.get_active_strategy_version()
        return None if active is None else active.version

    def propose(self, strategy: StrategyVersion) -> StrategyVersion:
        if strategy.status != "proposed":
            raise ValueError("only proposed strategy versions can be registered")
        parameters = validate_strategy_parameters(strategy.parameters)
        stored = replace(strategy, parameters=deepcopy(parameters))
        self._database.save_strategy_version(stored)
        return self.get(stored.version)

    def propose_with_relation(
        self, strategy: StrategyVersion, *, supersedes_version: str
    ) -> StrategyVersion:
        if strategy.status != "proposed":
            raise ValueError("only proposed strategy versions can be registered")
        parameters = validate_strategy_parameters(strategy.parameters)
        stored = replace(strategy, parameters=deepcopy(parameters))
        writer = getattr(self._database, "save_strategy_proposal_with_relation", None)
        if not callable(writer):
            raise ValueError("atomic strategy version relations are unavailable")
        writer(stored, predecessor=supersedes_version)
        return self.get(stored.version)

    def activate(self, version: str, *, confirmed: bool) -> StrategyVersion:
        if not confirmed:
            raise ValueError("strategy activation requires explicit confirmation")
        proposal = self._database.load_strategy_version(version)
        if proposal is None:
            raise KeyError(f"unknown strategy version: {version}")
        if proposal.status != "proposed":
            raise ValueError("only a proposed strategy version can be activated")
        validate_strategy_parameters(proposal.parameters, require_complete=True)
        if self.active_version == version:
            return replace(
                proposal,
                status="active",
                parameters=_freeze_mapping(proposal.parameters),
            )
        parameters_hash = canonical_strategy_parameters_hash(proposal.parameters)
        atomic_activate = getattr(self._database, "activate_strategy_version_with_grants", None)
        if callable(atomic_activate):
            authorization = atomic_activate(version, parameters_hash)
            if authorization == "replay_attestation_required":
                raise ValueError("replay attestation is required for strategy activation")
            if authorization == "replay_outcome_required":
                raise ValueError("replay outcome evidence is required for strategy activation")
            if authorization != "ok":
                raise ValueError("operator approval is required for strategy activation")
            return replace(
                proposal,
                status="active",
                parameters=_freeze_mapping(proposal.parameters),
            )
        consume_grants = getattr(self._database, "consume_strategy_activation_grants", None)
        if callable(consume_grants):
            authorization = consume_grants(version, parameters_hash)
            if authorization == "replay_attestation_required":
                raise ValueError("replay attestation is required for strategy activation")
            if authorization != "ok":
                raise ValueError("operator approval is required for strategy activation")
        else:
            consume_replay = getattr(self._database, "consume_replay_attestation", None)
            if not callable(consume_replay) or not consume_replay(version, parameters_hash):
                raise ValueError("replay attestation is required for strategy activation")
            consume_approval = getattr(self._database, "consume_strategy_approval", None)
            if not callable(consume_approval) or not consume_approval(version):
                raise ValueError("operator approval is required for strategy activation")
        self._database.set_active_strategy_version(version)
        return replace(proposal, status="active", parameters=_freeze_mapping(proposal.parameters))

    def get(self, version: str) -> StrategyVersion:
        strategy = self._database.load_strategy_version(version)
        if strategy is None:
            raise KeyError(f"unknown strategy version: {version}")
        status = "active" if version == self.active_version else strategy.status
        lifecycle_loader = getattr(self._database, "get_strategy_lifecycle_state", None)
        superseded_loader = getattr(self._database, "get_strategy_superseded_by", None)
        lifecycle = lifecycle_loader(version) if callable(lifecycle_loader) else status
        superseded_by = superseded_loader(version) if callable(superseded_loader) else None
        return replace(
            strategy,
            status=status,
            parameters=_freeze_mapping(strategy.parameters),
            lifecycle=lifecycle,
            superseded_by=superseded_by,
        )

    def list_versions(self) -> tuple[StrategyVersion, ...]:
        return tuple(
            self.get(strategy.version) for strategy in self._database.list_strategy_versions()
        )


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {key: _freeze_value(item) for key, item in deepcopy(dict(value)).items()}
    )


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    return value


def build_v4_research_arms(*, created_at: str) -> tuple[dict[str, object], ...]:
    """Return the immutable preregistered baseline plus six one-factor arms."""

    from .v3 import v3_proposal_parameters

    baseline_parameters = v3_proposal_parameters(1)
    changes = (
        "trend-quality",
        "breakout-overextension-cap",
        "no-recent-limit-up",
        "breadth-five-day-median",
        "size-bottom-30pct-filter",
        "signal-quality-rank",
    )

    def arm(version: str, role: str, change: str, parameters: Mapping[str, object]):
        payload = dict(parameters)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return {
            "arm_id": version,
            "version": version,
            "role": role,
            "change": change,
            "status": "proposed",
            "source": "tushare",
            "parent_version": "v0.3-policy-1" if role != "baseline" else None,
            "parameters": payload,
            "parameters_hash": hashlib.sha256(encoded.encode()).hexdigest(),
            "created_at": created_at,
        }

    result = [arm("v0.3-policy-1", "baseline", "baseline", baseline_parameters)]
    for change in changes:
        parameters: dict[str, object] = {
            **baseline_parameters,
            "research_factor": change,
            "v4_rule_engine_version": 4,
        }
        result.append(arm(f"v4-{change}", "challenger", change, parameters))
    return tuple(result)


def evaluate_v4_research_statistics(
    *,
    manifest_hash: str,
    primary_metric: str,
    daily_candidate_returns: Mapping[str, tuple[int, ...]],
    block_sessions: int = 20,
    bootstrap_samples: int = 10_000,
    seed: int | None = None,
    arm_metadata: Mapping[str, Mapping[str, object]] | None = None,
    replication_evidence: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Deterministic circular moving-block bootstrap with a White RC maximum."""

    if block_sessions != 20 or bootstrap_samples != 10_000:
        raise ValueError("v4 statistics require block=20 and exactly 10000 bootstrap samples")
    if seed is not None:
        raise ValueError("v4 statistics seed is derived from the manifest and cannot be overridden")
    if primary_metric != "20d_25bps_market_cap_matched_excess_bps":
        raise ValueError("v4 primary metric is frozen")
    baseline_id = "v0.3-policy-1"
    baseline = tuple(daily_candidate_returns.get(baseline_id, ()))
    if not baseline:
        raise ValueError("v4 statistics require the frozen baseline")
    if any(len(values) != len(baseline) for values in daily_candidate_returns.values()):
        raise ValueError("v4 statistics arms must share one signal-day calendar")
    actual_seed = int(
        hashlib.sha256(f"{manifest_hash}|v4-statistics-v1".encode()).hexdigest()[:16], 16
    )
    rng = random.Random(actual_seed)
    challengers = {
        name: tuple(value - base for value, base in zip(values, baseline, strict=True))
        for name, values in daily_candidate_returns.items()
        if name != baseline_id
    }
    expected_arms = {
        baseline_id,
        *(
            f"v4-{change}"
            for change in (
                "trend-quality",
                "breakout-overextension-cap",
                "no-recent-limit-up",
                "breadth-five-day-median",
                "size-bottom-30pct-filter",
                "signal-quality-rank",
            )
        ),
    }
    study_complete = set(daily_candidate_returns) == expected_arms
    replication_ok = bool(
        replication_evidence
        and replication_evidence.get("status") == "complete"
        and replication_evidence.get("completeness_rate_bps") == 10_000
        and float(replication_evidence.get("primary_metric_bps", -1)) >= 0
    )
    means = {name: sum(values) / len(values) for name, values in challengers.items()}
    boot_means: dict[str, list[float]] = {name: [] for name in challengers}
    white_maxima: list[float] = []
    centered = {
        name: tuple(value - means[name] for value in values) for name, values in challengers.items()
    }
    for _ in range(bootstrap_samples):
        indices: list[int] = []
        while len(indices) < len(baseline):
            start = rng.randrange(len(baseline))
            indices.extend((start + offset) % len(baseline) for offset in range(block_sessions))
        indices = indices[: len(baseline)]
        maxima: list[float] = []
        for name, values in challengers.items():
            sampled = sum(values[index] for index in indices) / len(indices)
            boot_means[name].append(sampled)
            maxima.append(sum(centered[name][index] for index in indices) / len(indices))
        white_maxima.append(max(maxima, default=0.0))
    observed_max = max(means.values(), default=0.0)
    family_p = sum(value >= observed_max for value in white_maxima) / bootstrap_samples
    arm_results: dict[str, dict[str, object]] = {}
    for name, mean in means.items():
        ordered = sorted(boot_means[name])
        lower = ordered[int(0.025 * (len(ordered) - 1))]
        upper = ordered[int(0.975 * (len(ordered) - 1))]
        metadata = dict((arm_metadata or {}).get(name, {}))
        complete = metadata.get("completeness_rate_bps") == 10_000
        executable_delta = int(metadata.get("executable_rate_delta_bps", -10_000))
        unexecutable_delta = int(metadata.get("unexecutable_rate_delta_bps", 10_000))
        eligible = (
            study_complete
            and replication_ok
            and len(baseline) >= block_sessions * 2
            and family_p <= 0.05
            and lower > 0
            and complete
            and executable_delta >= -200
            and unexecutable_delta <= 200
        )
        arm_results[name] = {
            "mean_primary_bps": mean,
            "ci95": [lower, upper],
            "eligible": eligible,
            "completeness_rate_bps": metadata.get("completeness_rate_bps"),
            "executable_rate_bps": metadata.get("executable_rate_bps"),
            "unexecutable_rate_bps": metadata.get("unexecutable_rate_bps"),
        }
    eligible_names = [name for name, result in arm_results.items() if result["eligible"]]
    eligible_names.sort(
        key=lambda name: (
            -float(arm_results[name]["mean_primary_bps"]),
            int(arm_results[name].get("unexecutable_rate_bps") or 10_001),
            name,
        )
    )
    winner_name = eligible_names[0] if eligible_names else None
    return {
        "schema": "v4-statistics-v1",
        "manifest_hash": manifest_hash,
        "primary_metric": primary_metric,
        "bootstrap_method": "circular_block_bootstrap",
        "block_sessions": block_sessions,
        "bootstrap_samples": bootstrap_samples,
        "seed": actual_seed,
        "multiple_testing_method": "white_reality_check",
        "family_wise_p_value": family_p,
        "study_complete": study_complete,
        "sina_replication_complete": replication_ok,
        "arms": arm_results,
        "winner": {
            "eligible": winner_name is not None,
            "arm_id": winner_name,
            "decision": "propose" if winner_name else "retain_baseline",
        },
    }
