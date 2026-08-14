"""Deterministic primitives for the long-lived strategy research program.

This module deliberately contains no provider clients and performs no I/O.  It
turns already captured facts into versioned hypotheses, continuous exploratory
features, point-in-time provider records, and reproducible multiple-testing
evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from statistics import median

from stock_mcp.domain import DailyBar, DailyPriceLimit

_HASH_PATTERN_LENGTH = 64


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _iso_date(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = (
            datetime.strptime(text, "%Y%m%d").date() if len(text) == 8 else date.fromisoformat(text)
        )
    except ValueError as error:
        raise ValueError(f"{field} must be a calendar date") from error
    return parsed.isoformat()


def _decimal_text(value: object) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    normalized = format(Decimal(str(value)), "f").rstrip("0").rstrip(".")
    return normalized or "0"


def _require_aware_timestamp(value: datetime, *, field: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")


def first_batch_hypotheses(*, registered_at: datetime) -> tuple[dict[str, object], ...]:
    """Return the first frozen research agenda without registering anything."""

    timestamp = registered_at.isoformat()
    definitions = (
        (
            "breadth-five-day-median-v1",
            "market-breadth",
            "Five-day median breadth",
            "A short breadth median may reduce one-day market-regime noise.",
            {"arm_id": "v4-breadth-five-day-median", "window_sessions": 5},
            ("daily_bars", "daily_price_limits"),
            "discovery_exhausted",
            "discovery_exhausted",
            "2026-08-07",
        ),
        (
            "breakout-overextension-cap-v1",
            "price-overextension",
            "Breakout overextension cap",
            "Very extended breakouts may have weaker subsequent excess returns.",
            {"arm_id": "v4-breakout-overextension-cap"},
            ("daily_bars",),
            "discovery_exhausted",
            "discovery_exhausted",
            "2026-08-07",
        ),
        (
            "no-recent-limit-up-v1",
            "attention-overreaction",
            "No recent limit-up",
            "Recent limit-up attention may predict weaker subsequent excess returns.",
            {"lookback_sessions": 5, "maximum_touched_limit_up_days": 0},
            ("daily_price_limits",),
            "frozen_forward",
            "discovery_exhausted",
            "2026-08-07",
        ),
        (
            "signal-quality-rank-v1",
            "cross-sectional-ranking",
            "Signal quality ranking",
            "Joint signal quality may improve ordering within a fixed eligible pool.",
            {"arm_id": "v4-signal-quality-rank"},
            ("daily_bars", "daily_price_limits"),
            "discovery_exhausted",
            "discovery_exhausted",
            "2026-08-07",
        ),
        (
            "size-bottom-30pct-filter-v1",
            "size-liquidity",
            "Bottom-size exclusion",
            "Removing the smallest tradable names may improve execution robustness.",
            {"arm_id": "v4-size-bottom-30pct-filter", "excluded_percentile_bps": 3_000},
            ("daily_bars", "share_capital_facts"),
            "discovery_exhausted",
            "discovery_exhausted",
            "2026-08-07",
        ),
        (
            "trend-quality-v1",
            "trend-quality",
            "Trend quality",
            "A smoother trend path may distinguish persistent moves from unstable jumps.",
            {"arm_id": "v4-trend-quality"},
            ("daily_bars",),
            "discovery_exhausted",
            "discovery_exhausted",
            "2026-08-07",
        ),
        (
            "extreme-return-abnormal-turnover-v1",
            "attention-overreaction",
            "Extreme return and abnormal turnover",
            "Industry-relative price salience combined with unusual turnover.",
            {"facts": ["industry_relative_return_bps", "turnover_ratio_bps"]},
            ("daily_bars", "daily_basic", "industry_classification"),
            "exploratory",
            "new_discovery",
            None,
        ),
        (
            "downside-tail-liquidity-v1",
            "risk-liquidity",
            "Downside tail and liquidity instability",
            "Recent downside tails and unstable turnover may identify fragile signals.",
            {"facts": ["downside_semideviation_bps", "worst_overnight_gap_bps"]},
            ("daily_bars", "daily_basic"),
            "exploratory",
            "new_discovery",
            None,
        ),
        (
            "overnight-intraday-separation-v1",
            "market-microstructure",
            "Overnight and intraday return separation",
            "Separating gap and session returns may distinguish information from chasing.",
            {"facts": ["overnight_return_bps", "intraday_return_bps"]},
            ("daily_bars",),
            "exploratory",
            "new_discovery",
            None,
        ),
        (
            "earnings-price-point-in-time-v1",
            "fundamental-valuation",
            "Point-in-time earnings and price",
            "Announcement-visible earnings and valuation facts for future research.",
            {"mode": "point_in_time_data_preparation"},
            ("daily_basic", "fina_indicator"),
            "data_preparation",
            "new_discovery",
            None,
        ),
        (
            "profitability-quality-point-in-time-v1",
            "fundamental-quality",
            "Point-in-time profitability quality",
            "Announcement-visible profitability facts for future research.",
            {"mode": "point_in_time_data_preparation"},
            ("fina_indicator",),
            "data_preparation",
            "new_discovery",
            None,
        ),
    )
    return tuple(
        {
            "hypothesis_id": hypothesis_id,
            "family": family,
            "title": title,
            "mechanism": mechanism,
            "formula": formula,
            "data_requirements": list(requirements),
            "status": status,
            "sample_role": sample_role,
            "frozen_after": frozen_after,
            "registered_at": timestamp,
        }
        for (
            hypothesis_id,
            family,
            title,
            mechanism,
            formula,
            requirements,
            status,
            sample_role,
            frozen_after,
        ) in definitions
    )


_V4_DISCOVERY_ARMS = {
    "v4-breadth-five-day-median": "breadth-five-day-median-v1",
    "v4-breakout-overextension-cap": "breakout-overextension-cap-v1",
    "v4-no-recent-limit-up": "no-recent-limit-up-v1",
    "v4-signal-quality-rank": "signal-quality-rank-v1",
    "v4-size-bottom-30pct-filter": "size-bottom-30pct-filter-v1",
    "v4-trend-quality": "trend-quality-v1",
}


def v4_discovery_trials_from_diagnostic(
    diagnostic: Mapping[str, object], *, recorded_at: datetime
) -> tuple[dict[str, object], ...]:
    """Convert the frozen v4 diagnostic into six immutable lifetime trials."""

    if diagnostic.get("schema") != "v4-study-diagnostic-v1":
        raise ValueError("v4 discovery diagnostic schema is unsupported")
    source_study_id = str(diagnostic.get("source_study_id") or "")
    manifest_hash = str(diagnostic.get("manifest_hash") or "")
    source_result_hash = str(diagnostic.get("source_result_hash") or "")
    diagnostic_hash = str(diagnostic.get("diagnostic_hash") or "")
    arms = diagnostic.get("arms")
    if not source_study_id or not isinstance(arms, Mapping):
        raise ValueError("v4 discovery diagnostic is incomplete")
    for label, value in (
        ("manifest", manifest_hash),
        ("source result", source_result_hash),
        ("diagnostic", diagnostic_hash),
    ):
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError(f"v4 discovery {label} hash must be lowercase SHA-256")
    trials: list[dict[str, object]] = []
    for arm_id, hypothesis_id in _V4_DISCOVERY_ARMS.items():
        arm = arms.get(arm_id)
        if not isinstance(arm, Mapping):
            raise ValueError(f"v4 discovery diagnostic is missing {arm_id}")
        result = {
            "schema": "v4-discovery-trial-v1",
            "source_study_id": source_study_id,
            "source_result_hash": source_result_hash,
            "diagnostic_hash": diagnostic_hash,
            "arm_id": arm_id,
            "diagnostic": dict(arm),
        }
        trials.append(
            {
                "trial_id": f"{source_study_id}:{arm_id}",
                "hypothesis_id": hypothesis_id,
                "manifest_hash": manifest_hash,
                "sample_role": "discovery_exhausted",
                "status": "completed",
                "result": result,
                "result_hash": _hash(result),
                "created_at": recorded_at.isoformat(),
                "completed_at": recorded_at.isoformat(),
            }
        )
    return tuple(trials)


def extreme_return_abnormal_turnover_facts(
    *,
    current_return_bps: int,
    industry_return_bps: int,
    prior_turnover_bps: Sequence[int],
    current_turnover_bps: int,
) -> dict[str, int]:
    if not prior_turnover_bps:
        raise ValueError("prior turnover evidence is required")
    prior_median = Decimal(str(median(prior_turnover_bps)))
    if prior_median <= 0:
        raise ValueError("prior turnover median must be positive")
    relative = int(current_return_bps) - int(industry_return_bps)
    ratio = int(
        (Decimal(current_turnover_bps) * Decimal(10_000) / prior_median).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    return {
        "industry_relative_return_bps": relative,
        "absolute_salience_bps": abs(relative),
        "turnover_ratio_bps": ratio,
    }


def downside_tail_liquidity_facts(
    *,
    prior_returns_bps: Sequence[int],
    overnight_gaps_bps: Sequence[int],
    turnover_bps: Sequence[int],
) -> dict[str, int]:
    if not prior_returns_bps or not overnight_gaps_bps or not turnover_bps:
        raise ValueError("risk evidence sequences must be non-empty")
    downside_squares = [min(0, int(value)) ** 2 for value in prior_returns_bps]
    downside_semideviation = math.isqrt(sum(downside_squares) // len(downside_squares))
    turnover_median = Decimal(str(median(turnover_bps)))
    if turnover_median <= 0:
        raise ValueError("turnover median must be positive")
    dispersion = int(
        (
            Decimal(max(turnover_bps) - min(turnover_bps)) * Decimal(10_000) / turnover_median
        ).to_integral_value(rounding=ROUND_HALF_UP)
    )
    return {
        "worst_return_bps": min(int(value) for value in prior_returns_bps),
        "worst_overnight_gap_bps": min(int(value) for value in overnight_gaps_bps),
        "downside_semideviation_bps": downside_semideviation,
        "turnover_dispersion_bps": dispersion,
    }


def _return_bps(end: int, start: int) -> int:
    if start <= 0 or end <= 0:
        raise ValueError("price evidence must be positive")
    return int(
        ((Decimal(end) - Decimal(start)) * Decimal(10_000) / Decimal(start)).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )


def overnight_intraday_facts(
    *, pre_close_1e4: int, open_1e4: int, close_1e4: int
) -> dict[str, int]:
    return {
        "overnight_return_bps": _return_bps(open_1e4, pre_close_1e4),
        "intraday_return_bps": _return_bps(close_1e4, open_1e4),
    }


def build_research_forward_observation(
    *,
    hypothesis_id: str,
    symbol: str,
    trade_date: date,
    source_timestamp: datetime,
    raw_inputs: Mapping[str, object],
) -> dict[str, object]:
    """Build one immutable continuous-feature observation without selecting stocks."""

    _require_aware_timestamp(source_timestamp, field="source_timestamp")
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise ValueError("research observation symbol is required")
    normalized_inputs = {
        str(key): list(value) if isinstance(value, tuple) else value
        for key, value in raw_inputs.items()
    }
    if hypothesis_id == "no-recent-limit-up-v1":
        raw_touched = tuple(raw_inputs.get("prior_limit_up_touched", ()))
        if any(not isinstance(value, bool) for value in raw_touched):
            raise ValueError("prior limit-up evidence must contain only boolean values")
        touched = tuple(bool(value) for value in raw_touched)
        if len(touched) != 5:
            raise ValueError("no-recent-limit-up requires exactly five prior sessions")
        count = sum(touched)
        facts: dict[str, object] = {
            "recent_limit_up_days": count,
            "passes_no_recent_limit_up": count == 0,
        }
    elif hypothesis_id == "extreme-return-abnormal-turnover-v1":
        facts = extreme_return_abnormal_turnover_facts(
            current_return_bps=int(raw_inputs["current_return_bps"]),
            industry_return_bps=int(raw_inputs["industry_return_bps"]),
            prior_turnover_bps=tuple(int(value) for value in raw_inputs["prior_turnover_bps"]),
            current_turnover_bps=int(raw_inputs["current_turnover_bps"]),
        )
    elif hypothesis_id == "downside-tail-liquidity-v1":
        facts = downside_tail_liquidity_facts(
            prior_returns_bps=tuple(int(value) for value in raw_inputs["prior_returns_bps"]),
            overnight_gaps_bps=tuple(int(value) for value in raw_inputs["overnight_gaps_bps"]),
            turnover_bps=tuple(int(value) for value in raw_inputs["turnover_bps"]),
        )
    elif hypothesis_id == "overnight-intraday-separation-v1":
        facts = overnight_intraday_facts(
            pre_close_1e4=int(raw_inputs["pre_close_1e4"]),
            open_1e4=int(raw_inputs["open_1e4"]),
            close_1e4=int(raw_inputs["close_1e4"]),
        )
    else:
        raise ValueError("research hypothesis does not have a forward feature builder")
    input_payload = {
        "schema": "research-forward-input-v1",
        "hypothesis_id": hypothesis_id,
        "symbol": normalized_symbol,
        "trade_date": trade_date.isoformat(),
        "source_timestamp": source_timestamp.isoformat(),
        "raw_inputs": normalized_inputs,
    }
    result_payload = {
        "schema": "research-forward-result-v1",
        "hypothesis_id": hypothesis_id,
        "symbol": normalized_symbol,
        "trade_date": trade_date.isoformat(),
        "facts": facts,
    }
    return {
        "hypothesis_id": hypothesis_id,
        "trade_date": trade_date.isoformat(),
        "symbol": normalized_symbol,
        "input_hash": _hash(input_payload),
        "result_hash": _hash(result_payload),
        "observation": facts,
        "recorded_at": source_timestamp.isoformat(),
    }


def record_research_forward_observation(
    repository: object,
    *,
    hypothesis_id: str,
    symbol: str,
    trade_date: date,
    source_timestamp: datetime,
    raw_inputs: Mapping[str, object],
) -> dict[str, object]:
    """Build and immutably persist one forward feature observation."""

    observation = build_research_forward_observation(
        hypothesis_id=hypothesis_id,
        symbol=symbol,
        trade_date=trade_date,
        source_timestamp=source_timestamp,
        raw_inputs=raw_inputs,
    )
    save = getattr(repository, "save_research_forward_observation", None)
    if not callable(save):
        raise TypeError("repository does not support forward research observations")
    save(observation)
    return observation


def build_research_forward_outcomes(
    *,
    observation: Mapping[str, object],
    signal_close_1e4: int,
    future_bars: Sequence[DailyBar],
    benchmark_close_path_1e4: Sequence[int],
    recorded_at: datetime,
) -> tuple[dict[str, object], ...]:
    """Build immutable 5/10/20-session diagnostic outcomes for one observation."""

    _require_aware_timestamp(recorded_at, field="recorded_at")
    required = ("hypothesis_id", "trade_date", "symbol", "result_hash")
    if any(observation.get(field) in (None, "") for field in required):
        raise ValueError("forward observation identity is incomplete")
    result_hash = str(observation["result_hash"])
    if len(result_hash) != _HASH_PATTERN_LENGTH or any(
        character not in "0123456789abcdef" for character in result_hash
    ):
        raise ValueError("forward observation result hash is invalid")
    bars = tuple(future_bars)
    if len(bars) != 20:
        raise ValueError("forward outcomes require exactly twenty future sessions")
    symbol = str(observation["symbol"])
    signal_date = date.fromisoformat(str(observation["trade_date"]))
    dates = tuple(bar.trade_date for bar in bars)
    if dates != tuple(sorted(set(dates))) or dates[0] <= signal_date:
        raise ValueError("forward outcome dates must be unique and strictly after the signal")
    if any(bar.symbol != symbol for bar in bars) or len({bar.source for bar in bars}) != 1:
        raise ValueError("forward outcome bars must be one symbol and one source")
    if any(bar.source_timestamp > recorded_at for bar in bars):
        raise ValueError("forward outcome cannot be recorded before its source evidence")
    evidence_available_at = max(bar.source_timestamp for bar in bars)
    benchmark = tuple(int(value) for value in benchmark_close_path_1e4)
    if len(benchmark) != 21:
        raise ValueError("forward outcomes require a twenty-session benchmark path")
    if signal_close_1e4 <= 0 or any(value <= 0 for value in benchmark):
        raise ValueError("forward outcome prices must be positive")

    outcomes: list[dict[str, object]] = []
    for horizon in (5, 10, 20):
        exit_bar = bars[horizon - 1]
        gross = _return_bps(exit_bar.close_1e4, signal_close_1e4)
        benchmark_return = _return_bps(benchmark[horizon], benchmark[0])
        result = {
            "schema": "research-forward-outcome-v1",
            "path": "signal-close-diagnostic",
            "entry_date": signal_date.isoformat(),
            "exit_date": exit_bar.trade_date.isoformat(),
            "gross_return_bps": gross,
            "benchmark_return_bps": benchmark_return,
            "excess_return_bps": gross - benchmark_return,
            "source": exit_bar.source,
        }
        hash_payload = {
            "schema": "research-forward-outcome-hash-v1",
            "hypothesis_id": str(observation["hypothesis_id"]),
            "signal_date": signal_date.isoformat(),
            "symbol": symbol,
            "horizon_sessions": horizon,
            "observation_result_hash": result_hash,
            "outcome": result,
        }
        outcomes.append(
            {
                "hypothesis_id": str(observation["hypothesis_id"]),
                "signal_date": signal_date.isoformat(),
                "symbol": symbol,
                "horizon_sessions": horizon,
                "observation_result_hash": result_hash,
                "outcome": result,
                "outcome_hash": _hash(hash_payload),
                "recorded_at": evidence_available_at.isoformat(),
            }
        )
    return tuple(outcomes)


def build_price_research_forward_bundle(
    *,
    signal_bar: DailyBar,
    prior_limits: Sequence[DailyPriceLimit],
    future_bars: Sequence[DailyBar],
    benchmark_close_path_1e4: Sequence[int],
    recorded_at: datetime,
    hypothesis_ids: Sequence[str] = (
        "no-recent-limit-up-v1",
        "overnight-intraday-separation-v1",
    ),
) -> dict[str, tuple[dict[str, object], ...]]:
    """Build price-only research evidence without provider access or strategy mutation."""

    requested = tuple(dict.fromkeys(hypothesis_ids))
    supported = {
        "no-recent-limit-up-v1",
        "overnight-intraday-separation-v1",
    }
    if not requested or any(item not in supported for item in requested):
        raise ValueError("price research contains an unsupported hypothesis")
    observations: list[dict[str, object]] = []
    if "no-recent-limit-up-v1" in requested:
        limits = tuple(prior_limits)
        if len(limits) != 5:
            raise ValueError("price research requires exactly five prior price-limit sessions")
        limit_dates = tuple(item.trade_date for item in limits)
        if (
            limit_dates != tuple(sorted(set(limit_dates)))
            or limit_dates[-1] >= signal_bar.trade_date
        ):
            raise ValueError("price-limit sessions must be unique and precede the signal")
        if any(item.symbol != signal_bar.symbol for item in limits):
            raise ValueError("price-limit sessions must match the signal symbol")
        if any(item.policy_exception for item in limits):
            raise ValueError("price-limit policy exceptions cannot enter research evidence")
        algorithms = tuple(item.algorithm for item in limits)
        if len(set(algorithms)) != 1:
            raise ValueError("price-limit research evidence must use one algorithm")
        observations.append(
            build_research_forward_observation(
                hypothesis_id="no-recent-limit-up-v1",
                symbol=signal_bar.symbol,
                trade_date=signal_bar.trade_date,
                source_timestamp=signal_bar.source_timestamp,
                raw_inputs={
                    "prior_limit_up_touched": tuple(item.touched_up for item in limits),
                    "prior_trade_dates": tuple(item.trade_date.isoformat() for item in limits),
                    "price_limit_algorithm": algorithms[0],
                },
            )
        )
    if "overnight-intraday-separation-v1" in requested:
        observations.append(
            build_research_forward_observation(
                hypothesis_id="overnight-intraday-separation-v1",
                symbol=signal_bar.symbol,
                trade_date=signal_bar.trade_date,
                source_timestamp=signal_bar.source_timestamp,
                raw_inputs={
                    "pre_close_1e4": signal_bar.pre_close_1e4,
                    "open_1e4": signal_bar.open_1e4,
                    "close_1e4": signal_bar.close_1e4,
                    "source": signal_bar.source,
                },
            )
        )
    outcomes = tuple(
        outcome
        for observation in observations
        for outcome in build_research_forward_outcomes(
            observation=observation,
            signal_close_1e4=signal_bar.close_1e4,
            future_bars=future_bars,
            benchmark_close_path_1e4=benchmark_close_path_1e4,
            recorded_at=recorded_at,
        )
    )
    return {"observations": tuple(observations), "outcomes": outcomes}


def record_stored_price_research_bundle(
    repository: object,
    *,
    symbol: str,
    signal_date: date,
    through: date,
    source: str,
    recorded_at: datetime,
    hypothesis_ids: Sequence[str] = (
        "no-recent-limit-up-v1",
        "overnight-intraday-separation-v1",
    ),
) -> dict[str, int]:
    """Load an exact stored calendar path and atomically persist mature price evidence."""

    if through <= signal_date:
        raise ValueError("stored price research requires dates after the signal")
    load_days = getattr(repository, "load_expected_trading_days", None)
    load_history = getattr(repository, "load_symbol_history", None)
    load_snapshot = getattr(repository, "load_market_snapshot", None)
    load_limits = getattr(repository, "load_daily_price_limits", None)
    save_bundle = getattr(repository, "save_research_forward_bundle", None)
    if not all(
        callable(item)
        for item in (load_days, load_history, load_snapshot, load_limits, save_bundle)
    ):
        raise TypeError("repository does not support stored price research")
    sessions = tuple(load_days(signal_date - timedelta(days=120), through, source=source))
    if signal_date not in sessions:
        raise ValueError("signal date is absent from the stored trading calendar")
    signal_index = sessions.index(signal_date)
    prior_dates = sessions[max(0, signal_index - 5) : signal_index]
    future_dates = sessions[signal_index + 1 : signal_index + 21]
    if len(prior_dates) != 5 or len(future_dates) != 20:
        raise ValueError("stored price research requires five prior and twenty future sessions")
    history = tuple(load_history(symbol, end_date=future_dates[-1], source=source, limit=26))
    by_date = {bar.trade_date: bar for bar in history}
    required_bar_dates = (signal_date, *future_dates)
    if any(day not in by_date for day in required_bar_dates):
        raise ValueError("stored price research has an incomplete same-symbol price path")
    signal_bar = by_date[signal_date]
    future_bars = tuple(by_date[day] for day in future_dates)
    if any(bar.source != source for bar in (signal_bar, *future_bars)):
        raise ValueError("stored price research has mixed price sources")

    prior_limits: list[DailyPriceLimit] = []
    if "no-recent-limit-up-v1" in hypothesis_ids:
        for day in prior_dates:
            facts = load_limits(day, source=source)
            fact = facts.get(symbol)
            if not isinstance(fact, Mapping):
                raise ValueError("stored price research has incomplete prior price-limit evidence")
            prior_limits.append(
                DailyPriceLimit(
                    symbol=symbol,
                    trade_date=day,
                    up_limit_1e4=int(fact["limit_up_1e4"]),
                    down_limit_1e4=int(fact["limit_down_1e4"]),
                    touched_up=bool(fact["touched_up"]),
                    touched_down=bool(fact["touched_down"]),
                    policy_exception=bool(fact["policy_exception"]),
                    algorithm=str(fact["algorithm"]),
                )
            )

    benchmark_path = [100_000]
    for day in future_dates:
        snapshot = load_snapshot(day, source=source, history_limit=1)
        security_by_symbol = {item.symbol: item for item in snapshot.securities}
        eligible_bars = tuple(
            bar
            for bar in snapshot.bars
            if bar.trade_date == day
            and security_by_symbol.get(bar.symbol) is not None
            and security_by_symbol[bar.symbol].board == "MAIN"
            and not security_by_symbol[bar.symbol].is_st
        )
        if not eligible_bars:
            raise ValueError("stored price research benchmark is incomplete")
        daily_return = int(
            (
                sum(Decimal(_return_bps(bar.close_1e4, bar.pre_close_1e4)) for bar in eligible_bars)
                / Decimal(len(eligible_bars))
            ).to_integral_value(rounding=ROUND_HALF_UP)
        )
        benchmark_path.append(
            int(
                (
                    Decimal(benchmark_path[-1]) * Decimal(10_000 + daily_return) / Decimal(10_000)
                ).to_integral_value(rounding=ROUND_HALF_UP)
            )
        )

    bundle = build_price_research_forward_bundle(
        signal_bar=signal_bar,
        prior_limits=prior_limits,
        future_bars=future_bars,
        benchmark_close_path_1e4=benchmark_path,
        recorded_at=recorded_at,
        hypothesis_ids=hypothesis_ids,
    )
    return save_bundle(observations=bundle["observations"], outcomes=bundle["outcomes"])


def normalize_tushare_daily_basic(
    row: Mapping[str, object], *, source_timestamp: datetime
) -> dict[str, object]:
    symbol = str(row.get("ts_code") or "").strip().upper()
    if not symbol:
        raise ValueError("daily_basic ts_code is required")
    visible_date = _iso_date(row.get("trade_date"), field="trade_date")
    payload = {
        field: _decimal_text(row.get(field))
        for field in ("turnover_rate_f", "pe_ttm", "pb", "float_share", "circ_mv")
    }
    payload = {key: value for key, value in payload.items() if value is not None}
    result = {
        "symbol": symbol,
        "interface": "daily_basic",
        "report_period": visible_date,
        "visible_date": visible_date,
        "revision_key": visible_date.replace("-", ""),
        "source": "tushare",
        "payload": payload,
        "source_timestamp": source_timestamp.isoformat(),
    }
    result["payload_hash"] = _hash(payload)
    return result


def normalize_tushare_fina_indicator(
    row: Mapping[str, object], *, source_timestamp: datetime
) -> dict[str, object]:
    symbol = str(row.get("ts_code") or "").strip().upper()
    if not symbol:
        raise ValueError("fina_indicator ts_code is required")
    visible_date = _iso_date(
        row.get("f_ann_date") or row.get("ann_date"), field="announcement date"
    )
    report_period = _iso_date(row.get("end_date"), field="end_date")
    payload = {field: _decimal_text(row.get(field)) for field in ("roe", "roa", "gross_margin")}
    update_flag = str(row.get("update_flag") or "").strip()
    if update_flag:
        payload["update_flag"] = update_flag
    payload = {key: value for key, value in payload.items() if value is not None}
    result = {
        "symbol": symbol,
        "interface": "fina_indicator",
        "report_period": report_period,
        "visible_date": visible_date,
        "revision_key": f"{visible_date.replace('-', '')}|{update_flag or '0'}",
        "source": "tushare",
        "payload": payload,
        "source_timestamp": source_timestamp.isoformat(),
    }
    result["payload_hash"] = _hash(payload)
    return result


def ingest_point_in_time_research_batch(
    repository: object,
    *,
    as_of: date,
    source_timestamp: datetime,
    daily_basic_rows: Sequence[Mapping[str, object]] = (),
    fina_indicator_rows: Sequence[Mapping[str, object]] = (),
) -> dict[str, int]:
    """Validate a complete caller-supplied provider batch before one repository write."""

    _require_aware_timestamp(source_timestamp, field="source_timestamp")
    daily = tuple(
        normalize_tushare_daily_basic(row, source_timestamp=source_timestamp)
        for row in daily_basic_rows
    )
    financial = tuple(
        normalize_tushare_fina_indicator(row, source_timestamp=source_timestamp)
        for row in fina_indicator_rows
    )
    if any(str(item["visible_date"]) != as_of.isoformat() for item in daily):
        raise ValueError("daily_basic batch contains a different or future trade date")
    if any(str(item["visible_date"]) > as_of.isoformat() for item in financial):
        raise ValueError("fina_indicator batch contains future announcement evidence")
    records = (*daily, *financial)
    save = getattr(repository, "save_point_in_time_fundamentals", None)
    if not callable(save):
        raise TypeError("repository does not support point-in-time fundamentals")
    saved = int(save(records))
    return {
        "daily_basic": len(daily),
        "fina_indicator": len(financial),
        "saved": saved,
    }


def point_in_time_research_facts(
    repository: object, *, symbol: str, as_of: date
) -> dict[str, object]:
    """Build a non-imputed valuation/profitability view as it was visible on a date."""

    load = getattr(repository, "load_point_in_time_fundamentals", None)
    if not callable(load):
        raise TypeError("repository does not support point-in-time fundamentals")
    records = tuple(load(symbol=symbol, as_of=as_of))

    def day_text(value: object) -> str:
        return value.isoformat() if isinstance(value, date) else str(value)

    daily = tuple(
        item
        for item in records
        if item.get("interface") == "daily_basic"
        and day_text(item.get("report_period")) == as_of.isoformat()
    )
    financial = tuple(item for item in records if item.get("interface") == "fina_indicator")
    selected_daily = max(daily, key=lambda item: day_text(item.get("visible_date")), default=None)
    selected_financial = max(
        financial,
        key=lambda item: (
            day_text(item.get("report_period")),
            day_text(item.get("visible_date")),
            str(item.get("revision_key")),
        ),
        default=None,
    )
    valuation = (
        {}
        if selected_daily is None
        else {
            key: value
            for key, value in dict(selected_daily.get("payload", {})).items()
            if key in {"turnover_rate_f", "pe_ttm", "pb", "float_share", "circ_mv"}
        }
    )
    profitability = (
        {}
        if selected_financial is None
        else {
            key: value
            for key, value in dict(selected_financial.get("payload", {})).items()
            if key in {"roe", "roa", "gross_margin", "update_flag"}
        }
    )
    result: dict[str, object] = {
        "schema": "point-in-time-research-facts-v1",
        "symbol": symbol,
        "as_of": as_of.isoformat(),
        "coverage_status": "complete" if valuation and profitability else "partial",
        "valuation": valuation,
        "valuation_visible_date": (
            None if selected_daily is None else day_text(selected_daily.get("visible_date"))
        ),
        "profitability": profitability,
        "profitability_visible_date": (
            None if selected_financial is None else day_text(selected_financial.get("visible_date"))
        ),
    }
    result["facts_hash"] = _hash(result)
    return result


def _circular_blocks(values: Sequence[float], *, block: int, rng: random.Random) -> list[float]:
    count = len(values)
    sampled: list[float] = []
    while len(sampled) < count:
        start = rng.randrange(count)
        sampled.extend(values[(start + offset) % count] for offset in range(block))
    return sampled[:count]


def evaluate_lifetime_research_statistics(
    *,
    manifest_hash: str,
    baseline: Sequence[int | float],
    challengers: Mapping[str, Sequence[int | float]],
    lifetime_trial_count: int,
    block_sessions: int = 20,
    bootstrap_samples: int = 10_000,
) -> dict[str, object]:
    """Compute deterministic White and Romano-Wolf evidence for paired daily deltas."""

    if len(manifest_hash) != _HASH_PATTERN_LENGTH:
        raise ValueError("manifest hash must be SHA-256")
    if not baseline or block_sessions < 1 or bootstrap_samples < 1:
        raise ValueError("statistics require observations and positive bootstrap settings")
    if lifetime_trial_count < len(challengers):
        raise ValueError("lifetime trial count cannot omit current trials")
    count = len(baseline)
    deltas: dict[str, tuple[float, ...]] = {}
    for arm_id, observations in sorted(challengers.items()):
        if len(observations) != count:
            raise ValueError("paired research series must have equal lengths")
        deltas[arm_id] = tuple(
            float(value) - float(base) for value, base in zip(observations, baseline, strict=True)
        )
    observed = {arm: sum(values) / count for arm, values in deltas.items()}
    centered = {
        arm: tuple(value - observed[arm] for value in values) for arm, values in deltas.items()
    }
    rng = random.Random(
        int(hashlib.sha256(f"{manifest_hash}|v5-statistics-v1".encode()).hexdigest(), 16)
    )
    bootstrap: list[dict[str, float]] = []
    for _ in range(bootstrap_samples):
        # One set of circular indices preserves cross-arm dependence.
        indexes = _circular_blocks(tuple(range(count)), block=block_sessions, rng=rng)
        bootstrap.append(
            {
                arm: sum(values[index] for index in indexes) / count
                for arm, values in centered.items()
            }
        )
    maximums = [max(sample.values(), default=0.0) for sample in bootstrap]
    white_p = (
        sum(value >= max(observed.values(), default=0.0) for value in maximums) / bootstrap_samples
    )
    ordered = sorted(observed, key=lambda arm: (-observed[arm], arm))
    adjusted: dict[str, float] = {}
    running = 0.0
    for position, arm in enumerate(ordered):
        remaining = ordered[position:]
        exceed = (
            sum(max(sample[item] for item in remaining) >= observed[arm] for sample in bootstrap)
            / bootstrap_samples
        )
        running = max(running, exceed)
        adjusted[arm] = running
    return {
        "schema": "v5-statistics-v1",
        "manifest_hash": manifest_hash,
        "lifetime_trial_count": lifetime_trial_count,
        "observed_mean_paired_delta_bps": observed,
        "family_test": {"method": "white_reality_check", "p_value": white_p},
        "stepdown_test": {
            "method": "romano_wolf_stepdown",
            "adjusted_p_values": adjusted,
        },
        "bootstrap": {
            "method": "circular_moving_block",
            "block_sessions": block_sessions,
            "samples": bootstrap_samples,
        },
    }
