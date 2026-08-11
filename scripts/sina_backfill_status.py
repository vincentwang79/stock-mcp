"""Read-only progress report for a checkpointed Sina backfill."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    database = args.database.resolve()
    manifest_path = args.manifest.resolve()
    if not database.is_file():
        parser.error(f"database does not exist: {database}")
    if not manifest_path.is_file():
        parser.error(f"manifest does not exist: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    run_id = str(manifest["run_id"])
    symbols = tuple(str(symbol) for symbol in manifest["symbols"])
    if not symbols or len(symbols) != len(set(symbols)):
        parser.error("manifest symbols must be non-empty and unique")

    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT symbol, status FROM sina_backfill_checkpoints "
            "WHERE run_id = ? ORDER BY symbol",
            (run_id,),
        ).fetchall()
    manifest_symbols = set(symbols)
    if any(str(row[0]) not in manifest_symbols for row in rows):
        parser.error("checkpoint contains a symbol outside the manifest")
    completed = tuple(str(row[0]) for row in rows if str(row[1]) == "completed")
    total = len(symbols)
    report = {
        "run_id": run_id,
        "total_symbols": total,
        "completed_symbols": len(completed),
        "pending_symbols": total - len(completed),
        "progress_bps": len(completed) * 10_000 // total,
        "last_completed_symbol": completed[-1] if completed else None,
    }
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
