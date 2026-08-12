"""Offline v0.4 Sina HTTP policy contracts with fully injected collaborators."""

from __future__ import annotations

import importlib
import json
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

FIXTURES = Path(__file__).with_name("fixtures") / "sina"
NOW = datetime(2026, 8, 7, 16, 35, tzinfo=UTC)


class _UnavailableRuntime:
    def __getattr__(self, name: str) -> object:
        def unavailable(*_args: object, **_kwargs: object) -> object:
            raise AssertionError(f"Sina runtime behavior is not implemented: {name}")

        return unavailable


def _runtime() -> object:
    try:
        return importlib.import_module("stock_mcp.providers.sina")
    except ModuleNotFoundError as error:
        if error.name != "stock_mcp.providers.sina":
            raise
        return _UnavailableRuntime()


class _Response:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}


class _HttpClient:
    def __init__(self, responses: list[_Response | Exception]) -> None:
        self._responses = iter(responses)
        self.calls: list[tuple[str, dict[str, object], tuple[float, float]]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, object],
        timeout: tuple[float, float],
    ) -> _Response:
        self.calls.append((url, params, timeout))
        response = next(self._responses)
        if isinstance(response, Exception):
            raise response
        return response


class _RateLimiter:
    def __init__(self) -> None:
        self.calls = 0

    def acquire(self) -> None:
        self.calls += 1


