"""Offline contract tests for A-share provider normalization."""

import unittest
from datetime import UTC, date, datetime

from stock_mcp.domain import DailyBar, Security

TRADE_DATE = date(2026, 8, 7)
SOURCE_TIMESTAMP = datetime(2026, 8, 7, 8, 30, tzinfo=UTC)


class ProviderContractTestCase(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from stock_mcp.providers.metadata import (
                normalize_baostock_securities,
                normalize_baostock_trading_calendar,
            )
            from stock_mcp.providers.normalization import (
                ProviderNormalizationError,
                normalize_akshare_snapshot,
                normalize_tushare_daily,
            )
        except (ImportError, AttributeError) as error:
            self.fail(f"provider normalization contract is not implemented: {error}")

        self.normalization_error = ProviderNormalizationError
        self.normalize_akshare_snapshot = normalize_akshare_snapshot
        self.normalize_baostock_securities = normalize_baostock_securities
        self.normalize_baostock_trading_calendar = normalize_baostock_trading_calendar
        self.normalize_tushare_daily = normalize_tushare_daily


class TushareDailyNormalizationContractTest(ProviderContractTestCase):
    def test_normalizes_symbol_date_and_tushare_units(self) -> None:
        bars = self.normalize_tushare_daily(
            [
                {
                    "ts_code": "600001.SH",
                    "trade_date": "20260807",
                    "open": "10.01",
                    "high": "10.55",
                    "low": "9.98",
                    "close": "10.25",
                    "pre_close": "10.00",
                    "vol": "1234.5",
                    "amount": "12345.678",
                }
            ],
            trade_date=TRADE_DATE,
            source_timestamp=SOURCE_TIMESTAMP,
        )

        self.assertEqual(
            bars,
            (
                DailyBar(
                    symbol="600001.SH",
                    trade_date=TRADE_DATE,
                    open_1e4=100_100,
                    high_1e4=105_500,
                    low_1e4=99_800,
                    close_1e4=102_500,
                    pre_close_1e4=100_000,
                    volume_shares=123_450,
                    amount_fen=1_234_567_800,
                    source="tushare",
                    source_timestamp=SOURCE_TIMESTAMP,
                ),
            ),
        )

    def test_rejects_missing_required_tushare_column(self) -> None:
        with self.assertRaisesRegex(self.normalization_error, "amount"):
            self.normalize_tushare_daily(
                [
                    {
                        "ts_code": "600001.SH",
                        "trade_date": "20260807",
                        "open": "10.01",
                        "high": "10.55",
                        "low": "9.98",
                        "close": "10.25",
                        "pre_close": "10.00",
                        "vol": "1234.5",
                    }
                ],
                trade_date=TRADE_DATE,
                source_timestamp=SOURCE_TIMESTAMP,
            )

    def test_rejects_illegal_tushare_ohlc(self) -> None:
        with self.assertRaisesRegex(self.normalization_error, "OHLC"):
            self.normalize_tushare_daily(
                [
                    {
                        "ts_code": "600001.SH",
                        "trade_date": "20260807",
                        "open": "10.01",
                        "high": "9.90",
                        "low": "9.98",
                        "close": "10.25",
                        "pre_close": "10.00",
                        "vol": "1234.5",
                        "amount": "12345.678",
                    }
                ],
                trade_date=TRADE_DATE,
                source_timestamp=SOURCE_TIMESTAMP,
            )

    def test_rejects_tushare_row_for_another_trade_date(self) -> None:
        with self.assertRaisesRegex(self.normalization_error, "trade_date"):
            self.normalize_tushare_daily(
                [
                    {
                        "ts_code": "600001.SH",
                        "trade_date": "20260806",
                        "open": "10.01",
                        "high": "10.55",
                        "low": "9.98",
                        "close": "10.25",
                        "pre_close": "10.00",
                        "vol": "1234.5",
                        "amount": "12345.678",
                    }
                ],
                trade_date=TRADE_DATE,
                source_timestamp=SOURCE_TIMESTAMP,
            )

    def test_rejects_mixed_source_tushare_row(self) -> None:
        with self.assertRaisesRegex(self.normalization_error, "source"):
            self.normalize_tushare_daily(
                [
                    {
                        "ts_code": "600001.SH",
                        "trade_date": "20260807",
                        "open": "10.01",
                        "high": "10.55",
                        "low": "9.98",
                        "close": "10.25",
                        "pre_close": "10.00",
                        "vol": "1234.5",
                        "amount": "12345.678",
                        "source": "akshare",
                    }
                ],
                trade_date=TRADE_DATE,
                source_timestamp=SOURCE_TIMESTAMP,
            )


class BaoStockMetadataNormalizationContractTest(ProviderContractTestCase):
    def test_joins_basic_profile_with_industry(self) -> None:
        securities = self.normalize_baostock_securities(
            [
                {
                    "code": "sh.600001",
                    "code_name": "样本主板",
                    "ipoDate": "2020-01-02",
                    "type": "1",
                    "status": "1",
                }
            ],
            [
                {
                    "code": "sh.600001",
                    "industry": "测试行业",
                    "industryClassification": "证监会行业分类",
                }
            ],
        )

        self.assertEqual(
            securities,
            (
                Security(
                    symbol="600001.SH",
                    name="样本主板",
                    exchange="SSE",
                    board="MAIN",
                    list_date=date(2020, 1, 2),
                    industry="测试行业",
                    is_st=False,
                ),
            ),
        )

    def test_normalizes_only_open_trading_days(self) -> None:
        trading_days = self.normalize_baostock_trading_calendar(
            [
                {"calendar_date": "2026-08-06", "is_trading_day": "1"},
                {"calendar_date": "2026-08-07", "is_trading_day": "1"},
                {"calendar_date": "2026-08-08", "is_trading_day": "0"},
            ]
        )

        self.assertEqual(trading_days, (date(2026, 8, 6), TRADE_DATE))

    def test_excludes_chinext_and_star_market_from_the_main_board_universe(self) -> None:
        securities = self.normalize_baostock_securities(
            [
                {
                    "code": "sz.300001",
                    "code_name": "创业板样本",
                    "ipoDate": "2020-01-02",
                    "type": "1",
                    "status": "1",
                },
                {
                    "code": "sh.688001",
                    "code_name": "科创板样本",
                    "ipoDate": "2020-01-02",
                    "type": "1",
                    "status": "1",
                },
            ],
            [],
        )

        self.assertEqual(securities, ())


class AKShareSnapshotNormalizationContractTest(ProviderContractTestCase):
    def test_normalizes_akshare_snapshot_units_and_sz_symbol(self) -> None:
        bars = self.normalize_akshare_snapshot(
            [
                {
                    "代码": "000001",
                    "日期": "2026-08-07",
                    "开盘": "12.34",
                    "最高": "12.80",
                    "最低": "12.20",
                    "收盘": "12.60",
                    "昨收": "12.30",
                    "成交量": "3210",
                    "成交额": "39641400",
                }
            ],
            trade_date=TRADE_DATE,
            source_timestamp=SOURCE_TIMESTAMP,
        )

        self.assertEqual(
            bars,
            (
                DailyBar(
                    symbol="000001.SZ",
                    trade_date=TRADE_DATE,
                    open_1e4=123_400,
                    high_1e4=128_000,
                    low_1e4=122_000,
                    close_1e4=126_000,
                    pre_close_1e4=123_000,
                    volume_shares=321_000,
                    amount_fen=3_964_140_000,
                    source="akshare",
                    source_timestamp=SOURCE_TIMESTAMP,
                ),
            ),
        )

    def test_rejects_missing_required_akshare_column(self) -> None:
        with self.assertRaisesRegex(self.normalization_error, "成交额"):
            self.normalize_akshare_snapshot(
                [
                    {
                        "代码": "000001",
                        "日期": "2026-08-07",
                        "开盘": "12.34",
                        "最高": "12.80",
                        "最低": "12.20",
                        "收盘": "12.60",
                        "昨收": "12.30",
                        "成交量": "3210",
                    }
                ],
                trade_date=TRADE_DATE,
                source_timestamp=SOURCE_TIMESTAMP,
            )

    def test_rejects_illegal_akshare_ohlc(self) -> None:
        with self.assertRaisesRegex(self.normalization_error, "OHLC"):
            self.normalize_akshare_snapshot(
                [
                    {
                        "代码": "000001",
                        "日期": "2026-08-07",
                        "开盘": "12.34",
                        "最高": "12.00",
                        "最低": "12.20",
                        "收盘": "12.60",
                        "昨收": "12.30",
                        "成交量": "3210",
                        "成交额": "39641400",
                    }
                ],
                trade_date=TRADE_DATE,
                source_timestamp=SOURCE_TIMESTAMP,
            )

    def test_rejects_akshare_row_for_another_trade_date(self) -> None:
        with self.assertRaisesRegex(self.normalization_error, "trade_date"):
            self.normalize_akshare_snapshot(
                [
                    {
                        "代码": "000001",
                        "日期": "2026-08-06",
                        "开盘": "12.34",
                        "最高": "12.80",
                        "最低": "12.20",
                        "收盘": "12.60",
                        "昨收": "12.30",
                        "成交量": "3210",
                        "成交额": "39641400",
                    }
                ],
                trade_date=TRADE_DATE,
                source_timestamp=SOURCE_TIMESTAMP,
            )

    def test_rejects_mixed_source_akshare_row(self) -> None:
        with self.assertRaisesRegex(self.normalization_error, "source"):
            self.normalize_akshare_snapshot(
                [
                    {
                        "代码": "000001",
                        "日期": "2026-08-07",
                        "开盘": "12.34",
                        "最高": "12.80",
                        "最低": "12.20",
                        "收盘": "12.60",
                        "昨收": "12.30",
                        "成交量": "3210",
                        "成交额": "39641400",
                        "source": "tushare",
                    }
                ],
                trade_date=TRADE_DATE,
                source_timestamp=SOURCE_TIMESTAMP,
            )


if __name__ == "__main__":
    unittest.main()
