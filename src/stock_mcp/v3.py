"""Deterministic rule engine v3.

The module is deliberately independent from the legacy ``MarketSnapshot``
engines.  It consumes a closed point-in-time input carrying exactly sixty
prior sessions, derived main-board price limits, and a versioned retrospective
industry reference.  Industry facts are explanatory only.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from fractions import Fraction
from types import MappingProxyType
from typing import Any

from .domain import (
    Candidate,
    DailyBar,
    DailyPriceLimit,
    DailyReview,
    EligibilityDecision,
    Evidence,
    MarketRegime,
    SetupType,
    StrategyVersion,
    V3MarketInput,
    V3SecurityInput,
)

PIPELINE_VERSION = "pipeline-v0.2"
INPUT_HASH_SCHEMA = "v3-input-v1"
RESULT_HASH_SCHEMA = "v3-result-v1"
OUTCOME_HASH_SCHEMA = "v3-outcome-v1"
ADJUSTMENT_ALGORITHM = "close-preclose-chain-v1"
PRICE_LIMIT_ALGORITHM = "mainboard-10pct-round-half-up-v1"
REQUIRED_WARMUP_SESSIONS = 60

_V3_PARAMETER_RANGES: dict[str, tuple[int, int]] = {
    "rule_engine_version": (3, 3),
    "regime_policy": (1, 2),
    "offensive_min_bps": (0, 10_000),
    "defensive_max_bps": (0, 10_000),
    "neutral_pullback_limit": (0, 50),
    "neutral_breakout_limit": (0, 50),
    "offensive_pullback_limit": (0, 50),
    "offensive_breakout_limit": (0, 50),
    "min_median_amount_fen": (0, 10**16),
    "liquidity_lookback_sessions": (20, 20),
    "trend_lookback_sessions": (60, 60),
    "pullback_peak_lookback_sessions": (20, 20),
    "pullback_min_prior_gain_bps": (0, 20_000),
    "pullback_max_drawdown_bps": (0, 10_000),
    "pullback_max_amount_ratio_bps": (0, 100_000),
    "breakout_lookback_sessions": (60, 60),
    "breakout_amount_lookback_sessions": (20, 20),
    "breakout_min_amount_ratio_bps": (10_000, 100_000),
    "recent_limit_up_lookback_sessions": (5, 5),
    "required_warmup_sessions": (60, 60),
}
V3_PARAMETER_NAMES = frozenset(_V3_PARAMETER_RANGES)

_V3_TEMPLATE_BASE = {
    "rule_engine_version": 3,
    "offensive_min_bps": 5_500,
    "defensive_max_bps": 4_000,
    "neutral_pullback_limit": 1,
    "neutral_breakout_limit": 1,
    "offensive_pullback_limit": 2,
    "offensive_breakout_limit": 1,
    "min_median_amount_fen": 5_000_000_000,
    "liquidity_lookback_sessions": 20,
    "trend_lookback_sessions": 60,
    "pullback_peak_lookback_sessions": 20,
    "pullback_min_prior_gain_bps": 1_200,
    "pullback_max_drawdown_bps": 350,
    "pullback_max_amount_ratio_bps": 10_000,
    "breakout_lookback_sessions": 60,
    "breakout_amount_lookback_sessions": 20,
    "breakout_min_amount_ratio_bps": 15_000,
    "recent_limit_up_lookback_sessions": 5,
    "required_warmup_sessions": 60,
}
V3_POLICY1_PARAMETERS = MappingProxyType({**_V3_TEMPLATE_BASE, "regime_policy": 1})
V3_POLICY2_PARAMETERS = MappingProxyType({**_V3_TEMPLATE_BASE, "regime_policy": 2})


def v3_proposal_parameters(policy: int) -> dict[str, int]:
    if policy not in {1, 2}:
        raise ValueError("v3 regime policy must be 1 or 2")
    template = V3_POLICY1_PARAMETERS if policy == 1 else V3_POLICY2_PARAMETERS
    return dict(template)


def validate_v3_parameters(
    parameters: Mapping[str, Any], *, require_complete: bool = False
) -> dict[str, int]:
    unknown = sorted(set(parameters) - V3_PARAMETER_NAMES)
    if unknown:
        raise ValueError("unsupported v3 strategy parameters: " + ", ".join(unknown))
    if require_complete:
        missing = sorted(V3_PARAMETER_NAMES - set(parameters))
        if missing:
            raise ValueError("missing v3 strategy parameters: " + ", ".join(missing))
    validated: dict[str, int] = {}
    for name, value in parameters.items():
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"strategy parameter {name} must be an integer")
        minimum, maximum = _V3_PARAMETER_RANGES[name]
        if not minimum <= value <= maximum:
            raise ValueError(f"strategy parameter {name} is out of range")
        validated[name] = value
    if require_complete and validated["defensive_max_bps"] >= validated["offensive_min_bps"]:
        raise ValueError("defensive_max_bps must be lower than offensive_min_bps")
    return validated


def _round_half_up(value: Fraction) -> int:
    return (value.numerator * 2 + value.denominator) // (2 * value.denominator)


def _round_price_to_cent_half_up(value_1e4: Fraction) -> int:
    return _round_half_up(value_1e4 / 100) * 100


def derive_daily_price_limit(bar: DailyBar, security: object | None = None) -> DailyPriceLimit:
    """Derive the ordinary 10% main-board limit without percentage approximation."""

    if bar.pre_close_1e4 <= 0:
        raise ValueError("pre_close must be positive for a price-limit fact")
    up = _round_price_to_cent_half_up(Fraction(bar.pre_close_1e4 * 110, 100))
    down = _round_price_to_cent_half_up(Fraction(bar.pre_close_1e4 * 90, 100))
    unsupported = bool(
        security is not None
        and (getattr(security, "board", None) != "MAIN" or bool(getattr(security, "is_st", False)))
    )
    exception = (
        unsupported
        or any(value > up for value in (bar.open_1e4, bar.high_1e4, bar.close_1e4))
        or any(value < down for value in (bar.open_1e4, bar.low_1e4, bar.close_1e4))
    )
    return DailyPriceLimit(
        symbol=bar.symbol,
        trade_date=bar.trade_date,
        up_limit_1e4=up,
        down_limit_1e4=down,
        touched_up=bar.high_1e4 >= up,
        touched_down=bar.low_1e4 <= down,
        policy_exception=exception,
        algorithm=PRICE_LIMIT_ALGORITHM,
    )


def adjusted_close_chain(
    prior_bars: Sequence[DailyBar], target_bar: DailyBar | None = None
) -> tuple[Fraction, ...]:
    """Return point-in-time adjusted closes anchored to the final target close."""

    bars = (*prior_bars, target_bar) if target_bar is not None else tuple(prior_bars)
    if not bars:
        return ()
    if tuple(bar.trade_date for bar in bars) != tuple(sorted({bar.trade_date for bar in bars})):
        raise ValueError("adjustment bars must be unique and strictly increasing")
    if any(bar.close_1e4 <= 0 or bar.pre_close_1e4 <= 0 for bar in bars):
        raise ValueError("adjustment bars require positive close and pre_close")
    factors: list[Fraction] = [Fraction(1)] * len(bars)
    for index in range(len(bars) - 2, -1, -1):
        current = bars[index]
        following = bars[index + 1]
        factors[index] = factors[index + 1] * Fraction(following.pre_close_1e4, current.close_1e4)
    return tuple(
        Fraction(bar.close_1e4) * factor for bar, factor in zip(bars, factors, strict=True)
    )


def _adjustment_factors(bars: Sequence[DailyBar]) -> tuple[Fraction, ...]:
    closes = adjusted_close_chain(bars)
    return tuple(value / bar.close_1e4 for value, bar in zip(closes, bars, strict=True))


def percentile_bps(
    values: tuple[int | Fraction, ...], *, higher_is_better: bool
) -> tuple[int, ...]:
    """Exact mid-rank percentiles; a singleton is always the best (10000)."""

    if not values:
        return ()
    if len(values) == 1:
        return (10_000,)
    result: list[int] = []
    for value in values:
        worse = sum(other < value for other in values)
        equal = sum(other == value for other in values)
        mid_position = Fraction(2 * worse + equal - 1, 2)
        percentile = mid_position * 10_000 / (len(values) - 1)
        score = percentile.numerator // percentile.denominator
        result.append(score if higher_is_better else 10_000 - score)
    return tuple(result)


def evaluate_v3_eligibility(
    security_input: V3SecurityInput, strategy: StrategyVersion
) -> EligibilityDecision:
    parameters = validate_v3_parameters(strategy.parameters, require_complete=True)
    security = security_input.security
    if security.board != "MAIN":
        return EligibilityDecision(False, "not_main_board")
    if security.is_st:
        return EligibilityDecision(False, "st_security")
    target = security_input.target_bar
    limit = security_input.price_limit
    fact_date = (
        target.trade_date if target is not None else limit.trade_date if limit is not None else None
    )
    if fact_date is None:
        return EligibilityDecision(False, "missing_target_or_limit_facts")
    if security.list_date > fact_date - timedelta(days=180):
        return EligibilityDecision(False, "listing_age_lt_180_days")
    if target is None or limit is None:
        return EligibilityDecision(False, "missing_target_or_limit_facts")
    if limit.symbol != target.symbol or limit.trade_date != target.trade_date:
        return EligibilityDecision(False, "missing_target_or_limit_facts")
    if limit.policy_exception:
        return EligibilityDecision(False, "limit_policy_exception")
    if len(security_input.prior_bars) != parameters["required_warmup_sessions"]:
        return EligibilityDecision(False, "insufficient_prior_history")
    median_amount = _median([bar.amount_fen for bar in security_input.prior_bars[-20:]])
    if median_amount < parameters["min_median_amount_fen"]:
        return EligibilityDecision(False, "low_median_liquidity")
    if limit.touched_up:
        return EligibilityDecision(False, "target_touched_up_limit")
    if _setup_metrics(security_input, parameters) is None:
        return EligibilityDecision(False, "no_eligible_setup")
    return EligibilityDecision(True, "eligible")


@dataclass(frozen=True, slots=True)
class _SetupMetrics:
    setup_type: SetupType
    primary: Fraction
    amount_ratio_bps: Fraction
    median_amount_fen: Fraction
    prior_gain_bps: Fraction
    drawdown_or_margin_bps: Fraction
    recent_limit_up_count: int


def _setup_metrics(item: V3SecurityInput, parameters: Mapping[str, int]) -> _SetupMetrics | None:
    bars = (*item.prior_bars, item.target_bar)
    if len(bars) != 61:
        return None
    factors = _adjustment_factors(bars)
    adjusted_closes = tuple(
        Fraction(bar.close_1e4) * factor for bar, factor in zip(bars, factors, strict=True)
    )
    adjusted_highs = tuple(
        Fraction(bar.high_1e4) * factor for bar, factor in zip(bars, factors, strict=True)
    )
    target = item.target_bar
    median_amount = _median([bar.amount_fen for bar in item.prior_bars[-20:]])
    if median_amount <= 0:
        return None
    amount_ratio = Fraction(target.amount_fen * 10_000, median_amount)
    prior_gain = _ratio_bps(adjusted_closes[-2], adjusted_closes[0])
    prior_twenty_high = max(adjusted_highs[-21:-1])
    drawdown = _ratio_bps(adjusted_closes[-1], prior_twenty_high)
    target_return = _ratio_bps(target.close_1e4, target.pre_close_1e4)
    recent_limit_count = sum(
        derive_daily_price_limit(bar).touched_up for bar in item.prior_bars[-5:]
    )
    if (
        prior_gain >= parameters["pullback_min_prior_gain_bps"]
        and -parameters["pullback_max_drawdown_bps"] <= drawdown <= 0
        and target_return <= 0
        and amount_ratio <= parameters["pullback_max_amount_ratio_bps"]
    ):
        return _SetupMetrics(
            SetupType.STRONG_PULLBACK,
            prior_gain,
            amount_ratio,
            median_amount,
            prior_gain,
            drawdown,
            recent_limit_count,
        )
    prior_sixty_high = max(adjusted_highs[:-1])
    margin = _ratio_bps(adjusted_closes[-1], prior_sixty_high)
    if (
        adjusted_closes[-1] > prior_sixty_high
        and target_return > 0
        and amount_ratio >= parameters["breakout_min_amount_ratio_bps"]
    ):
        return _SetupMetrics(
            SetupType.VOLUME_BREAKOUT,
            margin,
            amount_ratio,
            median_amount,
            prior_gain,
            margin,
            recent_limit_count,
        )
    return None


def _median(values: Sequence[int]) -> Fraction:
    ordered = sorted(values)
    if not ordered:
        return Fraction(0)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return Fraction(ordered[middle])
    return Fraction(ordered[middle - 1] + ordered[middle], 2)


def _ratio_bps(value: int | Fraction, base: int | Fraction) -> Fraction:
    if base <= 0:
        raise ValueError("ratio base must be positive")
    return (Fraction(value, base) - 1) * 10_000


def _regime(market: V3MarketInput, parameters: Mapping[str, int]) -> MarketRegime:
    values = (market.breadth.advance_ratio_bps, market.breadth.above_ma20_ratio_bps)
    if all(value >= parameters["offensive_min_bps"] for value in values):
        return MarketRegime.OFFENSIVE
    if all(value <= parameters["defensive_max_bps"] for value in values):
        return MarketRegime.DEFENSIVE
    return MarketRegime.NEUTRAL


def _quotas(regime: MarketRegime, parameters: Mapping[str, int]) -> tuple[int, int]:
    if parameters["regime_policy"] == 2 or regime is MarketRegime.OFFENSIVE:
        return (
            parameters["offensive_pullback_limit"],
            parameters["offensive_breakout_limit"],
        )
    if regime is MarketRegime.NEUTRAL:
        return parameters["neutral_pullback_limit"], parameters["neutral_breakout_limit"]
    return 0, 0


def generate_v3_daily_review(market: V3MarketInput, strategy: StrategyVersion) -> DailyReview:
    parameters = validate_v3_parameters(strategy.parameters, require_complete=True)
    if market.pipeline_version != PIPELINE_VERSION or market.input_hash_schema != INPUT_HASH_SCHEMA:
        raise ValueError("v3 market input contract version is unsupported")
    if len(market.prior_dates) != REQUIRED_WARMUP_SESSIONS:
        raise ValueError("v3 market input requires exactly sixty prior sessions")
    if (
        market.breadth.eligible_count <= 0
        or market.breadth.ma20_eligible_count * 10_000
        // market.breadth.eligible_count
        < 9_700
    ):
        raise ValueError("v3 market input requires at least 9700 bps ma20 coverage")
    if any(item.target_bar.trade_date != market.trade_date for item in market.securities):
        raise ValueError("v3 market input target dates do not match")
    if any(
        bar.source != market.source or bar.trade_date >= market.trade_date
        for item in market.securities
        for bar in item.prior_bars
    ) or any(item.target_bar.source != market.source for item in market.securities):
        raise ValueError("v3 market input contains mixed-source or future facts")

    pools: dict[SetupType, list[tuple[V3SecurityInput, _SetupMetrics]]] = defaultdict(list)
    for item in sorted(market.securities, key=lambda entry: entry.security.symbol):
        if not evaluate_v3_eligibility(item, strategy).eligible:
            continue
        metrics = _setup_metrics(item, parameters)
        if metrics is not None:
            pools[metrics.setup_type].append((item, metrics))

    industry_returns = _industry_returns(market)
    ranked_by_setup: dict[SetupType, list[Candidate]] = {}
    for setup_type, entries in pools.items():
        first_pct = percentile_bps(
            tuple(metrics.primary for _, metrics in entries), higher_is_better=True
        )
        amount_pct = percentile_bps(
            tuple(metrics.amount_ratio_bps for _, metrics in entries),
            higher_is_better=setup_type is SetupType.VOLUME_BREAKOUT,
        )
        liquidity_pct = percentile_bps(
            tuple(metrics.median_amount_fen for _, metrics in entries), higher_is_better=True
        )
        candidates: list[Candidate] = []
        for (item, metrics), p1, p2, p3 in zip(
            entries, first_pct, amount_pct, liquidity_pct, strict=True
        ):
            contributions = (p1 * 40 // 10_000, p2 * 30 // 10_000, p3 * 30 // 10_000)
            industry = item.industry.strip() or "unavailable"
            industry_value = (
                "unavailable"
                if industry == "unavailable"
                else industry_returns.get(industry, "unavailable")
            )
            evidence = (
                Evidence("primary_percentile_bps", p1, 0, True, contributions[0]),
                Evidence("amount_percentile_bps", p2, 0, True, contributions[1]),
                Evidence("liquidity_percentile_bps", p3, 0, True, contributions[2]),
                Evidence(
                    "prior_gain_bps",
                    _floor(metrics.prior_gain_bps),
                    parameters["pullback_min_prior_gain_bps"],
                    True,
                    0,
                ),
                Evidence("amount_ratio_bps", _floor(metrics.amount_ratio_bps), 0, True, 0),
                Evidence("recent_limit_up_count", metrics.recent_limit_up_count, 0, True, 0),
                Evidence(
                    "industry",
                    industry,
                    market.industry_reference.classification_standard,
                    industry != "unavailable",
                    0,
                ),
                Evidence(
                    "industry_strength_bps", industry_value, 0, industry_value != "unavailable", 0
                ),
                Evidence(
                    "classification_mode",
                    market.industry_reference.classification_mode,
                    "retrospective_current_mapping",
                    True,
                    0,
                ),
                Evidence(
                    "classification_as_of",
                    market.industry_reference.classification_as_of.isoformat(),
                    market.industry_reference.classification_as_of.isoformat(),
                    True,
                    0,
                ),
                Evidence(
                    "classification_mapping_sha256",
                    market.industry_reference.classification_mapping_sha256,
                    market.industry_reference.classification_mapping_sha256,
                    True,
                    0,
                ),
            )
            target = item.target_bar
            candidates.append(
                Candidate(
                    candidate_id=f"{market.trade_date.isoformat()}:{strategy.version}:{target.symbol}",
                    symbol=target.symbol,
                    name=item.security.name,
                    rank=0,
                    score=sum(contributions),
                    setup_type=setup_type,
                    strategy_version=strategy.version,
                    evidence=evidence,
                    confirmation_condition=f"close > {target.high_1e4}",
                    invalidation_condition=f"close < {target.low_1e4}",
                )
            )
        ranked_by_setup[setup_type] = sorted(
            candidates, key=lambda value: (-value.score, value.symbol)
        )

    regime = _regime(market, parameters)
    pullback_quota, breakout_quota = _quotas(regime, parameters)
    selected = [
        *ranked_by_setup.get(SetupType.STRONG_PULLBACK, ())[:pullback_quota],
        *ranked_by_setup.get(SetupType.VOLUME_BREAKOUT, ())[:breakout_quota],
    ]
    selected.sort(
        key=lambda value: (
            -value.score,
            0 if value.setup_type is SetupType.STRONG_PULLBACK else 1,
            value.symbol,
        )
    )
    candidates = tuple(
        replace(candidate, rank=index) for index, candidate in enumerate(selected, 1)
    )
    return DailyReview(
        status="ready",
        trade_date=market.trade_date,
        source=market.source,
        source_timestamp=market.source_timestamp,
        strategy_version=strategy.version,
        market_regime=regime,
        candidates=candidates,
    )


def _industry_returns(market: V3MarketInput) -> dict[str, int]:
    grouped: dict[str, list[Fraction]] = defaultdict(list)
    for item in market.securities:
        industry = item.industry.strip() or "unavailable"
        if industry == "unavailable":
            continue
        grouped[industry].append(
            _ratio_bps(item.target_bar.close_1e4, item.target_bar.pre_close_1e4)
        )
    return {
        industry: _floor(sum(values, Fraction(0)) / len(values))
        for industry, values in grouped.items()
    }


def _floor(value: Fraction) -> int:
    return value.numerator // value.denominator


def canonical_v3_market_input_hash(market: V3MarketInput) -> str:
    return _canonical_hash({"schema": INPUT_HASH_SCHEMA, "market": _jsonable(asdict(market))})


def canonical_v3_result_hash(
    market: V3MarketInput, strategy: StrategyVersion, review: DailyReview
) -> str:
    return _canonical_hash(
        {
            "schema": RESULT_HASH_SCHEMA,
            "parameters_hash": canonical_v3_strategy_parameters_hash(strategy.parameters),
            "industry_reference": _jsonable(asdict(market.industry_reference)),
            "review": _jsonable(asdict(review)),
        }
    )


def canonical_v3_strategy_parameters_hash(parameters: Mapping[str, Any]) -> str:
    """Bind v3 evidence to one complete, validated immutable rule map."""

    validated = validate_v3_parameters(parameters, require_complete=True)
    return _canonical_hash(validated)


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[no-any-return]
    if hasattr(value, "value"):
        return value.value  # type: ignore[no-any-return]
    return value
