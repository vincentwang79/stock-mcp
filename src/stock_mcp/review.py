from collections import defaultdict
from datetime import timedelta

from .domain import (
    Candidate,
    DailyBar,
    DailyReview,
    Evidence,
    MarketRegime,
    MarketSnapshot,
    Security,
    SetupType,
    StrategyVersion,
    V3MarketInput,
)
from .strategy import validate_strategy_parameters


class MixedSourceSnapshotError(ValueError):
    """Raised when one snapshot contains price bars from multiple providers."""


def generate_daily_review(
    snapshot: MarketSnapshot | V3MarketInput,
    strategy: StrategyVersion,
) -> DailyReview:
    """Dispatch to the immutable engine named by the stored strategy version."""
    parameters = validate_strategy_parameters(strategy.parameters, require_complete=True)
    engine_version = parameters["rule_engine_version"]
    if engine_version == 1:
        if not isinstance(snapshot, MarketSnapshot):
            raise ValueError("rule engine v1 requires a MarketSnapshot")
        return _generate_daily_review_v1(snapshot, strategy)
    if engine_version == 2:
        if not isinstance(snapshot, MarketSnapshot):
            raise ValueError("rule engine v2 requires a MarketSnapshot")
        return _generate_daily_review_v2(snapshot, strategy)
    if engine_version == 3:
        from .v3 import generate_v3_daily_review

        if not isinstance(snapshot, V3MarketInput):
            raise ValueError("rule engine v3 requires a V3MarketInput")
        return generate_v3_daily_review(snapshot, strategy)
    raise ValueError("unsupported rule_engine_version")


def _generate_daily_review_v1(
    snapshot: MarketSnapshot,
    strategy: StrategyVersion,
) -> DailyReview:
    """Frozen v1 screening/scoring semantics; changes require a new engine number."""

    return _generate_daily_review(snapshot, strategy, score_industry=True)


def _generate_daily_review_v2(
    snapshot: MarketSnapshot,
    strategy: StrategyVersion,
) -> DailyReview:
    """Keep industry strength as evidence without using partial coverage for ranking."""

    return _generate_daily_review(snapshot, strategy, score_industry=False)


