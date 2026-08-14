"""Executable, event-ordered v4 candidate outcome evidence."""

from __future__ import annotations

import hashlib
import json
import operator
import re
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from fractions import Fraction

OUTCOME_HASH_SCHEMA = "v4-outcome-v2"
BENCHMARK_SCHEMA = "v4-benchmark-v1"
_CONDITION = re.compile(r"close\s*(>=|<=|>|<)\s*([1-9][0-9]*)\Z")
_OPS: dict[str, Callable[[object, object], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
}


def evaluate_v4_candidate_outcomes(
    *,
    candidates: Sequence[Mapping[str, object]],
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, object]]],
    status_by_symbol: Mapping[str, Mapping[str, int]],
    mainboard_bars: Sequence[Mapping[str, object]],
    source: str,
    as_of: date,
) -> dict[str, dict[str, object]]:
    if any(str(row.get("source", source)) != source for row in mainboard_bars):
        raise ValueError("v4 benchmark cannot mix price sources")
    calendar = tuple(sorted({_as_date(row["trade_date"]) for row in mainboard_bars}))
    mainboard_by_symbol: dict[str, list[dict[str, object]]] = {}
    for raw_row in mainboard_bars:
        symbol = str(raw_row.get("symbol", ""))
        if symbol:
            mainboard_by_symbol.setdefault(symbol, []).append(dict(raw_row))
    for rows in mainboard_by_symbol.values():
        rows.sort(key=lambda row: _as_date(row["trade_date"]))
        adjusted = _adjusted_price_rows(rows)
        rows[:] = adjusted
    results: dict[str, dict[str, object]] = {}
    for candidate in candidates:
        candidate_id = _text(candidate, "candidate_id")
        symbol = _text(candidate, "symbol")
        signal_date = _as_date(candidate["trade_date"])
        if signal_date > as_of or candidate_id in results:
            raise ValueError("candidate identity/date is invalid")
        raw = [dict(row) for row in bars_by_symbol.get(symbol, ())]
        if any(str(row.get("source", source)) != source for row in raw):
            raise ValueError("v4 outcome cannot mix price sources")
        raw.sort(key=lambda row: _as_date(row["trade_date"]))
        raw = _adjusted_price_rows(raw, signal_close=candidate.get("signal_close_1e4"))
        post = [row for row in raw if signal_date < _as_date(row["trade_date"]) <= as_of]
        expected_dates = tuple(day for day in calendar if signal_date < day <= as_of)[:25]
        recorded_dates = tuple(_as_date(row["trade_date"]) for row in post[:25])
        calendar_complete = recorded_dates == expected_dates
        statuses = status_by_symbol.get(symbol, {})
        confirmation = _condition(candidate.get("confirmation_condition"))
        invalidation = _condition(candidate.get("invalidation_condition"))
        events: list[tuple[date, bool, bool]] = []
        for row in post[:5]:
            day = _as_date(row["trade_date"])
            close = _economic_price(row, "close")
            events.append((day, confirmation(close), invalidation(close)))
        confirmed_entry: date | None = None
        confirmed_status = "expired"
        event_date: date | None = None
        for day, is_confirmation, is_invalidation in events:
            if is_confirmation and is_invalidation:
                confirmed_status, event_date = "ambiguous", day
                break
            if is_invalidation:
                confirmed_status, event_date = "invalidated_before_entry", day
                break
            if is_confirmation:
                confirmed_status, event_date = "confirmed", day
                confirmed_entry = _next_executable_date(post, statuses, after=day)
                if confirmed_entry is None:
                    confirmed_status = "unavailable"
                break
        next_entry = _next_executable_date(post, statuses, after=signal_date)
        signal_path = _path(
            post,
            signal_date,
            costs=(10, 25, 50),
            expected_horizon_sessions=len(expected_dates),
        )
        next_path = _path(
            post,
            next_entry,
            costs=(10, 25, 50),
            entry_open=True,
            expected_horizon_sessions=len(expected_dates),
        )
        next_path = _terminalize_unexecutable_path(
            next_path,
            observed_rows=post,
            expected_dates=expected_dates,
        )
        confirmed_path = _path(
            post,
            confirmed_entry,
            costs=(10, 25, 50),
            entry_open=True,
            expected_horizon_sessions=len(expected_dates),
        )
        confirmed_path = _terminalize_unexecutable_path(
            confirmed_path,
            observed_rows=post,
            expected_dates=expected_dates,
        )
        confirmed_terminal_reason = None
        first_late_executable_date = None
        if confirmed_path["status"] == "unexecutable" and confirmed_entry is not None:
            first_late_executable_date = confirmed_entry
            confirmed_entry = None
            confirmed_terminal_reason = "entry_expired_before_20_session_horizon"
        market_cap = candidate.get("market_cap_fen")
        signal_path["benchmark"] = _benchmark_path(
            mainboard_by_symbol,
            calendar=calendar,
            signal_date=signal_date,
            entry_date=signal_date,
            entry_open=False,
            candidate_market_cap=market_cap,
        )
        next_path["benchmark"] = _benchmark_path(
            mainboard_by_symbol,
            calendar=calendar,
            signal_date=signal_date,
            entry_date=(next_entry if next_entry is not None else expected_dates[0]),
            entry_open=True,
            candidate_market_cap=market_cap,
        )
        confirmed_path["benchmark"] = _benchmark_path(
            mainboard_by_symbol,
            calendar=calendar,
            signal_date=signal_date,
            entry_date=confirmed_entry,
            entry_open=True,
            candidate_market_cap=market_cap,
        )
        for path in (signal_path, next_path, confirmed_path):
            _attach_market_cap_excess(path)
        results[candidate_id] = {
            "schema": OUTCOME_HASH_SCHEMA,
            "source": source,
            "signal_close_path": signal_path,
            "next_open_path": next_path,
            "confirmed_next_open_path": {
                **confirmed_path,
                "status": confirmed_status,
                "execution_status": confirmed_path["status"],
                "event_date": None if event_date is None else event_date.isoformat(),
                "entry_date": None if confirmed_entry is None else confirmed_entry.isoformat(),
                "execution_terminal_reason": confirmed_terminal_reason,
                "first_late_executable_date": (
                    None
                    if first_late_executable_date is None
                    else first_late_executable_date.isoformat()
                ),
            },
            "benchmark_schema": BENCHMARK_SCHEMA,
            "calendar_complete": calendar_complete,
        }
        paths = results[candidate_id]
        paths["completeness_status"] = (
            "complete"
            if calendar_complete
            and len(expected_dates) == 25
            and paths["next_open_path"]["benchmark"]["completeness_rate_bps"] == 10_000  # type: ignore[index]
            and all(
                paths[name]["status"] == "available"  # type: ignore[index]
                for name in ("signal_close_path",)
            )
            and paths["next_open_path"]["status"] in {"available", "unexecutable"}  # type: ignore[index]
            and (
                paths["confirmed_next_open_path"]["status"] != "confirmed"  # type: ignore[index]
                or (
                    paths["confirmed_next_open_path"]["execution_status"] == "unexecutable"  # type: ignore[index]
                    or (
                        paths["confirmed_next_open_path"]["execution_status"] == "available"  # type: ignore[index]
                        and paths["confirmed_next_open_path"]["benchmark"][  # type: ignore[index]
                            "completeness_rate_bps"
                        ]
                        == 10_000
                    )
                )
            )
            else "incomplete"
        )
    return results


