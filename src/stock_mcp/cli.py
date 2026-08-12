"""Small production command surface used by the Windows services."""

from __future__ import annotations

import json
import sqlite3
from argparse import ArgumentParser
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings


@dataclass(frozen=True, slots=True)
class DoctorReport:
    status: str
    missing: tuple[str, ...]
    checks: dict[str, str]


def _print_sina_backfill_progress(event: dict[str, object]) -> None:
    encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    print(f"stock-mcp: sina-backfill-stage {encoded}", flush=True)


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
            "inspect-database",
            "migrate",
            "backup",
            "restore",
            "backfill",
            "build-v3-facts",
            "prepare-sina-backfill-manifest",
            "backfill-sina",
            "verify-sina-backfill",
            "run-sina-shadow",
            "report-sina-qualification",
            "attest-sina-review",
            "approve-provider-source",
            "prepare-v4-study-manifest",
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
    parser.add_argument("--industry-json", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--capital-exclusions", type=Path)
    parser.add_argument("--sina-backfill-manifest", type=Path)
    parser.add_argument("--trade-date", type=date.fromisoformat)
    parser.add_argument("--through", type=date.fromisoformat)
    parser.add_argument("--dataset-hash")
    parser.add_argument("--qualification-id")
    parser.add_argument("--capability", action="append", default=[])
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

    if args.command == "inspect-database":
        if not settings.database_path.is_file():
            print("stock-mcp: database does not exist")
            return 2
        with sqlite3.connect(
            f"file:{settings.database_path.as_posix()}?mode=ro", uri=True
        ) as connection:
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
        token = settings.tushare_token or ""
        token_fingerprint = sha256(token.encode("utf-8")).hexdigest()[:12]
        print(f"stock-mcp: Tushare credential length={len(token)} sha256={token_fingerprint}")

        def report_incomplete(trade_date: date, error: Exception) -> None:
            nonlocal reported_failures
            reported_failures += 1
            if reported_failures > 3:
                if reported_failures == 4:
                    print("stock-mcp: further incomplete-day reasons omitted")
                return
            reason = str(error) if isinstance(error, ValueError) else type(error).__name__
            print(f"stock-mcp: backfill incomplete trade_date={trade_date} reason={reason}")

        def report_tushare_probe(trade_date: date, row_count: int) -> None:
            print(f"stock-mcp: Tushare latest-day probe trade_date={trade_date} rows={row_count}")

        result = run_production_backfill(
            settings,
            database,
            args.start,
            args.end,
            on_incomplete=report_incomplete,
            on_tushare_probe=report_tushare_probe,
        )
        print(
            "stock-mcp: backfill "
            f"published={len(result.published_dates)} incomplete={len(result.incomplete_dates)}"
        )
        return 0 if not result.incomplete_dates else 2

    if args.command == "build-v3-facts":
        from .backfill import build_v3_facts
        from .storage import Database

        if args.start is None or args.end is None:
            parser.error("build-v3-facts requires --start and --end")
        industry_json = args.industry_json or (
            settings.root / "current" / "a_share_mainboard_code_name.json"
        )
        database = Database(settings.database_path)
        database.initialize()
        report = build_v3_facts(
            database=database,
            industry_json_path=industry_json,
            source="tushare",
            start=args.start,
            end=args.end,
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report.get("ready") is True else 2

    if args.command == "prepare-sina-backfill-manifest":
        from .storage import Database

        if args.start is None or args.end is None or args.manifest is None:
            parser.error("prepare-sina-backfill-manifest requires --start, --end and --manifest")
        database = Database(settings.database_path)
        database.initialize()
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT symbol FROM snapshot_securities "
                "WHERE trade_date BETWEEN ? AND ? AND board = 'MAIN' ORDER BY symbol",
                (args.start.isoformat(), args.end.isoformat()),
            ).fetchall()
        symbols = [str(row[0]) for row in rows]
        payload = {
            "schema": "sina-backfill-manifest-v1",
            "run_id": f"sina-backfill-{args.start.isoformat()}-{args.end.isoformat()}",
            "symbols": symbols,
            "start": args.start.isoformat(),
            "end": args.end.isoformat(),
            "adapter_version": "sina-adapter-v1",
            "rate_limit_per_second": 0.5,
            "universe_source": "recorded_snapshot_union",
            "survivorship_limit": (
                "historical snapshot union may omit securities absent from all recorded snapshots"
            ),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload["manifest_hash"] = sha256(encoded.encode()).hexdigest()
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "manifest": str(args.manifest),
                    "symbols": len(symbols),
                    "manifest_hash": payload["manifest_hash"],
                },
                ensure_ascii=False,
            )
        )
        return 0 if symbols else 2

    if args.command in {"backfill-sina", "verify-sina-backfill"}:
        from .backfill import SinaBackfillService
        from .providers.sina import (
            FixedIntervalRateLimiter,
            SinaHttpTransport,
            SinaProvider,
            UrllibHttpClient,
        )
        from .storage import Database

        if args.manifest is None:
            parser.error(f"{args.command} requires --manifest")
        payload = json.loads(args.manifest.read_text(encoding="utf-8"))
        recorded_manifest_hash = str(payload.get("manifest_hash", ""))
        canonical_manifest = dict(payload)
        canonical_manifest.pop("manifest_hash", None)
        computed_manifest_hash = sha256(
            json.dumps(
                canonical_manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if recorded_manifest_hash != computed_manifest_hash:
            print("stock-mcp: Sina backfill manifest hash mismatch")
            return 2
        manifest = {
            **payload,
            "symbols": tuple(payload["symbols"]),
            "start": date.fromisoformat(payload["start"]),
            "end": date.fromisoformat(payload["end"]),
        }
        database = Database(settings.database_path)
        database.initialize()
        if args.command == "verify-sina-backfill":
            missing: list[str] = []
            for symbol in manifest["symbols"]:
                checkpoint = database.load_sina_backfill_checkpoint(
                    run_id=str(manifest["run_id"]), symbol=str(symbol)
                )
                if checkpoint is None or checkpoint.get("status") != "completed":
                    missing.append(str(symbol))
                    continue
                with database.connect() as connection:
                    bar_count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM daily_bars WHERE symbol=? AND source='sina' "
                            "AND trade_date BETWEEN ? AND ?",
                            (str(symbol), payload["start"], payload["end"]),
                        ).fetchone()[0]
                    )
                    capital_hashes = {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT payload_sha256 FROM share_capital_facts "
                            "WHERE symbol=? AND source='sina'",
                            (str(symbol),),
                        )
                    }
                if (
                    bar_count != int(checkpoint.get("session_count", -1))
                    or checkpoint.get("capital_payload_sha256") not in capital_hashes
                ):
                    missing.append(str(symbol))
            print(
                json.dumps(
                    {
                        "status": "complete" if not missing else "incomplete",
                        "missing_symbols": missing[:100],
                        "missing_count": len(missing),
                    },
                    ensure_ascii=False,
                )
            )
            return 0 if not missing else 2
        import time

        transport = SinaHttpTransport(
            client=UrllibHttpClient(
                proxy_url=settings.https_proxy,
                custom_ca_file=(
                    None if settings.custom_ca_file is None else str(settings.custom_ca_file)
                ),
            ),
            clock=lambda: datetime.now(UTC),
            rate_limiter=FixedIntervalRateLimiter(settings.sina_history_rate_per_second),
            sleeper=time.sleep,
            connect_timeout_seconds=settings.sina_connect_timeout_seconds,
            read_timeout_seconds=settings.sina_read_timeout_seconds,
            max_retries=settings.sina_max_retries,
        )
        result = SinaBackfillService(
            database=database,
            provider=SinaProvider(transport=transport, clock=lambda: datetime.now(UTC)),
            manifest=manifest,
            progress=_print_sina_backfill_progress,
        ).backfill()
        print(json.dumps(asdict(result), ensure_ascii=False))
        return 0 if not result.failed_symbols else 2

    if args.command == "run-sina-shadow":
        import time

        from .production import SinaShadowTask
        from .providers.sina import (
            FixedIntervalRateLimiter,
            SinaHttpTransport,
            SinaSpotProvider,
            UrllibHttpClient,
        )
        from .storage import Database

        if args.trade_date is None:
            parser.error("run-sina-shadow requires --trade-date")
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        if args.trade_date != now.date() or (now.hour, now.minute) < (16, 35):
            print("stock-mcp: Sina spot shadow only accepts the current closed trading day")
            return 2
        if not settings.sina_shadow_enabled:
            print("stock-mcp: Sina shadow is disabled by configuration")
            return 2
        transport = SinaHttpTransport(
            client=UrllibHttpClient(
                proxy_url=settings.https_proxy,
                custom_ca_file=(
                    None if settings.custom_ca_file is None else str(settings.custom_ca_file)
                ),
            ),
            clock=lambda: datetime.now(UTC),
            rate_limiter=FixedIntervalRateLimiter(settings.sina_spot_rate_per_second),
            sleeper=time.sleep,
            connect_timeout_seconds=settings.sina_connect_timeout_seconds,
            read_timeout_seconds=settings.sina_read_timeout_seconds,
            max_retries=settings.sina_max_retries,
        )
        database = Database(settings.database_path)
        database.initialize()
        run = SinaShadowTask(
            database,
            SinaSpotProvider(transport=transport, clock=lambda: datetime.now(UTC)),
        ).run(args.trade_date)
        print(json.dumps(run, ensure_ascii=False, default=str))
        return 0 if run["status"] == "success" else 2

    if args.command == "report-sina-qualification":
        from .provider_qualification import evaluate_provider_qualification
        from .storage import Database

        database = Database(settings.database_path)
        database.initialize()
        runs = database.list_provider_shadow_runs("sina", through_date=args.through, limit=20)
        through_date = (
            args.through
            if args.through is not None
            else (
                None
                if not runs
                else date.fromisoformat(max(str(run["trade_date"]) for run in runs))
            )
        )
        expected_window = (
            ()
            if through_date is None
            else database.load_expected_trading_days(
                through_date - timedelta(days=60), through_date, source="tushare"
            )[-20:]
        )
        preliminary = evaluate_provider_qualification(
            runs,
            adapter_version="sina-adapter-v1",
            configuration_hash=sha256(
                f"{settings.sina_history_rate_per_second}|{settings.sina_spot_rate_per_second}".encode()
            ).hexdigest(),
            windows_validation_complete=False,
            terms_attested=False,
            expected_trading_days=expected_window,
        )
        if not preliminary["through_date"]:
            print(json.dumps(preliminary, ensure_ascii=False))
            return 2
        recorded_at = datetime.now(UTC)
        database.save_provider_qualification({**preliminary, "recorded_at": recorded_at})
        attestation = database.get_provider_review_attestation(str(preliminary["qualification_id"]))
        report = evaluate_provider_qualification(
            runs,
            adapter_version="sina-adapter-v1",
            configuration_hash=str(preliminary["configuration_hash"]),
            windows_validation_complete=attestation is not None,
            terms_attested=bool(attestation and attestation["terms_confirmed"]),
            expected_trading_days=expected_window,
        )
        database.save_provider_qualification({**report, "recorded_at": recorded_at})
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["status"] == "qualified_for_manual_approval" else 2

    if args.command == "attest-sina-review":
        from .storage import Database

        if args.through is None or args.dataset_hash is None:
            parser.error("attest-sina-review requires --through and --dataset-hash")
        typed = input(
            "Type sina to confirm the difference report, non-commercial terms, and Windows "
            "decode/resume/shadow validation: "
        ).strip()
        if typed != "sina":
            return 2
        database = Database(settings.database_path)
        database.initialize()
        print(
            json.dumps(
                database.record_provider_attestation(
                    source="sina", through_date=args.through, dataset_hash=args.dataset_hash
                ),
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "approve-provider-source":
        from .storage import Database

        if not args.qualification_id:
            parser.error("approve-provider-source requires --qualification-id")
        typed = input(f"Type qualification id {args.qualification_id} to approve: ").strip()
        if typed != args.qualification_id:
            return 2
        database = Database(settings.database_path)
        database.initialize()
        capabilities = tuple(args.capability or ("enrichment", "backup_price"))
        print(
            json.dumps(
                database.approve_provider_source_capabilities(
                    qualification_id=args.qualification_id, capabilities=capabilities
                ),
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "prepare-v4-study-manifest":
        from .replay import (
            V4_CAPITAL_EXCLUSION_REASON,
            build_v4_replay_manifest,
            load_v4_capital_exclusions,
            load_v4_sina_backfill_universe,
            validate_v4_manifest_universe,
        )
        from .storage import Database

        database = Database(settings.database_path)
        database.initialize()
        sessions = database.load_expected_trading_days(
            date(2023, 8, 8), date(2026, 8, 7), source="tushare"
        )
        if len(sessions) != 727:
            print("stock-mcp: v4 manifest requires the fixed 727-session Tushare calendar")
            return 2
        if args.sina_backfill_manifest is None:
            print(
                "stock-mcp: v4 manifest requires --sina-backfill-manifest "
                "to freeze the collected security universe"
            )
            return 2
        try:
            universe_symbols, universe_source_manifest_hash = (
                load_v4_sina_backfill_universe(
                    args.sina_backfill_manifest, sessions[0], sessions[-1]
                )
            )
        except ValueError as error:
            print(f"stock-mcp: {error}")
            return 2
        with database.connect() as connection:
            missing_capital_symbols = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT b.symbol FROM daily_bars b "
                    "WHERE b.source='tushare' AND b.trade_date BETWEEN ? AND ? "
                    "AND NOT EXISTS (SELECT 1 FROM share_capital_facts c "
                    "WHERE c.source='sina' AND c.symbol=b.symbol "
                    "AND c.effective_date <= b.trade_date) ORDER BY b.symbol",
                    (sessions[60].isoformat(), sessions[-26].isoformat()),
                )
                if str(row[0]) in set(universe_symbols)
            )
            if missing_capital_symbols and args.capital_exclusions is None:
                print(
                    "stock-mcp: missing Sina share capital requires an explicit "
                    "--capital-exclusions governance file"
                )
                return 2
            try:
                excluded_symbols = (
                    load_v4_capital_exclusions(
                        args.capital_exclusions, missing_capital_symbols
                    )
                    if args.capital_exclusions is not None
                    else ()
                )
            except ValueError as error:
                print(f"stock-mcp: {error}")
                return 2
            excluded_set = set(excluded_symbols)
            included_set = set(universe_symbols) - excluded_set

            def query_hash(
                query: str, values: tuple[object, ...]
            ) -> tuple[str, int]:
                digest = sha256()
                count = 0
                cursor = connection.execute(query, values)
                while rows := cursor.fetchmany(1_000):
                    for row in rows:
                        if str(row[0]) not in included_set:
                            continue
                        digest.update(
                            json.dumps(tuple(row), separators=(",", ":"), default=str).encode()
                        )
                        digest.update(b"\n")
                        count += 1
                return digest.hexdigest(), count

            prices, price_rows = query_hash(
                "SELECT symbol, trade_date, open_1e4, high_1e4, low_1e4, close_1e4, "
                "pre_close_1e4, volume_shares, amount_fen, source_timestamp FROM daily_bars "
                "WHERE source='tushare' AND trade_date BETWEEN ? AND ? "
                "ORDER BY trade_date, symbol",
                (sessions[0].isoformat(), sessions[-1].isoformat()),
            )
            statuses, status_rows = query_hash(
                "SELECT symbol, trade_date, tradestatus, is_st, source_timestamp, batch_sha256 "
                "FROM daily_security_status WHERE source='baostock' "
                "AND trade_date BETWEEN ? AND ? ORDER BY trade_date, symbol",
                (sessions[0].isoformat(), sessions[-1].isoformat()),
            )
            capital, capital_rows = query_hash(
                "SELECT symbol, effective_date, outstanding_shares, source_timestamp, "
                "payload_sha256 FROM share_capital_facts WHERE source='sina' "
                "AND effective_date <= ? ORDER BY symbol, effective_date",
                (sessions[-1].isoformat(),),
            )
            price_days, _price_symbols = connection.execute(
                "SELECT COUNT(DISTINCT trade_date), COUNT(DISTINCT symbol) FROM daily_bars "
                "WHERE source='tushare' AND trade_date BETWEEN ? AND ?",
                (sessions[0].isoformat(), sessions[-1].isoformat()),
            ).fetchone()
            status_days = connection.execute(
                "SELECT COUNT(DISTINCT trade_date) FROM daily_security_status "
                "WHERE source='baostock' AND trade_date BETWEEN ? AND ?",
                (sessions[0].isoformat(), sessions[-1].isoformat()),
            ).fetchone()[0]
            snapshot_days = connection.execute(
                "SELECT COUNT(DISTINCT trade_date) FROM snapshot_securities "
                "WHERE source='tushare' AND trade_date BETWEEN ? AND ?",
                (sessions[0].isoformat(), sessions[-1].isoformat()),
            ).fetchone()[0]
            missing_price_rows = connection.execute(
                "SELECT COUNT(*) FROM snapshot_securities s "
                "WHERE s.source='tushare' AND s.trade_date BETWEEN ? AND ? "
                "AND NOT EXISTS (SELECT 1 FROM daily_bars b WHERE b.source='tushare' "
                "AND b.trade_date=s.trade_date AND b.symbol=s.symbol)",
                (sessions[0].isoformat(), sessions[-1].isoformat()),
            ).fetchone()[0]
            orphan_price_rows = connection.execute(
                "SELECT COUNT(*) FROM daily_bars b "
                "WHERE b.source='tushare' AND b.trade_date BETWEEN ? AND ? "
                "AND NOT EXISTS (SELECT 1 FROM snapshot_securities s WHERE s.source='tushare' "
                "AND s.trade_date=b.trade_date AND s.symbol=b.symbol)",
                (sessions[0].isoformat(), sessions[-1].isoformat()),
            ).fetchone()[0]
            missing_status_rows = connection.execute(
                "SELECT COUNT(*) FROM daily_bars b WHERE b.source='tushare' "
                "AND b.trade_date BETWEEN ? AND ? AND NOT EXISTS ("
                "SELECT 1 FROM daily_security_status s WHERE s.source='baostock' "
                "AND s.symbol=b.symbol AND s.trade_date=b.trade_date)",
                (sessions[0].isoformat(), sessions[-1].isoformat()),
            ).fetchone()[0]
        try:
            validate_v4_manifest_universe(
                expected_session_count=len(sessions),
                price_day_count=int(price_days),
                snapshot_day_count=int(snapshot_days),
                missing_price_rows=int(missing_price_rows),
                orphan_price_rows=int(orphan_price_rows),
            )
        except ValueError as error:
            print(f"stock-mcp: {error}")
            return 2
        if (
            not price_rows
            or not status_rows
            or not capital_rows
            or int(price_days) != 727
            or int(status_days) != 727
            or int(missing_status_rows) != 0
        ):
            print(
                "stock-mcp: v4 manifest requires bounded Tushare prices, "
                "BaoStock statuses, and Sina share-capital facts"
            )
            return 2
        manifest = build_v4_replay_manifest(
            source="tushare",
            sessions=sessions,
            bar_start=sessions[0],
            signal_start=sessions[60],
            signal_end=sessions[-26],
            outcome_through=sessions[-1],
            prices_hash=prices,
            statuses_hash=statuses,
            share_capital_hash=capital,
            industry_mapping_hash="829fb6481d3269a59a2f679b09c2d2d93ada2ffd0db54931f2ec61b646ac1c1a",
            universe_symbols=universe_symbols,
            excluded_symbols=excluded_symbols,
            exclusion_reason=(
                V4_CAPITAL_EXCLUSION_REASON if excluded_symbols else None
            ),
            universe_source_manifest_hash=universe_source_manifest_hash,
        )
        database.save_v4_dataset_manifest(manifest)
        if args.manifest is not None:
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            args.manifest.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(json.dumps(manifest, ensure_ascii=False))
        return 0

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
