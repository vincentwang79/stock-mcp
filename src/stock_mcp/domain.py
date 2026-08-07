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
