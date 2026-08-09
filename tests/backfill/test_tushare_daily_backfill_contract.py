"""Offline, point-in-time contracts for Tushare daily snapshot backfills."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from stock_mcp.config import Settings
from stock_mcp.domain import DailyBar, MarketSnapshot, Security, StrategyVersion
from stock_mcp.replay import HistoricalReplayService
from stock_mcp.storage import Database

try:
    from stock_mcp.backfill import TushareDailyBackfillService
except ImportError:
    TushareDailyBackfillService = None  # type: ignore[assignment,misc]

try:
    from stock_mcp.backfill import run_production_backfill
except ImportError:
    run_production_backfill = None  # type: ignore[assignment,misc]

SOURCE = "tushare"
START = date(2023, 8, 7)
MIDDLE = date(2025, 2, 7)
END = date(2026, 8, 7)
TRADING_DAYS = (START, MIDDLE, END)
SOURCE_TIMESTAMP = datetime(2026, 8, 7, 8, 30, tzinfo=UTC)


class RecordedTradingCalendar:
    """Fixed, offline calendar fixture; it never consults a market endpoint."""

    def __init__(self, trading_days: tuple[date, ...]) -> None:
        self.trading_days = frozenset(trading_days)
        self.queries: list[date] = []

    def is_trading_day(self, target: date) -> bool:
        self.queries.append(target)
        return target in self.trading_days


class RecordedTushareDailyProvider:
    """Recorded normalized Tushare results, injected instead of a live client."""

    source = SOURCE

    def __init__(self, snapshots: dict[date, list[MarketSnapshot]]) -> None:
        self._snapshots = snapshots
        self.requests: list[date] = []

    def fetch_snapshot(self, trade_date: date) -> MarketSnapshot:
        self.requests.append(trade_date)
        return self._snapshots[trade_date].pop(0)


class _SimulatedMonotonicClock:
    def __init__(self) -> None:
        self.current = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds


class RecordedStrategyRegistry:
    def __init__(self, strategies: StrategyVersion) -> None:
        self._strategies = {strategies.version: strategies}

    def get(self, version: str) -> StrategyVersion:
        return self._strategies[version]


def _strategy() -> StrategyVersion:
    return StrategyVersion(
        version="v0.1-proposed",
        status="proposed",
        parameters={
            "rule_engine_version": 1,
            "offensive_min_bps": 5_500,
            "defensive_max_bps": 4_000,
            "neutral_limit": 2,
            "offensive_limit": 3,
            "min_liquidity_amount_fen": 2_000_000_000,
            "max_consecutive_limit_up_days": 2,
            "strong_pullback_min_prior_gain_bps": 1_000,
            "strong_pullback_max_pullback_bps": 800,
            "volume_breakout_min_volume_ratio_bps": 15_000,
        },
    )


def _snapshot(trade_date: date, *, complete: bool = True) -> MarketSnapshot:
    securities = (
        Security(
            symbol="600001.SH",
            name="记录样本沪市",
            exchange="SSE",
            board="MAIN",
            list_date=date(2020, 1, 2),
            industry="测试",
            is_st=False,
        ),
        Security(
            symbol="000001.SZ",
            name="记录样本深市",
            exchange="SZSE",
            board="MAIN",
            list_date=date(2020, 1, 2),
            industry="测试",
            is_st=False,
        ),
    )
    bars = tuple(
        DailyBar(
            symbol=security.symbol,
            trade_date=trade_date,
            open_1e4=100_000 + index * 1_000,
            high_1e4=106_000 + index * 1_000,
            low_1e4=99_000 + index * 1_000,
            close_1e4=104_000 + index * 1_000,
            pre_close_1e4=100_000 + index * 1_000,
            volume_shares=1_000_000,
            amount_fen=8_000_000_000,
            source=SOURCE,
            source_timestamp=SOURCE_TIMESTAMP,
        )
        for index, security in enumerate(securities)
    )
    return MarketSnapshot(
        trade_date=trade_date,
        source=SOURCE,
        source_timestamp=SOURCE_TIMESTAMP,
        securities=securities,
        bars=bars if complete else bars[:1],
        advance_ratio_bps=6_000,
        above_ma20_ratio_bps=6_000,
    )


class TushareDailyBackfillContractTest(unittest.TestCase):
    def _service(
        self,
        *,
        database: Database,
        calendar: RecordedTradingCalendar,
        provider: RecordedTushareDailyProvider,
        **kwargs: object,
    ) -> object:
        self.assertIsNotNone(
            TushareDailyBackfillService,
            "Tushare historical backfill contract is not implemented: "
            "stock_mcp.backfill.TushareDailyBackfillService is missing",
        )
        return TushareDailyBackfillService(
            database=database,
            calendar=calendar,
            provider=provider,
            expected_main_board_count=2,
            **kwargs,
        )

    def test_backfills_recorded_tushare_trading_days_for_walk_forward_comparison(self) -> None:
        """Three-year recorded range becomes point-in-time snapshots usable by replay."""
        calendar = RecordedTradingCalendar(TRADING_DAYS)
        provider = RecordedTushareDailyProvider(
            {trade_date: [_snapshot(trade_date)] for trade_date in TRADING_DAYS}
        )
        strategy = _strategy()

        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "research.sqlite3")
            database.initialize()

            result = self._service(
                database=database, calendar=calendar, provider=provider
            ).backfill(START, END)

            self.assertEqual(result.published_dates, TRADING_DAYS)
            self.assertEqual(provider.requests, list(TRADING_DAYS))
            stored_dates = tuple(
                snapshot.trade_date for snapshot in database.load_market_snapshots(START, END)
            )
            self.assertEqual(
                stored_dates,
                TRADING_DAYS,
            )
            comparison = HistoricalReplayService(
                database, RecordedStrategyRegistry(strategy)
            ).compare(strategy.version, strategy.version, START, END)

        self.assertEqual(comparison["days_compared"], len(TRADING_DAYS))
        self.assertEqual(
            tuple(item["trade_date"] for item in comparison["daily"]),
            tuple(day.isoformat() for day in TRADING_DAYS),
        )

    def test_rerun_is_idempotent_and_does_not_refetch_published_trading_days(self) -> None:
        calendar = RecordedTradingCalendar((START,))
        provider = RecordedTushareDailyProvider({START: [_snapshot(START)]})

        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "research.sqlite3")
            database.initialize()
            service = self._service(database=database, calendar=calendar, provider=provider)

            first = service.backfill(START, START)
            second = service.backfill(START, START)

            self.assertEqual(first.published_dates, (START,))
            self.assertEqual(second.published_dates, ())
            self.assertEqual(provider.requests, [START])
            self.assertEqual(len(database.load_market_snapshots(START, START)), 1)

    def test_rerun_uses_lightweight_snapshot_presence_check(self) -> None:
        class PresenceOnlyDatabase:
            def __init__(self) -> None:
                self.presence_checks = 0
                self.full_snapshot_loads = 0

            def has_market_snapshot(self, target: date, *, source: str) -> bool:
                self.presence_checks += 1
                if target != START or source != SOURCE:
                    raise AssertionError("backfill presence lookup used the wrong snapshot key")
                return True

            def load_market_snapshots(self, *_args: object, **_kwargs: object) -> tuple[MarketSnapshot, ...]:
                self.full_snapshot_loads += 1
                return (_snapshot(START),)

            @staticmethod
            def save_market_snapshot(_snapshot: MarketSnapshot) -> None:
                raise AssertionError("an existing snapshot must not be written again")

        database = PresenceOnlyDatabase()
        result = self._service(
            database=database,  # type: ignore[arg-type]
            calendar=RecordedTradingCalendar((START,)),
            provider=RecordedTushareDailyProvider({}),
        ).backfill(START, START)

        self.assertEqual(result.published_dates, ())
        self.assertEqual(result.incomplete_dates, ())
        self.assertEqual(database.presence_checks, 1)
        self.assertEqual(database.full_snapshot_loads, 0)

    def test_incomplete_day_is_not_published_and_a_later_run_resumes_from_that_day(self) -> None:
        calendar = RecordedTradingCalendar((START, END))
        provider = RecordedTushareDailyProvider(
            {
                START: [_snapshot(START)],
                END: [_snapshot(END, complete=False), _snapshot(END)],
            }
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "research.sqlite3")
            database.initialize()
            service = self._service(database=database, calendar=calendar, provider=provider)

            first = service.backfill(START, END)

            self.assertEqual(first.published_dates, (START,))
            self.assertEqual(first.incomplete_dates, (END,))
            stored_dates = tuple(
                snapshot.trade_date for snapshot in database.load_market_snapshots(START, END)
            )
            self.assertEqual(
                stored_dates,
                (START,),
            )

            resumed = service.backfill(START, END)

            self.assertEqual(resumed.published_dates, (END,))
            self.assertEqual(provider.requests, [START, END, END])
            stored_dates = tuple(
                snapshot.trade_date for snapshot in database.load_market_snapshots(START, END)
            )
            self.assertEqual(
                stored_dates,
                (START, END),
            )

    def test_reports_the_first_incomplete_day_and_safe_validation_reason(self) -> None:
        reported: list[tuple[date, str]] = []
        provider = RecordedTushareDailyProvider({START: [_snapshot(START, complete=False)]})

        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "research.sqlite3")
            database.initialize()

            result = self._service(
                database=database,
                calendar=RecordedTradingCalendar((START,)),
                provider=provider,
                on_incomplete=lambda target, error: reported.append((target, str(error))),
            ).backfill(START, START)

        self.assertEqual(result.incomplete_dates, (START,))
        self.assertEqual(reported, [(START, "snapshot has incomplete target-day bars")])

    def test_rejects_future_bars_instead_of_publishing_a_look_ahead_snapshot(self) -> None:
        calendar = RecordedTradingCalendar((START,))
        future_bar = replace(_snapshot(START).bars[0], trade_date=START + timedelta(days=1))
        contaminated = replace(_snapshot(START), bars=(future_bar, _snapshot(START).bars[1]))
        provider = RecordedTushareDailyProvider({START: [contaminated]})

        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "research.sqlite3")
            database.initialize()

            result = self._service(
                database=database, calendar=calendar, provider=provider
            ).backfill(START, START)

            self.assertEqual(result.published_dates, ())
            self.assertEqual(result.incomplete_dates, (START,))
            self.assertEqual(database.load_market_snapshots(START, START), ())

    def test_database_failure_is_not_misreported_as_incomplete_provider_data(self) -> None:
        class FailingDatabase:
            @staticmethod
            def load_market_snapshots(*_args: object, **_kwargs: object) -> tuple[()]:
                return ()

            @staticmethod
            def save_market_snapshot(_snapshot: MarketSnapshot) -> None:
                raise OSError("database disk full")

        service = self._service(
            database=FailingDatabase(),
            calendar=RecordedTradingCalendar((START,)),
            provider=RecordedTushareDailyProvider({START: [_snapshot(START)]}),
        )

        with self.assertRaisesRegex(OSError, "disk full"):
            service.backfill(START, START)

    def test_spaces_tushare_attempts_and_retries_a_temporary_upstream_failure(self) -> None:
        clock = _SimulatedMonotonicClock()

        class TemporarilyUnavailableProvider:
            source = SOURCE

            def __init__(self) -> None:
                self.request_times: list[float] = []
                self.attempts = 0

            def fetch_snapshot(self, trade_date: date) -> MarketSnapshot:
                self.request_times.append(clock.monotonic())
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("HTTP 429 rate limit exceeded")
                return _snapshot(trade_date)

        provider = TemporarilyUnavailableProvider()
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "research.sqlite3")
            database.initialize()

            result = self._service(
                database=database,
                calendar=RecordedTradingCalendar((START,)),
                provider=provider,  # type: ignore[arg-type]
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            ).backfill(START, START)

        self.assertEqual(result.published_dates, (START,))
        self.assertEqual(provider.attempts, 2)
        self.assertGreaterEqual(provider.request_times[1] - provider.request_times[0], 1.34)
        self.assertEqual(clock.sleeps[0], 1.0)
        self.assertAlmostEqual(clock.sleeps[1], 0.34)

    def test_does_not_retry_a_validation_error(self) -> None:
        class InvalidSnapshotProvider:
            source = SOURCE

            def __init__(self) -> None:
                self.attempts = 0

            def fetch_snapshot(self, trade_date: date) -> MarketSnapshot:
                self.attempts += 1
                return _snapshot(trade_date, complete=False)

        provider = InvalidSnapshotProvider()
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "research.sqlite3")
            database.initialize()

            result = self._service(
                database=database,
                calendar=RecordedTradingCalendar((START,)),
                provider=provider,  # type: ignore[arg-type]
            ).backfill(START, START)

        self.assertEqual(result.incomplete_dates, (START,))
        self.assertEqual(provider.attempts, 1)

    def test_persists_the_tushare_trading_day_coverage_when_database_supports_it(self) -> None:
        class CalendarRecordingDatabase:
            def __init__(self) -> None:
                self.coverage: list[tuple[str, tuple[date, ...]]] = []

            @staticmethod
            def load_market_snapshots(*_args: object, **_kwargs: object) -> tuple[()]:
                return ()

            @staticmethod
            def save_market_snapshot(_snapshot: MarketSnapshot) -> None:
                return None

            def save_expected_trading_days(self, source: str, dates: tuple[date, ...]) -> None:
                self.coverage.append((source, dates))

        database = CalendarRecordingDatabase()
        service = self._service(
            database=database,  # type: ignore[arg-type]
            calendar=RecordedTradingCalendar((START, END)),
            provider=RecordedTushareDailyProvider(
                {START: [_snapshot(START)], END: [_snapshot(END)]}
            ),
        )

        service.backfill(START, END)

        self.assertEqual(database.coverage, [(SOURCE, (START, END))])


PRODUCTION_START = date(2023, 8, 7)
PRODUCTION_END = date(2023, 8, 8)
PRODUCTION_DAYS = (PRODUCTION_START, PRODUCTION_END)
PRODUCTION_TIMESTAMP = datetime(2026, 8, 7, 8, 30, tzinfo=UTC)


class _RecordedFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        if orient != "records":
            raise AssertionError("Tushare data frames must be read as records")
        return self._rows


class _RecordedTushareProductionClient:
    def __init__(self, rows_by_date: dict[str, list[dict[str, object]]]) -> None:
        self._rows_by_date = rows_by_date
        self.daily_requests: list[str] = []
        self.daily_basic_requests = 0

    def daily(self, *, trade_date: str) -> _RecordedFrame:
        self.daily_requests.append(trade_date)
        return _RecordedFrame(self._rows_by_date[trade_date])

    def daily_basic(self, **_kwargs: object) -> _RecordedFrame:
        self.daily_basic_requests += 1
        raise AssertionError("historical backfill must not use paid Tushare daily_basic")


class _BaoStockRows:
    def __init__(self, fields: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
        self.error_code = "0"
        self.fields = fields
        self._rows = rows
        self._offset = -1

    def next(self) -> bool:
        self._offset += 1
        return self._offset < len(self._rows)

    def get_row_data(self) -> list[str]:
        return list(self._rows[self._offset])


class _RecordedBaoStockClient:
    def __init__(self) -> None:
        self.login_calls = 0
        self.logout_calls = 0
        self.calendar_requests: list[tuple[str, str]] = []
        self.all_stock_requests: list[str] = []
        self.basic_requests: list[str] = []
        self.industry_requests = 0

    def login(self) -> _BaoStockRows:
        self.login_calls += 1
        return _BaoStockRows((), [])

    def logout(self) -> None:
        self.logout_calls += 1

    def query_trade_dates(self, *, start_date: str, end_date: str) -> _BaoStockRows:
        self.calendar_requests.append((start_date, end_date))
        return _BaoStockRows(
            ("calendar_date", "is_trading_day"),
            [(day.isoformat(), "1") for day in PRODUCTION_DAYS],
        )

    def query_all_stock(self, *, day: str) -> _BaoStockRows:
        self.all_stock_requests.append(day)
        rows = {
            PRODUCTION_START.isoformat(): [
                ("sh.600001", "常规甲", "1"),
                ("sh.600002", "退市样本", "1"),
                ("sh.600003", "*ST 样本", "1"),
                ("sz.000001", "停牌样本", "0"),
                ("sh.600004", "行业待定", "1"),
            ],
            PRODUCTION_END.isoformat(): [
                ("sh.600001", "常规甲", "1"),
                ("sh.600004", "行业待定", "1"),
            ],
        }
        return _BaoStockRows(("code", "code_name", "tradeStatus"), rows[day])

    def query_stock_basic(self) -> _BaoStockRows:
        """Match BaoStock's production API, which takes no ``date`` argument."""
        self.basic_requests.append("called")
        return _BaoStockRows(
            ("code", "code_name", "ipoDate", "type", "status"),
            [
                ("sh.600001", "常规甲", "2010-01-01", "1", "1"),
                ("sh.600002", "退市样本", "2010-01-01", "1", "0"),
                ("sh.600003", "*ST 样本", "2010-01-01", "1", "1"),
                ("sz.000001", "停牌样本", "2010-01-01", "1", "1"),
                ("sh.600004", "行业待定", "2010-01-01", "1", "1"),
            ],
        )

    def query_stock_industry(self) -> _BaoStockRows:
        self.industry_requests += 1
        return _BaoStockRows(
            ("code", "industry", "updateDate"),
            [
                ("sh.600001", "银行", "2023-08-01"),
                ("sh.600002", "采矿", "2023-08-01"),
                ("sh.600004", "未来行业", "2023-08-09"),
            ],
        )


