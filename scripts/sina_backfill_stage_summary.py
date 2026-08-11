"""Summarize redacted Sina backfill stage events from a durable run log."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PREFIX = "stock-mcp: sina-backfill-stage "


def _events(path: Path) -> tuple[dict[str, Any], ...]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            marker = line.find(PREFIX)
            if marker < 0:
                continue
            encoded = line[marker + len(PREFIX) :].strip()
            try:
                event = json.loads(encoded)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return tuple(events)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    if not args.log.is_file():
        parser.error(f"log does not exist: {args.log}")

    events = _events(args.log)
    completed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    failures: Counter[tuple[str, str]] = Counter()
    for event in events:
        stage = str(event.get("stage", "unknown"))
        status = str(event.get("event", "unknown"))
        if status == "complete" and "elapsed_seconds" in event:
            completed[stage].append(event)
        elif status == "failed":
            failures[(stage, str(event.get("error_class", "unknown")))] += 1

    stages: list[dict[str, object]] = []
    for stage, rows in sorted(completed.items()):
        slowest = max(rows, key=lambda row: float(row["elapsed_seconds"]))
        durations = [float(row["elapsed_seconds"]) for row in rows]
        stages.append(
            {
                "stage": stage,
                "completed": len(rows),
                "average_seconds": round(sum(durations) / len(durations), 6),
                "maximum_seconds": round(max(durations), 6),
                "maximum_symbol": str(slowest.get("symbol", "unknown")),
            }
        )

    report = {
        "event_count": len(events),
        "last_event": None if not events else events[-1],
        "stages": stages,
        "failures": [
            {"stage": stage, "error_class": error_class, "count": count}
            for (stage, error_class), count in sorted(failures.items())
        ],
    }
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if events else 2


if __name__ == "__main__":
    raise SystemExit(main())
