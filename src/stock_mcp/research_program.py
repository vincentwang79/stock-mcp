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
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from statistics import median

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