def validate_v4_outcome_batch(
    *, candidates: Sequence[Mapping[str, object]], outcomes: Mapping[str, Mapping[str, object]]
) -> None:
    """Require one terminal, benchmark-complete outcome for every candidate.

    A zero-candidate day is intentionally valid and contributes a zero daily
    return.  Any selected candidate with missing execution or benchmark facts
    makes the whole day incomplete instead of silently leaving the denominator.
    """

    candidate_ids = {_text(item, "candidate_id") for item in candidates}
    if candidate_ids != set(outcomes):
        raise ValueError("v4 outcome batch does not match the candidate set")
    for outcome in outcomes.values():
        if outcome.get("completeness_status") != "complete":
            raise ValueError("v4 outcome batch is incomplete")
        path = outcome.get("next_open_path")
        benchmark = path.get("benchmark") if isinstance(path, Mapping) else None
        if not (
            isinstance(benchmark, Mapping)
            and benchmark.get("completeness_rate_bps") == 10_000
            and isinstance(benchmark.get("market_cap_matched_excess_bps"), Mapping)
            and benchmark["market_cap_matched_excess_bps"].get(20) is not None
        ):
            raise ValueError("v4 benchmark evidence is incomplete")
        confirmed = outcome.get("confirmed_next_open_path")
        if isinstance(confirmed, Mapping) and confirmed.get("status") == "confirmed":
            if confirmed.get("execution_status") == "unexecutable":
                continue
            confirmed_benchmark = confirmed.get("benchmark")
            if not (
                confirmed.get("execution_status") == "available"
                and isinstance(confirmed_benchmark, Mapping)
                and confirmed_benchmark.get("completeness_rate_bps") == 10_000
                and confirmed.get("gross_return_20d_bps") is not None
            ):
                raise ValueError("v4 confirmed-entry outcome is incomplete")


