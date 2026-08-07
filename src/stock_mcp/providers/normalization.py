"""Normalise provider-shaped rows into immutable domain bars."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from stock_mcp.domain import DailyBar


class ProviderNormalizationError(ValueError):
    """A provider response cannot safely become a market snapshot."""


def normalize_tushare_daily(
    rows: Iterable[Mapping[str, object]],
    *,
    trade_date: date,
    source_timestamp: datetime,
) -> tuple[DailyBar, ...]:
    """Convert Tushare ``daily`` rows (hands / thousand yuan) to domain bars."""

    required = (
        "ts_code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "vol",
        "amount",
    )
    bars: list[DailyBar] = []
    for row in rows:
        _require_columns(row, required)
        _require_source(row, "tushare")
        _require_trade_date(row["trade_date"], trade_date)
        bars.append(
            _daily_bar(
                symbol=_tushare_symbol(row["ts_code"]),
                trade_date=trade_date,
                open_value=row["open"],
                high_value=row["high"],
                low_value=row["low"],
                close_value=row["close"],
                pre_close_value=row["pre_close"],
                volume_value=row["vol"],
                amount_value=row["amount"],
                volume_multiplier=100,
                amount_multiplier=100_000,
                source="tushare",
                source_timestamp=source_timestamp,
            )
        )
    return tuple(bars)


def normalize_akshare_snapshot(
    rows: Iterable[Mapping[str, object]],
    *,
    trade_date: date,
    source_timestamp: datetime,
) -> tuple[DailyBar, ...]:
    """Convert AKShare spot rows (hands / yuan) to domain bars.

    This conversion is intentionally only a normalisation boundary.  Whether a
    spot snapshot may be used for a formal daily report is a pipeline policy.
    """

    required = ("代码", "日期", "开盘", "最高", "最低", "收盘", "昨收", "成交量", "成交额")
    bars: list[DailyBar] = []
    for row in rows:
        _require_columns(row, required)
        _require_source(row, "akshare")
        _require_trade_date(row["日期"], trade_date)
        bars.append(
            _daily_bar(
                symbol=_akshare_symbol(row["代码"]),
                trade_date=trade_date,
                open_value=row["开盘"],
                high_value=row["最高"],
                low_value=row["最低"],
                close_value=row["收盘"],
                pre_close_value=row["昨收"],
                volume_value=row["成交量"],
                amount_value=row["成交额"],
                volume_multiplier=100,
                amount_multiplier=100,
                source="akshare",
                source_timestamp=source_timestamp,
            )
        )
    return tuple(bars)


def _daily_bar(
    *,
    symbol: str,
    trade_date: date,
    open_value: object,
    high_value: object,
    low_value: object,
    close_value: object,
    pre_close_value: object,
    volume_value: object,
    amount_value: object,
    volume_multiplier: int,
    amount_multiplier: int,
    source: str,
    source_timestamp: datetime,
) -> DailyBar:
    open_1e4 = _scaled_int(open_value, 10_000, "open")
    high_1e4 = _scaled_int(high_value, 10_000, "high")
    low_1e4 = _scaled_int(low_value, 10_000, "low")
    close_1e4 = _scaled_int(close_value, 10_000, "close")
    pre_close_1e4 = _scaled_int(pre_close_value, 10_000, "pre_close")
    volume_shares = _scaled_int(volume_value, volume_multiplier, "volume")
    amount_fen = _scaled_int(amount_value, amount_multiplier, "amount")

    if min(open_1e4, high_1e4, low_1e4, close_1e4, pre_close_1e4) <= 0:
        raise ProviderNormalizationError("OHLC values must be positive")
    if low_1e4 > min(open_1e4, close_1e4) or high_1e4 < max(open_1e4, close_1e4):
        raise ProviderNormalizationError("OHLC range is invalid")
    if volume_shares < 0 or amount_fen < 0:
        raise ProviderNormalizationError("volume and amount must not be negative")

    return DailyBar(
        symbol=symbol,
        trade_date=trade_date,
        open_1e4=open_1e4,
        high_1e4=high_1e4,
        low_1e4=low_1e4,
        close_1e4=close_1e4,
        pre_close_1e4=pre_close_1e4,
        volume_shares=volume_shares,
        amount_fen=amount_fen,
        source=source,
        source_timestamp=source_timestamp,
    )


def _require_columns(row: Mapping[str, object], required: tuple[str, ...]) -> None:
    missing = [column for column in required if column not in row]
    if missing:
        raise ProviderNormalizationError(f"missing required column: {', '.join(missing)}")


def _require_source(row: Mapping[str, object], expected: str) -> None:
    actual = row.get("source")
    if actual is not None and str(actual).strip().lower() != expected:
        raise ProviderNormalizationError(f"source contamination: expected {expected}")


def _require_trade_date(value: object, expected: date) -> None:
    if _parse_date(value, "trade_date") != expected:
        raise ProviderNormalizationError("trade_date does not match requested date")


def _parse_date(value: object, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for layout in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, layout).date()
        except ValueError:
            pass
    raise ProviderNormalizationError(f"{field} is not a valid date")


def _scaled_int(value: object, multiplier: int, field: str) -> int:
    try:
        scaled = Decimal(str(value).strip()) * multiplier
    except (InvalidOperation, AttributeError):
        raise ProviderNormalizationError(f"{field} is not numeric") from None
    if not scaled.is_finite() or scaled != scaled.to_integral_value():
        raise ProviderNormalizationError(f"{field} has unsupported precision")
    return int(scaled)


def _tushare_symbol(value: object) -> str:
    symbol = str(value).strip().upper()
    code, separator, suffix = symbol.partition(".")
    if not separator or len(code) != 6 or not code.isdigit() or suffix not in {"SH", "SZ"}:
        raise ProviderNormalizationError("ts_code is not a supported A-share symbol")
    return f"{code}.{suffix}"


def _akshare_symbol(value: object) -> str:
    code = str(value).strip()
    if len(code) != 6 or not code.isdigit():
        raise ProviderNormalizationError("代码 is not a six digit A-share symbol")
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    raise ProviderNormalizationError("代码 is not a supported Shanghai/Shenzhen A-share symbol")
