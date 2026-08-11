"""Read-only Windows-friendly database preflight without ``python -c`` quoting."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    database = args.database.resolve()
    if not database.is_file():
        parser.error(f"database does not exist: {database}")
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
        report = {
            "schema": int(connection.execute("PRAGMA user_version").fetchone()[0]),
            "integrity": str(connection.execute("PRAGMA integrity_check").fetchone()[0]),
            "tushare_days": int(
                connection.execute(
                    "SELECT COUNT(DISTINCT trade_date) FROM daily_bars WHERE source = ?",
                    ("tushare",),
                ).fetchone()[0]
            ),
            "tushare_rows": int(
                connection.execute(
                    "SELECT COUNT(*) FROM daily_bars WHERE source = ?", ("tushare",)
                ).fetchone()[0]
            ),
        }
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["integrity"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
