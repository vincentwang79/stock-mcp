from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta

from stock_mcp.domain import DailyBar, MarketSnapshot, Security
from stock_mcp.industry import RecordedIndustryReference
from stock_mcp.v3_facts import build_live_v3_market_input


class V3LiveInputContractTest(unittest.TestCase):
    def test_missing_tradable_history_fails_instead_of_silently_excluding_security(self) -> None:
        target = date(2026, 8, 20)
        sessions = tuple(target - timedelta(days=60 - index) for index in range(60))
        missing = sessions[-5]
        timestamp = datetime(2026, 8, 20, 9, tzinfo=UTC)
        security = Security(
            "600001.SH", "缺失可交易日", "SSE", "MAIN", date(2020, 1, 1), "银行", False
        )
        bars = tuple(
            _bar(security.symbol, session, timestamp)
            for session in (*sessions, target)
            if session != missing
        )
        snapshot = MarketSnapshot(target, "tushare", timestamp, (security,), bars, 5_000, 5_000)
        reference = RecordedIndustryReference(
            standard="fixture",
            mode="retrospective_current_mapping",
            as_of=date(2026, 8, 10),
            mapping_sha256="b" * 64,
            industries={security.symbol: "银行"},
        )

        with self.assertRaisesRegex(ValueError, "missing.*recorded suspension"):
            build_live_v3_market_input(
                snapshot,
                prior_dates=sessions,
                industry_reference=reference,
                trading_statuses={(security.symbol, missing): "1"},
            )


def _bar(symbol: str, trade_date: date, timestamp: datetime) -> DailyBar:
    return DailyBar(
        symbol,
        trade_date,
        100_000,
        101_000,
        99_000,
        100_000,
        100_000,
        1_000_000,
        10_000_000_000,
        "tushare",
        timestamp,
    )


if __name__ == "__main__":
    unittest.main()
