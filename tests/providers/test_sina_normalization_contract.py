"""Offline v0.4 Sina units, date-chain, and share-capital contracts."""

from __future__ import annotations

import importlib
import json
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from stock_mcp.domain import DailyBar

FIXTURES = Path(__file__).with_name("fixtures") / "sina"
TIMESTAMP = datetime(2026, 8, 7, 8, 30, tzinfo=UTC)


class _UnavailableNormalization:
    def __getattr__(self, name: str) -> object:
        def unavailable(*_args: object, **_kwargs: object) -> object:
            raise AssertionError(f"Sina normalization behavior is not implemented: {name}")

        return unavailable


def _normalization() -> object:
    try:
        return importlib.import_module("stock_mcp.providers.sina_normalization")
    except ModuleNotFoundError as error:
        if error.name != "stock_mcp.providers.sina_normalization":
            raise
        return _UnavailableNormalization()


def _history_rows() -> list[dict[str, object]]:
    return [
        {
            "date": "2026-08-06",
            "open": "10.00",
            "high": "10.30",
            "low": "9.90",
            "close": "10.10",
            "volume": "1000",
            "amount": "10100",
            "prevclose": "10.00",
            "postVol": "0",
            "postAmt": "0",
        },
        {
            "date": "2026-08-07",
            "open": "10.10",
            "high": "10.40",
            "low": "10.00",
            "close": "10.25",
            "volume": "2000",
            "amount": "20500",
            "prevclose": "",
            "postVol": "0",
            "postAmt": "0",
        },
    ]


