"""Point-in-time Tushare daily snapshot backfills."""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from time import monotonic, sleep
from typing import Any, Protocol

from .domain import MarketSnapshot, Security
from .providers.metadata import normalize_baostock_trading_calendar
from .providers.normalization import ProviderNormalizationError, _parse_date
from .providers.runtime import BaoStockTradingCalendar, TushareDailyProvider

TUSHARE_SOURCE = "tushare"
MAX_BACKFILL_DAYS = 1_100
MINIMUM_MAIN_BOARD_COUNT = 2_000
MINIMUM_TUSHARE_REQUEST_INTERVAL_SECONDS = 1.34
MAX_TUSHARE_FETCH_ATTEMPTS = 5
MAX_RETRY_BACKOFF_SECONDS = 30.0
BAOSTOCK_SOCKET_TIMEOUT_SECONDS = 30.0
MAX_BAOSTOCK_UNIVERSE_ATTEMPTS = 3
MAX_TUSHARE_RECENT_PROBE_LOOKBACK_DAYS = 10


class TradingCalendar(Protocol):
    def is_trading_day(self, target: date) -> bool: ...


class TushareSnapshotProvider(Protocol):
    source: str

    def fetch_snapshot(self, trade_date: date) -> MarketSnapshot: ...


@dataclass(frozen=True, slots=True)
class BackfillResult:
    published_dates: tuple[date, ...]
    incomplete_dates: tuple[date, ...]


