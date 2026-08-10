"""Deterministic offline outcome evidence for v3 replay candidates."""

from __future__ import annotations

import hashlib
import json
import operator
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from fractions import Fraction

OUTCOME_HASH_SCHEMA = "v3-outcome-v1"
_HORIZONS = (5, 10, 20)
_CONDITION = re.compile(r"close\s*(>=|<=|>|<|==)\s*([1-9][0-9]*)\Z")
_OPERATORS: dict[str, Callable[[int, int], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
}


def evaluate_v3_candidate_outcomes(
    *,
    candidates: Sequence[Mapping[str, object]],
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, object]]],
    equal_weight_mainboard_bars: Sequence[Mapping[str, object]],
    as_of: date,
) -> dict[str, dict[str, object]]:
    """Evaluate candidates against a fixed, point-in-time post-date window.

    The equal-weight main-board series supplies the trading-session calendar and
    benchmark. Its first post-date ``pre_close_1e4`` is the candidate-date
    benchmark close. The candidate series uses the same convention, avoiding
    any need to infer a starting price from strategy conditions.
    """

    if not isinstance(as_of, date):
        raise ValueError("as_of must be a date")
    result: dict[str, dict[str, object]] = {}
    for candidate in candidates:
        candidate_id = _required_text(candidate, "candidate_id")
        if candidate_id in result:
            raise ValueError(f"duplicate candidate_id: {candidate_id}")
        symbol = _required_text(candidate, "symbol")
        candidate_date = _as_date(candidate.get("trade_date"), field="candidate trade_date")
        if candidate_date > as_of:
            raise ValueError("candidate trade_date must not be later than as_of")

        benchmark = _post_date_bars(
            equal_weight_mainboard_bars,
            after=candidate_date,
            through=as_of,
            label="equal-weight main-board",
        )
        session_window = benchmark[:20]
        window_dates = {item["trade_date"] for item in session_window}
        candidate_bars = _post_date_bars(
            bars_by_symbol.get(symbol, ()),
            after=candidate_date,
            through=as_of,
            label=symbol,
        )
        candidate_window = tuple(
            item for item in candidate_bars if item["trade_date"] in window_dates
        )
        result[candidate_id] = _evaluate_one(candidate, candidate_window, session_window)
    return result


def attach_v3_outcome_hash(
    job: Mapping[str, object], outcomes: Mapping[str, object]
) -> dict[str, object]:
    """Return a copy of a replay job with a separate immutable outcome hash."""

    schema = job.get("outcome_hash_schema")
    if schema not in (None, OUTCOME_HASH_SCHEMA):
        raise ValueError("unsupported outcome hash schema")
    outcome_hash = _canonical_outcome_hash(outcomes)
    existing = job.get("outcome_hash")
    if existing is not None and existing != outcome_hash:
        raise ValueError("outcome hash is immutable once attached")
    updated = dict(job)
    updated["outcome_hash_schema"] = OUTCOME_HASH_SCHEMA
    updated["outcome_hash"] = outcome_hash
    return updated


def _evaluate_one(
    candidate: Mapping[str, object],
    candidate_bars: Sequence[Mapping[str, int | date]],
    benchmark_bars: Sequence[Mapping[str, int | date]],
) -> dict[str, object]:
    candidate_path = _adjusted_path(candidate_bars)
    benchmark_path = _adjusted_path(benchmark_bars)
    same_complete_calendar = (
        len(candidate_path) == 20
        and len(benchmark_path) == 20
        and tuple(bar["trade_date"] for bar in candidate_path)
        == tuple(bar["trade_date"] for bar in benchmark_path)
    )
    evidence: dict[str, object] = {
        "availability": (
            "complete" if same_complete_calendar else "partial" if candidate_path and benchmark_path
            else "unavailable"
        ),
        "path_status": "unavailable" if not candidate_path else "pending",
        "first_confirmation_date": None,
        "first_invalidation_date": None,
    }
    for horizon in _HORIZONS:
        evidence[f"return_{horizon}d_bps"] = None
        evidence[f"benchmark_return_{horizon}d_bps"] = None
        evidence[f"excess_return_{horizon}d_bps"] = None
    evidence["mfe_20d_bps"] = None
    evidence["mae_20d_bps"] = None

    if not benchmark_path or not candidate_path:
        return evidence

    confirmation = _parse_condition(candidate.get("confirmation_condition"), "confirmation")
    invalidation = _parse_condition(candidate.get("invalidation_condition"), "invalidation")
    for bar in candidate_path:
        close = bar["close_1e4"]
        bar_date = bar["trade_date"]
        assert isinstance(bar_date, date)
        if evidence["first_confirmation_date"] is None and confirmation(close):
            evidence["first_confirmation_date"] = bar_date.isoformat()
        if evidence["first_invalidation_date"] is None and invalidation(close):
            evidence["first_invalidation_date"] = bar_date.isoformat()

    if evidence["first_invalidation_date"] is not None:
        evidence["path_status"] = "invalidated"
    elif evidence["first_confirmation_date"] is not None:
        evidence["path_status"] = "confirmed"

    candidate_base = Fraction(_bar_price(candidate_bars[0], "pre_close_1e4"))
    benchmark_base = Fraction(_bar_price(benchmark_bars[0], "pre_close_1e4"))
    candidate_by_date = {bar["trade_date"]: bar for bar in candidate_path}
    for horizon in _HORIZONS:
        if len(benchmark_path) < horizon:
            continue
        benchmark_endpoint = benchmark_path[horizon - 1]
        endpoint_date = benchmark_endpoint["trade_date"]
        candidate_endpoint = candidate_by_date.get(endpoint_date)
        if candidate_endpoint is None:
            continue
        candidate_return = _return_bps(candidate_endpoint["close_1e4"], candidate_base)
        benchmark_return = _return_bps(benchmark_endpoint["close_1e4"], benchmark_base)
        evidence[f"return_{horizon}d_bps"] = candidate_return
        evidence[f"benchmark_return_{horizon}d_bps"] = benchmark_return
        evidence[f"excess_return_{horizon}d_bps"] = candidate_return - benchmark_return

    evidence["mfe_20d_bps"] = max(
        _return_bps(bar["high_1e4"], candidate_base) for bar in candidate_path
    )
    evidence["mae_20d_bps"] = min(
        _return_bps(bar["low_1e4"], candidate_base) for bar in candidate_path
    )
    return evidence


