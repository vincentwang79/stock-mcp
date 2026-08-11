"""Profile one Sina symbol by stage without writing the production database."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from stock_mcp.config import Settings
from stock_mcp.providers.sina import (
    ADAPTER_VERSION,
    CAPITAL_URL,
    HISTORY_URL,
    FetchEvidence,
    FetchResult,
    FixedIntervalRateLimiter,
    SinaHttpTransport,
    UrllibHttpClient,
    _evidence_record,
    _history_rows_for_range,
    _wire_symbol,
)
from stock_mcp.providers.sina_decode import decode_klc2, parse_jsonp_assignment
from stock_mcp.providers.sina_normalization import (
    normalize_sina_history,
    normalize_sina_share_capital,
)
from stock_mcp.storage import Database


class _TrackingDatabase(Database):
    """Track profiler-owned connections so Windows can remove the temp database."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(path)
        self.opened_connections: list[Any] = []

    def connect(self) -> Any:
        connection = super().connect()
        self.opened_connections.append(connection)
        return connection

    def close_all(self) -> None:
        for connection in reversed(self.opened_connections):
            connection.close()
        self.opened_connections.clear()


@dataclass
class _Timings:
    wall: dict[str, float]
    cpu: dict[str, float]

    def measure(self, name: str, operation: Callable[[], Any]) -> Any:
        wall_start = time.perf_counter()
        cpu_start = time.process_time()
        result = operation()
        self.wall[name] = time.perf_counter() - wall_start
        self.cpu[name] = time.process_time() - cpu_start
        return result

    def set(self, name: str, *, wall: float = 0.0, cpu: float = 0.0) -> None:
        self.wall[name] = wall
        self.cpu[name] = cpu


class _MeasuredRateLimiter:
    def __init__(self, inner: FixedIntervalRateLimiter) -> None:
        self._inner = inner
        self.wall_seconds = 0.0
        self.cpu_seconds = 0.0

    def acquire(self) -> None:
        wall_start = time.perf_counter()
        cpu_start = time.process_time()
        self._inner.acquire()
        self.wall_seconds += time.perf_counter() - wall_start
        self.cpu_seconds += time.process_time() - cpu_start


def _offline_result(payload: bytes, *, endpoint_kind: str, request_key: str) -> FetchResult:
    return FetchResult(
        payload,
        FetchEvidence(
            source="sina",
            endpoint_kind=endpoint_kind,
            request_key=request_key,
            http_date=None,
            retrieved_at=datetime.now(UTC),
            http_status=200,
            byte_length=len(payload),
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            adapter_version=ADAPTER_VERSION,
        ),
    )