class TushareDailyBackfillService:
    """Persist complete, single-source Tushare snapshots one trading day at a time."""

    def __init__(
        self,
        *,
        database: Any,
        calendar: TradingCalendar,
        provider: TushareSnapshotProvider,
        expected_main_board_count: int,
        monotonic: Callable[[], float] = monotonic,
        sleep: Callable[[float], None] = sleep,
        minimum_request_interval_seconds: float = MINIMUM_TUSHARE_REQUEST_INTERVAL_SECONDS,
        max_fetch_attempts: int = MAX_TUSHARE_FETCH_ATTEMPTS,
        on_incomplete: Callable[[date, Exception], None] | None = None,
    ) -> None:
        if provider.source != TUSHARE_SOURCE:
            raise ValueError("historical backfill requires the Tushare daily provider")
        if expected_main_board_count < 1:
            raise ValueError("expected main-board count must be positive")
        if minimum_request_interval_seconds <= 0:
            raise ValueError("minimum request interval must be positive")
        if max_fetch_attempts < 1:
            raise ValueError("max fetch attempts must be positive")
        self._database = database
        self._calendar = calendar
        self._provider = provider
        self._expected_main_board_count = expected_main_board_count
        self._monotonic = monotonic
        self._sleep = sleep
        self._minimum_request_interval_seconds = minimum_request_interval_seconds
        self._max_fetch_attempts = max_fetch_attempts
        self._on_incomplete = on_incomplete
        self._last_request_started_at: float | None = None

    def backfill(self, start: date, end: date) -> BackfillResult:
        if end < start:
            raise ValueError("backfill range is invalid")
        if (end - start).days > MAX_BACKFILL_DAYS:
            raise ValueError("backfill is limited to approximately three years")

        published: list[date] = []
        incomplete: list[date] = []
        trading_dates = self._trading_dates(start, end)
        self._save_expected_trading_days(trading_dates)
        for target in trading_dates:
            if self._is_published(target):
                continue
            try:
                snapshot = self._fetch_snapshot(target)
                self._validate_snapshot(snapshot, target)
            except Exception as error:
                incomplete.append(target)
                if self._on_incomplete is not None:
                    self._on_incomplete(target, error)
            else:
                # Persistence errors are intentionally outside the provider exception
                # boundary: an operator must see a failed database write rather than
                # treating it as a retryable upstream gap.
                self._database.save_market_snapshot(snapshot)
                published.append(target)
        return BackfillResult(tuple(published), tuple(incomplete))

    def _trading_dates(self, start: date, end: date) -> tuple[date, ...]:
        dates: list[date] = []
        target = start
        while target <= end:
            if self._calendar.is_trading_day(target):
                dates.append(target)
            target = date.fromordinal(target.toordinal() + 1)
        return tuple(dates)

    def _save_expected_trading_days(self, dates: tuple[date, ...]) -> None:
        save_expected_days = getattr(self._database, "save_expected_trading_days", None)
        if callable(save_expected_days):
            save_expected_days(TUSHARE_SOURCE, dates)

    def _fetch_snapshot(self, target: date) -> MarketSnapshot:
        for attempt in range(self._max_fetch_attempts):
            self._wait_for_request_slot()
            try:
                return self._provider.fetch_snapshot(target)
            except Exception as error:
                if not self._is_retryable_upstream_error(error) or (
                    attempt == self._max_fetch_attempts - 1
                ):
                    raise
                self._sleep(min(2.0**attempt, MAX_RETRY_BACKOFF_SECONDS))
        raise AssertionError("the bounded fetch retry loop must return or raise")

    def _wait_for_request_slot(self) -> None:
        now = self._monotonic()
        if self._last_request_started_at is not None:
            remaining = self._minimum_request_interval_seconds - (
                now - self._last_request_started_at
            )
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_started_at = self._monotonic()

    @staticmethod
    def _is_retryable_upstream_error(error: Exception) -> bool:
        if isinstance(error, ProviderNormalizationError | ValueError):
            return False
        if isinstance(error, ConnectionError | TimeoutError):
            return True
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "429",
                "500",
                "502",
                "503",
                "504",
                "rate limit",
                "too many requests",
                "throttl",
                "temporar",
                "timeout",
                "timed out",
                "connection reset",
                "connection aborted",
                "service unavailable",
                "bad gateway",
                "gateway timeout",
            )
        )

    def _is_published(self, target: date) -> bool:
        has_snapshot = getattr(self._database, "has_market_snapshot", None)
        if callable(has_snapshot):
            return bool(has_snapshot(target, source=TUSHARE_SOURCE))
        return bool(self._database.load_market_snapshots(target, target, source=TUSHARE_SOURCE))

    def _validate_snapshot(self, snapshot: MarketSnapshot, target: date) -> None:
        if snapshot.trade_date != target:
            raise ValueError("snapshot trade date does not match request")
        if snapshot.source != TUSHARE_SOURCE:
            raise ValueError("snapshot source is not Tushare")
        self._require_aware_timestamp(snapshot.source_timestamp)

        main_symbols = {
            security.symbol
            for security in snapshot.securities
            if security.board == "MAIN" and not security.is_st
        }
        known_symbols = {security.symbol for security in snapshot.securities}
        bar_symbols = {bar.symbol for bar in snapshot.bars}
        target_symbols = {bar.symbol for bar in snapshot.bars if bar.trade_date == target}
        bar_keys = {(bar.symbol, bar.trade_date) for bar in snapshot.bars}
        if len(main_symbols) < self._expected_main_board_count:
            raise ValueError("snapshot metadata has insufficient main-board coverage")
        if len(bar_keys) != len(snapshot.bars) or not bar_symbols.issubset(known_symbols):
            raise ValueError("snapshot contains duplicate or unknown securities")
        if not main_symbols.issubset(target_symbols):
            raise ValueError("snapshot has incomplete target-day bars")

        for bar in snapshot.bars:
            if bar.trade_date > target:
                raise ValueError("snapshot contains a future price bar")
            if bar.source != TUSHARE_SOURCE:
                raise ValueError("snapshot contains non-Tushare price bars")
            self._require_aware_timestamp(bar.source_timestamp)

    @staticmethod
    def _require_aware_timestamp(timestamp: object) -> None:
        offset = getattr(timestamp, "utcoffset", None)
        if not callable(offset) or offset() is None:
            raise ValueError("source timestamp must include a timezone")


class _HistoricalTushareProvider:
    source = TUSHARE_SOURCE

    def __init__(
        self,
        *,
        client: object,
        database: Any,
        securities_for_date: Callable[[date], tuple[Security, ...]],
        clock: Callable[[], datetime],
    ) -> None:
        self._client = client
        self._database = database
        self._securities_for_date = securities_for_date
        self._clock = clock

    def fetch_snapshot(self, trade_date: date) -> MarketSnapshot:
        securities = self._securities_for_date(trade_date)
        if not securities:
            raise ValueError("BaoStock returned no eligible historical main-board securities")
        return TushareDailyProvider(
            client=self._client,
            securities=securities,
            history_loader=lambda symbol, cutoff: self._database.load_symbol_history(
                symbol,
                end_date=date.fromordinal(cutoff.toordinal() - 1),
                source=TUSHARE_SOURCE,
                limit=60,
            ),
            clock=self._clock,
        ).fetch_snapshot(trade_date)


