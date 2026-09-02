"""Single-symbol Eastmoney quote normalization for explicit confirmation checks."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .runtime import ProviderRuntimeError


class EastmoneyQuoteProvider:
    """Normalize one injected, one-symbol Eastmoney quote response."""

    source = "eastmoney"

    def __init__(self, *, client: object, clock: object) -> None:
        self._client = client
        self._clock = clock

    def fetch_quote(self, symbol: str) -> dict[str, object]:
        code = _symbol_code(symbol)
        fetch = getattr(self._client, "fetch_quote_payload", None)
        if not callable(fetch):
            raise ProviderRuntimeError(
                "Eastmoney client does not provide fetch_quote_payload(symbol)"
            )
        try:
            payload = fetch(symbol)
        except Exception as error:
            raise ProviderRuntimeError("Eastmoney quote request failed") from error

        if not isinstance(payload, Mapping):
            raise ProviderRuntimeError("Eastmoney quote payload must be a mapping")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ProviderRuntimeError("Eastmoney quote payload has no data")
        if str(data.get("f57", "")).strip().zfill(6) != code:
            raise ProviderRuntimeError("Eastmoney quote symbol does not match requested symbol")
        if "f43" not in data or str(data["f43"]).strip() == "":
            raise ProviderRuntimeError("Eastmoney quote has no latest price")

        clock = self._clock
        if not callable(clock):
            raise ProviderRuntimeError("provider clock must be callable")
        return {
            "close_1e4": _price_1e4(data["f43"]),
            "source": self.source,
            "as_of": _timestamp(clock()),
        }


def _symbol_code(symbol: str) -> str:
    code, separator, exchange = symbol.strip().upper().partition(".")
    if separator != "." or exchange not in {"SH", "SZ"} or len(code) != 6 or not code.isdigit():
        raise ProviderRuntimeError("symbol must be a six digit .SH or .SZ A-share symbol")
    return code


def _price_1e4(value: object) -> int:
    try:
        price = Decimal(str(value).strip()) * 10_000
    except (InvalidOperation, AttributeError):
        raise ProviderRuntimeError("Eastmoney latest price is not numeric") from None
    if not price.is_finite() or price <= 0 or price != price.to_integral_value():
        raise ProviderRuntimeError("Eastmoney latest price is invalid")
    return int(price)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProviderRuntimeError("provider clock must return a timezone-aware datetime")
    return value