def _adjusted_path(
    bars: Sequence[Mapping[str, int | date]],
) -> tuple[dict[str, date | Fraction], ...]:
    if not bars:
        return ()
    previous_close = Fraction(_bar_price(bars[0], "pre_close_1e4"))
    result: list[dict[str, date | Fraction]] = []
    for bar in bars:
        pre_close = Fraction(_bar_price(bar, "pre_close_1e4"))
        adjusted_close = previous_close * _bar_price(bar, "close_1e4") / pre_close
        result.append(
            {
                "trade_date": _bar_date(bar),
                "close_1e4": adjusted_close,
                "high_1e4": previous_close * _bar_price(bar, "high_1e4") / pre_close,
                "low_1e4": previous_close * _bar_price(bar, "low_1e4") / pre_close,
            }
        )
        previous_close = adjusted_close
    return tuple(result)


def _post_date_bars(
    bars: Sequence[Mapping[str, object]],
    *,
    after: date,
    through: date,
    label: str,
) -> tuple[dict[str, int | date], ...]:
    selected: list[dict[str, int | date]] = []
    for raw in bars:
        bar_date = _as_date(raw.get("trade_date"), field=f"{label} bar trade_date")
        if not after < bar_date <= through:
            continue
        selected.append(
            {
                "trade_date": bar_date,
                **{
                    field: _positive_int(raw.get(field), field=f"{label} bar {field}")
                    for field in ("close_1e4", "high_1e4", "low_1e4")
                },
                "pre_close_1e4": _positive_int(
                    raw.get("pre_close_1e4"), field=f"{label} bar pre_close_1e4"
                ),
            }
        )
    selected.sort(key=_bar_date)
    dates = tuple(_bar_date(bar) for bar in selected)
    if len(dates) != len(set(dates)):
        raise ValueError(f"{label} bars must have unique trade dates")
    return tuple(selected)


def _parse_condition(value: object, label: str) -> Callable[[int], bool]:
    if not isinstance(value, str):
        raise ValueError(f"{label} condition must be a string")
    match = _CONDITION.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"unsupported {label} condition")
    comparison, threshold = match.groups()
    operation = _OPERATORS[comparison]
    threshold_value = int(threshold)
    return lambda close: operation(close, threshold_value)


def _canonical_outcome_hash(outcomes: Mapping[str, object]) -> str:
    payload = {"schema": OUTCOME_HASH_SCHEMA, "outcomes": _jsonable(outcomes)}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        ordered = sorted(value.items(), key=lambda pair: str(pair[0]))
        return {str(key): _jsonable(item) for key, item in ordered}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ValueError(f"outcome contains unsupported value: {type(value).__name__}")


def _required_text(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"candidate {field} must be a non-empty string")
    return item


def _as_date(value: object, *, field: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{field} must be an ISO date") from error
    raise ValueError(f"{field} must be a date")


def _positive_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _bar_date(bar: Mapping[str, int | date]) -> date:
    value = bar["trade_date"]
    if not isinstance(value, date):  # pragma: no cover - normalized internally
        raise TypeError("normalized bar date is invalid")
    return value


def _bar_price(bar: Mapping[str, int | date], field: str) -> int:
    value = bar.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _return_bps(value: int | Fraction, base: int | Fraction) -> int:
    ratio = (Fraction(value) - Fraction(base)) * 10_000 / Fraction(base)
    return ratio.numerator // ratio.denominator