class SinaRuntimeContractTest(unittest.TestCase):
    def _transport(
        self, responses: list[_Response | Exception]
    ) -> tuple[object, _HttpClient, list[float]]:
        runtime = _runtime()
        client = _HttpClient(responses)
        sleeps: list[float] = []
        transport = runtime.SinaHttpTransport(  # type: ignore[attr-defined]
            client=client,
            clock=lambda: NOW,
            rate_limiter=_RateLimiter(),
            sleeper=sleeps.append,
            connect_timeout_seconds=1.5,
            read_timeout_seconds=4.0,
            max_retries=2,
            jitter_seconds=lambda _attempt: 0.25,
        )
        return transport, client, sleeps

    def test_429_retries_after_server_retry_after_and_preserves_fetch_evidence(self) -> None:
        transport, client, sleeps = self._transport(
            [_Response(429, b"busy", {"Retry-After": "3"}), _Response(200, b"[]", {"Date": "x"})]
        )

        result = transport.get(  # type: ignore[attr-defined]
            endpoint_kind="spot_count",
            url="https://offline.invalid/count",
            request_key="2026-08-07",
        )

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(sleeps, [3.0])
        self.assertEqual(result.payload, b"[]")
        self.assertEqual(result.evidence.endpoint_kind, "spot_count")
        self.assertEqual(result.evidence.http_status, 200)
        self.assertEqual(result.evidence.byte_length, 2)
        self.assertEqual(result.evidence.http_date, "x")
        self.assertEqual(result.evidence.adapter_version, "sina-adapter-v1")
        self.assertEqual(len(result.evidence.payload_sha256), 64)

    def test_only_timeout_connection_429_and_5xx_are_retried(self) -> None:
        transport, client, sleeps = self._transport([_Response(400, b"bad")])

        with self.assertRaisesRegex(Exception, "(?:400|HTTP|non-retriable)"):
            transport.get(  # type: ignore[attr-defined]
                endpoint_kind="history",
                url="https://offline.invalid/history",
                request_key="sh600001",
            )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(sleeps, [])

    def test_history_normalizes_only_the_requested_window_with_one_prior_close(self) -> None:
        runtime = _runtime()
        payload = json.dumps(
            [
                {
                    "date": "1992-04-13",
                    "prevclose": 0,
                    "open": 22,
                    "high": 26.55,
                    "low": 22,
                    "close": 24.3,
                    "volume": 31_900,
                    "amount": 3_781_000,
                },
                {
                    "date": "2023-08-07",
                    "open": 9.9,
                    "high": 10.1,
                    "low": 9.8,
                    "close": 10,
                    "volume": 100,
                    "amount": 1_000,
                },
                {
                    "date": "2023-08-08",
                    "open": 10,
                    "high": 10.3,
                    "low": 9.9,
                    "close": 10.2,
                    "volume": 110,
                    "amount": 1_100,
                },
                {
                    "date": "2023-08-09",
                    "open": 10.2,
                    "high": 10.4,
                    "low": 10.1,
                    "close": 10.3,
                    "volume": 120,
                    "amount": 1_200,
                },
            ],
            separators=(",", ":"),
        ).encode()
        transport, _client, _sleeps = self._transport([_Response(200, payload)])
        provider = runtime.SinaProvider(transport=transport, clock=lambda: NOW)  # type: ignore[attr-defined]

        rows = provider.fetch_history(  # type: ignore[attr-defined]
            "000007.SZ", start=date(2023, 8, 8), end=date(2023, 8, 9)
        )

        self.assertEqual([date(2023, 8, 8), date(2023, 8, 9)], [row["trade_date"] for row in rows])
        self.assertEqual(100_000, rows[0]["pre_close_1e4"])

    def test_share_capital_applies_the_required_window_and_null_is_structured_failure(
        self,
    ) -> None:
        runtime = _runtime()
        wire = "sh600054"
        payload = (
            f"var KKE_ShareAmount_{wire}="
            '[{"date":"1996-11-22","amount":0},'
            '{"date":"2019-02-13","amount":100}];'
        ).encode()
        transport, _client, _sleeps = self._transport([_Response(200, payload)])
        provider = runtime.SinaProvider(transport=transport, clock=lambda: NOW)  # type: ignore[attr-defined]

        rows = provider.fetch_share_capital(  # type: ignore[attr-defined]
            "600054.SH", required_from=date(2023, 8, 8)
        )

        self.assertEqual([date(2019, 2, 13)], [row["effective_date"] for row in rows])

        null_payload = b"var KKE_ShareAmount_sh600190=null;"
        transport, _client, _sleeps = self._transport([_Response(200, null_payload)])
        provider = runtime.SinaProvider(transport=transport, clock=lambda: NOW)  # type: ignore[attr-defined]
        with self.assertRaisesRegex(Exception, "payload validation failed") as raised:
            provider.fetch_share_capital(  # type: ignore[attr-defined]
                "600190.SH", required_from=date(2023, 8, 8)
            )
        self.assertEqual(
            "SinaNormalizationError", raised.exception.evidence["error_class"]
        )

    def test_5xx_retries_with_injected_deterministic_backoff_and_stops_at_bound(self) -> None:
        transport, client, sleeps = self._transport(
            [_Response(503, b"one"), _Response(503, b"two"), _Response(503, b"three")]
        )

        with self.assertRaisesRegex(Exception, "(?:503|retry)"):
            transport.get(  # type: ignore[attr-defined]
                endpoint_kind="history",
                url="https://offline.invalid/history",
                request_key="sh600001",
            )
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(sleeps, [1.25, 2.25])

    def test_spot_pages_are_serial_and_require_count_to_match_all_records(self) -> None:
        runtime = _runtime()
        fixture = json.loads(FIXTURES.joinpath("spot_pages.json").read_text())
        responses = [
            _Response(200, fixture["count"].encode()),
            _Response(200, json.dumps(fixture["pages"]["1"]).encode()),
            _Response(200, json.dumps(fixture["pages"]["2"]).encode()),
        ]
        transport, client, _sleeps = self._transport(responses)
        provider = runtime.SinaSpotProvider(transport=transport, clock=lambda: NOW)  # type: ignore[attr-defined]

        result = provider.fetch_pages(date(2026, 8, 7))

        self.assertEqual(len(client.calls), 3)
        self.assertEqual([call[1].get("page") for call in client.calls[1:]], [1, 2])
        self.assertTrue(all(call[1].get("num") == 80 for call in client.calls[1:]))
        self.assertEqual(tuple(row["symbol"] for row in result.rows), ("sh600001", "sz000001"))
        self.assertEqual(result.expected_count, 2)
        self.assertEqual(result.actual_count, 2)


if __name__ == "__main__":
    unittest.main()