def run_production_backfill(
    settings: Any,
    database: Any,
    start: date,
    end: date,
    *,
    tushare_client: object | None = None,
    baostock_client: object | None = None,
    clock: Callable[[], datetime] | None = None,
    minimum_main_board_count: int = MINIMUM_MAIN_BOARD_COUNT,
    on_incomplete: Callable[[date, Exception], None] | None = None,
    on_tushare_probe: Callable[[date, int], None] | None = None,
) -> BackfillResult:
    """Backfill point-in-time Tushare snapshots with a dated BaoStock universe."""

    if not getattr(settings, "tushare_token", None):
        raise ValueError("Tushare token is required for historical backfill")
    if tushare_client is None:
        import tushare  # type: ignore[import-not-found]

        tushare_client = tushare.pro_api(settings.tushare_token)
    if baostock_client is None:
        import baostock  # type: ignore[import-not-found]

        baostock_client = baostock
    resolved_clock = clock or (lambda: datetime.now().astimezone())
    if on_tushare_probe is not None:
        probe_date, probe_rows = _latest_tushare_daily_row_count(tushare_client, end)
        on_tushare_probe(probe_date, probe_rows)

    login = getattr(baostock_client, "login", None)
    logout = getattr(baostock_client, "logout", None)
    if not callable(login) or not callable(logout):
        raise ValueError("BaoStock client does not provide login/logout")
    original_socket_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(BAOSTOCK_SOCKET_TIMEOUT_SECONDS)
    _login_baostock(login)
    try:
        calendar_rows = _baostock_rows(
            _call_baostock(
                baostock_client,
                "query_trade_dates",
                start_date=start.isoformat(),
                end_date=end.isoformat(),
            )
        )
        basic_rows = _baostock_rows(_call_baostock(baostock_client, "query_stock_basic"))
        industry_rows = _baostock_rows(_call_baostock(baostock_client, "query_stock_industry"))

        def securities_for_date(target: date) -> tuple[Security, ...]:
            status_rows = _query_baostock_universe_with_reconnect(
                baostock_client,
                target,
                login=login,
                logout=logout,
            )
            return _historical_securities(
                basic_rows,
                industry_rows,
                status_rows,
                target=target,
            )

        calendar = BaoStockTradingCalendar(normalize_baostock_trading_calendar(calendar_rows))
        provider = _HistoricalTushareProvider(
            client=tushare_client,
            database=database,
            securities_for_date=securities_for_date,
            clock=resolved_clock,
        )
        return TushareDailyBackfillService(
            database=database,
            calendar=calendar,
            provider=provider,
            expected_main_board_count=minimum_main_board_count,
            on_incomplete=on_incomplete,
        ).backfill(start, end)
    finally:
        try:
            logout()
        finally:
            socket.setdefaulttimeout(original_socket_timeout)


def _login_baostock(login: Callable[[], object]) -> None:
    login_result = login()
    if str(getattr(login_result, "error_code", "0")) != "0":
        raise RuntimeError("BaoStock login failed")


def _tushare_daily_row_count(client: object, target: date) -> int:
    daily = getattr(client, "daily", None)
    if not callable(daily):
        raise ValueError("Tushare client does not provide daily()")
    frame = daily(trade_date=target.strftime("%Y%m%d"))
    to_dict = getattr(frame, "to_dict", None)
    if not callable(to_dict):
        raise ValueError("Tushare daily() response is not tabular")
    rows = to_dict(orient="records")
    if not isinstance(rows, list):
        raise ValueError("Tushare daily() response cannot be read as records")
    return len(rows)


def _latest_tushare_daily_row_count(client: object, end: date) -> tuple[date, int]:
    """Probe newest-to-oldest so a weekend or holiday cannot mask recent availability."""

    last_target = end
    for days_ago in range(MAX_TUSHARE_RECENT_PROBE_LOOKBACK_DAYS):
        target = date.fromordinal(end.toordinal() - days_ago)
        last_target = target
        row_count = _tushare_daily_row_count(client, target)
        if row_count:
            return target, row_count
    return last_target, 0


