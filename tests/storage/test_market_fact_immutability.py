"""Market facts are immutable inputs to deterministic historical replay."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from stock_mcp.domain import DailyBar, MarketSnapshot, Security
from stock_mcp.storage import Database

TRADE_DATE = date(2026, 8, 7)
AS_OF = datetime(2026, 8, 7, 8, 30, tzinfo=UTC)


def _bar() -> DailyBar:
    return DailyBar(
        symbol="600001.SH",
        trade_date=TRADE_DATE,
        open_1e4=100_000,
        high_1e4=106_000,
        low_1e4=99_000,
        close_1e4=104_000,
        pre_close_1e4=100_000,
        volume_shares=1_000_000,
        amount_fen=8_000_000_000,
        source="tushare",
        source_timestamp=AS_OF,
    )


def _security() -> Security:
    return Security(
        symbol="600001.SH",
        name="固定样本",
        exchange="SSE",
        board="MAIN",
        list_date=date(2020, 1, 2),
        industry="银行",
        is_st=False,
    )


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        trade_date=TRADE_DATE,
        source="tushare",
        source_timestamp=AS_OF,
        securities=(_security(),),
        bars=(_bar(),),
        advance_ratio_bps=6_000,
        above_ma20_ratio_bps=5_500,
    )


class MarketFactImmutabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "research.sqlite3")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_conflicting_daily_bar_is_rejected_without_rewriting_original(self) -> None:
        original = _bar()
        self.database.save_daily_bars((original,))

        with self.assertRaisesRegex(ValueError, "immutable"):
            self.database.save_daily_bars((replace(original, high_1e4=110_000, close_1e4=108_000),))

        self.assertEqual(
            self.database.load_daily_bars(TRADE_DATE, "tushare"),
            (original,),
        )

    def test_snapshot_conflict_is_atomic_and_preserves_all_original_facts(self) -> None:
        original = _snapshot()
        self.database.save_market_snapshot(original)
        conflicting = replace(
            original,
            source_timestamp=AS_OF.replace(minute=31),
            bars=(replace(original.bars[0], close_1e4=105_000),),
            securities=(replace(original.securities[0], industry="未来行业"),),
            advance_ratio_bps=7_000,
        )

        with self.assertRaisesRegex(ValueError, "immutable"):
            self.database.save_market_snapshot(conflicting)

        self.assertEqual(
            self.database.load_market_snapshots(TRADE_DATE, TRADE_DATE),
            (original,),
        )


if __name__ == "__main__":
    unittest.main()
