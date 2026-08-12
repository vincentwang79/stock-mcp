from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from stock_mcp.storage import Database


class V4StatusFactDerivationTest(unittest.TestCase):
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
            count = connection.execute(
                "SELECT COUNT(*) FROM daily_security_status"
            ).fetchone()[0]
        self.assertEqual(1, count)


if __name__ == "__main__":
    unittest.main()