class SinaNormalizationContractTest(unittest.TestCase):
    def test_history_preserves_sina_units_and_derives_only_a_same_series_prevclose(self) -> None:
        normalizer = _normalization()

        bars = normalizer.normalize_sina_history(  # type: ignore[attr-defined]
            _history_rows(), symbol="600001.SH", source_timestamp=TIMESTAMP
        )

        self.assertEqual(
            bars,
            (
                DailyBar(
                    symbol="600001.SH",
                    trade_date=date(2026, 8, 6),
                    open_1e4=100_000,
                    high_1e4=103_000,
                    low_1e4=99_000,
                    close_1e4=101_000,
                    pre_close_1e4=100_000,
                    volume_shares=1_000,
                    amount_fen=1_010_000,
                    source="sina",
                    source_timestamp=TIMESTAMP,
                ),
                DailyBar(
                    symbol="600001.SH",
                    trade_date=date(2026, 8, 7),
                    open_1e4=101_000,
                    high_1e4=104_000,
                    low_1e4=100_000,
                    close_1e4=102_500,
                    pre_close_1e4=101_000,
                    volume_shares=2_000,
                    amount_fen=2_050_000,
                    source="sina",
                    source_timestamp=TIMESTAMP,
                ),
            ),
        )

    def test_history_rejects_an_unverifiable_first_prevclose_and_duplicate_date(self) -> None:
        normalizer = _normalization()
        without_chain = _history_rows()
        without_chain[0]["prevclose"] = ""
        with self.assertRaisesRegex(Exception, "(?:prevclose|pre_close|first)"):
            normalizer.normalize_sina_history(  # type: ignore[attr-defined]
                without_chain, symbol="600001.SH", source_timestamp=TIMESTAMP
            )

        duplicate = _history_rows() + [_history_rows()[1]]
        with self.assertRaisesRegex(Exception, "(?:duplicate|date)"):
            normalizer.normalize_sina_history(  # type: ignore[attr-defined]
                duplicate, symbol="600001.SH", source_timestamp=TIMESTAMP
            )

    def test_share_capital_uses_ten_thousand_share_units_and_never_backward_fills(self) -> None:
        normalizer = _normalization()
        fixture = json.loads(FIXTURES.joinpath("share_capital.json").read_text())

        facts = normalizer.normalize_sina_share_capital(  # type: ignore[attr-defined]
            fixture["rows"], symbol="600001.SH", source_timestamp=TIMESTAMP
        )

        self.assertEqual(facts[0].effective_date, date(2026, 8, 6))
        self.assertEqual(facts[0].outstanding_shares, 12_345)
        self.assertIsNone(
            normalizer.outstanding_shares_on(  # type: ignore[attr-defined]
                facts, trade_date=date(2026, 8, 5)
            )
        )
        self.assertEqual(
            normalizer.outstanding_shares_on(facts, trade_date=date(2026, 8, 7)),  # type: ignore[attr-defined]
            12_345,
        )

    def test_share_capital_accepts_the_recorded_object_shape(self) -> None:
        normalizer = _normalization()

        facts = normalizer.normalize_sina_share_capital(  # type: ignore[attr-defined]
            [{"date": "2024-12-31", "amount": 1940557.185}],
            symbol="000001.SZ",
            source_timestamp=TIMESTAMP,
        )

        self.assertEqual(facts[0].effective_date, date(2024, 12, 31))
        self.assertEqual(facts[0].outstanding_shares, 19_405_571_850)

    def test_share_capital_discards_only_prewindow_invalid_prefix(self) -> None:
        normalizer = _normalization()

        facts = normalizer.normalize_sina_share_capital(  # type: ignore[attr-defined]
            [
                {"date": "1996-11-22", "amount": 0},
                {"date": "2006-07-24", "amount": 19_303.986},
                {"date": "2006-07-24", "amount": 13_993.2},
                {"date": "2007-07-24", "amount": 26_336.5742},
                {"date": "2024-12-31", "amount": 30_000},
            ],
            symbol="600061.SH",
            source_timestamp=TIMESTAMP,
            required_from=date(2023, 8, 8),
        )

        self.assertEqual(
            [date(2007, 7, 24), date(2024, 12, 31)],
            [fact.effective_date for fact in facts],
        )
        self.assertEqual(263_365_742, facts[0].outstanding_shares)

    def test_share_capital_never_discards_window_anomalies_or_null_payload(self) -> None:
        normalizer = _normalization()
        with self.assertRaisesRegex(Exception, "window|positive|duplicate"):
            normalizer.normalize_sina_share_capital(  # type: ignore[attr-defined]
                [
                    {"date": "2023-08-08", "amount": 10},
                    {"date": "2023-08-08", "amount": 11},
                ],
                symbol="600061.SH",
                source_timestamp=TIMESTAMP,
                required_from=date(2023, 8, 8),
            )
        with self.assertRaisesRegex(Exception, "array|unavailable"):
            normalizer.normalize_sina_share_capital(  # type: ignore[attr-defined]
                None,
                symbol="600190.SH",
                source_timestamp=TIMESTAMP,
                required_from=date(2023, 8, 8),
            )

    def test_spot_keeps_provider_fields_and_derives_fen_market_cap_with_half_up_rounding(
        self,
    ) -> None:
        normalizer = _normalization()
        fixture = json.loads(FIXTURES.joinpath("spot_pages.json").read_text())

        records = normalizer.normalize_sina_spot(  # type: ignore[attr-defined]
            fixture["pages"]["1"], trade_date=date(2026, 8, 7), source_timestamp=TIMESTAMP
        )

        record = records[0]
        self.assertEqual(record.upstream_market_cap_fen, 12_345_670_000)
        self.assertEqual(record.upstream_circulating_market_cap_fen, 10_000_010_000)
        self.assertEqual(str(record.upstream_turnover_rate), "0.08")
        metrics = normalizer.derive_sina_share_metrics(  # type: ignore[attr-defined]
            close_1e4=100_050, volume_shares=1_000, outstanding_shares=1
        )
        self.assertEqual(metrics.market_cap_fen, 1_001)
        self.assertEqual(str(metrics.turnover_rate), "1000")


if __name__ == "__main__":
    unittest.main()