def _benchmark_path(
    rows_by_symbol: Mapping[str, Sequence[Mapping[str, object]]],
    *,
    calendar: Sequence[date],
    signal_date: date,
    entry_date: date | None,
    entry_open: bool,
    candidate_market_cap: object,
) -> dict[str, object]:
    empty = {
        "schema": BENCHMARK_SCHEMA,
        "completeness_rate_bps": 0,
        "all_mainboard_return_bps": {5: None, 10: None, 20: None},
        "market_cap_decile_return_bps": {5: None, 10: None, 20: None},
        "market_cap_matched_excess_bps": {5: None, 10: None, 20: None},
    }
    if entry_date is None or not rows_by_symbol:
        return empty
    expected = tuple(day for day in calendar if day > signal_date)
    entry_index = next((index for index, day in enumerate(expected) if day >= entry_date), None)
    if entry_index is None or len(expected) - entry_index < 20:
        return empty
    horizon_dates = expected[entry_index : entry_index + 20]
    peer_returns: dict[str, dict[int, int]] = {}
    peer_caps: dict[str, int] = {}
    for symbol, raw_rows in rows_by_symbol.items():
        by_date = {_as_date(row["trade_date"]): row for row in raw_rows}
        if any(day not in by_date for day in horizon_dates):
            continue
        first = by_date[horizon_dates[0]]
        cap = first.get("signal_market_cap_fen")
        if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
            continue
        base = _economic_price(first, "open" if entry_open else "pre_close")
        peer_returns[symbol] = {
            horizon: _return_bps(
                _economic_price(by_date[horizon_dates[horizon - 1]], "close"),
                base,
            )
            for horizon in (5, 10, 20)
        }
        peer_caps[symbol] = cap
    total = len(rows_by_symbol)
    completeness = len(peer_returns) * 10_000 // total if total else 0
    if completeness != 10_000 or not isinstance(candidate_market_cap, int):
        return {**empty, "completeness_rate_bps": completeness}
    all_returns = {
        horizon: _floor_average(tuple(values[horizon] for values in peer_returns.values()))
        for horizon in (5, 10, 20)
    }
    sorted_caps = sorted(peer_caps.values())
    candidate_rank = bisect_right(sorted_caps, candidate_market_cap) - 1
    candidate_decile = min(9, max(0, candidate_rank) * 10 // len(sorted_caps))
    matched = tuple(
        symbol
        for symbol, cap in peer_caps.items()
        if min(9, (bisect_right(sorted_caps, cap) - 1) * 10 // len(sorted_caps)) == candidate_decile
    )
    matched_returns = {
        horizon: _floor_average(tuple(peer_returns[symbol][horizon] for symbol in matched))
        for horizon in (5, 10, 20)
    }
    return {
        "schema": BENCHMARK_SCHEMA,
        "completeness_rate_bps": completeness,
        "all_mainboard_return_bps": all_returns,
        "market_cap_decile_return_bps": matched_returns,
        "market_cap_matched_excess_bps": {horizon: None for horizon in (5, 10, 20)},
    }


def _floor_average(values: Sequence[int]) -> int:
    if not values:
        raise ValueError("v4 benchmark decile has no peers")
    result = Fraction(sum(values), len(values))
    return result.numerator // result.denominator


def _attach_market_cap_excess(path: dict[str, object]) -> None:
    benchmark = path.get("benchmark")
    if not isinstance(benchmark, dict):
        return
    matched = benchmark.get("market_cap_decile_return_bps")
    if not isinstance(matched, dict):
        return
    benchmark["market_cap_matched_excess_bps"] = {
        horizon: (
            None
            if path.get(f"gross_return_{horizon}d_bps") is None or matched.get(horizon) is None
            else int(path[f"gross_return_{horizon}d_bps"]) - int(matched[horizon])
        )
        for horizon in (5, 10, 20)
    }


def canonical_v4_outcome_hash(outcomes: Mapping[str, object]) -> str:
    payload = {"schema": OUTCOME_HASH_SCHEMA, "outcomes": _jsonable(outcomes)}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _next_executable_date(
    rows: Sequence[Mapping[str, object]], statuses: Mapping[str, int], *, after: date
) -> date | None:
    for row in rows:
        day = _as_date(row["trade_date"])
        if day > after and int(statuses.get(day.isoformat(), 0)) == 1:
            high = _positive_int(row.get("high_1e4"), "high")
            low = _positive_int(row.get("low_1e4"), "low")
            if high != low:  # one-price days are not assumed executable
                return day
    return None


def _path(
    rows: Sequence[Mapping[str, object]],
    entry_date: date | None,
    *,
    costs: tuple[int, ...],
    entry_open: bool = False,
    expected_horizon_sessions: int = 20,
) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "unavailable" if entry_date is None else "available",
        "entry_date": None if entry_date is None else entry_date.isoformat(),
        "gross_return_5d_bps": None,
        "gross_return_10d_bps": None,
        "gross_return_20d_bps": None,
        "net_return_bps_by_cost": {cost: {5: None, 10: None, 20: None} for cost in costs},
        "mfe_20d_bps": None,
        "mae_20d_bps": None,
    }
    if entry_date is None:
        return result
    selected = [row for row in rows if _as_date(row["trade_date"]) >= entry_date][:20]
    if not selected:
        return result
    result["status"] = (
        "available" if len(selected) == 20 and expected_horizon_sessions >= 20 else "partial"
    )
    first = selected[0]
    base = _economic_price(first, "open" if entry_open else "pre_close")
    for horizon in (5, 10, 20):
        if len(selected) >= horizon:
            gross = _return_bps(_economic_price(selected[horizon - 1], "close"), base)
            result[f"gross_return_{horizon}d_bps"] = gross
            for cost in costs:
                result["net_return_bps_by_cost"][cost][horizon] = gross - cost  # type: ignore[index]
    result["mfe_20d_bps"] = max(_return_bps(_economic_price(row, "high"), base) for row in selected)
    result["mae_20d_bps"] = min(_return_bps(_economic_price(row, "low"), base) for row in selected)
    return result


def _zero_return_terminal(status: str, *, costs: tuple[int, ...]) -> dict[str, object]:
    return {
        "status": status,
        "entry_date": None,
        "gross_return_5d_bps": 0,
        "gross_return_10d_bps": 0,
        "gross_return_20d_bps": 0,
        "net_return_bps_by_cost": {cost: {5: 0, 10: 0, 20: 0} for cost in costs},
        "mfe_20d_bps": 0,
        "mae_20d_bps": 0,
    }


def _terminalize_unexecutable_path(
    path: dict[str, object],
    *,
    observed_rows: Sequence[Mapping[str, object]],
    expected_dates: Sequence[date],
) -> dict[str, object]:
    """Turn a fully observed but too-late entry into a recorded zero-return outcome."""

    if (
        len(expected_dates) == 25
        and len(observed_rows) >= 25
        and path.get("status") in {"partial", "unavailable"}
    ):
        return _zero_return_terminal("unexecutable", costs=(10, 25, 50))
    return path


def _condition(value: object) -> Callable[[int | Fraction], bool]:
    match = _CONDITION.fullmatch(str(value).strip())
    if match is None:
        raise ValueError("unsupported v4 outcome condition")
    operation, threshold = match.groups()
    return lambda close: _OPS[operation](close, int(threshold))


def _return_bps(value: int | Fraction, base: int | Fraction) -> int:
    result = (Fraction(value, base) - 1) * 10_000
    return result.numerator // result.denominator


def _adjusted_price_rows(
    rows: Sequence[Mapping[str, object]], *, signal_close: object | None = None
) -> list[dict[str, object]]:
    """Attach point-in-time economic price levels using the close/pre-close chain."""

    if not rows:
        return []
    first = rows[0]
    anchor = (
        _positive_int(signal_close, "signal close")
        if signal_close is not None
        else _positive_int(
            first.get("signal_close_1e4", first.get("pre_close_1e4")), "signal close"
        )
    )
    level = Fraction(anchor)
    adjusted: list[dict[str, object]] = []
    for raw in rows:
        pre_close = _positive_int(raw.get("pre_close_1e4"), "pre_close")
        row = dict(raw)
        row["_economic_pre_close"] = level
        for field in ("open", "high", "low", "close"):
            value = _positive_int(raw.get(f"{field}_1e4"), field)
            row[f"_economic_{field}"] = level * Fraction(value, pre_close)
        level = row["_economic_close"]  # type: ignore[assignment]
        adjusted.append(row)
    return adjusted


def _economic_price(row: Mapping[str, object], field: str) -> int | Fraction:
    value = row.get(f"_economic_{field}")
    if isinstance(value, Fraction) and value > 0:
        return value
    return _positive_int(row.get(f"{field}_1e4"), field)


def _as_date(value: object) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _text(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"candidate {key} is missing")
    return item


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda p: str(p[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    return value
