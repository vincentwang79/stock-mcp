"""Offline RED contracts for rebuilding v3 facts from recorded local evidence."""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from stock_mcp.domain import DailyBar, MarketSnapshot, Security
from stock_mcp.storage import Database

SOURCE = "recorded-tushare-2026-08-07"
TRADE_DATE = date(2026, 8, 7)
AS_OF = datetime(2026, 8, 7, 8, 30, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def _snapshot() -> MarketSnapshot:
    securities = (
        Security(
            symbol="600001.SH",
            name="本地行业样本",
            exchange="SSE",
            board="MAIN",
            list_date=date(2020, 1, 2),
            industry="",
            is_st=False,
        ),
        Security(
            symbol="600002.SH",
            name="无行业样本",
            exchange="SSE",
            board="MAIN",
            list_date=date(2020, 1, 2),
            industry="",
            is_st=False,
        ),
    )
    bars = tuple(
        DailyBar(
            symbol=security.symbol,
            trade_date=TRADE_DATE,
            open_1e4=100_000,
            high_1e4=110_000 if index == 0 else 105_000,
            low_1e4=99_000,
            close_1e4=110_000 if index == 0 else 104_000,
            pre_close_1e4=100_000,
            volume_shares=1_000_000,
            amount_fen=8_000_000_000,
            source=SOURCE,
            source_timestamp=AS_OF,
        )
        for index, security in enumerate(securities)
    )
    return MarketSnapshot(
        trade_date=TRADE_DATE,
        source=SOURCE,
        source_timestamp=AS_OF,
        securities=securities,
        bars=bars,
        advance_ratio_bps=6_000,
        above_ma20_ratio_bps=5_500,
    )


class BuildV3FactsContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "research.sqlite3")
        self.database.initialize()
        self.database.save_market_snapshot(_snapshot())
        self.database.save_expected_trading_days(SOURCE, (TRADE_DATE,))
        self.industry_json = Path(self.temporary.name) / "recorded-industries.json"
        self.industry_json.write_text(
            json.dumps({"600001.SH": "银行"}, sort_keys=True), encoding="utf-8"
        )

    def test_build_v3_facts_uses_only_local_sqlite_and_recorded_industry_json(self) -> None:
        build = self._require_builder()
        before_bars = self.database.load_daily_bars(TRADE_DATE, SOURCE)
        before_snapshot = self.database.load_market_snapshot(TRADE_DATE, source=SOURCE)

        with patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("build-v3-facts must not access the network"),
        ):
            report = build(
                database=self.database,
                industry_json_path=self.industry_json,
                source=SOURCE,
                start=TRADE_DATE,
                end=TRADE_DATE,
            )

        self.assertEqual(before_bars, self.database.load_daily_bars(TRADE_DATE, SOURCE))
        self.assertEqual(
            before_snapshot,
            self.database.load_market_snapshot(TRADE_DATE, source=SOURCE),
        )
        self.assertEqual(2, report["price_limits_written"])
        self.assertEqual(2, report["snapshot_features_written"])
        self.assertEqual(("600002.SH",), tuple(report["industry_unavailable_symbols"]))
        self.assertTrue(report["ready"])
        self.assertEqual((), tuple(report["data_gap_dates"]))

    def test_missing_recorded_calendar_and_snapshots_cannot_report_ready(self) -> None:
        empty = Database(Path(self.temporary.name) / "empty.sqlite3")
        empty.initialize()

        report = self._require_builder()(
            database=empty,
            industry_json_path=self.industry_json,
            source=SOURCE,
            start=date(2023, 8, 8),
            end=date(2026, 8, 7),
        )

        self.assertFalse(report["ready"])
        self.assertFalse(report["calendar_available"])

    def test_release_industry_mapping_has_the_recorded_mainboard_coverage(self) -> None:
        from stock_mcp.industry import load_industry_reference

        reference = load_industry_reference(ROOT / "a_share_mainboard_code_name.json")

        self.assertEqual("新浪财经行业分类", reference.standard)
        self.assertEqual("retrospective_current_mapping", reference.mode)
        self.assertEqual(date(2026, 8, 10), reference.as_of)
        self.assertEqual(3_098, len(reference.industries))
        self.assertEqual(11, sum(value == "unavailable" for value in reference.industries.values()))
        self.assertRegex(reference.mapping_sha256, r"^[0-9a-f]{64}$")

    def test_recovery_rerun_is_idempotent_and_does_not_group_unavailable_industries(self) -> None:
        build = self._require_builder()
        first = build(
            database=self.database,
            industry_json_path=self.industry_json,
            source=SOURCE,
            start=TRADE_DATE,
            end=TRADE_DATE,
        )
        load_prices = self._require_database_method("load_daily_price_limits")
        load_features = self._require_database_method("load_v3_snapshot_features")
        prices = load_prices(TRADE_DATE, source=SOURCE)
        features = load_features(TRADE_DATE, source=SOURCE)
        second = build(
            database=self.database,
            industry_json_path=self.industry_json,
            source=SOURCE,
            start=TRADE_DATE,
            end=TRADE_DATE,
        )

        self.assertEqual(2, first["price_limits_written"])
        self.assertEqual(2, first["snapshot_features_written"])
        self.assertEqual(0, second["price_limits_written"])
        self.assertEqual(0, second["snapshot_features_written"])
        self.assertEqual(prices, load_prices(TRADE_DATE, source=SOURCE))
        self.assertEqual(features, load_features(TRADE_DATE, source=SOURCE))
        self.assertEqual("unavailable", features["600002.SH"]["industry"])
        self.assertIsNone(features["600002.SH"]["industry_group"])
        self.assertNotEqual(
            features["600001.SH"]["industry_group"],
            features["600002.SH"]["industry_group"],
        )

    def test_formal_industry_metadata_is_preserved_with_exchange_normalized_symbols(self) -> None:
        build = self._require_builder()
        formal = Path(self.temporary.name) / "recorded-industries-formal.json"
        formal.write_text(
            json.dumps(
                {
                    "metadata": {
                        "standard": "recorded-sina-industry",
                        "mode": "retrospective_current_mapping",
                        "as_of": "2026-08-07",
                        "mapping_sha256": "3" * 64,
                    },
                    "stocks": [
                        {
                            "code": "600001",
                            "exchange": "SSE",
                            "industry": "银行",
                        },
                        {
                            "code": "600002",
                            "exchange": "SZSE",
                            "industry": "电力设备",
                        },
                    ],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        build(
            database=self.database,
            industry_json_path=formal,
            source=SOURCE,
            start=TRADE_DATE,
            end=TRADE_DATE,
        )

        features = self._require_database_method("load_v3_snapshot_features")(
            TRADE_DATE, source=SOURCE
        )
        from stock_mcp.industry import load_industry_reference

        mapping_hash = load_industry_reference(formal).mapping_sha256
        self.assertEqual("银行", features["600001.SH"]["industry"])
        self.assertEqual("recorded-sina-industry", features["600001.SH"]["industry_standard"])
        self.assertEqual("retrospective_current_mapping", features["600001.SH"]["industry_mode"])
        self.assertEqual("2026-08-07", features["600001.SH"]["industry_as_of"])
        self.assertEqual(mapping_hash, features["600001.SH"]["industry_mapping_sha256"])
        self.assertNotEqual("3" * 64, mapping_hash, "declared hashes are not a trust anchor")

    def test_conflicting_existing_feature_rejects_before_writing_new_price_limits(self) -> None:
        build = self._require_builder()
        self._require_database_method("save_v3_snapshot_features")(
            trade_date=TRADE_DATE,
            source=SOURCE,
            features={"600001.SH": {"industry": "冲突夹具"}},
        )

        with self.assertRaisesRegex(ValueError, "immutable|conflict"):
            build(
                database=self.database,
                industry_json_path=self.industry_json,
                source=SOURCE,
                start=TRADE_DATE,
                end=TRADE_DATE,
            )

        self.assertEqual(
            {},
            self._require_database_method("load_daily_price_limits")(TRADE_DATE, source=SOURCE),
        )

    def test_v3_input_does_not_substitute_an_older_bar_for_a_missing_market_session(self) -> None:
        from stock_mcp.v3_facts import load_v3_market_input

        market = load_v3_market_input(_SuspendedHistoryRepository(), TRADE_DATE, source=SOURCE)

        self.assertEqual(1, market.breadth.eligible_count)
        self.assertNotEqual(
            market.prior_dates,
            tuple(bar.trade_date for bar in market.securities[1].prior_bars),
        )

    def _require_builder(self):
        from stock_mcp import backfill

        build = getattr(backfill, "build_v3_facts", None)
        self.assertTrue(callable(build), "stock_mcp.backfill.build_v3_facts() is required")
        return build

    def _require_database_method(self, name: str):
        method = getattr(self.database, name, None)
        self.assertTrue(callable(method), f"Schema v10 requires Database.{name}()")
        return method


if __name__ == "__main__":
    unittest.main()


class _SuspendedHistoryRepository:
    def __init__(self) -> None:
        self.securities = _snapshot().securities
        all_prior = tuple(TRADE_DATE - timedelta(days=offset) for offset in range(61, 0, -1))
        first_dates = all_prior[1:]
        second_dates = (*all_prior[:59], all_prior[60])
        bars = []
        for security, sessions in zip(self.securities, (first_dates, second_dates), strict=True):
            bars.extend(self._bar(security.symbol, session) for session in sessions)
            bars.append(self._bar(security.symbol, TRADE_DATE))
        self.bars = tuple(bars)
        self.expected_prior = first_dates

    def load_expected_trading_days(self, start, end, *, source):
        return self.expected_prior

    def load_market_snapshot(self, target, *, source, history_limit):
        return MarketSnapshot(
            trade_date=target,
            source=source,
            source_timestamp=AS_OF,
            securities=self.securities,
            bars=self.bars,
            advance_ratio_bps=5_000,
            above_ma20_ratio_bps=5_000,
        )

    def load_daily_price_limits(self, target, *, source):
        return {
            security.symbol: {
                "algorithm": "mainboard-10pct-round-half-up-v1",
                "limit_down_1e4": 90_000,
                "limit_up_1e4": 110_000,
                "policy_exception": False,
                "touched_down": False,
                "touched_up": False,
            }
            for security in self.securities
        }

    def load_v3_snapshot_features(self, target, *, source):
        return {
            security.symbol: {
                "industry": "银行",
                "industry_standard": "新浪财经行业分类",
                "industry_mode": "retrospective_current_mapping",
                "industry_as_of": "2026-08-10",
                "industry_mapping_sha256": "a" * 64,
            }
            for security in self.securities
        }

    @staticmethod
    def _bar(symbol: str, session: date) -> DailyBar:
        return DailyBar(
            symbol=symbol,
            trade_date=session,
            open_1e4=100_000,
            high_1e4=101_000,
            low_1e4=99_000,
            close_1e4=100_000,
            pre_close_1e4=100_000,
            volume_shares=1_000_000,
            amount_fen=8_000_000_000,
            source=SOURCE,
            source_timestamp=AS_OF,
        )
