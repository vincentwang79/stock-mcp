"""Offline production contracts for direct on-demand Eastmoney quotes."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def raise_for_status(self) -> None:
        return None

    def json(self, **kwargs: object) -> object:
        return json.loads(self._payload.decode("utf-8"), **kwargs)


class _Session:
    def __init__(self, captured: dict[str, object]) -> None:
        self.captured = captured
        self.trust_env = True

    def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
        timeout: float,
    ) -> _Response:
        self.captured["url"] = url
        self.captured["params"] = params
        self.captured["headers"] = headers
        self.captured["timeout"] = timeout
        self.captured["trust_env_at_request"] = self.trust_env
        return _Response({"rc": 0, "data": {"f43": 15.5, "f57": "601058"}})

    def close(self) -> None:
        self.captured["closed"] = True


class DirectEastmoneyQuoteContractTests(unittest.TestCase):
    def test_uses_one_direct_symbol_request_without_an_akshare_market_scan(self) -> None:
        from stock_mcp.production import DirectEastmoneyQuoteProvider

        captured: dict[str, object] = {}

        def new_session() -> _Session:
            return _Session(captured)

        provider = DirectEastmoneyQuoteProvider(
            session_factory=new_session,
            clock=lambda: datetime(2026, 9, 2, 16, 35, tzinfo=UTC),
        )

        quote = provider.fetch_quote("601058.SH")

        self.assertEqual(155_000, quote["close_1e4"])
        self.assertEqual("eastmoney", quote["source"])
        self.assertIn("82.push2.eastmoney.com/api/qt/stock/get", str(captured["url"]))
        self.assertEqual("1.601058", captured["params"]["secid"])
        self.assertEqual(15.0, captured["timeout"])
        self.assertFalse(captured["trust_env_at_request"])
        self.assertTrue(captured["closed"])


if __name__ == "__main__":
    unittest.main()