def _sqlite_counts(database: Any) -> tuple[int, int]:
    connection = database.connect()
    try:
        daily_bar_rows = int(
            connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
        )
        share_capital_rows = int(
            connection.execute("SELECT COUNT(*) FROM share_capital_facts").fetchone()[0]
        )
        return daily_bar_rows, share_capital_rows
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--history-fixture", type=Path)
    parser.add_argument("--capital-fixture", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (args.history_fixture is None) != (args.capital_fixture is None):
        parser.error("history and capital fixtures must be provided together")
    if args.start > args.end:
        parser.error("start must not be after end")

    total_wall_start = time.perf_counter()
    total_cpu_start = time.process_time()
    timings = _Timings({}, {})
    wire = _wire_symbol(str(args.symbol))
    limiter: _MeasuredRateLimiter | None = None
    transport: SinaHttpTransport | None = None
    if args.history_fixture is None:
        settings = Settings.load(root=args.root)
        limiter = _MeasuredRateLimiter(
            FixedIntervalRateLimiter(settings.sina_history_rate_per_second)
        )
        transport = SinaHttpTransport(
            client=UrllibHttpClient(
                proxy_url=settings.https_proxy,
                custom_ca_file=(
                    None if settings.custom_ca_file is None else str(settings.custom_ca_file)
                ),
            ),
            clock=lambda: datetime.now(UTC),
            rate_limiter=limiter,
            sleeper=time.sleep,
            connect_timeout_seconds=settings.sina_connect_timeout_seconds,
            read_timeout_seconds=settings.sina_read_timeout_seconds,
            max_retries=settings.sina_max_retries,
        )

    def fetch(
        *, endpoint_kind: str, url: str, params: dict[str, object] | None = None
    ) -> FetchResult:
        if transport is None:
            fixture = args.history_fixture if endpoint_kind == "history" else args.capital_fixture
            assert fixture is not None
            return _offline_result(
                fixture.read_bytes(), endpoint_kind=endpoint_kind, request_key=wire
            )
        return transport.get(
            endpoint_kind=endpoint_kind,
            url=url,
            request_key=wire,
            params=params,
        )

    prior_wait_wall = 0.0
    prior_wait_cpu = 0.0
    history_result = timings.measure(
        "history_http",
        lambda: fetch(endpoint_kind="history", url=HISTORY_URL.format(symbol=wire)),
    )
    if limiter is not None:
        prior_wait_wall = limiter.wall_seconds
        prior_wait_cpu = limiter.cpu_seconds
        timings.wall["history_http"] -= prior_wait_wall
        timings.cpu["history_http"] -= prior_wait_cpu
    timings.set("history_rate_limit_wait", wall=prior_wait_wall, cpu=prior_wait_cpu)
    history_rows = timings.measure("history_decode", lambda: decode_klc2(history_result.payload))
    selected_rows = timings.measure(
        "history_window",
        lambda: _history_rows_for_range(history_rows, start=args.start, end=args.end),
    )
    bars = timings.measure(
        "history_normalize",
        lambda: normalize_sina_history(
            selected_rows,
            symbol=args.symbol,
            source_timestamp=history_result.evidence.retrieved_at,
        ),
    )

    capital_result = timings.measure(
        "capital_http",
        lambda: fetch(
            endpoint_kind="share_capital",
            url=CAPITAL_URL.format(symbol=wire),
            params={"_": 20, "symbol": wire},
        ),
    )
    capital_wait_wall = 0.0 if limiter is None else limiter.wall_seconds - prior_wait_wall
    capital_wait_cpu = 0.0 if limiter is None else limiter.cpu_seconds - prior_wait_cpu
    timings.wall["capital_http"] -= capital_wait_wall
    timings.cpu["capital_http"] -= capital_wait_cpu
    timings.set(
        "capital_rate_limit_wait", wall=capital_wait_wall, cpu=capital_wait_cpu
    )
    capital_rows = timings.measure(
        "capital_decode",
        lambda: parse_jsonp_assignment(
            capital_result.payload, assignment=f"KKE_ShareAmount_{wire}"
        ),
    )
    capital_facts = timings.measure(
        "capital_normalize",
        lambda: normalize_sina_share_capital(
            capital_rows,
            symbol=args.symbol,
            source_timestamp=capital_result.evidence.retrieved_at,
            payload_sha256=capital_result.evidence.payload_sha256,
        ),
    )

    with tempfile.TemporaryDirectory(prefix="stock-mcp-sina-profile-") as directory:
        database = _TrackingDatabase(Path(directory) / "profile.sqlite3")
        try:
            timings.measure("sqlite_initialize", database.initialize)
            checkpoint = {
                "run_id": "sina-stage-profile",
                "symbol": args.symbol,
                "status": "completed",
                "history_payload_sha256": history_result.evidence.payload_sha256,
                "capital_payload_sha256": capital_result.evidence.payload_sha256,
                "first_date": bars[0].trade_date if bars else None,
                "last_date": bars[-1].trade_date if bars else None,
                "session_count": len(bars),
            }
            timings.measure(
                "sqlite_atomic_write",
                lambda: database.save_sina_backfill_symbol(
                    bars=bars,
                    capital_facts=capital_facts,
                    fetch_evidence=(
                        _evidence_record(history_result.evidence),
                        _evidence_record(capital_result.evidence),
                    ),
                    checkpoint=checkpoint,
                ),
            )
            daily_bar_rows, share_capital_rows = _sqlite_counts(database)
        finally:
            database.close_all()

    timings.set(
        "total",
        wall=time.perf_counter() - total_wall_start,
        cpu=time.process_time() - total_cpu_start,
    )
    report = {
        "status": "ok",
        "symbol": args.symbol,
        "adapter_version": ADAPTER_VERSION,
        "history": {
            "payload_bytes": len(history_result.payload),
            "payload_sha256": history_result.evidence.payload_sha256,
            "decoded_rows": len(history_rows),
            "selected_rows": len(selected_rows),
            "normalized_bars": len(bars),
        },
        "share_capital": {
            "payload_bytes": len(capital_result.payload),
            "payload_sha256": capital_result.evidence.payload_sha256,
            "decoded_rows": len(capital_rows),
            "normalized_facts": len(capital_facts),
        },
        "sqlite": {
            "database": "temporary",
            "daily_bar_rows": daily_bar_rows,
            "share_capital_rows": share_capital_rows,
        },
        "wall_seconds": {name: round(value, 6) for name, value in timings.wall.items()},
        "cpu_seconds": {name: round(value, 6) for name, value in timings.cpu.items()},
    }
    encoded = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
