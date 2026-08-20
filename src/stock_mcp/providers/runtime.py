"""Runtime provider adapters with injected clients and no import-time SDK use."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from stock_mcp.domain import DailyBar, MarketSnapshot, Security
from stock_mcp.providers.metadata import normalize_baostock_trading_calendar
from stock_mcp.providers.normalization import (
    ProviderNormalizationError,
    normalize_akshare_snapshot,
    normalize_tushare_daily,
)
from stock_mcp.research_program import ingest_point_in_time_research_batch


class ProviderRuntimeError(ValueError):
    """A runtime provider response cannot safely be used by the pipeline."""


HistoryLoader = Callable[[str, date], Iterable[DailyBar]]
Clock = Callable[[], datetime]


class TushareDailyProvider:
    """Formal daily source backed by an injected Tushare-compatible client."""

    source = "tushare"
    has_historical_mirror = True

    def __init__(
        self,
        *,
        client: object,
        securities: Sequence[Security],
        history_loader: HistoryLoader,
        clock: Clock,
    ) -> None:
        self._client = client
        self._securities = tuple(securities)
        self._history_loader = history_loader
        self._clock = clock

    def fetch_snapshot(self, trade_date: date) -> MarketSnapshot:
        source_timestamp = _timestamp(self._clock())
        response = _call_daily(self._client, trade_date)
        eligible_symbols = {
            security.symbol
            for security in self._securities
            if security.board == "MAIN" and not security.is_st
        }
        records = tuple(
            row
            for row in _records(response)
            if str(row.get("ts_code", "")).strip().upper() in eligible_symbols
        )
        try:
            normalized = normalize_tushare_daily(
                records,
                trade_date=trade_date,
                source_timestamp=source_timestamp,
            )
        except ProviderNormalizationError as error:
            raise ProviderRuntimeError(str(error)) from error
        bars = _research_universe_bars(normalized, self._securities)
        if not bars:
            raise ProviderRuntimeError("Tushare returned no bars for the configured universe")
        histories: dict[str, tuple[DailyBar, ...]] = {}
        for bar in bars:
            history = tuple(
                item
                for item in self._history_loader(bar.symbol, trade_date)
                if item.symbol == bar.symbol and item.trade_date < trade_date
            )
            if any(item.source != self.source for item in history):
                raise ProviderRuntimeError("Tushare history source does not match snapshot source")
            keys = {(item.symbol, item.trade_date) for item in history}
            if len(keys) != len(history):
                raise ProviderRuntimeError("Tushare history contains duplicate daily bars")
            histories[bar.symbol] = tuple(sorted(history, key=lambda item: item.trade_date))
        all_bars = tuple(item for bar in bars for item in (*histories[bar.symbol], bar))
        return MarketSnapshot(
            trade_date=trade_date,
            source=self.source,
            source_timestamp=source_timestamp,
            securities=self._securities,
            bars=all_bars,
            advance_ratio_bps=_advance_ratio(bars),
            above_ma20_ratio_bps=self._above_ma20_ratio(bars, histories),
        )

    def _above_ma20_ratio(
        self, bars: tuple[DailyBar, ...], histories: Mapping[str, tuple[DailyBar, ...]]
    ) -> int:
        above = 0
        for bar in bars:
            closes = [item.close_1e4 for item in histories[bar.symbol][-19:]]
            closes.append(bar.close_1e4)
            if len(closes) == 20 and bar.close_1e4 > sum(closes) / len(closes):
                above += 1
        return above * 10_000 // len(bars)


class TushareResearchFactProvider:
    """Daily point-in-time research facts backed by an injected Tushare client."""

    source = "tushare"

    def __init__(self, *, client: object, clock: Clock) -> None:
        self._client = client
        self._clock = clock

    def fetch(self, as_of: date) -> dict[str, object]:
        encoded_date = as_of.strftime("%Y%m%d")
        daily_call = getattr(self._client, "daily_basic", None)
        financial_call = getattr(self._client, "fina_indicator_vip", None)
        if not callable(daily_call) or not callable(financial_call):
            raise ProviderRuntimeError(
                "Tushare research client requires daily_basic() and fina_indicator_vip()"
            )
        daily = self._research_records(daily_call(trade_date=encoded_date))
        financial = self._research_records(financial_call(ann_date=encoded_date))
        if not daily:
            raise ProviderRuntimeError("Tushare daily_basic returned no rows")
        self._reject_duplicates(daily, fields=("ts_code",), label="daily_basic")
        self._reject_duplicates(
            financial,
            fields=("ts_code", "ann_date", "end_date"),
            optional_fields=("update_flag",),
            label="fina_indicator_vip",
        )
        return {
            "as_of": as_of,
            "source_timestamp": _timestamp(self._clock()),
            "daily_basic_rows": daily,
            "fina_indicator_rows": financial,
        }

    @staticmethod
    def _research_records(response: object) -> tuple[Mapping[str, object], ...]:
        to_dict = getattr(response, "to_dict", None)
        if callable(to_dict):
            response = to_dict(orient="records")
        if isinstance(response, Mapping | str | bytes):
            raise ProviderRuntimeError("Tushare research response must contain row mappings")
        try:
            records = tuple(response)  # type: ignore[arg-type]
        except TypeError as error:
            raise ProviderRuntimeError("Tushare research response must be iterable") from error
        if not all(isinstance(row, Mapping) for row in records):
            raise ProviderRuntimeError("Tushare research response contains a non-mapping row")
        return records  # type: ignore[return-value]

    @staticmethod
    def _reject_duplicates(
        rows: Sequence[Mapping[str, object]],
        *,
        fields: tuple[str, ...],
        label: str,
        optional_fields: tuple[str, ...] = (),
    ) -> None:
        required_keys = [tuple(str(row.get(field) or "") for field in fields) for row in rows]
        if any(not all(key) for key in required_keys):
            raise ProviderRuntimeError(f"Tushare {label} response has an incomplete key")
        keys = [
            (*required, *(str(row.get(field) or "") for field in optional_fields))
            for required, row in zip(required_keys, rows, strict=True)
        ]
        if len(keys) != len(set(keys)):
            raise ProviderRuntimeError(f"Tushare {label} response contains duplicate rows")


def collect_tushare_research_day(
    repository: object, *, provider: TushareResearchFactProvider, as_of: date
) -> dict[str, int]:
    """Fetch both interfaces before atomically handing normalized facts to storage."""

    batch = provider.fetch(as_of)
    return ingest_point_in_time_research_batch(
        repository,
        as_of=as_of,
        source_timestamp=batch["source_timestamp"],  # type: ignore[arg-type]
        daily_basic_rows=batch["daily_basic_rows"],  # type: ignore[arg-type]
        fina_indicator_rows=batch["fina_indicator_rows"],  # type: ignore[arg-type]
    )


class BaoStockTradingCalendar:
    """In-memory trading calendar normalised from BaoStock records."""

    def __init__(self, trading_days: Iterable[date]) -> None:
        self._trading_days = frozenset(trading_days)

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, object]]) -> BaoStockTradingCalendar:
        return cls(normalize_baostock_trading_calendar(rows))

    def is_trading_day(self, target: date) -> bool:
        return target in self._trading_days

    def prior_trading_days(self, target: date, count: int) -> tuple[date, ...]:
        if count < 0:
            raise ValueError("trading-day count cannot be negative")
        prior = tuple(sorted(day for day in self._trading_days if day < target))
        if len(prior) < count:
            raise ValueError(f"calendar does not contain {count} prior trading sessions")
        return prior[-count:]


class AKShareSnapshotProvider:
    """Explicit current-day spot snapshot; never a historical daily mirror."""

    source = "akshare"
    has_historical_mirror = False

    def __init__(self, *, client: object, securities: Sequence[Security], clock: Clock) -> None:
        self._client = client
        self._securities = tuple(securities)
        self._clock = clock

    def fetch_snapshot(self, trade_date: date) -> MarketSnapshot:
        source_timestamp = _timestamp(self._clock())
        if trade_date != source_timestamp.date():
            raise ProviderRuntimeError(
                "AKShare spot snapshots are only available for the current date"
            )
        response = _call_spot(self._client)
        rows = (_akshare_daily_row(row, trade_date) for row in _records(response))
        try:
            normalized = normalize_akshare_snapshot(
                rows,
                trade_date=trade_date,
                source_timestamp=source_timestamp,
            )
        except ProviderNormalizationError as error:
            raise ProviderRuntimeError(str(error)) from error
        bars = _research_universe_bars(normalized, self._securities)
        if not bars:
            raise ProviderRuntimeError("AKShare returned no bars for the configured universe")
        return MarketSnapshot(
            trade_date=trade_date,
            source=self.source,
            source_timestamp=source_timestamp,
            securities=self._securities,
            bars=bars,
            advance_ratio_bps=_advance_ratio(bars),
            above_ma20_ratio_bps=0,
        )


class AKShareQuoteProvider:
    """On-demand quote adapter; requests data only on ``fetch_quote``."""

    source = "akshare"

    def __init__(self, *, client: object, clock: Clock) -> None:
        self._client = client
        self._clock = clock

    def fetch_quote(self, symbol: str) -> dict[str, object]:
        code = _symbol_code(symbol)
        response = _call_spot(self._client)
        for row in _records(response):
            if str(row.get("代码", "")).strip().zfill(6) != code:
                continue
            if "最新价" not in row or str(row["最新价"]).strip() == "":
                raise ProviderRuntimeError("AKShare quote has no latest price")
            return {
                "close_1e4": _price_1e4(row["最新价"]),
                "source": self.source,
                "as_of": _timestamp(self._clock()),
            }
        raise ProviderRuntimeError(f"AKShare quote not found for {symbol}")


def _call_daily(client: object, trade_date: date) -> object:
    daily = getattr(client, "daily", None)
    if not callable(daily):
        raise ProviderRuntimeError("Tushare client does not provide daily(trade_date=...)")
    return daily(trade_date=trade_date.strftime("%Y%m%d"))


def _call_spot(client: object) -> object:
    spot = getattr(client, "stock_zh_a_spot_em", None)
    if not callable(spot):
        raise ProviderRuntimeError("AKShare client does not provide stock_zh_a_spot_em()")
    return spot()


def _records(response: object) -> tuple[Mapping[str, object], ...]:
    to_dict = getattr(response, "to_dict", None)
    if callable(to_dict):
        response = to_dict("records")
    if isinstance(response, Mapping | str | bytes):
        raise ProviderRuntimeError("provider response must be an iterable of row mappings")
    try:
        records = tuple(response)  # type: ignore[arg-type]
    except TypeError as error:
        raise ProviderRuntimeError("provider response must be iterable") from error
    if not all(isinstance(row, Mapping) for row in records):
        raise ProviderRuntimeError("provider response contains a non-mapping row")
    return records  # type: ignore[return-value]


def _universe_bars(
    bars: tuple[DailyBar, ...], securities: Sequence[Security]
) -> tuple[DailyBar, ...]:
    allowed = {security.symbol for security in securities}
    selected = tuple(bar for bar in bars if bar.symbol in allowed)
    symbols = [bar.symbol for bar in selected]
    if len(symbols) != len(set(symbols)):
        raise ProviderRuntimeError("provider returned duplicate daily bars")
    return selected


def _research_universe_bars(
    bars: tuple[DailyBar, ...], securities: Sequence[Security]
) -> tuple[DailyBar, ...]:
    eligible = tuple(
        security for security in securities if security.board == "MAIN" and not security.is_st
    )
    return _universe_bars(bars, eligible)


def _advance_ratio(bars: Sequence[DailyBar]) -> int:
    return sum(bar.close_1e4 > bar.pre_close_1e4 for bar in bars) * 10_000 // len(bars)


def _akshare_daily_row(row: Mapping[str, object], trade_date: date) -> dict[str, object]:
    converted = dict(row)
    converted.setdefault("日期", trade_date.isoformat())
    if "收盘" not in converted and "最新价" in converted:
        converted["收盘"] = converted["最新价"]
    return converted


def _symbol_code(symbol: str) -> str:
    code, separator, exchange = symbol.strip().upper().partition(".")
    if separator != "." or exchange not in {"SH", "SZ"} or len(code) != 6 or not code.isdigit():
        raise ProviderRuntimeError("symbol must be a six digit .SH or .SZ A-share symbol")
    return code


def _price_1e4(value: object) -> int:
    try:
        price = Decimal(str(value).strip()) * 10_000
    except (InvalidOperation, AttributeError):
        raise ProviderRuntimeError("AKShare latest price is not numeric") from None
    if not price.is_finite() or price <= 0 or price != price.to_integral_value():
        raise ProviderRuntimeError("AKShare latest price is invalid")
    return int(price)


def _timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProviderRuntimeError("provider clock must return a timezone-aware datetime")
    return value
