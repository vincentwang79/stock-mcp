"""Offline Windows-friendly Sina KLC decoder preflight."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from stock_mcp.providers.sina_decode import decode_klc2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args()
    fixture = args.fixture.resolve()
    if not fixture.is_file():
        parser.error(f"fixture does not exist: {fixture}")

    rows = decode_klc2(fixture.read_bytes())
    report = {"status": "ok", "rows": len(rows), "first": rows[0]}
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
