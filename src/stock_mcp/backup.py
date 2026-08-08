"""Verified SQLite online backup, retention, and restore."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .storage import Database


class BackupIntegrityError(RuntimeError):
    """A backup cannot be trusted or safely restored."""


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    database_path: Path
    checksum_path: Path
    sha256: str


class BackupManager:
    def __init__(self, directory: str | Path, *, retention: int = 14) -> None:
        if retention < 1:
            raise ValueError("backup retention must be positive")
        self.directory = Path(directory)
        self.retention = retention

    def create(self, database: Database, *, label: str) -> BackupArtifact:
        if re.fullmatch(r"[0-9A-Za-z._-]+", label) is None:
            raise ValueError("backup label contains unsupported characters")
        self.directory.mkdir(parents=True, exist_ok=True)
        backup_path = self.directory / f"stock-mcp-{label}.sqlite3"
        checksum_path = backup_path.with_suffix(backup_path.suffix + ".sha256")
        database.backup_to(backup_path)
        self._assert_sqlite_integrity(backup_path)
        digest = self._digest(backup_path)
        checksum_path.write_text(f"{digest}  {backup_path.name}\n", encoding="ascii")
        self._apply_retention()
        return BackupArtifact(backup_path, checksum_path, digest)

    def restore(self, backup_path: str | Path, destination: str | Path) -> None:
        source_path = Path(backup_path)
        checksum_path = source_path.with_suffix(source_path.suffix + ".sha256")
        expected = self._read_checksum(checksum_path, source_path.name)
        actual = self._digest(source_path)
        if actual != expected:
            raise BackupIntegrityError("backup SHA-256 does not match its manifest")
        self._assert_sqlite_integrity(source_path)

        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination_path.with_suffix(destination_path.suffix + ".restore.tmp")
        if temporary.exists():
            temporary.unlink()
        try:
            with (
                sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True) as source,
                sqlite3.connect(temporary) as target,
            ):
                source.backup(target)
            self._assert_sqlite_integrity(temporary)
            for sidecar in (
                Path(str(destination_path) + "-wal"),
                Path(str(destination_path) + "-shm"),
            ):
                if sidecar.exists():
                    sidecar.unlink()
            self._replace_atomically(temporary, destination_path)
        finally:
            if temporary.exists():
                with suppress(PermissionError):
                    temporary.unlink()

    @staticmethod
    def _replace_atomically(source: Path, destination: Path, *, attempts: int = 30) -> None:
        for attempt in range(1, attempts + 1):
            try:
                os.replace(source, destination)
                return
            except PermissionError as error:
                if attempt == attempts:
                    raise BackupIntegrityError(
                        "database restore could not replace "
                        f"{destination} after {attempts} attempts"
                    ) from error
                time.sleep(1)

    def _apply_retention(self) -> None:
        backups = sorted(
            self.directory.glob("stock-mcp-*.sqlite3"),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
        for obsolete in backups[: max(0, len(backups) - self.retention)]:
            checksum = obsolete.with_suffix(obsolete.suffix + ".sha256")
            obsolete.unlink()
            if checksum.exists():
                checksum.unlink()

    @staticmethod
    def _assert_sqlite_integrity(path: Path) -> None:
        try:
            with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
                result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        except (sqlite3.Error, OSError) as error:
            raise BackupIntegrityError(f"backup cannot be opened: {error}") from error
        if result != "ok":
            raise BackupIntegrityError(f"SQLite integrity check failed: {result}")

    @staticmethod
    def _digest(path: Path) -> str:
        digest = sha256()
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as error:
            raise BackupIntegrityError(f"backup cannot be read: {error}") from error
        return digest.hexdigest()

    @staticmethod
    def _read_checksum(path: Path, expected_name: str) -> str:
        try:
            line = path.read_text(encoding="ascii").strip()
        except OSError as error:
            raise BackupIntegrityError("backup checksum manifest is missing") from error
        digest, separator, name = line.partition("  ")
        if not separator or re.fullmatch(r"[0-9a-f]{64}", digest) is None or name != expected_name:
            raise BackupIntegrityError("backup checksum manifest is invalid")
        return digest
