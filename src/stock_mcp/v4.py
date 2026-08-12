"""Preregistered, single-factor v4 research transformations.

This module never mutates v3 candidates.  It projects a frozen v0.3-policy-1
daily result into one named challenger using only point-in-time v4 features.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections.abc import Mapping, Sequence

from .domain import (
    DailyReview,
    SetupType,
    StrategyVersion,
    V3BreadthFacts,
    V3MarketInput,
    V3SecurityInput,
)
from .v3 import _generate_v3_daily_review, _SetupMetrics

PIPELINE_VERSION = "pipeline-v0.3"
INPUT_HASH_SCHEMA = "v4-input-v1"
RESULT_HASH_SCHEMA = "v4-result-v1"
MANIFEST_SCHEMA = "v4-manifest-v1"
OUTCOME_HASH_SCHEMA = "v4-outcome-v2"
BENCHMARK_SCHEMA = "v4-benchmark-v1"
STATISTICS_SCHEMA = "v4-statistics-v1"

CHALLENGERS = (
    "trend-quality",
    "breakout-overextension-cap",
    "no-recent-limit-up",
    "breadth-five-day-median",
    "size-bottom-30pct-filter",
    "signal-quality-rank",
)


def generate_v4_daily_review(
    *,
    market: V3MarketInput,
    strategy: StrategyVersion,
    prior_four_breadth: tuple[V3BreadthFacts, ...],
    features: Mapping[str, Mapping[str, object]],
    arm_id: str,
) -> DailyReview:
    """Evaluate one preregistered arm on the complete point-in-time v3 pool.

    Eligibility filters and signal-quality ranking are applied before the
    per-setup quota.  This is intentionally different from projecting the
    already selected baseline candidates, which could not replenish a slot.
    """

    normalized_arm = _normalized_arm(arm_id)
    breadth_override = None
    if normalized_arm == "breadth-five-day-median":
        if len(prior_four_breadth) != 4:
            raise ValueError("v4 breadth arm requires exactly four prior breadth facts")
        facts = (*prior_four_breadth, market.breadth)
        advance = int(statistics.median(item.advance_ratio_bps for item in facts))
        above = int(statistics.median(item.above_ma20_ratio_bps for item in facts))
        breadth_override = V3BreadthFacts(
            advance_count=advance * market.breadth.eligible_count // 10_000,
            eligible_count=market.breadth.eligible_count,
            above_ma20_count=above * market.breadth.ma20_eligible_count // 10_000,
            ma20_eligible_count=market.breadth.ma20_eligible_count,
            advance_ratio_bps=advance,
            above_ma20_ratio_bps=above,
        )

    def include(item: V3SecurityInput, metrics: _SetupMetrics) -> bool:
        if normalized_arm in {"baseline", "breadth-five-day-median", "signal-quality-rank"}:
            return True
        symbol = item.security.symbol
        facts = features.get(symbol)
        if facts is None:
            return False
        if normalized_arm == "trend-quality":
            return int(facts.get("return_20d_bps", -1)) > 0 and bool(facts.get("ma20_rising_5d"))
        if normalized_arm == "breakout-overextension-cap":
            return (
                metrics.setup_type is not SetupType.VOLUME_BREAKOUT
                or int(facts.get("breakout_overextension_bps", 10_001)) <= 200
            )
        if normalized_arm == "no-recent-limit-up":
            return int(facts.get("prior_20_touched_up_count", -1)) == 0
        if normalized_arm == "size-bottom-30pct-filter":
            return int(facts.get("market_cap_percentile_bps", -1)) >= 3_000
        raise AssertionError("unreachable v4 arm")

    ranking = None
    if normalized_arm == "signal-quality-rank":
        ranking = {
            symbol: (4 * int(facts.get("primary_percentile_bps", -1)))
            + (3 * int(facts.get("amount_percentile_bps", -1)))
            for symbol, facts in features.items()
            if int(facts.get("primary_percentile_bps", -1)) >= 0
            and int(facts.get("amount_percentile_bps", -1)) >= 0
        }
    return _generate_v3_daily_review(
        market,
        strategy,
        include=include,
        ranking_bps=ranking,
        breadth_override=breadth_override,
    )


def _normalized_arm(arm_id: str) -> str:
    normalized = (
        "baseline" if arm_id in {"baseline", "v0.3-policy-1"} else arm_id.removeprefix("v4-")
    )
    if normalized not in CHALLENGERS and normalized != "baseline":
        raise ValueError("v4 arm is not preregistered")
    return normalized


def apply_v4_arm(
    *,
    arm_id: str,
    candidates: Sequence[Mapping[str, object]],
    features: Mapping[str, Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    normalized_arm = _normalized_arm(arm_id)
    projected: list[dict[str, object]] = []
    for candidate in candidates:
        item = dict(candidate)
        symbol = str(item.get("symbol", ""))
        facts = features.get(symbol)
        if normalized_arm == "baseline":
            projected.append(item)
            continue
        if facts is None:
            continue
        keep = True
        if normalized_arm == "trend-quality":
            keep = int(facts.get("return_20d_bps", -1)) > 0 and bool(facts.get("ma20_rising_5d"))
        elif normalized_arm == "breakout-overextension-cap":
            keep = (
                item.get("setup_type") != "volume_breakout"
                or int(facts.get("breakout_overextension_bps", 10_001)) <= 200
            )
        elif normalized_arm == "no-recent-limit-up":
            keep = int(facts.get("prior_20_touched_up_count", -1)) == 0
        elif normalized_arm == "breadth-five-day-median":
            keep = bool(facts.get("five_day_breadth_complete"))
        elif normalized_arm == "size-bottom-30pct-filter":
            keep = int(facts.get("market_cap_percentile_bps", -1)) >= 3_000
        elif normalized_arm == "signal-quality-rank":
            primary = int(facts.get("primary_percentile_bps", -1))
            amount = int(facts.get("amount_percentile_bps", -1))
            keep = primary >= 0 and amount >= 0
            if keep:
                item["score"] = (4 * primary + 3 * amount) // 700
        if keep:
            projected.append(item)
    projected.sort(key=lambda item: (-int(item.get("score", 0)), str(item.get("symbol", ""))))
    return tuple({**item, "rank": rank} for rank, item in enumerate(projected[:3], 1))


def canonical_v4_result_hash(
    *,
    manifest_hash: str,
    arm_id: str,
    signal_date: str,
    candidates: Sequence[Mapping[str, object]],
) -> str:
    payload = {
        "schema": RESULT_HASH_SCHEMA,
        "manifest_hash": manifest_hash,
        "arm_id": arm_id,
        "signal_date": signal_date,
        "candidates": [dict(item) for item in candidates],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