def _query_baostock_universe_with_reconnect(
    client: object,
    target: date,
    *,
    login: Callable[[], object],
    logout: Callable[[], object],
) -> tuple[dict[str, object], ...]:
    for attempt in range(MAX_BAOSTOCK_UNIVERSE_ATTEMPTS):
        try:
            return _baostock_rows(
                _call_baostock(client, "query_all_stock", day=target.isoformat())
            )
        except Exception:
            if attempt == MAX_BAOSTOCK_UNIVERSE_ATTEMPTS - 1:
                raise
            logout()
            _login_baostock(login)
            sleep(min(2.0**attempt, MAX_RETRY_BACKOFF_SECONDS))
    raise AssertionError("bounded BaoStock reconnect loop must return or raise")


def _call_baostock(client: object, name: str, **kwargs: object) -> object:
    operation = getattr(client, name, None)
    if not callable(operation):
        raise ValueError(f"BaoStock client does not provide {name}()")
    return operation(**kwargs)


def _baostock_rows(result: object) -> tuple[dict[str, object], ...]:
    if str(getattr(result, "error_code", "0")) != "0":
        raise RuntimeError("BaoStock query failed")
    fields = tuple(getattr(result, "fields", ()))
    next_row = getattr(result, "next", None)
    get_row = getattr(result, "get_row_data", None)
    if not fields or not callable(next_row) or not callable(get_row):
        raise ValueError("BaoStock result has no iterable fields")
    rows: list[dict[str, object]] = []
    while next_row():
        rows.append(dict(zip(fields, get_row(), strict=True)))
    return tuple(rows)


def _historical_securities(
    basic_rows: Iterable[Mapping[str, object]],
    industry_rows: Iterable[Mapping[str, object]],
    status_rows: Iterable[Mapping[str, object]],
    *,
    target: date,
) -> tuple[Security, ...]:
    basics = {_baostock_symbol(row.get("code")): row for row in basic_rows}
    industries: dict[str, str] = {}
    for row in industry_rows:
        symbol = _baostock_symbol(row.get("code"))
        update_date = _parse_date(row.get("updateDate"), "updateDate")
        if update_date <= target:
            industries[symbol] = str(row.get("industry", "")).strip()

    securities: list[Security] = []
    for row in status_rows:
        symbol = _baostock_symbol(row.get("code"))
        code, exchange = symbol.split(".")
        if not _is_main_board(code, exchange) or _trade_status(row) != "1":
            continue
        basic = basics.get(symbol)
        if basic is None or str(basic.get("type", "")).strip() != "1":
            continue
        name = str(row.get("code_name", "")).strip()
        if not name:
            raise ProviderNormalizationError("historical code_name must not be blank")
        if _is_st_name(name):
            continue
        securities.append(
            Security(
                symbol=symbol,
                name=name,
                exchange="SSE" if exchange == "SH" else "SZSE",
                board="MAIN",
                list_date=_parse_date(basic.get("ipoDate"), "ipoDate"),
                industry=industries.get(symbol, ""),
                is_st=False,
            )
        )
    return tuple(sorted(securities, key=lambda security: security.symbol))


def _trade_status(row: Mapping[str, object]) -> str:
    for name in ("tradeStatus", "trade_status", "tradestatus"):
        if name in row:
            return str(row[name]).strip()
    raise ProviderNormalizationError("missing historical tradeStatus")


def _is_st_name(name: str) -> bool:
    normalized = name.strip().upper().replace(" ", "")
    return normalized.startswith(("ST", "*ST", "S*ST", "SST"))


def _is_main_board(code: str, exchange: str) -> bool:
    if exchange == "SH":
        return code.startswith(("600", "601", "603", "605"))
    return exchange == "SZ" and code.startswith(("000", "001", "002", "003"))


def _baostock_symbol(value: object) -> str:
    raw = str(value).strip().lower()
    prefix, separator, code = raw.partition(".")
    exchange = {"sh": "SH", "sz": "SZ"}.get(prefix)
    if not separator or exchange is None or len(code) != 6 or not code.isdigit():
        raise ProviderNormalizationError("code is not a supported BaoStock A-share symbol")
    return f"{code}.{exchange}"
