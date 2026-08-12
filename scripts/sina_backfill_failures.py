"""Export the complete pending Sina backfill set with latest redacted evidence."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path


def _wire_symbol(symbol: str) -> str:
    return ("sh" if symbol.endswith(".SH") else "sz") + symbol[:6]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    database = args.database.resolve()
    manifest_path = args.manifest.resolve()
    output = args.output.resolve()
    if not database.is_file():
        parser.error(f"database does not exist: {database}")
    if not manifest_path.is_file():
        parser.error(f"manifest does not exist: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    run_id = str(manifest["run_id"])
    symbols = tuple(str(symbol) for symbol in manifest["symbols"])
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
        completed = {
            str(row[0])
            for row in connection.execute(
                "SELECT symbol FROM sina_backfill_checkpoints "
                "WHERE run_id=? AND status='completed'",
                (run_id,),
            )
        }
        evidence = connection.execute(
            "SELECT request_key, endpoint_kind, http_status, error_class, retrieved_at "
            "FROM provider_fetch_evidence WHERE source='sina' AND status='failed' "
            "ORDER BY retrieved_at DESC, fetch_id DESC"
        ).fetchall()
    latest: dict[str, tuple[object, ...]] = {}
    for row in evidence:
        latest.setdefault(str(row[0]), row)
    pending = [symbol for symbol in symbols if symbol not in completed]
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = ("symbol", "endpoint_kind", "http_status", "error_class", "retrieved_at")
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for symbol in pending:
            row = latest.get(_wire_symbol(symbol))
            writer.writerow(
                {
                    "symbol": symbol,
                    "endpoint_kind": "" if row is None else row[1],
                    "http_status": "" if row is None or row[2] is None else row[2],
                    "error_class": "" if row is None or row[3] is None else row[3],
                    "retrieved_at": "" if row is None else row[4],
                }
            )
    print(
        json.dumps(
            {"run_id": run_id, "pending_count": len(pending), "output": str(output)},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
