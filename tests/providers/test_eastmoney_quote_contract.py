"""Offline contracts for the single-symbol Eastmoney confirmation quote."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

NOW = datetime(2026, 9, 2, 16, 35, tzinfo=UTC)


class _QuoteClient:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.symbols: list[str] = []

    def fetch_quote_payload(self, symbol: str) -> object:
        self.symbols.append(symbol)
        return self.payload


class _UnavailableQuoteClient:
    def fetch_quote_payload(self, _symbol: str) -> object:
        raise OSError("connection closed by remote host")


class EastmoneyQuoteProviderContractTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from stock_mcp.providers.eastmoney import EastmoneyQuoteProvider
            from stock_mcp.providers.runtime import ProviderRuntimeError
        except ImportError as error:
            self.fail(f"Eastmoney quote adapter is not implemented: {error}")
        self.EastmoneyQuoteProvider = EastmoneyQuoteProvider
        self.ProviderRuntimeError = ProviderRuntimeError

    def test_fetches_exactly_one_symbol_and_normalizes_the_close(self) -> None:
        client = _QuoteClient({"rc": 0, "data": {"f43": "15.50", "f57": "601058"}})

        provider = self.EastmoneyQuoteProvider(client=client, clock=lambda: NOW)

        quote = provider.fetch_quote("601058.SH")

        self.assertEqual(["601058.SH"], client.symbols)
        self.assertEqual(
            {"close_1e4": 155_000, "source": "eastmoney", "as_of": NOW},
            quote,
        )

    def test_rejects_a_mismatched_or_missing_quote_payload(self) -> None:
        mismatched = _QuoteClient({"rc": 0, "data": {"f43": "15.50", "f57": "000001"}})

        with self.assertRaisesRegex(self.ProviderRuntimeError, "symbol"):
            self.EastmoneyQuoteProvider(client=mismatched, clock=lambda: NOW).fetch_quote(
                "601058.SH"
            )

        missing_price = _QuoteClient({"rc": 0, "data": {"f57": "601058"}})
        with self.assertRaisesRegex(self.ProviderRuntimeError, "latest price"):
            self.EastmoneyQuoteProvider(client=missing_price, clock=lambda: NOW).fetch_quote(
                "601058.SH"
            )

    def test_normalizes_transport_failure_to_provider_runtime_error(self) -> None:
        provider = self.EastmoneyQuoteProvider(client=_UnavailableQuoteClient(), clock=lambda: NOW)

        with self.assertRaisesRegex(self.ProviderRuntimeError, "quote request failed"):
            provider.fetch_quote("601058.SH")


if __name__ == "__main__":
    unittest.main()
