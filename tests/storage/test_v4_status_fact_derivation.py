from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from stock_mcp import backfill
from stock_mcp.storage import Database


class V4StatusFactDerivationTest(unittest.TestCase):
    def test_network_status_backfill_persists_trading_and_suspended_mainboard_rows(self) -> None:
        class Result:
            error_code = "0"
            fields = ("code", "code_name", "tradeStatus")

            def __init__(self) -> None:
                self.rows = iter(
                    (
                        ("sh.600001", "one", "1"),
                        ("sh.600002", "two", "0"),
                    )
                )
                self.current: tuple[str, str, str] | None = None

            def next(self) -> bool:
                self.current = next(self.rows, None)
                return self.current is not None

            def get_row_data(self) -> tuple[str, str, str]:
                assert self.current is not None
                return self.current

        class Client:
            def query_all_stock(self, *, day: str) -> Result:
                self.day = day
                return Result()

        operation = getattr(backfill, "backfill_baostock_daily_statuses", None)
        self.assertTrue(callable(operation))
        if not callable(operation):
            return
        report = operation(
            database=self.database,
            client=Client(),
            sessions=(date(2026, 8, 7),),
            source_timestamp="2026-08-07T08:00:00+00:00",
            minimum_main_board_count=2,
        )

        self.assertEqual(2, report["rows"])
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT symbol,tradestatus FROM daily_security_status ORDER BY symbol"
            ).fetchall()
        self.assertEqual([("600001.SH", "1"), ("600002.SH", "0")], [tuple(row) for row in rows])

    def test_status_checkpoint_rejects_and_retries_a_truncated_success_response(self) -> None:
        class Result:
            error_code = "0"
            fields = ("code", "code_name", "tradeStatus")

            def __init__(self, rows: tuple[tuple[str, str, str], ...]) -> None:
                self.rows = iter(rows)
                self.current: tuple[str, str, str] | None = None

            def next(self) -> bool:
                self.current = next(self.rows, None)
                return self.current is not None

            def get_row_data(self) -> tuple[str, str, str]:
                assert self.current is not None
                return self.current

        class Client:
            def __init__(self) -> None:
                self.calls = 0

            def query_all_stock(self, *, day: str) -> Result:
                self.calls += 1
                rows = (("sh.600001", "one", "1"),)
                if self.calls == 2:
                    rows += (("sh.600002", "two", "0"),)
                return Result(rows)

        client = Client()
        operation = backfill.backfill_baostock_daily_statuses
        with self.assertRaisesRegex(ValueError, "coverage"):
            operation(
                database=self.database,
                client=client,
                sessions=(date(2026, 8, 7),),
                source_timestamp="2026-08-07T08:00:00+00:00",
                minimum_main_board_count=2,
            )
        report = operation(
            database=self.database,
            client=client,
            sessions=(date(2026, 8, 7),),
            source_timestamp="2026-08-07T08:00:00+00:00",
            minimum_main_board_count=2,
        )
        self.assertEqual(2, client.calls)
        self.assertEqual(2, report["rows"])

    def test_missing_symbol_within_recorded_lifecycle_is_not_inferred_as_suspended(self) -> None:
        class Result:
            error_code = "0"
            fields = ("code", "code_name", "tradeStatus")

            def __init__(self) -> None:
                self.rows = iter(
                    (
                        ("sh.600001", "one", "1"),
                        ("sh.600002", "two", "1"),
                    )
                )
                self.current: tuple[str, str, str] | None = None

            def next(self) -> bool:
                self.current = next(self.rows, None)
                return self.current is not None

            def get_row_data(self) -> tuple[str, str, str]:
                assert self.current is not None
                return self.current

        class Client:
            def query_all_stock(self, *, day: str) -> Result:
                return Result()

        with self.database.connect() as connection:
            connection.executemany(
                "INSERT INTO market_snapshots VALUES(?, 'tushare', "
                "'2026-08-07T08:00:00+00:00', 5000, 5000)",
                (("2026-08-06",), ("2026-08-08",)),
            )
            connection.execute(
                "INSERT INTO snapshot_securities VALUES"
                "('2026-08-06','tushare','600003.SH','three','SSE','MAIN',"
                "'2000-01-01','fixture',0)"
            )
            connection.execute(
                "INSERT INTO snapshot_securities VALUES"
                "('2026-08-08','tushare','600003.SH','three','SSE','MAIN',"
                "'2000-01-01','fixture',0)"
            )

        with self.assertRaisesRegex(ValueError, "recorded lifecycle"):
            backfill.backfill_baostock_daily_statuses(
                database=self.database,
                client=Client(),
                sessions=(date(2026, 8, 7),),
                source_timestamp="2026-08-07T08:00:00+00:00",
                minimum_main_board_count=2,
            )
        with self.database.connect() as connection:
            inferred = connection.execute(
                "SELECT COUNT(*) FROM daily_security_status WHERE symbol='600003.SH'"
            ).fetchone()[0]
        self.assertEqual(0, inferred)

    def test_symbol_after_recorded_lifecycle_does_not_block_status_backfill(self) -> None:
        class Result:
            error_code = "0"
            fields = ("code", "code_name", "tradeStatus")

            def __init__(self) -> None:
                self.rows = iter(
                    (
                        ("sh.600001", "one", "1"),
                        ("sh.600002", "two", "1"),
                    )
                )
                self.current: tuple[str, str, str] | None = None

            def next(self) -> bool:
                self.current = next(self.rows, None)
                return self.current is not None

            def get_row_data(self) -> tuple[str, str, str]:
                assert self.current is not None
                return self.current

        class Client:
            def query_all_stock(self, *, day: str) -> Result:
                return Result()

        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO market_snapshots VALUES"
                "('2026-08-06','tushare','2026-08-06T08:00:00+00:00',5000,5000)"
            )
            connection.execute(
                "INSERT INTO snapshot_securities VALUES"
                "('2026-08-06','tushare','600003.SH','ended','SSE','MAIN',"
                "'2000-01-01','fixture',0)"
            )

        report = backfill.backfill_baostock_daily_statuses(
            database=self.database,
            client=Client(),
            sessions=(date(2026, 8, 7),),
            source_timestamp="2026-08-07T08:00:00+00:00",
            minimum_main_board_count=2,
        )

        self.assertEqual(2, report["rows"])
        with self.database.connect() as connection:
            inferred = connection.execute(
                "SELECT COUNT(*) FROM daily_security_status WHERE symbol='600003.SH'"
            ).fetchone()[0]
        self.assertEqual(0, inferred)

    def test_unknown_trade_status_is_rejected_without_persisting_a_checkpoint(self) -> None:
        class Result:
            error_code = "0"
            fields = ("code", "code_name", "tradeStatus")

            def __init__(self) -> None:
                self.rows = iter((("sh.600001", "one", "2"), ("sh.600002", "two", "1")))
                self.current: tuple[str, str, str] | None = None

            def next(self) -> bool:
                self.current = next(self.rows, None)
                return self.current is not None

            def get_row_data(self) -> tuple[str, str, str]:
                assert self.current is not None
                return self.current

        class Client:
            def query_all_stock(self, *, day: str) -> Result:
                return Result()

        with self.assertRaisesRegex(ValueError, "tradeStatus"):
            backfill.backfill_baostock_daily_statuses(
                database=self.database,
                client=Client(),
                sessions=(date(2026, 8, 7),),
                source_timestamp="2026-08-07T08:00:00+00:00",
                minimum_main_board_count=2,
            )
        with self.database.connect() as connection:
            rows = connection.execute("SELECT COUNT(*) FROM daily_security_status").fetchone()[0]
            checkpoints = connection.execute(
                "SELECT COUNT(*) FROM provider_backfill_checkpoints"
            ).fetchone()[0]
        self.assertEqual((0, 0), (rows, checkpoints))

    def test_storage_rejects_unknown_trade_status_from_any_import_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "tradeStatus"):
            self.database.save_daily_security_statuses(
                (
                    {
                        "symbol": "600001.SH",
                        "trade_date": date(2026, 8, 7),
                        "source": "baostock",
                        "tradestatus": "2",
                        "is_st": False,
                        "source_timestamp": "2026-08-07T08:00:00+00:00",
                        "batch_sha256": "a" * 64,
                    },
                )
            )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "stock.sqlite3")
        self.database.initialize()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO market_snapshots VALUES"
                "('2026-08-07','tushare','2026-08-07T08:00:00+00:00',5000,5000)"
            )
            connection.executemany(
                "INSERT INTO snapshot_securities VALUES"
                "('2026-08-07','tushare',?,?,'SSE','MAIN','2000-01-01','fixture',?)",
                (
                    ("600001.SH", "one", 0),
                    ("600002.SH", "two", 0),
                ),
            )
            connection.executemany(
                "INSERT INTO daily_bars VALUES"
                "(?,'2026-08-07',100000,101000,99000,100000,100000,1000,10000000,"
                "'tushare','2026-08-07T08:00:00+00:00')",
                (("600001.SH",), ("600002.SH",)),
            )

    def test_derives_idempotent_baostock_statuses_from_legacy_eligible_snapshots(self) -> None:
        build = getattr(self.database, "build_v4_legacy_status_facts", None)
        self.assertTrue(callable(build))
        if not callable(build):
            return

        first = build(start=date(2026, 8, 7), end=date(2026, 8, 7))
        second = build(start=date(2026, 8, 7), end=date(2026, 8, 7))

        self.assertEqual(
            {
                "schema": "legacy-baostock-snapshot-status-v1",
                "days": 1,
                "eligible_rows": 2,
                "inserted_rows": 2,
                "existing_rows": 0,
            },
            first,
        )
        self.assertEqual(0, second["inserted_rows"])
        self.assertEqual(2, second["existing_rows"])
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT symbol,tradestatus,is_st,source_timestamp,batch_sha256 "
                "FROM daily_security_status ORDER BY symbol"
            ).fetchall()
        self.assertEqual(["600001.SH", "600002.SH"], [str(row[0]) for row in rows])
        self.assertTrue(all(str(row[1]) == "1" and int(row[2]) == 0 for row in rows))
        self.assertTrue(all(str(row[3]) == "2026-08-07T08:00:00+00:00" for row in rows))
        self.assertTrue(all(len(str(row[4])) == 64 for row in rows))

    def test_conflicting_existing_status_aborts_without_partial_inserts(self) -> None:
        self.database.save_daily_security_statuses(
            (
                {
                    "symbol": "600001.SH",
                    "trade_date": date(2026, 8, 7),
                    "source": "baostock",
                    "tradestatus": "0",
                    "is_st": False,
                    "source_timestamp": "2026-08-07T08:00:00+00:00",
                    "batch_sha256": "a" * 64,
                },
            )
        )
        build = getattr(self.database, "build_v4_legacy_status_facts", None)
        self.assertTrue(callable(build))
        if not callable(build):
            return

        with self.assertRaisesRegex(ValueError, "conflict|status"):
            build(start=date(2026, 8, 7), end=date(2026, 8, 7))
        with self.database.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM daily_security_status").fetchone()[0]
        self.assertEqual(1, count)


if __name__ == "__main__":
    unittest.main()
