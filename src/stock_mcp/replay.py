from __future__ import annotations

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
    reviews: list[DailyReview] = []
    previous_date = None
    for snapshot in snapshots:
        if previous_date is not None and snapshot.trade_date <= previous_date:
            raise ValueError("walk-forward snapshot dates must be strictly increasing")
        if any(bar.trade_date > snapshot.trade_date for bar in snapshot.bars):
            raise ValueError("walk-forward snapshots must not contain future bars")
        reviews.append(generate_daily_review(snapshot, strategy))
        previous_date = snapshot.trade_date
    return tuple(reviews)


class HistoricalReplayService:
    """Compare immutable strategy versions over recorded point-in-time snapshots."""

    def __init__(self, database: Any, strategy_registry: Any) -> None:
        self._database = database
        self._strategies = strategy_registry

    def compare(self, left: str, right: str, start: date, end: date) -> dict[str, object]:
        if end < start:
            raise ValueError("strategy comparison range is invalid")
        if (end - start).days > 1_100:
            raise ValueError("strategy comparison is limited to three years")
        snapshots = self._database.load_market_snapshots(start, end, source="tushare")
        if not snapshots:
            raise ValueError("no recorded normalized snapshots in the requested range")
        left_reviews = walk_forward(snapshots, self._strategies.get(left))
        right_reviews = walk_forward(snapshots, self._strategies.get(right))
        return {
            "left_version": left,
            "right_version": right,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days_compared": len(snapshots),
            "left_candidate_count": sum(len(review.candidates) for review in left_reviews),
            "right_candidate_count": sum(len(review.candidates) for review in right_reviews),
            "daily": [
                {
                    "trade_date": left_review.trade_date.isoformat(),
                    "left_candidates": len(left_review.candidates),
                    "right_candidates": len(right_review.candidates),
                }
                for left_review, right_review in zip(left_reviews, right_reviews, strict=True)
            ],
        }
