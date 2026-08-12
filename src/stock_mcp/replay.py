from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from .domain import DailyReview, MarketSnapshot, StrategyVersion
from .review import generate_daily_review

V4_MANIFEST_SCHEMA = "v4-manifest-v1"
V4_CAPITAL_EXCLUSION_REASON = "sina_share_capital_unavailable"


def canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _v4_symbol_hash(symbols: tuple[str, ...]) -> str:
    encoded = json.dumps(symbols, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def load_v4_capital_exclusions(
    path: Path, missing_symbols: tuple[str, ...]
) -> tuple[str, ...]:
    """Load an explicit governance exception and require an exact evidence match."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("v4 capital exclusion file is invalid") from error
    if (
        not isinstance(document, dict)
        or document.get("schema") != "v4-capital-exclusions-v1"
        or document.get("reason") != V4_CAPITAL_EXCLUSION_REASON
        or not isinstance(document.get("symbols"), list)
    ):
        raise ValueError("v4 capital exclusion document is invalid")
    symbols = tuple(document["symbols"])
    if symbols != tuple(sorted(set(symbols))):
        raise ValueError("v4 capital exclusion symbols must be unique and sorted")
    uncovered = set(missing_symbols) - set(symbols)
    if uncovered:
        raise ValueError(
            "v4 capital exclusions do not cover every recorded missing-capital symbol"
        )
    return symbols


def load_v4_sina_backfill_universe(
    path: Path, start: date, end: date
) -> tuple[tuple[str, ...], str]:
    """Load the exact, hash-verified security universe collected by Sina backfill."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Sina backfill manifest is invalid") from error
    if not isinstance(document, dict):
        raise ValueError("Sina backfill manifest is invalid")
    recorded_hash = document.get("manifest_hash")
    canonical = dict(document)
    canonical.pop("manifest_hash", None)
    symbols_value = document.get("symbols")
    if (
        document.get("schema") != "sina-backfill-manifest-v1"
        or document.get("adapter_version") != "sina-adapter-v1"
        or document.get("start") != start.isoformat()
        or document.get("end") != end.isoformat()
        or not isinstance(symbols_value, list)
        or symbols_value != sorted(set(symbols_value))
        or recorded_hash != canonical_json_sha256(canonical)
    ):
        raise ValueError("Sina backfill manifest hash or boundaries are invalid")
    return tuple(symbols_value), str(recorded_hash)


