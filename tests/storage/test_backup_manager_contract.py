from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

from stock_mcp.backup import BackupIntegrityError, BackupManager
from stock_mcp.domain import DailyBar
from stock_mcp.storage import Database


def _bar(day: int) -> DailyBar:
    return DailyBar(
        symbol="600000.SH",
        trade_date=date(2026, 8, day),
        open_1e4=100_000,
        high_1e4=105_000,
        low_1e4=99_000,
        close_1e4=103_000,
        pre_close_1e4=100_000,
        volume_shares=1_000_000,
        amount_fen=10_300_000_000,
        source="fixture",
        source_timestamp=datetime(2026, 8, day, 8, 30, tzinfo=UTC),
    )


class BackupManagerContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.database = Database(self.root / "data" / "stock.sqlite3")
        self.database.initialize()
        self.database.save_daily_bars((_bar(7),))

    def test_backup_has_checksum_and_can_restore_to_a_new_database(self) -> None:
        manager = BackupManager(self.root / "backups", retention=3)
        artifact = manager.create(self.database, label="20260807")
        restored_path = self.root / "restored" / "stock.sqlite3"

        manager.restore(artifact.database_path, restored_path)

        self.assertTrue(artifact.checksum_path.is_file())
        restored = Database(restored_path)
        restored.initialize()
        self.assertEqual((_bar(7),), restored.load_daily_bars(date(2026, 8, 7), "fixture"))

    def test_corruption_is_rejected_before_restore(self) -> None:
        manager = BackupManager(self.root / "backups", retention=3)
        artifact = manager.create(self.database, label="20260807")
        artifact.database_path.write_bytes(artifact.database_path.read_bytes() + b"corrupt")

        with self.assertRaises(BackupIntegrityError):
            manager.restore(artifact.database_path, self.root / "restored.sqlite3")

    def test_restore_removes_stale_wal_and_shared_memory_sidecars(self) -> None:
        manager = BackupManager(self.root / "backups", retention=3)
        artifact = manager.create(self.database, label="20260807")
        destination = self.root / "restored.sqlite3"
        wal = Path(str(destination) + "-wal")
        shm = Path(str(destination) + "-shm")
        wal.write_bytes(b"stale-wal")
        shm.write_bytes(b"stale-shm")

        manager.restore(artifact.database_path, destination)

        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())

    def test_restore_retries_a_transient_windows_file_lock_during_atomic_replace(self) -> None:
        manager = BackupManager(self.root / "backups", retention=3)
        artifact = manager.create(self.database, label="20260807")
        destination = self.root / "restored.sqlite3"
        real_replace = __import__("os").replace
        attempts = 0

        def replace_after_one_lock(source: str | Path, target: str | Path) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError("simulated transient Windows file lock")
            real_replace(source, target)

        with (
            patch("stock_mcp.backup.os.replace", side_effect=replace_after_one_lock),
            patch("stock_mcp.backup.time.sleep", create=True) as sleep,
        ):
            manager.restore(artifact.database_path, destination)

        self.assertEqual(attempts, 2)
        sleep.assert_called_once()
        restored = Database(destination).load_daily_bars(date(2026, 8, 7), "fixture")
        self.assertEqual((_bar(7),), restored)

    def test_retention_removes_oldest_complete_backup_pair(self) -> None:
        manager = BackupManager(self.root / "backups", retention=2)

        first = manager.create(self.database, label="20260805")
        manager.create(self.database, label="20260806")
        manager.create(self.database, label="20260807")

        self.assertFalse(first.database_path.exists())
        self.assertFalse(first.checksum_path.exists())
        self.assertEqual(2, len(tuple((self.root / "backups").glob("*.sqlite3"))))


if __name__ == "__main__":
    unittest.main()
