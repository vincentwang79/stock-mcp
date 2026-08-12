from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from typing import Any


class MarketRegime(StrEnum):
    OFFENSIVE = "offensive"
    NEUTRAL = "neutral"
    DEFENSIVE = "defensive"


class SetupType(StrEnum):
    STRONG_PULLBACK = "strong_pullback"
    VOLUME_BREAKOUT = "volume_breakout"


@dataclass(frozen=True, slots=True)
class Security:
    symbol: str
    name: str
    exchange: str
    board: str
    list_date: date
    industry: str
    is_st: bool


@dataclass(frozen=True, slots=True)
class DailyBar:
    symbol: str
    trade_date: date
    open_1e4: int
    high_1e4: int
    low_1e4: int
    close_1e4: int
    pre_close_1e4: int
    volume_shares: int
    amount_fen: int
    source: str
    source_timestamp: datetime

    def with_source(self, source: str) -> DailyBar:
        return replace(self, source=source)


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    trade_date: date
    source: str
    source_timestamp: datetime
    securities: tuple[Security, ...]
    bars: tuple[DailyBar, ...]
    advance_ratio_bps: int
    above_ma20_ratio_bps: int


@dataclass(frozen=True, slots=True)
class StrategyVersion:
    version: str
    status: str
    parameters: Mapping[str, Any]
    lifecycle: str | None = None
    superseded_by: str | None = None


@dataclass(frozen=True, slots=True)
class Evidence:
    metric: str
    value: int | str
    threshold: int | str
    passed: bool
    score_contribution: int


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    symbol: str
    name: str
    rank: int
    score: int
    setup_type: SetupType
    strategy_version: str
    evidence: tuple[Evidence, ...]
    confirmation_condition: str
    invalidation_condition: str


@dataclass(frozen=True, slots=True)
class DailyReview:
    status: str
    trade_date: date
    source: str
    source_timestamp: datetime
    strategy_version: str
    market_regime: MarketRegime
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True, slots=True)
class IndustryClassificationReference:
    """Versioned, retrospective industry labels used only by rule engine v3."""

    classification_standard: str
    classification_mode: str
    classification_as_of: date
    classification_mapping_sha256: str
    industries: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class DailyPriceLimit:
    """One derived main-board price-limit fact, expressed in 1e4 yuan units."""

    symbol: str
    trade_date: date
    up_limit_1e4: int
    down_limit_1e4: int
    touched_up: bool
    touched_down: bool
    policy_exception: bool
    algorithm: str


@dataclass(frozen=True, slots=True)
class V3SecurityInput:
    """Point-in-time facts for exactly one v3 security evaluation."""

    security: Security
    prior_bars: tuple[DailyBar, ...]
    target_bar: DailyBar
    price_limit: DailyPriceLimit
    industry: str


@dataclass(frozen=True, slots=True)
class V3BreadthFacts:
    advance_count: int
    eligible_count: int
    above_ma20_count: int
    ma20_eligible_count: int
    advance_ratio_bps: int
    above_ma20_ratio_bps: int


@dataclass(frozen=True, slots=True)
class V3MarketInput:
    """Immutable input boundary for the v3 rule engine."""

    trade_date: date
    source: str
    source_timestamp: datetime
    prior_dates: tuple[date, ...]
    securities: tuple[V3SecurityInput, ...]
    breadth: V3BreadthFacts
    industry_reference: IndustryClassificationReference
    pipeline_version: str = "pipeline-v0.2"
    input_hash_schema: str = "v3-input-v1"


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    eligible: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ProviderFetchEvidence:
    fetch_id: str
    source: str
    endpoint_kind: str
    request_key: str
    trade_date: date | None
    http_date: str | None
    retrieved_at: datetime
    http_status: int | None
    byte_length: int
    payload_sha256: str
    adapter_version: str
    status: str
    error_class: str | None = None


@dataclass(frozen=True, slots=True)
class ShareCapitalFact:
    symbol: str
    effective_date: date
    source: str
    outstanding_shares: int
    source_timestamp: datetime
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class DailySecurityStatus:
    symbol: str
    trade_date: date
    source: str
    tradestatus: str
    is_st: bool
    source_timestamp: datetime
    batch_sha256: str


@dataclass(frozen=True, slots=True)
class ProviderDailyMetric:
    symbol: str
    trade_date: date
    price_source: str
    capital_source: str
    upstream_market_cap_fen: int | None
    derived_market_cap_fen: int | None
    upstream_turnover_rate: str | None
    derived_turnover_rate: str | None
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class V4DatasetManifest:
    manifest_hash: str
    price_source: str
    bar_start: date
    warmup_sessions: int
    signal_start: date
    signal_end: date
    outcome_through: date
    calendar_hash: str
    price_hash: str
    status_hash: str
    capital_hash: str
    industry_hash: str
    adapter_version: str
    universe_symbols: tuple[str, ...]
    included_symbols: tuple[str, ...]
    excluded_symbols: tuple[str, ...]
    exclusion_reason: str | None
    capital_coverage_bps: int
    universe_symbol_count: int
    included_symbol_count: int
    excluded_symbol_count: int
    universe_symbols_hash: str
    included_symbols_hash: str
    excluded_symbols_hash: str
    universe_source_manifest_hash: str
    schema: str = "v4-manifest-v1"
