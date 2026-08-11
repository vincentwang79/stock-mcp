"""Offline RED contracts for v0.4 Sina evidence and qualification storage."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from stock_mcp.domain import DailyBar, MarketSnapshot, Security
from stock_mcp.storage import Database

AS_OF = datetime(2026, 8, 7, 8, 30, tzinfo=UTC)
TRADE_DATE = date(2026, 8, 7)
SOURCE = "sina"
PAYLOAD_SHA256 = "a" * 64


def _fetch_evidence() -> dict[str, object]:
    return {
        "fetch_id": "sina-spot-2026-08-07-page-1",
        "source": SOURCE,
        "endpoint_kind": "spot_page",
        "request_key": "hs_a:page=1:num=80",
        "trade_date": TRADE_DATE,
        "http_date": "Fri, 07 Aug 2026 08:30:00 GMT",
        "retrieved_at": AS_OF,
        "http_status": 200,
        "byte_length": 1_024,
        "payload_sha256": PAYLOAD_SHA256,
        "adapter_version": "sina-adapter-v1",
        "status": "success",
        "error_class": None,
    }


class SinaFactImmutabilityContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "research.sqlite3"
        self.database = Database(self.path)
        self.database.initialize()

    def test_v10_database_migrates_forward_to_v11_without_erasing_v10_evidence(self) -> None:
        legacy = Path(self.temporary.name) / "legacy-v10.sqlite3"
        with sqlite3.connect(legacy) as connection:
            connection.execute("CREATE TABLE retained_v10_evidence (value TEXT NOT NULL)")
            connection.execute("INSERT INTO retained_v10_evidence VALUES ('recorded')")
            connection.execute("PRAGMA user_version = 10")

        Database(legacy).initialize()

        with sqlite3.connect(legacy) as connection:
            self.assertEqual(11, connection.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            self.assertTrue(
                {
                    "provider_fetch_evidence",
                    "share_capital_facts",
                    "daily_security_status",
                    "provider_shadow_runs",
                    "provider_qualifications",
                    "provider_attestations",
                    "provider_approvals",
                    "provider_registry",
                    "sina_backfill_checkpoints",
                }.issubset(tables)
            )
            self.assertEqual(
                "recorded",
                connection.execute("SELECT value FROM retained_v10_evidence").fetchone()[0],
            )

    def test_fetch_capital_and_status_facts_are_idempotent_but_never_overwritten(self) -> None:
        save_fetch = self._require("save_provider_fetch_evidence")
        save_capital = self._require("save_share_capital_facts")
        save_status = self._require("save_daily_security_statuses")
        fetch = _fetch_evidence()
        capital = {
            "symbol": "600000.SH",
            "effective_date": date(2026, 8, 1),
            "source": SOURCE,
            "outstanding_shares": 29_352_080_397,
            "source_timestamp": AS_OF,
            "payload_sha256": PAYLOAD_SHA256,
        }
        status = {
            "symbol": "600000.SH",
            "trade_date": TRADE_DATE,
            "source": "baostock",
            "tradestatus": "1",
            "is_st": False,
            "source_timestamp": AS_OF,
            "batch_sha256": "b" * 64,
        }

        save_fetch(fetch)
        save_fetch(fetch)
        save_capital((capital,))
        save_capital((capital,))
        save_status((status,))
        save_status((status,))

        with self.assertRaisesRegex(ValueError, "immutable|conflict"):
            save_fetch({**fetch, "http_status": 503})
        with self.assertRaisesRegex(ValueError, "immutable|conflict"):
            save_capital(({**capital, "outstanding_shares": 29_352_080_398},))
        with self.assertRaisesRegex(ValueError, "immutable|conflict"):
            save_status(({**status, "is_st": True},))

    def test_spot_batch_rolls_back_snapshot_when_its_fetch_evidence_conflicts(self) -> None:
        save_fetch = self._require("save_provider_fetch_evidence")
        save_batch = self._require("save_sina_spot_batch")
        original_fetch = _fetch_evidence()
        conflicting_fetch = {**original_fetch, "http_status": 503, "status": "failed"}
        snapshot = MarketSnapshot(
            trade_date=TRADE_DATE,
            source=SOURCE,
            source_timestamp=AS_OF,
            securities=(
                Security(
                    symbol="600000.SH",
                    name="recorded",
                    exchange="SSE",
                    board="MAIN",
                    list_date=date(2020, 1, 1),
                    industry="unavailable",
                    is_st=False,
                ),
            ),
            bars=(
                DailyBar(
                    symbol="600000.SH",
                    trade_date=TRADE_DATE,
                    open_1e4=100_000,
                    high_1e4=102_000,
                    low_1e4=99_000,
                    close_1e4=101_000,
                    pre_close_1e4=100_000,
                    volume_shares=1_000_000,
                    amount_fen=10_100_000_000,
                    source=SOURCE,
                    source_timestamp=AS_OF,
                ),
            ),
            advance_ratio_bps=6_000,
            above_ma20_ratio_bps=6_000,
        )
        save_fetch(original_fetch)

        with self.assertRaisesRegex(ValueError, "immutable|conflict"):
            save_batch(
                snapshot=snapshot,
                fetch_evidence=(conflicting_fetch,),
                metrics={
                    "expected_security_count": 1,
                    "actual_security_count": 1,
                    "expected_page_count": 1,
                    "actual_page_count": 1,
                },
            )

        self.assertFalse(
            self.database.has_market_snapshot(TRADE_DATE, source=SOURCE),
            "a rejected evidence write must not leave a partial Sina spot snapshot",
        )

    def test_backfill_symbol_writes_facts_and_checkpoint_in_one_transaction(self) -> None:
        save_symbol = self._require("save_sina_backfill_symbol")
        capital = {
            "symbol": "600000.SH",
            "effective_date": TRADE_DATE,
            "source": SOURCE,
            "outstanding_shares": 10_000_000,
            "source_timestamp": AS_OF,
            "payload_sha256": "e" * 64,
        }
        self.database.save_share_capital_facts((capital,))
        bar = DailyBar(
            symbol="600000.SH",
            trade_date=TRADE_DATE,
            open_1e4=100_000,
            high_1e4=102_000,
            low_1e4=99_000,
            close_1e4=101_000,
            pre_close_1e4=100_000,
            volume_shares=1_000_000,
            amount_fen=10_100_000_000,
            source=SOURCE,
            source_timestamp=AS_OF,
        )
        checkpoint = {
            "run_id": "run-atomic",
            "symbol": "600000.SH",
            "status": "completed",
            "history_payload_sha256": "d" * 64,
            "capital_payload_sha256": "e" * 64,
            "first_date": TRADE_DATE,
            "last_date": TRADE_DATE,
            "session_count": 1,
        }

        with self.assertRaisesRegex(ValueError, "immutable|conflict"):
            save_symbol(
                bars=(bar,),
                capital_facts=({**capital, "outstanding_shares": 10_000_001},),
                fetch_evidence=(),
                checkpoint=checkpoint,
            )

        self.assertEqual(
            (),
            self.database.load_symbol_history(
                "600000.SH", source=SOURCE, end_date=TRADE_DATE, limit=1
            ),
        )
        self.assertIsNone(
            self.database.load_sina_backfill_checkpoint(run_id="run-atomic", symbol="600000.SH")
        )

    def test_shadow_qualification_attestation_approval_and_registry_are_separate_immutable_gates(
        self,
    ) -> None:
        save_shadow = self._require("save_provider_shadow_run")
        save_qualification = self._require("save_provider_qualification")
        attest = self._require("record_provider_attestation")
        approve = self._require("approve_provider_source")
        register = self._require("register_provider_source")
        shadow = {
            "source": SOURCE,
            "trade_date": TRADE_DATE,
            "adapter_version": "sina-adapter-v1",
            "expected_security_count": 2,
            "actual_security_count": 2,
            "expected_page_count": 1,
            "actual_page_count": 1,
            "missing_count": 0,
            "duplicate_count": 0,
            "invalid_count": 0,
            "field_coverage_bps": 10_000,
            "same_source_history_ok": True,
            "dataset_hash": "c" * 64,
            "status": "success",
        }
        qualification = {
            "source": SOURCE,
            "through_date": TRADE_DATE,
            "status": "qualified_for_manual_approval",
            "dataset_hash": "c" * 64,
            "recorded_at": AS_OF,
        }

        save_shadow(shadow)
        save_shadow(shadow)
        save_qualification(qualification)
        save_qualification(qualification)
        attestation = attest(source=SOURCE, through_date=TRADE_DATE, dataset_hash="c" * 64)
        self.assertEqual(SOURCE, attestation["source"])
        self.assertNotEqual(
            "registered",
            register(source=SOURCE, through_date=TRADE_DATE, dataset_hash="c" * 64),
            "a registry entry requires separate host approval",
        )
        approval = approve(source=SOURCE, through_date=TRADE_DATE, dataset_hash="c" * 64)
        self.assertEqual(SOURCE, approval["source"])
        self.assertEqual(
            "registered",
            register(source=SOURCE, through_date=TRADE_DATE, dataset_hash="c" * 64),
        )
        qualification_id = str(attestation["qualification_id"])
        self.database.approve_provider_source_capabilities(
            qualification_id=qualification_id,
            capabilities=("enrichment", "backup_price"),
        )
        activated = self.database.activate_provider_source(
            source=SOURCE,
            qualification_id=qualification_id,
            capabilities=("enrichment", "backup_price"),
            idempotency_key="activate-recorded-sina",
        )
        self.assertEqual(qualification_id, activated["qualification_id"])
        with self.assertRaisesRegex(ValueError, "immutable|conflict"):
            save_shadow({**shadow, "actual_security_count": 1})

    def test_collecting_qualification_recheck_is_idempotent_before_upgrade(self) -> None:
        base = {
            "qualification_id": "qualification-sina-window-1",
            "source": SOURCE,
            "through_date": TRADE_DATE,
            "status": "collecting",
            "dataset_hash": "c" * 64,
            "window_hash": "d" * 64,
            "consecutive_window_complete": True,
            "configuration_hash": "e" * 64,
        }

        self.database.save_provider_qualification({**base, "recorded_at": AS_OF})
        self.database.save_provider_qualification(
            {**base, "recorded_at": datetime(2026, 8, 7, 8, 31, tzinfo=UTC)}
        )
        self.database.save_provider_qualification(
            {
                **base,
                "status": "qualified_for_manual_approval",
                "recorded_at": datetime(2026, 8, 7, 8, 32, tzinfo=UTC),
            }
        )

        stored = self.database.get_provider_qualification(SOURCE)
        self.assertIsNotNone(stored)
        self.assertEqual("qualified_for_manual_approval", stored["status"])

    def test_host_capability_approval_rejects_an_invented_qualification_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "qualified|attested"):
            self.database.approve_provider_source_capabilities(
                qualification_id="invented", capabilities=("enrichment", "backup_price")
            )

    def _require(self, name: str):
        value = getattr(self.database, name, None)
        self.assertTrue(callable(value), f"Schema v11 requires Database.{name}()")
        return value


if __name__ == "__main__":
    unittest.main()