def _generate_daily_review(
    snapshot: MarketSnapshot,
    strategy: StrategyVersion,
    *,
    score_industry: bool,
) -> DailyReview:
    """Generate one deterministic review with an engine-frozen industry policy."""

    _validate_single_source(snapshot)
    regime = _classify_regime(snapshot, strategy)
    quota = _candidate_quota(regime, strategy)
    securities = {security.symbol: security for security in snapshot.securities}
    bars_by_symbol: dict[str, list[DailyBar]] = defaultdict(list)
    for bar in snapshot.bars:
        if bar.trade_date <= snapshot.trade_date:
            bars_by_symbol[bar.symbol].append(bar)
    for bars in bars_by_symbol.values():
        bars.sort(key=lambda bar: bar.trade_date)

    target_bars = {
        symbol: bars[-1]
        for symbol, bars in bars_by_symbol.items()
        if bars and bars[-1].trade_date == snapshot.trade_date
    }
    industry_returns = _industry_returns(target_bars, securities)
    ranked: list[tuple[int, str, Candidate]] = []

    for symbol in sorted(target_bars):
        bar = target_bars[symbol]
        security = securities.get(bar.symbol)
        if security is None or security.board != "MAIN" or security.is_st:
            continue
        if security.list_date > snapshot.trade_date - timedelta(days=180):
            continue
        minimum_liquidity = int(strategy.parameters["min_liquidity_amount_fen"])
        if bar.amount_fen < minimum_liquidity:
            continue

        history = bars_by_symbol[bar.symbol]
        maximum_limit_up_days = int(strategy.parameters["max_consecutive_limit_up_days"])
        if _consecutive_limit_up_days(history) > maximum_limit_up_days:
            continue

        setup = _classify_setup(history, strategy)
        if setup is None:
            continue
        setup_type, setup_evidence = setup

        return_bps = ((bar.close_1e4 - bar.pre_close_1e4) * 10_000) // bar.pre_close_1e4
        liquidity_points = min(30, bar.amount_fen // 400_000_000)
        momentum_points = max(0, min(50, return_bps // 10))
        has_industry = bool(security.industry.strip())
        industry_strength_bps = industry_returns.get(security.industry) if has_industry else None
        industry_points = (
            max(-10, min(20, industry_strength_bps // 100))
            if score_industry and industry_strength_bps is not None
            else 0
        )
        score = int(20 + liquidity_points + momentum_points + industry_points)
        evidence = (
            Evidence(
                metric="base_score",
                value=20,
                threshold=20,
                passed=True,
                score_contribution=20,
            ),
            Evidence(
                metric="setup_inclusion",
                value=setup_type,
                threshold="eligible_setup",
                passed=True,
                score_contribution=0,
            ),
            *setup_evidence,
            Evidence(
                metric="daily_return_bps",
                value=return_bps,
                threshold=0,
                passed=return_bps > 0,
                score_contribution=int(momentum_points),
            ),
            Evidence(
                metric="amount_fen",
                value=bar.amount_fen,
                threshold=0,
                passed=bar.amount_fen > 0,
                score_contribution=int(liquidity_points),
            ),
            Evidence(
                metric="industry_strength_bps",
                value="unavailable" if industry_strength_bps is None else industry_strength_bps,
                threshold=0,
                passed=industry_strength_bps is not None and industry_strength_bps > 0,
                score_contribution=int(industry_points),
            ),
        )
        ranked.append(
            (
                score,
                bar.symbol,
                Candidate(
                    candidate_id=f"{snapshot.trade_date.isoformat()}:{strategy.version}:{bar.symbol}",
                    symbol=bar.symbol,
                    name=security.name,
                    rank=0,
                    score=score,
                    setup_type=setup_type,
                    strategy_version=strategy.version,
                    evidence=evidence,
                    confirmation_condition=f"close > {bar.high_1e4}",
                    invalidation_condition=f"close < {bar.low_1e4}",
                ),
            )
        )

    ranked.sort(key=lambda item: (-item[0], item[1]))
    candidates = tuple(
        Candidate(
            candidate_id=candidate.candidate_id,
            symbol=candidate.symbol,
            name=candidate.name,
            rank=index,
            score=candidate.score,
            setup_type=candidate.setup_type,
            strategy_version=candidate.strategy_version,
            evidence=candidate.evidence,
            confirmation_condition=candidate.confirmation_condition,
            invalidation_condition=candidate.invalidation_condition,
        )
        for index, (_, _, candidate) in enumerate(ranked[:quota], start=1)
    )

    return DailyReview(
        status="ready",
        trade_date=snapshot.trade_date,
        source=snapshot.source,
        source_timestamp=snapshot.source_timestamp,
        strategy_version=strategy.version,
        market_regime=regime,
        candidates=candidates,
    )


def _validate_single_source(snapshot: MarketSnapshot) -> None:
    if any(
        bar.trade_date <= snapshot.trade_date and bar.source != snapshot.source
        for bar in snapshot.bars
    ):
        raise MixedSourceSnapshotError("all price bars must use the snapshot source")


def _return_bps(close: int, pre_close: int) -> int:
    if pre_close <= 0:
        return 0
    return ((close - pre_close) * 10_000) // pre_close


def _consecutive_limit_up_days(bars: list[DailyBar]) -> int:
    count = 0
    for bar in reversed(bars):
        # Main-board daily limits are nominally 10%; 9.5% tolerates tick rounding.
        if _return_bps(bar.close_1e4, bar.pre_close_1e4) < 950:
            break
        count += 1
    return count


def _classify_setup(
    bars: list[DailyBar], strategy: StrategyVersion
) -> tuple[SetupType, tuple[Evidence, ...]] | None:
    target = bars[-1]
    prior = bars[:-1]
    target_return_bps = _return_bps(target.close_1e4, target.pre_close_1e4)

    if prior:
        prior_gain_bps = _return_bps(prior[-1].close_1e4, prior[0].close_1e4)
        pullback_bps = _return_bps(target.close_1e4, target.pre_close_1e4)
        minimum_prior_gain = int(strategy.parameters["strong_pullback_min_prior_gain_bps"])
        maximum_pullback = int(strategy.parameters["strong_pullback_max_pullback_bps"])
        if prior_gain_bps >= minimum_prior_gain and -maximum_pullback <= pullback_bps <= 0:
            return (
                SetupType.STRONG_PULLBACK,
                (
                    Evidence(
                        metric="prior_gain_bps",
                        value=prior_gain_bps,
                        threshold=minimum_prior_gain,
                        passed=True,
                        score_contribution=0,
                    ),
                    Evidence(
                        metric="pullback_bps",
                        value=pullback_bps,
                        threshold=f"-{maximum_pullback}..0",
                        passed=True,
                        score_contribution=0,
                    ),
                ),
            )

        average_volume = sum(bar.volume_shares for bar in prior) // len(prior)
        volume_ratio_bps = (
            (target.volume_shares * 10_000) // average_volume if average_volume > 0 else 0
        )
        minimum_volume_ratio = int(strategy.parameters["volume_breakout_min_volume_ratio_bps"])
        prior_high = max(bar.high_1e4 for bar in prior)
        if (
            target.close_1e4 > prior_high
            and target_return_bps > 0
            and volume_ratio_bps >= minimum_volume_ratio
        ):
            return (
                SetupType.VOLUME_BREAKOUT,
                (
                    Evidence(
                        metric="volume_ratio_bps",
                        value=volume_ratio_bps,
                        threshold=minimum_volume_ratio,
                        passed=True,
                        score_contribution=0,
                    ),
                    Evidence(
                        metric="breakout_prior_high_1e4",
                        value=target.close_1e4,
                        threshold=prior_high,
                        passed=True,
                        score_contribution=0,
                    ),
                ),
            )

    return None


def _industry_returns(
    target_bars: dict[str, DailyBar], securities: dict[str, Security]
) -> dict[str, int]:
    values: dict[str, list[int]] = defaultdict(list)
    for symbol, bar in target_bars.items():
        security = securities.get(symbol)
        if (
            security is not None
            and security.board == "MAIN"
            and not security.is_st
            and security.industry.strip()
        ):
            values[security.industry].append(_return_bps(bar.close_1e4, bar.pre_close_1e4))
    return {industry: sum(returns) // len(returns) for industry, returns in values.items()}


def _classify_regime(
    snapshot: MarketSnapshot,
    strategy: StrategyVersion,
) -> MarketRegime:
    offensive_min = int(strategy.parameters["offensive_min_bps"])
    defensive_max = int(strategy.parameters["defensive_max_bps"])
    breadth_values = (snapshot.advance_ratio_bps, snapshot.above_ma20_ratio_bps)
    if all(value >= offensive_min for value in breadth_values):
        return MarketRegime.OFFENSIVE
    if all(value <= defensive_max for value in breadth_values):
        return MarketRegime.DEFENSIVE
    return MarketRegime.NEUTRAL


def _candidate_quota(regime: MarketRegime, strategy: StrategyVersion) -> int:
    if regime is MarketRegime.DEFENSIVE:
        return 0
    if regime is MarketRegime.NEUTRAL:
        return int(strategy.parameters["neutral_limit"])
    return int(strategy.parameters["offensive_limit"])
