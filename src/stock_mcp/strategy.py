from __future__ import annotations

import hashlib
import json
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
