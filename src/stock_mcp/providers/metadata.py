"""BaoStock metadata normalisation without a BaoStock runtime dependency."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date

from stock_mcp.domain import Security
from stock_mcp.providers.normalization import ProviderNormalizationError, _parse_date


def normalize_baostock_securities(
    basic_rows: Iterable[Mapping[str, object]],
    industry_rows: Iterable[Mapping[str, object]],
) -> tuple[Security, ...]:
    """Join active listed A-share basics to their optional industry labels."""

    industries: dict[str, str] = {}
    for row in industry_rows:
        code = row.get("code")
        if code is None:
            raise ProviderNormalizationError("missing required column: code")
        industry = str(row.get("industry", "")).strip()
        industries[_baostock_symbol(code)] = industry

    securities: list[Security] = []
    for row in basic_rows:
        _require_basic_columns(row)
        # BaoStock uses 1 for equities and active listing status.  Ignore other
        # instruments rather than allowing them into the A-share universe.
        if str(row["type"]).strip() != "1" or str(row["status"]).strip() != "1":
            continue
        symbol = _baostock_symbol(row["code"])
        code, exchange = symbol.split(".")
        if exchange == "SH" and not code.startswith(("600", "601", "603", "605")):
            continue
        if exchange == "SZ" and not code.startswith(("000", "001", "002", "003")):
            continue
        name = str(row["code_name"]).strip()
        if not name:
            raise ProviderNormalizationError("code_name must not be blank")
        securities.append(
            Security(
                symbol=symbol,
                name=name,
                exchange="SSE" if exchange == "SH" else "SZSE",
                board="MAIN",
                list_date=_parse_date(row["ipoDate"], "ipoDate"),
                industry=industries.get(symbol, ""),
                is_st=name.upper().startswith("ST") or name.startswith("*ST"),
            )
        )
    return tuple(sorted(securities, key=lambda security: security.symbol))


def normalize_baostock_trading_calendar(rows: Iterable[Mapping[str, object]]) -> tuple[date, ...]:
    """Return sorted, deduplicated open market dates from BaoStock calendar rows."""

    trading_days: set[date] = set()
    for row in rows:
        if "calendar_date" not in row:
            raise ProviderNormalizationError("missing required column: calendar_date")
        if "is_trading_day" not in row:
            raise ProviderNormalizationError("missing required column: is_trading_day")
        if str(row["is_trading_day"]).strip() == "1":
            trading_days.add(_parse_date(row["calendar_date"], "calendar_date"))
    return tuple(sorted(trading_days))


def _require_basic_columns(row: Mapping[str, object]) -> None:
    missing = [
        column for column in ("code", "code_name", "ipoDate", "type", "status") if column not in row
    ]
    if missing:
        raise ProviderNormalizationError(f"missing required column: {', '.join(missing)}")


def _baostock_symbol(value: object) -> str:
    raw = str(value).strip().lower()
    prefix, separator, code = raw.partition(".")
    exchange = {"sh": "SH", "sz": "SZ"}.get(prefix)
    if not separator or exchange is None or len(code) != 6 or not code.isdigit():
        raise ProviderNormalizationError("code is not a supported BaoStock A-share symbol")
    return f"{code}.{exchange}"
