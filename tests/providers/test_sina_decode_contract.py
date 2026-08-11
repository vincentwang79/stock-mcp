"""Offline v0.4 Sina protocol decoder contracts; no endpoint is contacted."""

from __future__ import annotations

import importlib
import json
import unittest
from pathlib import Path

FIXTURES = Path(__file__).with_name("fixtures") / "sina"


class _UnavailableDecoder:
    def __getattr__(self, _name: str) -> object:
        def unavailable(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("UNIMPLEMENTED_SINA_DECODER_CAPABILITY")

        return unavailable


def _decoder() -> object:
    """Keep RED actionable if the module is not present yet, not an ImportError."""
    try:
        return importlib.import_module("stock_mcp.providers.sina_decode")
    except ModuleNotFoundError as error:
        if error.name != "stock_mcp.providers.sina_decode":
            raise
        return _UnavailableDecoder()


class SinaDecodeContractTest(unittest.TestCase):
    def test_recorded_compressed_klc_is_decoded_without_executing_javascript(self) -> None:
        decoder = _decoder()
        payload = (Path(__file__).parent / "fixtures" / "sina" / "recorded_klc_kl.js").read_bytes()

        self.assertEqual(
            (
                {
                    "date": "2024-04-02",
                    "prevclose": 17.43,
                    "open": 50,
                    "high": 58.1,
                    "low": 48.1,
                    "close": 51.88,
                    "volume": 28_991_027,
                    "amount": 1_522_338_078,
                },
            ),
            decoder.decode_klc2(payload),  # type: ignore[attr-defined]
        )

    def test_strict_jsonp_returns_only_the_expected_assignment_value(self) -> None:
        decoder = _decoder()
        payload = b'var KKE_ShareAmount_sh600001=[["2026-08-06","1.2345"]];'

        value = decoder.parse_jsonp_assignment(  # type: ignore[attr-defined]
            payload, assignment="KKE_ShareAmount_sh600001"
        )

        self.assertEqual(value, [["2026-08-06", "1.2345"]])

    def test_jsonp_rejects_trailing_statements_instead_of_executing_them(self) -> None:
        decoder = _decoder()
        payload = FIXTURES.joinpath("corrupt_klc_kl.js").read_bytes()

        with self.assertRaisesRegex(Exception, "(?:JSONP|KLC|trailing|payload)"):
            decoder.parse_jsonp_assignment(  # type: ignore[attr-defined]
                payload, assignment="KLC_KL_sh600001"
            )

    def test_klc_decoder_rejects_corrupt_payload_without_eval_or_partial_rows(self) -> None:
        decoder = _decoder()
        payload = FIXTURES.joinpath("corrupt_klc_kl.js").read_bytes()

        with self.assertRaisesRegex(Exception, "(?:KLC|decode|payload)"):
            decoder.decode_klc2(payload)  # type: ignore[attr-defined]

    def test_spot_json_accepts_a_fixed_json_array_and_rejects_jsonp(self) -> None:
        decoder = _decoder()
        fixture = json.loads(FIXTURES.joinpath("spot_pages.json").read_text())
        payload = json.dumps(fixture["pages"]["1"], separators=(",", ":")).encode()

        rows = decoder.decode_spot_json(payload)  # type: ignore[attr-defined]

        self.assertEqual(rows, tuple(fixture["pages"]["1"]))
        with self.assertRaisesRegex(Exception, "(?:JSON|payload)"):
            decoder.decode_spot_json(b"callback([])")  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
