"""Offline RED contract for resumable Sina history and capital backfills."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from stock_mcp.storage import Database

TRADE_DATE = date(2026, 8, 7)
AS_OF = datetime(2026, 8, 7, 8, 30, tzinfo=UTC)


class RecordedSinaProvider:
    """Fixed fixture provider; it has no HTTP client and cannot reach a live endpoint."""

    source = "sina"
    adapter_version = "sina-adapter-v1"

    def __init__(self) -> None:
        self.history_requests: list[str] = []
        self.capital_requests: list[str] = []

    def fetch_history(
        self, symbol: str, *, start: date, end: date
    ) -> tuple[dict[str, object], ...]:
        self.history_requests.append(symbol)
        return (
            {
                "symbol": symbol,
                "trade_date": TRADE_DATE,
                "open_1e4": 100_000,
                "high_1e4": 102_000,
                "low_1e4": 99_000,
                "close_1e4": 101_000,
                "pre_close_1e4": 100_000,
                "volume_shares": 1_000_000,
                "amount_fen": 10_100_000_000,
                "source": "sina",
                "source_timestamp": AS_OF,
                "payload_sha256": "d" * 64,
            },
        )

    def fetch_share_capital(self, symbol: str) -> tuple[dict[str, object], ...]:
        self.capital_requests.append(symbol)
        return (
            {
                "symbol": symbol,
                "effective_date": TRADE_DATE,
                "source": "sina",
                "outstanding_shares": 10_000_000,
                "source_timestamp": AS_OF,
                "payload_sha256": "e" * 64,
            },
        )


class SinaBackfillContractTest(unittest.TestCase):
    def test_each_security_checkpoint_is_recoverable_and_replay_verifies_hash_without_refetch(
        self,
    ) -> None:
        from stock_mcp import backfill

        service_type = getattr(backfill, "SinaBackfillService", None)
        self.assertTrue(
            callable(service_type), "v0.4 requires SinaBackfillService at the backfill seam"
        )
        assert service_type is not None
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "research.sqlite3")
            database.initialize()
            provider = RecordedSinaProvider()
            service = service_type(
                database=database,
                provider=provider,
                manifest={
                    "run_id": "recorded-sina-backfill-1",
                    "symbols": ("600000.SH",),
                    "start": TRADE_DATE,
                    "end": TRADE_DATE,
                    "adapter_version": "sina-adapter-v1",
                    "rate_limit_per_second": 1,
                },
            )

            first = service.backfill()
            second = service.backfill()

            self.assertEqual(("600000.SH",), first.completed_symbols)
            self.assertEqual(("600000.SH",), second.verified_symbols)
            self.assertEqual(["600000.SH"], provider.history_requests)
            self.assertEqual(["600000.SH"], provider.capital_requests)
            checkpoint = database.load_sina_backfill_checkpoint(
                run_id="recorded-sina-backfill-1", symbol="600000.SH"
            )
            self.assertEqual("completed", checkpoint["status"])
            self.assertEqual("d" * 64, checkpoint["history_payload_sha256"])


if __name__ == "__main__":
    unittest.main()