def validate_v4_manifest_symbol_coverage(manifest: dict[str, object]) -> None:
    """Validate the immutable partition of the study's security universe."""

    universe = tuple(manifest.get("universe_symbols", ()))
    included = tuple(manifest.get("included_symbols", ()))
    excluded = tuple(manifest.get("excluded_symbols", ()))
    source_manifest_hash = str(manifest.get("universe_source_manifest_hash", ""))
    if (
        not universe
        or universe != tuple(sorted(set(universe)))
        or included != tuple(sorted(set(included)))
        or excluded != tuple(sorted(set(excluded)))
        or set(included) & set(excluded)
        or set(included) | set(excluded) != set(universe)
    ):
        raise ValueError("v4 manifest included/excluded symbols must partition the universe")
    if excluded and manifest.get("exclusion_reason") != V4_CAPITAL_EXCLUSION_REASON:
        raise ValueError("v4 manifest exclusion reason is invalid")
    if (
        manifest.get("universe_source") != "sina-backfill-manifest-v1"
        or len(source_manifest_hash) != 64
        or any(character not in "0123456789abcdef" for character in source_manifest_hash)
    ):
        raise ValueError("v4 manifest universe source is invalid")
    expected = {
        "universe_symbol_count": len(universe),
        "included_symbol_count": len(included),
        "excluded_symbol_count": len(excluded),
        "capital_coverage_bps": len(included) * 10_000 // len(universe),
        "universe_symbols_hash": _v4_symbol_hash(universe),
        "included_symbols_hash": _v4_symbol_hash(included),
        "excluded_symbols_hash": _v4_symbol_hash(excluded),
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError("v4 manifest symbol coverage metadata is inconsistent")


def validate_v4_manifest_universe(
    *,
    expected_session_count: int,
    price_day_count: int,
    snapshot_day_count: int,
    missing_price_rows: int,
    orphan_price_rows: int,
) -> None:
    """Reject a manifest whose daily price universe is incomplete or self-inconsistent."""

    if (
        price_day_count != expected_session_count
        or snapshot_day_count != expected_session_count
        or missing_price_rows != 0
        or orphan_price_rows != 0
    ):
        raise ValueError("v4 manifest price universe is not complete")


def build_v4_replay_manifest(
    *,
    source: str,
    sessions: tuple[date, ...],
    bar_start: date,
    signal_start: date,
    signal_end: date,
    outcome_through: date,
    prices_hash: str,
    statuses_hash: str,
    share_capital_hash: str,
    industry_mapping_hash: str,
    universe_symbols: tuple[str, ...],
    excluded_symbols: tuple[str, ...] = (),
    exclusion_reason: str | None = None,
    universe_source_manifest_hash: str,
) -> dict[str, object]:
    if source != "tushare":
        raise ValueError("v4 primary research manifest requires the Tushare price source")
    if not sessions or sessions != tuple(sorted(set(sessions))):
        raise ValueError("v4 manifest sessions must be unique and increasing")
    if (
        not universe_symbols
        or universe_symbols != tuple(sorted(set(universe_symbols)))
        or excluded_symbols != tuple(sorted(set(excluded_symbols)))
        or not set(excluded_symbols).issubset(universe_symbols)
        or set(universe_symbols) == set(excluded_symbols)
    ):
        raise ValueError("v4 manifest universe and excluded symbols are invalid")
    if (bool(excluded_symbols)) != (exclusion_reason == V4_CAPITAL_EXCLUSION_REASON):
        raise ValueError("v4 manifest exclusion reason is invalid")
    if len(sessions) < 86:
        raise ValueError("v4 manifest requires 60 warmup and 25 outcome-only sessions")
    if (
        bar_start != sessions[0]
        or signal_start != sessions[60]
        or signal_end != sessions[-26]
        or outcome_through != sessions[-1]
    ):
        raise ValueError("v4 manifest boundaries must match the frozen session windows")
    for label, value in (
        ("prices", prices_hash),
        ("statuses", statuses_hash),
        ("share capital", share_capital_hash),
        ("industry", industry_mapping_hash),
        ("universe source manifest", universe_source_manifest_hash),
    ):
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError(f"v4 manifest {label} hash must be lowercase SHA-256")
    payload: dict[str, object] = {
        "schema": V4_MANIFEST_SCHEMA,
        "source": source,
        "price_adapter_version": "tushare-lock-v1",
        "status_source": "baostock",
        "status_adapter_version": "baostock-lock-v1",
        "share_capital_source": "sina",
        "share_capital_adapter_version": "sina-adapter-v1",
        "bar_start": bar_start.isoformat(),
        "warmup_sessions": 60,
        "signal_start": signal_start.isoformat(),
        "signal_end": signal_end.isoformat(),
        "confirmation_window_sessions": 5,
        "outcome_horizon_sessions": 20,
        "outcome_through": outcome_through.isoformat(),
        "sessions": [value.isoformat() for value in sessions],
        "calendar_hash": hashlib.sha256(
            "|".join(value.isoformat() for value in sessions).encode()
        ).hexdigest(),
        "prices_hash": prices_hash,
        "statuses_hash": statuses_hash,
        "share_capital_hash": share_capital_hash,
        "industry_mapping_hash": industry_mapping_hash,
        "universe_symbols": list(universe_symbols),
        "included_symbols": list(
            tuple(symbol for symbol in universe_symbols if symbol not in set(excluded_symbols))
        ),
        "excluded_symbols": list(excluded_symbols),
        "universe_symbol_count": len(universe_symbols),
        "included_symbol_count": len(universe_symbols) - len(excluded_symbols),
        "excluded_symbol_count": len(excluded_symbols),
        "capital_coverage_bps": (
            (len(universe_symbols) - len(excluded_symbols)) * 10_000
            // len(universe_symbols)
        ),
        "exclusion_reason": exclusion_reason,
        "universe_source": "sina-backfill-manifest-v1",
        "universe_source_manifest_hash": universe_source_manifest_hash,
    }
    payload["universe_symbols_hash"] = _v4_symbol_hash(
        tuple(payload["universe_symbols"])
    )
    payload["included_symbols_hash"] = _v4_symbol_hash(
        tuple(payload["included_symbols"])
    )
    payload["excluded_symbols_hash"] = _v4_symbol_hash(
        tuple(payload["excluded_symbols"])
    )
    validate_v4_manifest_symbol_coverage(payload)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["manifest_hash"] = hashlib.sha256(encoded.encode()).hexdigest()
    return payload


def validate_v4_replay_certification(replay: dict[str, object]) -> None:
    outcomes = replay.get("outcomes")
    if not isinstance(outcomes, dict) or not outcomes:
        raise ValueError("v4 outcome evidence is incomplete")
    if any(
        not isinstance(item, dict)
        or item.get("status") in {"unavailable", "partial"}
        or item.get("completeness_status") in {"incomplete", "unavailable"}
        for item in outcomes.values()
    ):
        raise ValueError("v4 outcome evidence is incomplete")
    if replay.get("benchmark_completeness") != "complete":
        raise ValueError("v4 benchmark evidence must be complete")


def walk_forward(
    snapshots: Iterable[MarketSnapshot],
    strategy: StrategyVersion,
) -> tuple[DailyReview, ...]:
    """Generate one review per strictly increasing, point-in-time snapshot."""
    recorded_snapshots = tuple(snapshots)
    _validate_point_in_time_snapshots(recorded_snapshots)
    return tuple(generate_daily_review(snapshot, strategy) for snapshot in recorded_snapshots)


def _validate_point_in_time_snapshots(snapshots: Iterable[MarketSnapshot]) -> None:
    """Reject a replay sequence that is unordered or contains a future market fact."""

    previous_date = None
    for snapshot in snapshots:
        if previous_date is not None and snapshot.trade_date <= previous_date:
            raise ValueError("walk-forward snapshot dates must be strictly increasing")
        if any(bar.trade_date > snapshot.trade_date for bar in snapshot.bars):
            raise ValueError("walk-forward snapshots must not contain future bars")
        previous_date = snapshot.trade_date


class HistoricalReplayService:
    """Compare immutable strategy versions over recorded point-in-time snapshots."""

    def __init__(self, database: Any, strategy_registry: Any) -> None:
        self._database = database
        self._strategies = strategy_registry

    def replay_for_governance(self, version: str, start: date, end: date) -> dict[str, object]:
        """Replay one immutable strategy over a complete recorded three-year calendar."""

        if not 1_095 <= (end - start).days <= 1_100:
            raise ValueError("governance replay must cover 1095 to 1100 calendar days")
        expected_loader = getattr(self._database, "load_expected_trading_days", None)
        if not callable(expected_loader):
            raise ValueError("governance replay requires a recorded trading calendar")
        expected_dates = tuple(expected_loader(start, end, source="tushare"))
        if len(expected_dates) < 600:
            raise ValueError("governance replay requires at least 600 expected trading days")
        snapshots = tuple(self._database.load_market_snapshots(start, end, source="tushare"))
        snapshot_dates = tuple(snapshot.trade_date for snapshot in snapshots)
        if snapshot_dates != expected_dates:
            raise ValueError("governance replay snapshots must exactly match the trading calendar")
        _validate_point_in_time_snapshots(snapshots)

        strategy = self._strategies.get(version)
        reviews = walk_forward(snapshots[20:], strategy)
        daily = [
            {"trade_date": review.trade_date.isoformat(), **_review_result(review)}
            for review in reviews
        ]
        from .strategy import canonical_strategy_parameters_hash

        result: dict[str, object] = {
            "strategy_version": version,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "snapshot_dates": [snapshot_date.isoformat() for snapshot_date in snapshot_dates],
            "days_replayed": len(reviews),
            "daily": daily,
            "parameters_hash": canonical_strategy_parameters_hash(strategy.parameters),
            "dataset_hash": _dataset_hash(snapshots),
        }
        result["result_hash"] = _result_hash(daily)
        return result

    def compare(self, left: str, right: str, start: date, end: date) -> dict[str, object]:
        if left == right:
            raise ValueError("strategy comparison requires distinct strategy versions")
        if end < start:
            raise ValueError("strategy comparison range is invalid")
        if (end - start).days > 1_100:
            raise ValueError("strategy comparison is limited to three years")
        snapshots = self._database.load_market_snapshots(start, end, source="tushare")
        if not snapshots:
            raise ValueError("no recorded normalized snapshots in the requested range")
        expected_loader = getattr(self._database, "load_expected_trading_days", None)
        expected_dates = (
            tuple(expected_loader(start, end, source="tushare"))
            if callable(expected_loader)
            else ()
        )
        snapshot_dates = tuple(snapshot.trade_date for snapshot in snapshots)
        replay_is_governance_grade = (
            (end - start).days >= 1_095
            and len(expected_dates) >= 600
            and snapshot_dates == expected_dates
        )
        replay_snapshots = snapshots[20:] if replay_is_governance_grade else snapshots
        left_reviews = walk_forward(replay_snapshots, self._strategies.get(left))
        right_reviews = walk_forward(replay_snapshots, self._strategies.get(right))
        return {
            "left_version": left,
            "right_version": right,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days_compared": len(replay_snapshots),
            "left_candidate_count": sum(len(review.candidates) for review in left_reviews),
            "right_candidate_count": sum(len(review.candidates) for review in right_reviews),
            "daily": [
                {
                    "trade_date": left_review.trade_date.isoformat(),
                    "left": _review_result(left_review),
                    "right": _review_result(right_review),
                }
                for left_review, right_review in zip(left_reviews, right_reviews, strict=True)
            ],
        }


def _review_result(review: DailyReview) -> dict[str, object]:
    return {
        "market_regime": review.market_regime.value,
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "symbol": candidate.symbol,
                "score": candidate.score,
                "setup_type": candidate.setup_type.value,
                "confirmation_condition": candidate.confirmation_condition,
                "invalidation_condition": candidate.invalidation_condition,
                "industry_evidence": {
                    evidence.metric: evidence.value
                    for evidence in candidate.evidence
                    if evidence.metric
                    in {
                        "industry",
                        "industry_strength_bps",
                        "classification_mode",
                        "classification_as_of",
                        "classification_mapping_sha256",
                    }
                },
                "evidence": [
                    {
                        "metric": evidence.metric,
                        "value": evidence.value,
                        "threshold": evidence.threshold,
                        "passed": evidence.passed,
                        "score_contribution": evidence.score_contribution,
                    }
                    for evidence in candidate.evidence
                ],
            }
            for candidate in review.candidates
        ],
    }


