"""Small production command surface used by the Windows services."""

from __future__ import annotations

import json
from argparse import ArgumentParser
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from .config import Settings


@dataclass(frozen=True, slots=True)
class DoctorReport:
    status: str
    missing: tuple[str, ...]
    checks: dict[str, str]


def doctor(settings: Settings) -> DoctorReport:
    """Return redacted readiness facts; never include secret values."""
    missing = settings.missing_secrets
    checks = {
        "bind": f"{settings.host}:{settings.port}{settings.mcp_path}",
        "database_parent": str(settings.database_path.parent),
        "secrets": "missing" if missing else "configured",
    }
    return DoctorReport(
        status="configuration_required" if missing else "ready",
        missing=missing,
        checks=checks,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(prog="stock-mcp")
    parser.add_argument(
        "command",
        choices=(
            "doctor",
            "migrate",
            "backup",
            "restore",
            "backfill",
            "approve-strategy",
            "serve",
        ),
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args(argv)
    settings = Settings.load(root=args.root)

    if args.command == "doctor":
        report = doctor(settings)
        if args.json:
            print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        else:
            print(f"stock-mcp: {report.status}")
            if report.missing:
                print("missing: " + ", ".join(report.missing))
        return 0 if report.status == "ready" else 2

    if args.command == "migrate":
        from .storage import Database

        Database(settings.database_path).initialize()
        print("stock-mcp: database ready")
        return 0

    if args.command == "backup":
        from .backup import BackupManager
        from .storage import Database

        if args.destination is None:
            parser.error("backup requires --destination")
        prefix = "stock-mcp-"
        suffix = ".sqlite3"
        if not args.destination.name.startswith(prefix) or not args.destination.name.endswith(
            suffix
        ):
            parser.error("backup destination must be named stock-mcp-<label>.sqlite3")
        label = args.destination.name[len(prefix) : -len(suffix)]
        artifact = BackupManager(args.destination.parent, retention=14).create(
            Database(settings.database_path), label=label
        )
        print(f"stock-mcp: backup ready {artifact.database_path}")
        return 0

    if args.command == "restore":
        from .backup import BackupManager

        if args.source is None:
            parser.error("restore requires --source")
        BackupManager(args.source.parent, retention=14).restore(args.source, settings.database_path)
        print("stock-mcp: database restored")
        return 0

    if args.command == "backfill":
        from .backfill import run_production_backfill
        from .storage import Database

        if args.start is None or args.end is None:
            parser.error("backfill requires --start and --end")
        database = Database(settings.database_path)
        database.initialize()
        reported_failures = 0

        def report_incomplete(trade_date: date, error: Exception) -> None:
            nonlocal reported_failures
            reported_failures += 1
            if reported_failures > 3:
                if reported_failures == 4:
                    print("stock-mcp: further incomplete-day reasons omitted")
                return
            reason = str(error) if isinstance(error, ValueError) else type(error).__name__
            print(f"stock-mcp: backfill incomplete trade_date={trade_date} reason={reason}")

        result = run_production_backfill(
            settings, database, args.start, args.end, on_incomplete=report_incomplete
        )
        print(
            "stock-mcp: backfill "
            f"published={len(result.published_dates)} incomplete={len(result.incomplete_dates)}"
        )
        return 0 if not result.incomplete_dates else 2

    if args.command == "approve-strategy":
        from .storage import Database

        if not args.version:
            parser.error("approve-strategy requires --version")
        typed = input(f"Type strategy version {args.version} to approve activation: ").strip()
        if typed != args.version:
            print("stock-mcp: approval cancelled")
            return 2
        database = Database(settings.database_path)
        database.initialize()
        database.approve_strategy_version(args.version)
        print(f"stock-mcp: one-time approval recorded for {args.version}")
        return 0

    from .service import serve

    return serve(settings)


if __name__ == "__main__":
    raise SystemExit(main())