def _production_tushare_rows(target: date) -> list[dict[str, object]]:
    symbols = (
        ("600001.SH", "10.00"),
        ("600002.SH", "11.00"),
        ("600003.SH", "12.00"),
        ("000001.SZ", "13.00"),
        ("600004.SH", "14.00"),
    )
    return [
        {
            "ts_code": symbol,
            "trade_date": target.strftime("%Y%m%d"),
            "open": close,
            "high": str(float(close) + 0.20),
            "low": str(float(close) - 0.10),
            "close": str(float(close) + 0.10),
            "pre_close": close,
            "vol": "100",
            "amount": "1000",
        }
        for symbol, close in symbols
    ]


class ProductionBackfillCompositionContractTest(unittest.TestCase):
    def _run(
        self,
        *,
        database: Database,
        tushare: _RecordedTushareProductionClient,
        baostock: _RecordedBaoStockClient,
    ) -> object:
        self.assertIsNotNone(
            run_production_backfill,
            "production historical backfill contract is not implemented: "
            "stock_mcp.backfill.run_production_backfill is missing",
        )
        return run_production_backfill(
            settings=Settings(root=database.path.parent.parent, tushare_token="fixture"),
            database=database,
            start=PRODUCTION_START,
            end=PRODUCTION_END,
            tushare_client=tushare,
            baostock_client=baostock,
            clock=lambda: PRODUCTION_TIMESTAMP,
            minimum_main_board_count=2,
        )

    @staticmethod
    def _clients() -> tuple[_RecordedTushareProductionClient, _RecordedBaoStockClient]:
        tushare = _RecordedTushareProductionClient(
            {day.strftime("%Y%m%d"): _production_tushare_rows(day) for day in PRODUCTION_DAYS}
        )
        return tushare, _RecordedBaoStockClient()

    def test_calls_tushare_daily_for_each_historical_trading_day_without_daily_basic(self) -> None:
        tushare, baostock = self._clients()

        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "data" / "research.sqlite3")
            database.initialize()

            result = self._run(database=database, tushare=tushare, baostock=baostock)
            snapshots = database.load_market_snapshots(PRODUCTION_START, PRODUCTION_END)

        self.assertEqual(result.published_dates, PRODUCTION_DAYS)
        self.assertEqual(tushare.daily_requests, ["20230807", "20230808"])
        self.assertEqual(tushare.daily_basic_requests, 0)
        self.assertTrue(
            all(snapshot.source_timestamp == PRODUCTION_TIMESTAMP for snapshot in snapshots)
        )
        self.assertTrue(
            all(
                bar.source_timestamp == PRODUCTION_TIMESTAMP
                and bar.trade_date <= snapshot.trade_date
                for snapshot in snapshots
                for bar in snapshot.bars
            )
        )

    def test_opens_one_baostock_session_for_range_calendar_and_daily_universe(self) -> None:
        tushare, baostock = self._clients()

        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "data" / "research.sqlite3")
            database.initialize()

            self._run(database=database, tushare=tushare, baostock=baostock)

        self.assertEqual(baostock.login_calls, 1)
        self.assertEqual(baostock.logout_calls, 1)
        self.assertEqual(
            baostock.calendar_requests,
            [(PRODUCTION_START.isoformat(), PRODUCTION_END.isoformat())],
        )
        self.assertEqual(baostock.basic_requests, ["called"])
        self.assertEqual(
            baostock.all_stock_requests,
            [day.isoformat() for day in PRODUCTION_DAYS],
        )

    def test_uses_point_in_time_universe_without_st_suspended_or_future_industry(self) -> None:
        tushare, baostock = self._clients()

        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "data" / "research.sqlite3")
            database.initialize()

            self._run(database=database, tushare=tushare, baostock=baostock)
            first, second = database.load_market_snapshots(PRODUCTION_START, PRODUCTION_END)

        first_by_symbol = {security.symbol: security for security in first.securities}
        self.assertEqual(
            tuple(first_by_symbol),
            ("600001.SH", "600002.SH", "600004.SH"),
            "a historical universe retains a then-listed stock even if it is now delisted",
        )
        self.assertNotIn("600003.SH", first_by_symbol, "the target-day ST name is excluded")
        self.assertNotIn("000001.SZ", first_by_symbol, "the target-day suspension is excluded")
        self.assertEqual(first_by_symbol["600004.SH"].industry, "")
        self.assertNotIn("600002.SH", {security.symbol for security in second.securities})

    def test_existing_snapshot_skips_tushare_provider_requests(self) -> None:
        tushare, baostock = self._clients()

        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "data" / "research.sqlite3")
            database.initialize()
            database.save_market_snapshot(_snapshot(PRODUCTION_START))

            self._run(database=database, tushare=tushare, baostock=baostock)

        self.assertEqual(tushare.daily_requests, ["20230808"])

    def test_probes_the_latest_tushare_day_before_opening_baostock(self) -> None:
        tushare, baostock = self._clients()
        probes: list[tuple[date, int]] = []

        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "data" / "research.sqlite3")
            database.initialize()
            result = run_production_backfill(
                settings=Settings(root=database.path.parent.parent, tushare_token="fixture"),
                database=database,
                start=PRODUCTION_START,
                end=PRODUCTION_END,
                tushare_client=tushare,
                baostock_client=baostock,
                clock=lambda: PRODUCTION_TIMESTAMP,
                minimum_main_board_count=2,
                on_tushare_probe=lambda target, count: probes.append((target, count)),
            )

        self.assertEqual(result.published_dates, PRODUCTION_DAYS)
        self.assertEqual(probes, [(PRODUCTION_END, 5)])
        self.assertEqual(tushare.daily_requests, ["20230808", "20230807", "20230808"])
        self.assertEqual(baostock.login_calls, 1)

    def test_latest_probe_walks_back_to_the_nearest_day_with_tushare_rows(self) -> None:
        friday = date(2023, 8, 11)
        saturday = date(2023, 8, 12)
        tushare = _RecordedTushareProductionClient(
            {
                saturday.strftime("%Y%m%d"): [],
                friday.strftime("%Y%m%d"): _production_tushare_rows(friday),
            }
        )
        probes: list[tuple[date, int]] = []

        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "data" / "research.sqlite3")
            database.initialize()
            database.save_market_snapshot(_snapshot(PRODUCTION_START))
            database.save_market_snapshot(_snapshot(PRODUCTION_END))
            result = run_production_backfill(
                settings=Settings(root=database.path.parent.parent, tushare_token="fixture"),
                database=database,
                start=PRODUCTION_START,
                end=saturday,
                tushare_client=tushare,
                baostock_client=_RecordedBaoStockClient(),
                clock=lambda: PRODUCTION_TIMESTAMP,
                minimum_main_board_count=2,
                on_tushare_probe=lambda target, count: probes.append((target, count)),
            )

        self.assertEqual(result.incomplete_dates, ())
        self.assertEqual(probes, [(friday, 5)])
        self.assertEqual(tushare.daily_requests[:2], ["20230812", "20230811"])

    def test_reconnects_baostock_after_a_transient_daily_universe_failure(self) -> None:
        class FlakyBaoStockClient(_RecordedBaoStockClient):
            def __init__(self) -> None:
                super().__init__()
                self.all_stock_attempts = 0

            def query_all_stock(self, *, day: str) -> _BaoStockRows:
                self.all_stock_attempts += 1
                if self.all_stock_attempts == 1:
                    raise TimeoutError("simulated BaoStock socket timeout")
                return super().query_all_stock(day=day)

        tushare = _RecordedTushareProductionClient(
            {day.strftime("%Y%m%d"): _production_tushare_rows(day) for day in PRODUCTION_DAYS}
        )
        baostock = FlakyBaoStockClient()

        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "data" / "research.sqlite3")
            database.initialize()

            result = self._run(database=database, tushare=tushare, baostock=baostock)

        self.assertEqual(result.published_dates, PRODUCTION_DAYS)
        self.assertEqual(baostock.login_calls, 2)
        self.assertEqual(baostock.logout_calls, 2)


if __name__ == "__main__":
    unittest.main()