def _dataset_hash(snapshots: Iterable[MarketSnapshot]) -> str:
    """Hash every normalized fact used by a governance replay."""

    payload: list[dict[str, object]] = []
    for snapshot in snapshots:
        payload.append(
            {
                "trade_date": snapshot.trade_date.isoformat(),
                "source": snapshot.source,
                "source_timestamp": snapshot.source_timestamp.isoformat(),
                "advance_ratio_bps": snapshot.advance_ratio_bps,
                "above_ma20_ratio_bps": snapshot.above_ma20_ratio_bps,
                "securities": [
                    {
                        "symbol": security.symbol,
                        "name": security.name,
                        "exchange": security.exchange,
                        "board": security.board,
                        "list_date": security.list_date.isoformat(),
                        "industry": security.industry,
                        "is_st": security.is_st,
                    }
                    for security in sorted(snapshot.securities, key=lambda item: item.symbol)
                ],
                "bars": [
                    {
                        "symbol": bar.symbol,
                        "trade_date": bar.trade_date.isoformat(),
                        "open_1e4": bar.open_1e4,
                        "high_1e4": bar.high_1e4,
                        "low_1e4": bar.low_1e4,
                        "close_1e4": bar.close_1e4,
                        "pre_close_1e4": bar.pre_close_1e4,
                        "volume_shares": bar.volume_shares,
                        "amount_fen": bar.amount_fen,
                        "source": bar.source,
                        "source_timestamp": bar.source_timestamp.isoformat(),
                    }
                    for bar in sorted(
                        snapshot.bars, key=lambda item: (item.trade_date, item.symbol)
                    )
                ],
            }
        )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _result_hash(daily: list[dict[str, object]]) -> str:
    """Hash the complete ordered, structured daily output of one replay."""

    encoded = json.dumps(daily, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
