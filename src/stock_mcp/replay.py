from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import date
from typing import Any

from .domain import DailyReview, MarketSnapshot, StrategyVersion
from .review import generate_daily_review


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
