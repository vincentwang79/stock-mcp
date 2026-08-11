"""Preregistered, single-factor v4 research transformations.

This module never mutates v3 candidates.  It projects a frozen v0.3-policy-1
daily result into one named challenger using only point-in-time v4 features.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

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


def apply_v4_arm(
    *,
    arm_id: str,
    candidates: Sequence[Mapping[str, object]],
    features: Mapping[str, Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    normalized_arm = (
        "baseline" if arm_id in {"baseline", "v0.3-policy-1"} else arm_id.removeprefix("v4-")
    )
    if normalized_arm not in CHALLENGERS and normalized_arm != "baseline":
        raise ValueError("v4 arm is not preregistered")
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
