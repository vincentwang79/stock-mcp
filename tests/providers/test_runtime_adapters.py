"""Offline runtime-adapter contracts; all clients are injected fakes."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta

from stock_mcp.domain import DailyBar, Security

TRADE_DATE = date(2026, 8, 7)
NOW = datetime(2026, 8, 7, 16, 35, tzinfo=UTC)
SECURITIES = (
    Security("600001.SH", "样本沪市", "SSE", "MAIN", date(2020, 1, 2), "测试", False),
    Security("000001.SZ", "样本深市", "SZSE", "MAIN", date(2020, 1, 2), "测试", False),
)


class _Frame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.orient: str | None = None

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        self.orient = orient
        return self.rows


class _TushareClient:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.frame = _Frame(rows)
        self.requested_date: str | None = None

    def daily(self, *, trade_date: str) -> _Frame:
        self.requested_date = trade_date
        return self.frame


class _AKShareClient:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.frame = _Frame(rows)
        self.calls = 0

    def stock_zh_a_spot_em(self) -> _Frame:
        self.calls += 1
        return self.frame


class _UnavailableAKShareClient:
    def stock_zh_a_spot_em(self) -> _Frame:
        raise OSError("proxy tunnel closed upstream connection")


def _tushare_rows() -> list[dict[str, object]]:
    return [
        {
            "ts_code": "600001.SH",
            "trade_date": "20260807",
            "open": "10.00",
            "high": "11.20",
            "low": "9.90",
            "close": "11.00",
            "pre_close": "10.00",
            "vol": "100",
            "amount": "1000",
        },
        {
            "ts_code": "000001.SZ",
            "trade_date": "20260807",
            "open": "10.00",
            "high": "10.10",
            "low": "8.90",
            "close": "9.00",
            "pre_close": "10.00",
            "vol": "100",
            "amount": "1000",
        },
    ]


def _history_bar(symbol: str, trade_date: date, close_1e4: int) -> DailyBar:
    return DailyBar(
        symbol=symbol,
        trade_date=trade_date,
        open_1e4=close_1e4,
        high_1e4=close_1e4,
        low_1e4=close_1e4,
        close_1e4=close_1e4,
        pre_close_1e4=close_1e4,
        volume_shares=0,
        amount_fen=0,
        source="tushare",
        source_timestamp=NOW,
    )


class RuntimeAdapterContractTestCase(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from stock_mcp.providers.runtime import (
                AKShareQuoteProvider,
                AKShareSnapshotProvider,
                BaoStockTradingCalendar,
                ProviderRuntimeError,
                TushareDailyProvider,
            )
        except (ImportError, AttributeError) as error:
            self.fail(f"runtime provider contract is not implemented: {error}")
        self.AKShareQuoteProvider = AKShareQuoteProvider
        self.AKShareSnapshotProvider = AKShareSnapshotProvider
        self.BaoStockTradingCalendar = BaoStockTradingCalendar
        self.ProviderRuntimeError = ProviderRuntimeError
        self.TushareDailyProvider = TushareDailyProvider


class TushareRuntimeAdapterContractTest(RuntimeAdapterContractTestCase):
    def test_ignores_non_research_exchange_rows_before_normalization(self) -> None:
        rows = _tushare_rows() + [
            {
                "ts_code": "920001.BJ",
                "trade_date": "20260807",
                "open": "10.00",
                "high": "10.50",
                "low": "9.90",
                "close": "10.20",
                "pre_close": "10.00",
                "vol": "100",
                "amount": "1000",
            }
        ]

        snapshot = self.TushareDailyProvider(
            client=_TushareClient(rows),
            securities=SECURITIES,
            history_loader=lambda _symbol, _until: (),
            clock=lambda: NOW,
        ).fetch_snapshot(TRADE_DATE)

        target_symbols = tuple(bar.symbol for bar in snapshot.bars if bar.trade_date == TRADE_DATE)
        self.assertEqual(target_symbols, ("600001.SH", "000001.SZ"))

    def test_fetches_day_dataframe_normalizes_and_calculates_snapshot_breadth(self) -> None:
        client = _TushareClient(_tushare_rows())
        requested_until: list[date] = []

        def history_loader(symbol: str, until: date) -> tuple[DailyBar, ...]:
            requested_until.append(until)
            # A future high close must not contaminate MA20 breadth.
            history = tuple(
                _history_bar(symbol, TRADE_DATE - timedelta(days=20 - index), 100_000)
                for index in range(1, 20)
            )
            return history + (_history_bar(symbol, TRADE_DATE + timedelta(days=1), 9_999_999),)

        snapshot = self.TushareDailyProvider(
            client=client,
            securities=SECURITIES,
            history_loader=history_loader,
            clock=lambda: NOW,
        ).fetch_snapshot(TRADE_DATE)

        self.assertEqual(client.requested_date, "20260807")
        self.assertEqual(client.frame.orient, "records")
        self.assertEqual(requested_until, [TRADE_DATE, TRADE_DATE])
        self.assertEqual(snapshot.source, "tushare")
        self.assertEqual(snapshot.source_timestamp, NOW)
        self.assertEqual(snapshot.securities, SECURITIES)
        target = tuple(bar for bar in snapshot.bars if bar.trade_date == TRADE_DATE)
        self.assertEqual(tuple(bar.symbol for bar in target), ("600001.SH", "000001.SZ"))
        self.assertEqual(
            40, len(snapshot.bars), "19 same-source history rows per symbol are retained"
        )
        self.assertTrue(all(bar.source == "tushare" for bar in snapshot.bars))
        self.assertTrue(all(bar.trade_date <= TRADE_DATE for bar in snapshot.bars))
        self.assertEqual(snapshot.advance_ratio_bps, 5_000)
        self.assertEqual(snapshot.above_ma20_ratio_bps, 5_000)

    def test_passes_no_future_cutoff_to_history_loader(self) -> None:
        client = _TushareClient(_tushare_rows())

        def future_history_loader(symbol: str, until: date) -> tuple[DailyBar, ...]:
            self.assertLessEqual(until, TRADE_DATE)
            return ()

        snapshot = self.TushareDailyProvider(
            client=client,
            securities=SECURITIES,
            history_loader=future_history_loader,
            clock=lambda: NOW,
        ).fetch_snapshot(TRADE_DATE)

        self.assertEqual(snapshot.trade_date, TRADE_DATE)

    def test_rejects_mixed_source_history_instead_of_relabelling_it(self) -> None:
        client = _TushareClient(_tushare_rows())

        def mixed_history(symbol: str, until: date) -> tuple[DailyBar, ...]:
            return (
                _history_bar(symbol, until - timedelta(days=1), 100_000).with_source("akshare"),
            )

        with self.assertRaisesRegex(self.ProviderRuntimeError, "history source"):
            self.TushareDailyProvider(
                client=client,
                securities=SECURITIES,
                history_loader=mixed_history,
                clock=lambda: NOW,
            ).fetch_snapshot(TRADE_DATE)

    def test_st_securities_do_not_change_breadth_or_ma20_metrics(self) -> None:
        st_security = Security("600099.SH", "ST样本", "SSE", "MAIN", date(2020, 1, 2), "测试", True)
        rows = _tushare_rows() + [
            {
                "ts_code": st_security.symbol,
                "trade_date": "20260807",
                "open": "10.00",
                "high": "20.00",
                "low": "10.00",
                "close": "20.00",
                "pre_close": "10.00",
                "vol": "100",
                "amount": "2000",
            }
        ]

        snapshot = self.TushareDailyProvider(
            client=_TushareClient(rows),
            securities=(*SECURITIES, st_security),
            history_loader=lambda symbol, _until: tuple(
                _history_bar(symbol, TRADE_DATE - timedelta(days=20 - index), 100_000)
                for index in range(1, 20)
            ),
            clock=lambda: NOW,
        ).fetch_snapshot(TRADE_DATE)

        self.assertEqual(5_000, snapshot.advance_ratio_bps)
        self.assertEqual(5_000, snapshot.above_ma20_ratio_bps)


class BaoStockTradingCalendarContractTest(RuntimeAdapterContractTestCase):
    def test_constructs_trading_day_lookup_from_baostock_rows(self) -> None:
        calendar = self.BaoStockTradingCalendar.from_rows(
            [
                {"calendar_date": "2026-08-07", "is_trading_day": "1"},
                {"calendar_date": "2026-08-08", "is_trading_day": "0"},
            ]
        )

        self.assertTrue(calendar.is_trading_day(TRADE_DATE))
        self.assertFalse(calendar.is_trading_day(date(2026, 8, 8)))


class AKShareRuntimeAdapterContractTest(RuntimeAdapterContractTestCase):
    def test_spot_snapshot_only_accepts_clock_today_and_never_claims_history_mirror(self) -> None:
        client = _AKShareClient(
            [
                {
                    "代码": "000001",
                    "开盘": "10.00",
                    "最高": "10.50",
                    "最低": "9.90",
                    "最新价": "10.25",
                    "昨收": "10.00",
                    "成交量": "100",
                    "成交额": "1025000",
                }
            ]
        )
        provider = self.AKShareSnapshotProvider(
            client=client, securities=SECURITIES, clock=lambda: NOW
        )

        snapshot = provider.fetch_snapshot(TRADE_DATE)

        self.assertEqual(client.calls, 1)
        self.assertFalse(provider.has_historical_mirror)
        self.assertEqual(snapshot.source, "akshare")
        self.assertEqual(snapshot.bars[0].symbol, "000001.SZ")
        self.assertEqual(snapshot.bars[0].close_1e4, 102_500)
        with self.assertRaisesRegex(self.ProviderRuntimeError, "current date"):
            provider.fetch_snapshot(TRADE_DATE - timedelta(days=1))

    def test_quote_fetch_is_explicit_and_returns_only_a_matching_normalized_symbol(self) -> None:
        client = _AKShareClient(
            [
                {"代码": "600001", "最新价": "11.25"},
                {"代码": "000001", "最新价": "10.25"},
            ]
        )
        provider = self.AKShareQuoteProvider(client=client, clock=lambda: NOW)

        quote = provider.fetch_quote("600001.SH")

        self.assertEqual(client.calls, 1)
        self.assertEqual(quote, {"close_1e4": 112_500, "source": "akshare", "as_of": NOW})

    def test_quote_rejects_unknown_symbol_and_missing_latest_price(self) -> None:
        client = _AKShareClient([{"代码": "000001", "最新价": "10.25"}])
        provider = self.AKShareQuoteProvider(client=client, clock=lambda: NOW)

        with self.assertRaisesRegex(self.ProviderRuntimeError, "not found"):
            provider.fetch_quote("600001.SH")

        with self.assertRaisesRegex(self.ProviderRuntimeError, "latest price"):
            self.AKShareQuoteProvider(
                client=_AKShareClient([{"代码": "600001"}]),
                clock=lambda: NOW,
            ).fetch_quote("600001.SH")

    def test_quote_normalizes_transport_failure_to_provider_runtime_error(self) -> None:
        provider = self.AKShareQuoteProvider(client=_UnavailableAKShareClient(), clock=lambda: NOW)

        with self.assertRaisesRegex(self.ProviderRuntimeError, "quote request failed"):
            provider.fetch_quote("600001.SH")


if __name__ == "__main__":
    unittest.main()
