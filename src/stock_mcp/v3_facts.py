"""Rebuild immutable v3 facts exclusively from recorded local evidence."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .domain import (
    DailyPriceLimit,
    IndustryClassificationReference,
    MarketSnapshot,
    V3BreadthFacts,
    V3MarketInput,
    V3SecurityInput,
)
from .industry import RecordedIndustryReference, load_industry_reference
from .v3 import adjusted_close_chain, derive_daily_price_limit


class LiveV3EvidenceError(ValueError):
    """A live v3 input has one or more unresolved recorded-evidence gaps."""

    def __init__(self, report: dict[str, object]) -> None:
        self.report = report
        super().__init__(
            "live v3 history is missing without a recorded suspension: "
            + json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )


def build_live_v3_market_input(
    snapshot: MarketSnapshot,
    *,
    prior_dates: tuple[date, ...],
    industry_reference: RecordedIndustryReference,
    trading_statuses: Mapping[tuple[str, date], str],
) -> tuple[V3MarketInput, dict[str, object]]:
    """Build one live v3 input while degrading incomplete securities individually.

    ``snapshot`` may contain older per-security bars returned by a bounded history
    query.  Only bars on the exact recorded market calendar are retained, so a
    suspension cannot be silently replaced by an older bar and cannot block
    otherwise complete securities.
    """

    target = snapshot.trade_date
    if len(prior_dates) != 60 or tuple(sorted(set(prior_dates))) != prior_dates:
        raise ValueError("live v3 input requires sixty unique prior market sessions")
    if any(day >= target for day in prior_dates):
        raise ValueError("live v3 prior calendar contains target or future dates")
    if industry_reference.as_of is None:
        raise ValueError("live v3 industry reference requires an as-of date")
    bars_by_symbol: dict[str, list[Any]] = {}
    for bar in snapshot.bars:
        if bar.source != snapshot.source or bar.trade_date > target:
            raise ValueError("live v3 input contains mixed-source or future bars")
        bars_by_symbol.setdefault(bar.symbol, []).append(bar)
    security_by_symbol = {security.symbol: security for security in snapshot.securities}
    prior_date_set = set(prior_dates)
    target_bars = {
        symbol: matches[0]
        for symbol, bars in bars_by_symbol.items()
        if len(matches := [bar for bar in bars if bar.trade_date == target]) == 1
        and symbol in security_by_symbol
    }

    security_inputs: list[V3SecurityInput] = []
    features: dict[str, object] = {}
    eligible_count = 0
    advance_count = 0
    ma20_eligible_count = 0
    above_ma20_count = 0
    industries: dict[str, str] = {}
    missing_status_gaps: dict[date, list[str]] = defaultdict(list)
    tradable_price_gaps: dict[date, list[str]] = defaultdict(list)
    recorded_suspension_count = 0
    for security in snapshot.securities:
        otherwise_eligible = (
            security.board == "MAIN"
            and not security.is_st
            and (target - security.list_date).days >= 180
        )
        if not otherwise_eligible or security.symbol in target_bars:
            continue
        trade_status = trading_statuses.get((security.symbol, target))
        if trade_status == "0":
            recorded_suspension_count += 1
        elif trade_status == "1":
            tradable_price_gaps[target].append(security.symbol)
        elif trade_status is None:
            missing_status_gaps[target].append(security.symbol)
        else:
            raise ValueError("live v3 tradeStatus must be 0 or 1")
    for symbol in sorted(target_bars):
        security = security_by_symbol[symbol]
        target_bar = target_bars[symbol]
        prior = tuple(
            sorted(
                (bar for bar in bars_by_symbol[symbol] if bar.trade_date in prior_date_set),
                key=lambda bar: bar.trade_date,
            )
        )
        limit = derive_daily_price_limit(target_bar, security)
        industry = industry_reference.industries.get(symbol, "unavailable")
        industries[symbol] = industry
        security_inputs.append(V3SecurityInput(security, prior, target_bar, limit, industry))
        features[symbol] = {
            "industry": industry,
            "industry_group": None if industry == "unavailable" else industry,
            "price_limit_state": _price_limit_state(limit),
            "industry_standard": industry_reference.standard,
            "industry_mode": industry_reference.mode,
            "industry_as_of": industry_reference.as_of.isoformat(),
            "industry_mapping_sha256": industry_reference.mapping_sha256,
        }
        otherwise_eligible = (
            security.board == "MAIN"
            and not security.is_st
            and (target - security.list_date).days >= 180
            and not limit.policy_exception
        )
        observed_prior_dates = {bar.trade_date for bar in prior}
        missing_dates = tuple(day for day in prior_dates if day not in observed_prior_dates)
        if otherwise_eligible:
            for missing_date in missing_dates:
                trade_status = trading_statuses.get((symbol, missing_date))
                if trade_status == "0":
                    recorded_suspension_count += 1
                elif trade_status == "1":
                    tradable_price_gaps[missing_date].append(symbol)
                elif trade_status is None:
                    missing_status_gaps[missing_date].append(symbol)
                else:
                    raise ValueError("live v3 tradeStatus must be 0 or 1")
        basic_eligible = (
            otherwise_eligible and tuple(bar.trade_date for bar in prior) == prior_dates
        )
        if not basic_eligible:
            continue
        eligible_count += 1
        if target_bar.close_1e4 > target_bar.pre_close_1e4:
            advance_count += 1
        adjusted = adjusted_close_chain(prior, target_bar)
        if len(adjusted) >= 20:
            ma20_eligible_count += 1
            if adjusted[-1] > sum(adjusted[-20:]) / 20:
                above_ma20_count += 1
    if missing_status_gaps or tradable_price_gaps:
        raise LiveV3EvidenceError(
            {
                "schema": "live-v3-evidence-audit-v1",
                "status": "incomplete",
                "missing_status_count": sum(map(len, missing_status_gaps.values())),
                "tradable_price_gap_count": sum(map(len, tradable_price_gaps.values())),
                "recorded_suspension_count": recorded_suspension_count,
                "missing_status_dates": _grouped_gap_report(missing_status_gaps),
                "tradable_price_gap_dates": _grouped_gap_report(tradable_price_gaps),
            }
        )
    if not target_bars:
        raise ValueError("live v3 input contains no target-day securities")
    if not eligible_count:
        raise ValueError("live v3 market breadth has no eligible main-board securities")
    coverage_bps = ma20_eligible_count * 10_000 // eligible_count
    if coverage_bps < 9_700:
        raise ValueError("live v3 ma20 coverage is below 9700 bps")
    reference = IndustryClassificationReference(
        classification_standard=industry_reference.standard,
        classification_mode=industry_reference.mode,
        classification_as_of=industry_reference.as_of,
        classification_mapping_sha256=industry_reference.mapping_sha256,
        industries=industries,
    )
    return (
        V3MarketInput(
            trade_date=target,
            source=snapshot.source,
            source_timestamp=snapshot.source_timestamp,
            prior_dates=prior_dates,
            securities=tuple(security_inputs),
            breadth=V3BreadthFacts(
                advance_count=advance_count,
                eligible_count=eligible_count,
                above_ma20_count=above_ma20_count,
                ma20_eligible_count=ma20_eligible_count,
                advance_ratio_bps=advance_count * 10_000 // eligible_count,
                above_ma20_ratio_bps=above_ma20_count * 10_000 // ma20_eligible_count,
            ),
            industry_reference=reference,
        ),
        features,
    )


def _grouped_gap_report(gaps: Mapping[date, list[str]]) -> list[dict[str, object]]:
    return [
        {
            "trade_date": trade_date.isoformat(),
            "count": len(symbols),
            "sample_symbols": sorted(symbols)[:10],
        }
        for trade_date, symbols in sorted(gaps.items())
    ]


def load_v3_market_input(
    database: Any,
    target: date,
    *,
    source: str,
    prior_history_sessions: int = 60,
    included_symbols: frozenset[str] | None = None,
) -> V3MarketInput:
    """Load one complete v3 input from locally persisted v10 facts and price history."""

    if prior_history_sessions != 60:
        raise ValueError("v3 requires exactly sixty prior history sessions")
    snapshot = database.load_market_snapshot(
        target, source=source, history_limit=prior_history_sessions + 1
    )
    recorded_prior_dates = tuple(
        database.load_expected_trading_days(
            target - timedelta(days=180), target - timedelta(days=1), source=source
        )
    )
    prior_dates = recorded_prior_dates[-prior_history_sessions:]
    if len(prior_dates) != prior_history_sessions:
        raise ValueError("v3 input does not contain sixty recorded prior market sessions")
    limits = database.load_daily_price_limits(target, source=source)
    features = database.load_v3_snapshot_features(target, source=source)
    if not limits or not features:
        raise ValueError("v3 facts have not been built for the target date")
    bars_by_symbol: dict[str, list[Any]] = {}
    for bar in snapshot.bars:
        if bar.trade_date > target or bar.source != source:
            raise ValueError("v3 input contains future or mixed-source bars")
        bars_by_symbol.setdefault(bar.symbol, []).append(bar)
    security_inputs: list[V3SecurityInput] = []
    industries: dict[str, str] = {}
    standard: str | None = None
    mode: str | None = None
    classification_as_of: date | None = None
    mapping_hash: str | None = None
    eligible_count = 0
    advance_count = 0
    ma20_eligible_count = 0
    above_ma20_count = 0
    securities = tuple(
        security
        for security in snapshot.securities
        if included_symbols is None or security.symbol in included_symbols
    )
    if not securities:
        raise ValueError("v3 input contains no securities in the requested universe")
    for security in securities:
        bars = sorted(bars_by_symbol.get(security.symbol, ()), key=lambda item: item.trade_date)
        target_bars = [bar for bar in bars if bar.trade_date == target]
        if len(target_bars) != 1:
            raise ValueError(f"v3 target bar is missing for {security.symbol}")
        prior = tuple(bar for bar in bars if bar.trade_date < target)
        fact = limits.get(security.symbol)
        feature = features.get(security.symbol)
        if not isinstance(fact, Mapping) or not isinstance(feature, Mapping):
            raise ValueError(f"v3 persisted facts are missing for {security.symbol}")
        limit = DailyPriceLimit(
            symbol=security.symbol,
            trade_date=target,
            up_limit_1e4=int(fact["limit_up_1e4"]),
            down_limit_1e4=int(fact["limit_down_1e4"]),
            touched_up=bool(fact["touched_up"]),
            touched_down=bool(fact["touched_down"]),
            policy_exception=bool(fact["policy_exception"]),
            algorithm=str(fact["algorithm"]),
        )
        industry = str(feature.get("industry") or "unavailable")
        industries[security.symbol] = industry
        standard = _same_metadata(standard, feature.get("industry_standard"), "standard")
        mode = _same_metadata(mode, feature.get("industry_mode"), "mode")
        raw_as_of = feature.get("industry_as_of")
        parsed_as_of = None if raw_as_of is None else date.fromisoformat(str(raw_as_of))
        if classification_as_of is not None and parsed_as_of != classification_as_of:
            raise ValueError("v3 industry as-of metadata conflicts within the snapshot")
        classification_as_of = classification_as_of or parsed_as_of
        mapping_hash = _same_metadata(
            mapping_hash, feature.get("industry_mapping_sha256"), "mapping hash"
        )
        target_bar = target_bars[0]
        item = V3SecurityInput(security, prior, target_bar, limit, industry)
        security_inputs.append(item)
        basic_eligible = (
            security.board == "MAIN"
            and not security.is_st
            and (target - security.list_date).days >= 180
            and tuple(bar.trade_date for bar in prior) == prior_dates
            and not limit.policy_exception
        )
        if not basic_eligible:
            continue
        eligible_count += 1
        if target_bar.close_1e4 > target_bar.pre_close_1e4:
            advance_count += 1
        adjusted = adjusted_close_chain(prior, target_bar)
        if len(adjusted) >= 20:
            ma20_eligible_count += 1
            ma20 = sum(adjusted[-20:]) / 20
            if adjusted[-1] > ma20:
                above_ma20_count += 1
    if not eligible_count:
        raise ValueError("v3 market breadth has no eligible main-board securities")
    coverage_bps = ma20_eligible_count * 10_000 // eligible_count
    if coverage_bps < 9_700:
        raise ValueError("v3 ma20 coverage is below 9700 bps")
    if standard is None or mode is None or classification_as_of is None or mapping_hash is None:
        raise ValueError("v3 industry reference metadata is incomplete")
    reference = IndustryClassificationReference(
        classification_standard=standard,
        classification_mode=mode,
        classification_as_of=classification_as_of,
        classification_mapping_sha256=mapping_hash,
        industries=industries,
    )
    return V3MarketInput(
        trade_date=target,
        source=source,
        source_timestamp=snapshot.source_timestamp,
        prior_dates=prior_dates,
        securities=tuple(security_inputs),
        breadth=V3BreadthFacts(
            advance_count=advance_count,
            eligible_count=eligible_count,
            above_ma20_count=above_ma20_count,
            ma20_eligible_count=ma20_eligible_count,
            advance_ratio_bps=advance_count * 10_000 // eligible_count,
            above_ma20_ratio_bps=above_ma20_count * 10_000 // ma20_eligible_count,
        ),
        industry_reference=reference,
    )


def _same_metadata(current: str | None, value: object, label: str) -> str:
    resolved = str(value or "").strip()
    if not resolved:
        raise ValueError(f"v3 industry {label} is missing")
    if current is not None and current != resolved:
        raise ValueError(f"v3 industry {label} conflicts within the snapshot")
    return resolved


def build_v3_facts(
    *,
    database: Any,
    industry_json_path: Path | str,
    source: str,
    start: date,
    end: date,
) -> dict[str, object]:
    """Derive v3 evidence from existing SQLite snapshots without altering them."""

    if end < start:
        raise ValueError("v3 facts range is invalid")
    if not source:
        raise ValueError("v3 facts require a recorded source")
    reference = load_industry_reference(industry_json_path)
    expected_dates = tuple(database.load_expected_trading_days(start, end, source=source))
    snapshot_dates = tuple(database.load_market_snapshot_dates(start, end, source=source))
    snapshot_date_set = set(snapshot_dates)
    expected_date_set = set(expected_dates)
    data_gap_dates = tuple(day for day in expected_dates if day not in snapshot_date_set)
    unexpected_snapshot_dates = tuple(day for day in snapshot_dates if day not in expected_date_set)
    price_limits_written = 0
    snapshot_features_written = 0
    limit_policy_exceptions = 0
    classified_symbols: set[str] = set()
    observed_symbols: set[str] = set()
    unavailable: set[str] = set()
    for trade_date in snapshot_dates:
        snapshot = database.load_market_snapshot(trade_date, source=source, history_limit=1)
        limits, features, day_unavailable = _facts_for_snapshot(snapshot, reference.industries)
        limit_policy_exceptions += sum(
            bool(fact["policy_exception"]) for fact in limits.values() if isinstance(fact, Mapping)
        )
        observed_symbols.update(features)
        classified_symbols.update(
            symbol
            for symbol, feature in features.items()
            if isinstance(feature, Mapping) and feature.get("industry") != "unavailable"
        )
        persisted_features = {
            symbol: {
                **feature,
                "industry_standard": reference.standard,
                "industry_mode": reference.mode,
                "industry_as_of": None if reference.as_of is None else reference.as_of.isoformat(),
                "industry_mapping_sha256": reference.mapping_sha256,
            }
            for symbol, feature in features.items()
        }
        _reject_conflicting_facts(
            database.load_daily_price_limits(trade_date, source=source),
            limits,
            "daily price-limit",
        )
        _reject_conflicting_facts(
            database.load_v3_snapshot_features(trade_date, source=source),
            persisted_features,
            "v3 snapshot-feature",
        )
        price_limits_written += database.save_daily_price_limits(
            trade_date=trade_date, source=source, limits=limits
        )
        snapshot_features_written += database.save_v3_snapshot_features(
            trade_date=trade_date,
            source=source,
            features=persisted_features,
        )
        unavailable.update(day_unavailable)
    calendar_available = bool(expected_dates)
    warmup_count = min(60, len(expected_dates)) if calendar_available else 0
    fixed_governance_range = (start, end) == (date(2023, 8, 8), date(2026, 8, 7))
    expected_coverage_valid = not fixed_governance_range or len(expected_dates) == 727
    return {
        "source": source,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "ready": (
            calendar_available
            and not data_gap_dates
            and not unexpected_snapshot_dates
            and expected_coverage_valid
        ),
        "calendar_available": calendar_available,
        "expected_coverage_valid": expected_coverage_valid,
        "expected_dates": tuple(day.isoformat() for day in expected_dates),
        "processed_dates": tuple(day.isoformat() for day in snapshot_dates),
        "data_gap_dates": tuple(day.isoformat() for day in data_gap_dates),
        "unexpected_snapshot_dates": tuple(day.isoformat() for day in unexpected_snapshot_dates),
        "warmup_dates": warmup_count,
        "assessable_dates": max(0, len(expected_dates) - warmup_count),
        "price_limits_written": price_limits_written,
        "snapshot_features_written": snapshot_features_written,
        "limit_policy_exceptions": limit_policy_exceptions,
        "industry_classified_symbols": len(classified_symbols),
        "industry_observed_symbols": len(observed_symbols),
        "industry_unavailable_symbols": tuple(sorted(unavailable)),
    }


def _reject_conflicting_facts(existing: object, expected: object, name: str) -> None:
    if existing and existing != expected:
        raise ValueError(f"{name} facts are immutable; conflicting batch")


def _facts_for_snapshot(
    snapshot: MarketSnapshot, industries: Mapping[str, str]
) -> tuple[dict[str, object], dict[str, object], tuple[str, ...]]:
    trade_date = snapshot.trade_date
    source = snapshot.source
    securities = {security.symbol: security for security in snapshot.securities}
    target_bars = tuple(bar for bar in snapshot.bars if bar.trade_date == trade_date)
    if len(target_bars) != len(securities) or {bar.symbol for bar in target_bars} != set(
        securities
    ):
        raise ValueError("recorded market snapshot has incomplete target-day bars")
    if any(bar.source != source for bar in target_bars):
        raise ValueError("recorded market snapshot has mixed price sources")
    limits: dict[str, object] = {}
    features: dict[str, object] = {}
    unavailable: list[str] = []
    for bar in sorted(target_bars, key=lambda item: item.symbol):
        limit = derive_daily_price_limit(bar, securities[bar.symbol])
        industry = industries.get(bar.symbol, "unavailable")
        if industry == "unavailable":
            unavailable.append(bar.symbol)
        limits[bar.symbol] = {
            "algorithm": limit.algorithm,
            "limit_down_1e4": limit.down_limit_1e4,
            "limit_up_1e4": limit.up_limit_1e4,
            "policy_exception": limit.policy_exception,
            "touched_down": limit.touched_down,
            "touched_up": limit.touched_up,
        }
        features[bar.symbol] = {
            "industry": industry,
            "industry_group": None if industry == "unavailable" else industry,
            "price_limit_state": _price_limit_state(limit),
        }
    return limits, features, tuple(unavailable)


def _price_limit_state(limit: DailyPriceLimit) -> str:
    if limit.policy_exception:
        return "policy_exception"
    if limit.touched_up and limit.touched_down:
        return "limit_up_and_down"
    if limit.touched_up:
        return "limit_up"
    if limit.touched_down:
        return "limit_down"
    return "none"
