"""SQLite persistence for immutable market research records.

All prices and amounts are persisted as the integer fixed-point values defined in
``stock_mcp.domain``.  The database deliberately stores only serialised domain
values: provider client code and strategy calculations stay outside this module.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from stock_mcp.domain import (
    Candidate,
    DailyBar,
    DailyReview,
    Evidence,
    MarketRegime,
    MarketSnapshot,
    Security,
    SetupType,
    StrategyVersion,
)

SCHEMA_VERSION = 13


class IdempotencyKeyReuseError(ValueError):
    """Raised when an idempotency key is reused for a different request."""


class Database:
    """A small SQLite repository with a fresh, correctly configured connection.

    A connection is intentionally short lived for each operation.  That keeps
    service restarts and online backup straightforward and makes the PRAGMA
    guarantees apply to every caller, rather than only the initial migration
    connection.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def schema_version(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def initialize(self) -> None:
        if self.path.exists():
            with sqlite3.connect(self.path) as existing:
                version = int(existing.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise ValueError(
                    f"database schema version {version} is newer than supported "
                    f"version {SCHEMA_VERSION}"
                )
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_bars (
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    open_1e4 INTEGER NOT NULL,
                    high_1e4 INTEGER NOT NULL,
                    low_1e4 INTEGER NOT NULL,
                    close_1e4 INTEGER NOT NULL,
                    pre_close_1e4 INTEGER NOT NULL,
                    volume_shares INTEGER NOT NULL,
                    amount_fen INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    source_timestamp TEXT NOT NULL,
                    PRIMARY KEY (symbol, trade_date, source)
                );

                CREATE TABLE IF NOT EXISTS market_snapshots (
                    trade_date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_timestamp TEXT NOT NULL,
                    advance_ratio_bps INTEGER NOT NULL,
                    above_ma20_ratio_bps INTEGER NOT NULL,
                    PRIMARY KEY (trade_date, source)
                );

                CREATE TABLE IF NOT EXISTS snapshot_securities (
                    trade_date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    board TEXT NOT NULL,
                    list_date TEXT NOT NULL,
                    industry TEXT NOT NULL,
                    is_st INTEGER NOT NULL CHECK (is_st IN (0, 1)),
                    PRIMARY KEY (trade_date, source, symbol),
                    FOREIGN KEY (trade_date, source)
                        REFERENCES market_snapshots(trade_date, source)
                );

                CREATE TABLE IF NOT EXISTS strategy_versions (
                    version TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    parameters_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_reviews (
                    trade_date TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_timestamp TEXT NOT NULL,
                    market_regime TEXT NOT NULL,
                    PRIMARY KEY (trade_date, strategy_version),
                    FOREIGN KEY (strategy_version) REFERENCES strategy_versions(version)
                );

                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    trade_date TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    score INTEGER NOT NULL,
                    setup_type TEXT NOT NULL,
                    confirmation_condition TEXT NOT NULL,
                    invalidation_condition TEXT NOT NULL,
                    FOREIGN KEY (trade_date, strategy_version)
                        REFERENCES daily_reviews(trade_date, strategy_version)
                );

                CREATE TABLE IF NOT EXISTS candidate_evidence (
                    candidate_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    metric TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    threshold_json TEXT NOT NULL,
                    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
                    score_contribution INTEGER NOT NULL,
                    PRIMARY KEY (candidate_id, ordinal),
                    FOREIGN KEY (candidate_id) REFERENCES candidates(candidate_id)
                );

                CREATE TABLE IF NOT EXISTS watchlist_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    detail TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS candidate_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE,
                    status TEXT,
                    event_date TEXT,
                    price_1e4 INTEGER,
                    reason TEXT
                );

                CREATE TABLE IF NOT EXISTS active_strategy (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version TEXT NOT NULL,
                    FOREIGN KEY (version) REFERENCES strategy_versions(version)
                );

                CREATE TABLE IF NOT EXISTS strategy_approvals (
                    version TEXT PRIMARY KEY,
                    parameters_hash TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    FOREIGN KEY (version) REFERENCES strategy_versions(version)
                );

                CREATE TABLE IF NOT EXISTS replay_attestations (
                    version TEXT PRIMARY KEY,
                    parameters_hash TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY (version) REFERENCES strategy_versions(version)
                );

                CREATE TABLE IF NOT EXISTS strategy_replay_jobs (
                    job_id TEXT PRIMARY KEY,
                    strategy_version TEXT NOT NULL,
                    parameters_hash TEXT NOT NULL,
                    source TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    expected_sessions_json TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('queued', 'running', 'completed', 'failed')),
                    dataset_hash TEXT,
                    result_hash TEXT,
                    summary_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    FOREIGN KEY (strategy_version) REFERENCES strategy_versions(version)
                );

                CREATE TABLE IF NOT EXISTS strategy_replay_days (
                    job_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    output_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    PRIMARY KEY (job_id, trade_date),
                    FOREIGN KEY (job_id) REFERENCES strategy_replay_jobs(job_id)
                );

                CREATE TABLE IF NOT EXISTS strategy_replay_attestations (
                    strategy_version TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE,
                    parameters_hash TEXT NOT NULL,
                    dataset_hash TEXT NOT NULL,
                    result_hash TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    session_count INTEGER NOT NULL,
                    certified_at TEXT NOT NULL,
                    FOREIGN KEY (strategy_version) REFERENCES strategy_versions(version),
                    FOREIGN KEY (job_id) REFERENCES strategy_replay_jobs(job_id)
                );

                CREATE TABLE IF NOT EXISTS pipeline_runs (
                    trade_date TEXT NOT NULL,
                    pipeline_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    strategy_version TEXT,
                    error TEXT,
                    PRIMARY KEY (trade_date, pipeline_version),
                    FOREIGN KEY (strategy_version) REFERENCES strategy_versions(version)
                );

                CREATE TABLE IF NOT EXISTS schedule_outcomes (
                    trade_date TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    next_at TEXT,
                    pipeline_version TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS watchlists (
                    name TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS watchlist_items (
                    watchlist_name TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    PRIMARY KEY (watchlist_name, symbol),
                    UNIQUE (watchlist_name, ordinal),
                    FOREIGN KEY (watchlist_name) REFERENCES watchlists(name) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS review_notes (
                    note_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    note TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE
                );

                CREATE TABLE IF NOT EXISTS idempotent_writes (
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL,
                    PRIMARY KEY (operation, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS v4_evidence_revision (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    revision INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO v4_evidence_revision(singleton, revision) VALUES(1, 0);
                """
            )
            self._migrate(connection)
            self._ensure_v4_evidence_revision_triggers(connection)

    def save_daily_bars(self, bars: Iterable[DailyBar]) -> None:
        records = tuple(bars)
        if not records:
            return
        with self.connect() as connection:
            self._save_daily_bars(connection, records)

    def load_daily_bars(self, trade_date: date, source: str) -> tuple[DailyBar, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, trade_date, open_1e4, high_1e4, low_1e4, close_1e4,
                       pre_close_1e4, volume_shares, amount_fen, source, source_timestamp
                FROM daily_bars
                WHERE trade_date = ? AND source = ?
                ORDER BY symbol
                """,
                (trade_date.isoformat(), source),
            ).fetchall()
        return tuple(self._daily_bar_from_row(row) for row in rows)

    def load_symbol_history(
        self,
        symbol: str,
        *,
        end_date: date,
        source: str,
        limit: int,
    ) -> tuple[DailyBar, ...]:
        if limit < 1:
            raise ValueError("history limit must be positive")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, trade_date, open_1e4, high_1e4, low_1e4, close_1e4,
                       pre_close_1e4, volume_shares, amount_fen, source, source_timestamp
                FROM daily_bars
                WHERE symbol = ? AND source = ? AND trade_date <= ?
                ORDER BY trade_date DESC LIMIT ?
                """,
                (symbol, source, end_date.isoformat(), limit),
            ).fetchall()
        return tuple(self._daily_bar_from_row(row) for row in reversed(rows))

    def save_market_snapshot(self, snapshot: MarketSnapshot) -> None:
        with self.connect() as connection:
            self._save_market_snapshot(connection, snapshot)

    def save_provider_fetch_evidence(self, evidence: dict[str, object]) -> None:
        with self._idempotent_write_connection() as connection:
            self._save_provider_fetch_evidence(connection, evidence)

    def _save_provider_fetch_evidence(
        self, connection: sqlite3.Connection, evidence: dict[str, object]
    ) -> None:
        required = (
            "fetch_id",
            "source",
            "endpoint_kind",
            "request_key",
            "retrieved_at",
            "byte_length",
            "payload_sha256",
            "adapter_version",
            "status",
        )
        if any(evidence.get(name) in (None, "") for name in required):
            raise ValueError("provider fetch evidence is incomplete")
        values = (
            str(evidence["source"]),
            str(evidence["endpoint_kind"]),
            str(evidence["request_key"]),
            self._iso(evidence.get("trade_date")),
            None if evidence.get("http_date") is None else str(evidence["http_date"]),
            self._iso(evidence["retrieved_at"]),
            None if evidence.get("http_status") is None else int(evidence["http_status"]),
            int(evidence["byte_length"]),
            str(evidence["payload_sha256"]),
            str(evidence["adapter_version"]),
            str(evidence["status"]),
            None if evidence.get("error_class") is None else str(evidence["error_class"]),
        )
        self._immutable_insert(
            connection,
            "provider_fetch_evidence",
            "fetch_id",
            str(evidence["fetch_id"]),
            (
                "source",
                "endpoint_kind",
                "request_key",
                "trade_date",
                "http_date",
                "retrieved_at",
                "http_status",
                "byte_length",
                "payload_sha256",
                "adapter_version",
                "status",
                "error_class",
            ),
            values,
        )

    def save_share_capital_facts(self, facts: Iterable[dict[str, object] | object]) -> None:
        with self._idempotent_write_connection() as connection:
            self._save_share_capital_facts(connection, facts)

    def _save_share_capital_facts(
        self,
        connection: sqlite3.Connection,
        facts: Iterable[dict[str, object] | object],
    ) -> None:
        for fact in facts:
            item = self._mapping(fact)
            key = (str(item["symbol"]), self._iso(item["effective_date"]), str(item["source"]))
            values = (
                int(item["outstanding_shares"]),
                self._iso(item["source_timestamp"]),
                str(item["payload_sha256"]),
            )
            self._immutable_composite_insert(
                connection,
                "share_capital_facts",
                ("symbol", "effective_date", "source"),
                key,
                ("outstanding_shares", "source_timestamp", "payload_sha256"),
                values,
            )

    def save_sina_backfill_symbol(
        self,
        *,
        bars: Iterable[DailyBar],
        capital_facts: Iterable[dict[str, object] | object],
        fetch_evidence: Iterable[dict[str, object]],
        checkpoint: dict[str, object],
    ) -> None:
        """Atomically publish one Sina symbol's facts, evidence and checkpoint."""

        recorded_bars = tuple(bars)
        recorded_capital = tuple(capital_facts)
        symbol = str(checkpoint["symbol"])
        if not recorded_bars or any(
            bar.source != "sina" or bar.symbol != symbol for bar in recorded_bars
        ):
            raise ValueError("Sina backfill bars must be complete and single-source")
        if any(
            str(self._mapping(fact).get("source")) != "sina"
            or str(self._mapping(fact).get("symbol")) != symbol
            for fact in recorded_capital
        ):
            raise ValueError("Sina share-capital facts must match the checkpoint symbol")
        key = (str(checkpoint["run_id"]), symbol)
        encoded = self._jsonable_json(checkpoint)
        with self._idempotent_write_connection() as connection:
            for evidence in fetch_evidence:
                self._save_provider_fetch_evidence(connection, evidence)
            self._save_daily_bars(connection, recorded_bars)
            self._save_share_capital_facts(connection, recorded_capital)
            self._immutable_composite_insert(
                connection,
                "sina_backfill_checkpoints",
                ("run_id", "symbol"),
                key,
                ("status", "checkpoint_json"),
                (str(checkpoint["status"]), encoded),
            )

    def save_daily_security_statuses(self, statuses: Iterable[dict[str, object] | object]) -> None:
        with self._idempotent_write_connection() as connection:
            self._save_daily_security_statuses(connection, statuses)

    def _save_daily_security_statuses(
        self, connection: sqlite3.Connection, statuses: Iterable[dict[str, object] | object]
    ) -> None:
        for status in statuses:
            item = self._mapping(status)
            trade_status = str(item["tradestatus"])
            if trade_status not in {"0", "1"}:
                raise ValueError("BaoStock tradeStatus must be 0 or 1")
            key = (str(item["symbol"]), self._iso(item["trade_date"]), str(item["source"]))
            values = (
                trade_status,
                int(bool(item["is_st"])),
                self._iso(item["source_timestamp"]),
                str(item["batch_sha256"]),
            )
            self._immutable_composite_insert(
                connection,
                "daily_security_status",
                ("symbol", "trade_date", "source"),
                key,
                ("tradestatus", "is_st", "source_timestamp", "batch_sha256"),
                values,
            )

    def save_baostock_status_batch(
        self,
        *,
        run_id: str,
        trade_date: date,
        statuses: Iterable[dict[str, object] | object],
        checkpoint: dict[str, object],
    ) -> None:
        """Atomically persist a complete dated BaoStock universe and its checkpoint."""

        recorded = tuple(statuses)
        if not recorded or checkpoint.get("status") != "complete":
            raise ValueError("BaoStock status batch is incomplete")
        encoded = self._jsonable_json(checkpoint)
        with self._idempotent_write_connection() as connection:
            for status in recorded:
                item = self._mapping(status)
                key = (
                    str(item["symbol"]),
                    self._iso(item["trade_date"]),
                    str(item["source"]),
                )
                existing = connection.execute(
                    "SELECT tradestatus,is_st FROM daily_security_status "
                    "WHERE symbol=? AND trade_date=? AND source=?",
                    key,
                ).fetchone()
                if existing is not None:
                    if (str(existing[0]), int(existing[1])) != (
                        str(item["tradestatus"]),
                        int(bool(item["is_st"])),
                    ):
                        raise ValueError("daily_security_status fact is immutable; status conflict")
                    continue
                self._save_daily_security_statuses(connection, (item,))
            self._immutable_composite_insert(
                connection,
                "provider_backfill_checkpoints",
                ("run_id", "request_key"),
                (run_id, trade_date.isoformat()),
                ("status", "checkpoint_json"),
                ("complete", encoded),
            )

    def load_provider_backfill_checkpoint(
        self, *, run_id: str, request_key: str
    ) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT checkpoint_json FROM provider_backfill_checkpoints "
                "WHERE run_id=? AND request_key=?",
                (run_id, request_key),
            ).fetchone()
        return None if row is None else json.loads(str(row[0]))

    def build_v4_legacy_status_facts(self, *, start: date, end: date) -> dict[str, object]:
        """Derive eligible BaoStock status facts from recorded legacy snapshots.

        Historical snapshots were admitted only after the dated BaoStock universe
        reported ``tradeStatus=1`` and the security was non-ST.  Schema v10 kept
        that eligibility in ``snapshot_securities`` but did not copy it into the
        v11 status table.  This migration is explicit, deterministic and atomic.
        """

        if end < start:
            raise ValueError("v4 status fact range is invalid")
        schema = "legacy-baostock-snapshot-status-v1"
        inserted_rows = 0
        existing_rows = 0
        eligible_rows = 0
        days = 0
        with self._idempotent_write_connection() as connection:
            snapshots = connection.execute(
                "SELECT m.trade_date, m.source_timestamp FROM market_snapshots m "
                "WHERE m.source='tushare' AND m.trade_date BETWEEN ? AND ? "
                "ORDER BY m.trade_date",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
            for trade_date_value, source_timestamp_value in snapshots:
                trade_date_text = str(trade_date_value)
                source_timestamp = str(source_timestamp_value)
                securities = tuple(
                    (str(row[0]), int(row[1]))
                    for row in connection.execute(
                        "SELECT s.symbol, s.is_st FROM snapshot_securities s "
                        "JOIN daily_bars b ON b.symbol=s.symbol "
                        "AND b.trade_date=s.trade_date AND b.source=s.source "
                        "WHERE s.source='tushare' AND s.trade_date=? "
                        "ORDER BY s.symbol",
                        (trade_date_text,),
                    )
                )
                if not securities:
                    continue
                days += 1
                eligible_rows += len(securities)
                batch_sha256 = hashlib.sha256(
                    self._json(
                        {
                            "schema": schema,
                            "trade_date": trade_date_text,
                            "source_timestamp": source_timestamp,
                            "statuses": [
                                [symbol, "1", bool(is_st)] for symbol, is_st in securities
                            ],
                        }
                    ).encode()
                ).hexdigest()
                conflicts = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM snapshot_securities s "
                        "JOIN daily_bars b ON b.symbol=s.symbol "
                        "AND b.trade_date=s.trade_date AND b.source=s.source "
                        "JOIN daily_security_status d ON d.symbol=s.symbol "
                        "AND d.trade_date=s.trade_date AND d.source='baostock' "
                        "WHERE s.source='tushare' AND s.trade_date=? "
                        "AND (d.tradestatus!='1' OR d.is_st!=s.is_st)",
                        (trade_date_text,),
                    ).fetchone()[0]
                )
                if conflicts:
                    raise ValueError("daily_security_status fact is immutable; status conflict")
                existing_rows += int(
                    connection.execute(
                        "SELECT COUNT(*) FROM snapshot_securities s "
                        "JOIN daily_bars b ON b.symbol=s.symbol "
                        "AND b.trade_date=s.trade_date AND b.source=s.source "
                        "JOIN daily_security_status d ON d.symbol=s.symbol "
                        "AND d.trade_date=s.trade_date AND d.source='baostock' "
                        "WHERE s.source='tushare' AND s.trade_date=?",
                        (trade_date_text,),
                    ).fetchone()[0]
                )
                inserted_rows += int(
                    connection.execute(
                        "INSERT INTO daily_security_status(symbol,trade_date,source,"
                        "tradestatus,is_st,source_timestamp,batch_sha256) "
                        "SELECT s.symbol,s.trade_date,'baostock','1',s.is_st,?,? "
                        "FROM snapshot_securities s JOIN daily_bars b ON b.symbol=s.symbol "
                        "AND b.trade_date=s.trade_date AND b.source=s.source "
                        "LEFT JOIN daily_security_status d ON d.symbol=s.symbol "
                        "AND d.trade_date=s.trade_date AND d.source='baostock' "
                        "WHERE s.source='tushare' AND s.trade_date=? AND d.symbol IS NULL",
                        (source_timestamp, batch_sha256, trade_date_text),
                    ).rowcount
                )
        return {
            "schema": schema,
            "days": days,
            "eligible_rows": eligible_rows,
            "inserted_rows": inserted_rows,
            "existing_rows": existing_rows,
        }

    def save_provider_daily_metrics(self, metrics: Iterable[dict[str, object] | object]) -> None:
        with self._idempotent_write_connection() as connection:
            self._save_provider_daily_metrics(connection, metrics)

    def _save_provider_daily_metrics(
        self,
        connection: sqlite3.Connection,
        metrics: Iterable[dict[str, object] | object],
    ) -> None:
        for metric in metrics:
            item = self._mapping(metric)
            key = (
                str(item["symbol"]),
                self._iso(item["trade_date"]),
                str(item["price_source"]),
                str(item["capital_source"]),
            )
            values = (
                None
                if item.get("upstream_market_cap_fen") is None
                else int(item["upstream_market_cap_fen"]),
                int(item["derived_market_cap_fen"]),
                None
                if item.get("upstream_turnover_rate") is None
                else str(item["upstream_turnover_rate"]),
                str(item["derived_turnover_rate"]),
                str(item["evidence_sha256"]),
            )
            self._immutable_composite_insert(
                connection,
                "provider_daily_metrics",
                ("symbol", "trade_date", "price_source", "capital_source"),
                key,
                (
                    "upstream_market_cap_fen",
                    "derived_market_cap_fen",
                    "upstream_turnover_rate",
                    "derived_turnover_rate",
                    "evidence_sha256",
                ),
                values,
            )

    def load_share_capital_fact(
        self, symbol: str, *, on_date: date, source: str = "sina"
    ) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT effective_date, outstanding_shares, source_timestamp, payload_sha256 "
                "FROM share_capital_facts WHERE symbol=? AND source=? AND effective_date<=? "
                "ORDER BY effective_date DESC LIMIT 1",
                (symbol, source, on_date.isoformat()),
            ).fetchone()
        if row is None:
            return None
        return {
            "symbol": symbol,
            "effective_date": date.fromisoformat(str(row[0])),
            "source": source,
            "outstanding_shares": int(row[1]),
            "source_timestamp": datetime.fromisoformat(str(row[2])),
            "payload_sha256": str(row[3]),
        }

    def save_sina_spot_batch(
        self,
        *,
        snapshot: MarketSnapshot,
        fetch_evidence: Iterable[dict[str, object]],
        metrics: dict[str, object],
        daily_metrics: Iterable[dict[str, object] | object] = (),
        shadow_run: dict[str, object] | None = None,
    ) -> None:
        if snapshot.source != "sina" or any(bar.source != "sina" for bar in snapshot.bars):
            raise ValueError("Sina spot batch cannot mix price sources")
        with self._idempotent_write_connection() as connection:
            for evidence in fetch_evidence:
                self._save_provider_fetch_evidence(connection, evidence)
            encoded = self._jsonable_json(metrics)
            key = (snapshot.trade_date.isoformat(), snapshot.source)
            self._immutable_composite_insert(
                connection,
                "provider_spot_batches",
                ("trade_date", "source"),
                key,
                ("metrics_json",),
                (encoded,),
            )
            self._save_market_snapshot(connection, snapshot)
            self._save_provider_daily_metrics(connection, daily_metrics)
            if shadow_run is not None:
                self._save_provider_shadow_run(connection, shadow_run)

    def save_provider_shadow_run(self, run: dict[str, object]) -> None:
        with self._idempotent_write_connection() as connection:
            self._save_provider_shadow_run(connection, run)

    def _save_provider_shadow_run(
        self, connection: sqlite3.Connection, run: dict[str, object]
    ) -> None:
        key = (str(run["source"]), self._iso(run["trade_date"]), str(run["adapter_version"]))
        payload = self._jsonable_json(run)
        self._immutable_composite_insert(
            connection,
            "provider_shadow_runs",
            ("source", "trade_date", "adapter_version"),
            key,
            ("run_json", "status", "dataset_hash"),
            (payload, str(run["status"]), str(run["dataset_hash"])),
        )

    def list_provider_shadow_runs(
        self, source: str, *, through_date: date | None = None, limit: int = 100
    ) -> tuple[dict[str, object], ...]:
        clause = "source = ?"
        values: list[object] = [source]
        if through_date is not None:
            clause += " AND trade_date <= ?"
            values.append(through_date.isoformat())
        values.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT run_json FROM provider_shadow_runs WHERE {clause} "
                "ORDER BY trade_date DESC LIMIT ?",
                values,
            ).fetchall()
        return tuple(json.loads(str(row[0])) for row in rows)

    def save_provider_qualification(self, qualification: dict[str, object]) -> None:
        key = (str(qualification["source"]), self._iso(qualification["through_date"]))
        payload = self._jsonable_json(qualification)
        status = str(qualification["status"])
        if status not in {"collecting", "qualified_for_manual_approval", "failed", "expired"}:
            raise ValueError("unsupported provider qualification status")
        qualification_id = str(
            qualification.get("qualification_id")
            or f"{qualification['source']}:{key[1]}:{str(qualification['dataset_hash'])[:12]}"
        )
        window_hash = str(qualification.get("window_hash") or qualification["dataset_hash"])
        recorded_at = self._iso(qualification["recorded_at"])
        with self._idempotent_write_connection() as connection:
            existing = connection.execute(
                "SELECT status, dataset_hash, report_json FROM provider_qualifications "
                "WHERE source = ? AND through_date = ?",
                key,
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO provider_qualifications VALUES (?, ?, ?, ?, ?, ?)",
                    (*key, status, str(qualification["dataset_hash"]), recorded_at, payload),
                )
            elif existing != (status, str(qualification["dataset_hash"]), payload):
                existing_report = json.loads(str(existing[2]))
                current_report = json.loads(payload)
                existing_report.pop("recorded_at", None)
                current_report.pop("recorded_at", None)
                same_evidence = (
                    existing[0] == status
                    and existing[1] == str(qualification["dataset_hash"])
                    and existing_report == current_report
                )
                if same_evidence:
                    # A repeated report command has a new observation timestamp,
                    # but it is not new qualification evidence.  Preserve the
                    # original immutable report so the later attested transition
                    # can proceed without manufacturing a conflict.
                    payload = str(existing[2])
                else:
                    allowed = existing[0] == "collecting" and status in {
                        "qualified_for_manual_approval",
                        "failed",
                    }
                    allowed = allowed or (
                        existing[0] == "qualified_for_manual_approval" and status == "expired"
                    )
                    if not allowed or existing[1] != str(qualification["dataset_hash"]):
                        raise ValueError(
                            "provider qualification transition conflicts with evidence"
                        )
                    connection.execute(
                        "UPDATE provider_qualifications SET status=?, recorded_at=?, report_json=? "
                        "WHERE source=? AND through_date=?",
                        (status, recorded_at, payload, *key),
                    )
            if qualification_id:
                row = connection.execute(
                    "SELECT source, through_date, status, window_hash, report_json "
                    "FROM provider_qualification_reports WHERE qualification_id = ?",
                    (qualification_id,),
                ).fetchone()
                values = (str(qualification["source"]), key[1], status, window_hash, payload)
                if row is None:
                    connection.execute(
                        "INSERT INTO provider_qualification_reports VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (qualification_id, *values, recorded_at),
                    )
                elif tuple(row) != values:
                    if row[0:2] != values[0:2] or row[3] != window_hash:
                        raise ValueError("provider qualification report is immutable")
                    connection.execute(
                        "UPDATE provider_qualification_reports SET status=?, report_json=? "
                        "WHERE qualification_id=?",
                        (status, payload, qualification_id),
                    )

    def get_provider_qualification(self, source: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM provider_qualifications WHERE source = ? "
                "ORDER BY through_date DESC LIMIT 1",
                (source,),
            ).fetchone()
        return None if row is None else json.loads(str(row[0]))

    def get_provider_review_attestation(self, qualification_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT window_hash, terms_confirmed, attested_at FROM "
                "provider_qualification_review_attestations WHERE qualification_id = ?",
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "qualification_id": qualification_id,
            "window_hash": str(row[0]),
            "terms_confirmed": bool(row[1]),
            "attested_at": str(row[2]),
        }

    def record_provider_attestation(
        self, *, source: str, through_date: date, dataset_hash: str
    ) -> dict[str, object]:
        result = {
            "source": source,
            "through_date": through_date.isoformat(),
            "dataset_hash": dataset_hash,
            "attested_at": datetime.now(UTC).isoformat(),
        }
        with self._idempotent_write_connection() as connection:
            report = connection.execute(
                "SELECT qualification_id, window_hash FROM provider_qualification_reports "
                "WHERE source=? AND through_date=? ORDER BY created_at DESC LIMIT 1",
                (source, through_date.isoformat()),
            ).fetchone()
            if report is None or report[1] != dataset_hash:
                raise ValueError("recorded provider qualification report is required")
            existing = connection.execute(
                "SELECT dataset_hash, attested_at FROM provider_attestations "
                "WHERE source = ? AND through_date = ?",
                (source, through_date.isoformat()),
            ).fetchone()
            if existing is not None and existing[0] != dataset_hash:
                raise ValueError("provider attestation is immutable; conflicting evidence")
            if existing is None:
                connection.execute(
                    "INSERT INTO provider_attestations VALUES (?, ?, ?, ?)",
                    (source, through_date.isoformat(), dataset_hash, result["attested_at"]),
                )
            else:
                result["attested_at"] = str(existing[1])
            connection.execute(
                "INSERT OR IGNORE INTO provider_qualification_review_attestations("
                "qualification_id, window_hash, terms_confirmed, attested_at) "
                "VALUES (?, ?, 1, ?)",
                (str(report[0]), dataset_hash, result["attested_at"]),
            )
            result["qualification_id"] = str(report[0])
        return result

    def approve_provider_source(
        self, *, source: str, through_date: date, dataset_hash: str
    ) -> dict[str, object]:
        with self._idempotent_write_connection() as connection:
            qualification = connection.execute(
                "SELECT status, dataset_hash FROM provider_qualifications "
                "WHERE source = ? AND through_date = ?",
                (source, through_date.isoformat()),
            ).fetchone()
            attested = connection.execute(
                "SELECT dataset_hash FROM provider_attestations "
                "WHERE source = ? AND through_date = ?",
                (source, through_date.isoformat()),
            ).fetchone()
            if qualification != ("qualified_for_manual_approval", dataset_hash) or attested != (
                dataset_hash,
            ):
                raise ValueError("provider qualification and review attestation are required")
            approved_at = datetime.now(UTC).isoformat()
            existing = connection.execute(
                "SELECT dataset_hash, approved_at FROM provider_approvals "
                "WHERE source = ? AND through_date = ?",
                (source, through_date.isoformat()),
            ).fetchone()
            if existing is not None and existing[0] != dataset_hash:
                raise ValueError("provider approval is immutable; conflicting evidence")
            if existing is None:
                connection.execute(
                    "INSERT INTO provider_approvals VALUES (?, ?, ?, ?)",
                    (source, through_date.isoformat(), dataset_hash, approved_at),
                )
            else:
                approved_at = str(existing[1])
        return {
            "source": source,
            "through_date": through_date.isoformat(),
            "dataset_hash": dataset_hash,
            "approved_at": approved_at,
        }

    def register_provider_source(
        self,
        *,
        source: str,
        through_date: date,
        dataset_hash: str,
        capabilities: Iterable[str] = ("enrichment", "backup_price"),
    ) -> str:
        normalized = tuple(sorted(set(capabilities)))
        if not normalized or not set(normalized).issubset({"enrichment", "backup_price"}):
            raise ValueError("unsupported provider capability")
        with self._idempotent_write_connection() as connection:
            approval = connection.execute(
                "SELECT dataset_hash FROM provider_approvals WHERE source = ? AND through_date = ?",
                (source, through_date.isoformat()),
            ).fetchone()
            if approval != (dataset_hash,):
                return "approval_required"
            payload = self._json(list(normalized))
            existing = connection.execute(
                "SELECT qualification_id, capabilities_json FROM provider_registry "
                "WHERE source = ?",
                (source,),
            ).fetchone()
            qualification_id = f"{source}:{through_date.isoformat()}:{dataset_hash[:12]}"
            if existing is not None and existing != (qualification_id, payload):
                raise ValueError("provider registry is immutable; conflicting activation")
            connection.execute(
                "INSERT OR IGNORE INTO provider_registry("
                "source, qualification_id, capabilities_json, "
                "activated_at) VALUES (?, ?, ?, ?)",
                (source, qualification_id, payload, datetime.now(UTC).isoformat()),
            )
        return "registered"

    def load_sina_backfill_checkpoint(
        self, *, run_id: str, symbol: str
    ) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT checkpoint_json FROM sina_backfill_checkpoints "
                "WHERE run_id = ? AND symbol = ?",
                (run_id, symbol),
            ).fetchone()
        return None if row is None else json.loads(str(row[0]))

    def save_sina_backfill_checkpoint(self, checkpoint: dict[str, object]) -> None:
        key = (str(checkpoint["run_id"]), str(checkpoint["symbol"]))
        encoded = self._jsonable_json(checkpoint)
        with self._idempotent_write_connection() as connection:
            self._immutable_composite_insert(
                connection,
                "sina_backfill_checkpoints",
                ("run_id", "symbol"),
                key,
                ("status", "checkpoint_json"),
                (str(checkpoint["status"]), encoded),
            )

    def save_v4_dataset_manifest(self, manifest: dict[str, object]) -> dict[str, object]:
        from .replay import validate_v4_manifest_symbol_coverage

        manifest_hash = str(manifest.get("manifest_hash", ""))
        self._validate_sha256(manifest_hash, "v4 manifest")
        if (
            manifest.get("schema") != "v4-manifest-v1"
            or manifest.get("source") != "tushare"
            or manifest.get("share_capital_source") != "sina"
            or manifest.get("status_source") != "baostock"
        ):
            raise ValueError("v4 manifest provider provenance is invalid")
        validate_v4_manifest_symbol_coverage(manifest)
        canonical = dict(manifest)
        canonical.pop("manifest_hash", None)
        computed = hashlib.sha256(self._json(canonical).encode()).hexdigest()
        if computed != manifest_hash:
            raise ValueError("v4 manifest hash does not match its canonical content")
        encoded = self._jsonable_json(manifest)
        with self._idempotent_write_connection() as connection:
            self._immutable_insert(
                connection,
                "v4_dataset_manifests",
                "manifest_hash",
                manifest_hash,
                ("manifest_json", "price_source", "created_at"),
                (
                    encoded,
                    str(manifest.get("source", manifest.get("price_source", ""))),
                    str(manifest.get("created_at", datetime.now(UTC).isoformat())),
                ),
            )
        return dict(manifest)

    def get_v4_dataset_manifest(self, manifest_hash: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM v4_dataset_manifests WHERE manifest_hash = ?",
                (manifest_hash,),
            ).fetchone()
        return None if row is None else json.loads(str(row[0]))

    def compute_v4_evidence_hashes(
        self, *, start: date, end: date, included_symbols: tuple[str, ...]
    ) -> dict[str, str]:
        """Hash the exact local facts a v4 manifest binds, without live I/O."""

        if not included_symbols or included_symbols != tuple(sorted(set(included_symbols))):
            raise ValueError("v4 evidence symbols are invalid")
        included = set(included_symbols)

        def digest_rows(rows: object) -> str:
            digest = hashlib.sha256()
            for row in rows:
                if str(row[0]) not in included:
                    continue
                digest.update(json.dumps(tuple(row), separators=(",", ":"), default=str).encode())
                digest.update(b"\n")
            return digest.hexdigest()

        with self.connect() as connection:
            calendar = hashlib.sha256()
            for row in connection.execute(
                "SELECT trade_date FROM expected_trading_days WHERE source='tushare' "
                "AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
                (start.isoformat(), end.isoformat()),
            ):
                calendar.update(str(row[0]).encode())
                calendar.update(b"\n")
            prices = digest_rows(
                connection.execute(
                    "SELECT symbol, trade_date, open_1e4, high_1e4, low_1e4, close_1e4, "
                    "pre_close_1e4, volume_shares, amount_fen, source_timestamp FROM daily_bars "
                    "WHERE source='tushare' AND trade_date BETWEEN ? AND ? "
                    "ORDER BY trade_date, symbol",
                    (start.isoformat(), end.isoformat()),
                )
            )
            security_metadata = digest_rows(
                connection.execute(
                    "SELECT symbol, trade_date, name, exchange, board, list_date, industry, "
                    "is_st FROM "
                    "snapshot_securities WHERE source='tushare' AND trade_date BETWEEN ? AND ? "
                    "ORDER BY trade_date, symbol",
                    (start.isoformat(), end.isoformat()),
                )
            )
            snapshot_metadata = hashlib.sha256()
            for row in connection.execute(
                "SELECT trade_date,source_timestamp "
                "FROM market_snapshots WHERE source='tushare' AND trade_date BETWEEN ? AND ? "
                "ORDER BY trade_date",
                (start.isoformat(), end.isoformat()),
            ):
                snapshot_metadata.update(
                    json.dumps(tuple(row), separators=(",", ":"), default=str).encode()
                )
                snapshot_metadata.update(b"\n")
            price_limits = digest_rows(
                connection.execute(
                    "SELECT symbol, trade_date, fact_json FROM daily_price_limits "
                    "WHERE source='tushare' AND trade_date BETWEEN ? AND ? "
                    "ORDER BY trade_date, symbol",
                    (start.isoformat(), end.isoformat()),
                )
            )
            industry_features = digest_rows(
                connection.execute(
                    "SELECT symbol, trade_date, json_extract(feature_json,'$.industry'), "
                    "json_extract(feature_json,'$.industry_mapping_sha256'), "
                    "json_extract(feature_json,'$.industry_standard'), "
                    "json_extract(feature_json,'$.industry_mode'), "
                    "json_extract(feature_json,'$.industry_as_of') "
                    "FROM v3_snapshot_features WHERE source='tushare' "
                    "AND trade_date BETWEEN ? AND ? ORDER BY trade_date, symbol",
                    (start.isoformat(), end.isoformat()),
                )
            )
            statuses = digest_rows(
                connection.execute(
                    "SELECT symbol, trade_date, tradestatus, is_st, source_timestamp, batch_sha256 "
                    "FROM daily_security_status WHERE source='baostock' "
                    "AND trade_date BETWEEN ? AND ? ORDER BY trade_date, symbol",
                    (start.isoformat(), end.isoformat()),
                )
            )
            capital = digest_rows(
                connection.execute(
                    "SELECT symbol, effective_date, outstanding_shares, source_timestamp, "
                    "payload_sha256 FROM share_capital_facts WHERE source='sina' "
                    "AND effective_date <= ? ORDER BY symbol, effective_date",
                    (end.isoformat(),),
                )
            )
            industry = set()
            for row in connection.execute(
                "SELECT symbol, json_extract(feature_json,'$.industry_mapping_sha256'), "
                "json_extract(feature_json,'$.industry_standard'), "
                "json_extract(feature_json,'$.industry_mode'), "
                "json_extract(feature_json,'$.industry_as_of') "
                "FROM v3_snapshot_features WHERE source='tushare' "
                "AND trade_date BETWEEN ? AND ? ORDER BY trade_date, symbol",
                (start.isoformat(), end.isoformat()),
            ):
                if str(row[0]) in included and row[1] is not None:
                    industry.add(tuple(str(item) for item in row[1:]))
        if len(industry) != 1:
            raise ValueError("v4 industry mapping evidence conflicts across the manifest window")
        industry_metadata = industry.pop()
        return {
            "prices_hash": hashlib.sha256(
                self._json(
                    {
                        "bars": prices,
                        "calendar": calendar.hexdigest(),
                        "security_metadata": security_metadata,
                        "snapshot_metadata": snapshot_metadata.hexdigest(),
                        "price_limits": price_limits,
                        "industry_features": industry_features,
                    }
                ).encode()
            ).hexdigest(),
            "statuses_hash": statuses,
            "share_capital_hash": capital,
            "industry_mapping_hash": hashlib.sha256(
                self._json({"industry_reference": industry_metadata}).encode()
            ).hexdigest(),
        }

    def get_v4_evidence_revision(self) -> int:
        """Return the O(1) revision advanced by immutable evidence table writes."""

        with self.connect() as connection:
            return int(
                connection.execute(
                    "SELECT revision FROM v4_evidence_revision WHERE singleton=1"
                ).fetchone()[0]
            )

    @staticmethod
    def _ensure_v4_evidence_revision_triggers(connection: sqlite3.Connection) -> None:
        tables = (
            "daily_bars",
            "market_snapshots",
            "snapshot_securities",
            "daily_price_limits",
            "v3_snapshot_features",
            "daily_security_status",
            "share_capital_facts",
            "expected_trading_days",
        )
        for table in tables:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists is None:
                continue
            for action in ("INSERT", "UPDATE", "DELETE"):
                name = f"v4_evidence_revision_{table}_{action.lower()}"
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {name} AFTER {action} ON {table} "
                    "BEGIN UPDATE v4_evidence_revision SET revision=revision+1 "
                    "WHERE singleton=1; END"
                )

    def create_v4_study_run(
        self,
        *,
        manifest_hash: str,
        idempotency_key: str,
        arms: Iterable[dict[str, object]],
    ) -> dict[str, object]:
        operation = "v4:start_research"
        request = {"manifest_hash": manifest_hash}
        request_hash = self._request_hash(operation, request)
        with self._idempotent_write_connection() as connection:
            cached = self._idempotent_result(connection, operation, idempotency_key, request_hash)
            if cached is not None:
                return dict(cached)
            if (
                connection.execute(
                    "SELECT 1 FROM v4_dataset_manifests WHERE manifest_hash = ?", (manifest_hash,)
                ).fetchone()
                is None
            ):
                raise ValueError("unknown v4 dataset manifest")
            study_id = f"v4-study-{uuid4().hex}"
            created_at = datetime.now(UTC).isoformat()
            input_hash = hashlib.sha256(
                self._json({"schema": "v4-input-v1", "manifest_hash": manifest_hash}).encode()
            ).hexdigest()
            connection.execute(
                "INSERT INTO v4_study_runs("
                "study_id, manifest_hash, status, input_hash, created_at) "
                "VALUES (?, ?, 'queued', ?, ?)",
                (study_id, manifest_hash, input_hash, created_at),
            )
            for arm in arms:
                connection.execute(
                    "INSERT INTO v4_study_arms(study_id, arm_id, parameters_json, "
                    "parameters_hash, parent_version, unique_difference, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'queued')",
                    (
                        study_id,
                        str(arm["arm_id"]),
                        self._jsonable_json(arm["parameters"]),
                        str(arm["parameters_hash"]),
                        str(arm.get("parent_version") or ""),
                        str(arm["change"]),
                    ),
                )
            result = {
                "study_id": study_id,
                "replay_id": study_id,
                "manifest_hash": manifest_hash,
                "status": "queued",
                "certified": False,
                "active": False,
                "outcome_hash_schema": "v4-outcome-v2",
                "created_at": created_at,
            }
            self._save_idempotent_result(
                connection, operation, idempotency_key, request_hash, result
            )
        return result

    def get_v4_study_run(self, study_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT study_id, manifest_hash, status, input_hash, result_hash, report_json, "
                "error, created_at, started_at, completed_at FROM v4_study_runs WHERE study_id = ?",
                (study_id,),
            ).fetchone()
        if row is None:
            return None
        return self._v4_study_run_from_row(row)

    @staticmethod
    def _v4_study_run_from_row(row: sqlite3.Row | tuple[object, ...]) -> dict[str, object]:
        return {
            "study_id": str(row[0]),
            "replay_id": str(row[0]),
            "manifest_hash": str(row[1]),
            "status": str(row[2]),
            "input_hash": str(row[3]),
            "result_hash": None if row[4] is None else str(row[4]),
            "report": None if row[5] is None else json.loads(str(row[5])),
            "error": None if row[6] is None else str(row[6]),
            "created_at": str(row[7]),
            "started_at": None if row[8] is None else str(row[8]),
            "completed_at": None if row[9] is None else str(row[9]),
            "outcome_hash_schema": "v4-outcome-v2",
            "certified": False,
            "active": False,
        }

    def list_v4_study_arms(self, study_id: str) -> tuple[dict[str, object], ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT arm_id, parameters_json, parameters_hash, parent_version, "
                "unique_difference, status FROM v4_study_arms WHERE study_id = ? ORDER BY arm_id",
                (study_id,),
            ).fetchall()
        return tuple(
            {
                "arm_id": str(row[0]),
                "parameters": json.loads(str(row[1])),
                "parameters_hash": str(row[2]),
                "parent_version": str(row[3]),
                "change": str(row[4]),
                "status": str(row[5]),
            }
            for row in rows
        )

    def list_v4_study_days(
        self, *, study_id: str, arm_id: str, after_signal_date: date | None, limit: int
    ) -> tuple[dict[str, object], ...]:
        after = "0001-01-01" if after_signal_date is None else after_signal_date.isoformat()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT signal_date, result_json, result_hash FROM v4_study_days "
                "WHERE study_id = ? AND arm_id = ? AND signal_date > ? "
                "ORDER BY signal_date LIMIT ?",
                (study_id, arm_id, after, limit),
            ).fetchall()
        return tuple(
            {
                "signal_date": str(row[0]),
                "result": json.loads(str(row[1])),
                "result_hash": str(row[2]),
            }
            for row in rows
        )

    def get_v4_study_progress(self, *, study_id: str) -> dict[str, dict[str, object]]:
        """Return the compact durable cursor without decoding historical result JSON."""

        with self.connect() as connection:
            arms = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT arm_id FROM v4_study_arms WHERE study_id=? ORDER BY arm_id",
                    (study_id,),
                )
            )
            rows = connection.execute(
                "SELECT arm_id,COUNT(*),MAX(signal_date) FROM v4_study_days "
                "WHERE study_id=? GROUP BY arm_id",
                (study_id,),
            ).fetchall()
        values = {str(row[0]): (int(row[1]), str(row[2])) for row in rows}
        return {
            arm_id: {
                "completed_count": values.get(arm_id, (0, ""))[0],
                "last_signal_date": None if arm_id not in values else values[arm_id][1],
            }
            for arm_id in arms
        }

    def save_v4_study_candidate_outcomes(
        self, *, study_id: str, arm_id: str, outcomes: dict[str, object]
    ) -> None:
        """Persist immutable outcome-v2 evidence for one study arm."""

        with self._idempotent_write_connection() as connection:
            self._require_running_v4_arm(connection, study_id, arm_id)
            self._save_v4_candidate_outcomes(
                connection, study_id=study_id, arm_id=arm_id, outcomes=outcomes
            )

    def _save_v4_candidate_outcomes(
        self,
        connection: sqlite3.Connection,
        *,
        study_id: str,
        arm_id: str,
        outcomes: dict[str, object],
    ) -> None:
        manifest_row = connection.execute(
            "SELECT manifest_hash FROM v4_study_runs WHERE study_id=?", (study_id,)
        ).fetchone()
        if manifest_row is None:
            raise ValueError("unknown v4 study")
        manifest_hash = str(manifest_row[0])
        for candidate_id, outcome in sorted(outcomes.items()):
            if not isinstance(outcome, dict):
                raise ValueError("v4 candidate outcome is invalid")
            encoded = self._jsonable_json(outcome)
            outcome_hash = hashlib.sha256(
                self._json(
                    {
                        "schema": "v4-outcome-v2",
                        "manifest_hash": manifest_hash,
                        "arm_id": arm_id,
                        "candidate_id": candidate_id,
                        "outcome": outcome,
                    }
                ).encode()
            ).hexdigest()
            existing = connection.execute(
                "SELECT outcome_json,outcome_hash FROM v4_study_candidate_outcomes "
                "WHERE study_id=? AND arm_id=? AND candidate_id=?",
                (study_id, arm_id, candidate_id),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != encoded or str(existing[1]) != outcome_hash:
                    raise ValueError("immutable v4 candidate outcome conflict")
                continue
            connection.execute(
                "INSERT INTO v4_study_candidate_outcomes(study_id,arm_id,candidate_id,"
                "outcome_json,outcome_hash) VALUES(?,?,?,?,?)",
                (study_id, arm_id, candidate_id, encoded, outcome_hash),
            )

    def list_v4_study_candidate_outcomes(self, *, study_id: str, arm_id: str) -> dict[str, object]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT candidate_id,outcome_json FROM v4_study_candidate_outcomes "
                "WHERE study_id=? AND arm_id=? ORDER BY candidate_id",
                (study_id, arm_id),
            ).fetchall()
        return {str(row[0]): json.loads(str(row[1])) for row in rows}

    def save_v4_study_statistics(self, *, study_id: str, statistics: dict[str, object]) -> None:
        """Persist one immutable study-wide statistics artifact."""

        if statistics.get("schema") != "v4-statistics-v1":
            raise ValueError("v4 statistics schema is invalid")
        encoded = self._jsonable_json(statistics)
        statistics_hash = hashlib.sha256(
            self._json({"schema": "v4-statistics-v1", "statistics": statistics}).encode()
        ).hexdigest()
        with self._idempotent_write_connection() as connection:
            run = connection.execute(
                "SELECT manifest_hash,status FROM v4_study_runs WHERE study_id=?", (study_id,)
            ).fetchone()
            if run is None or str(run[1]) != "running":
                raise ValueError("v4 study is not running")
            if statistics.get("manifest_hash") != str(run[0]):
                raise ValueError("v4 statistics manifest conflicts with the study")
            existing = connection.execute(
                "SELECT statistics_json,statistics_hash FROM v4_study_statistics "
                "WHERE study_id=? AND arm_id='__study__'",
                (study_id,),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != encoded or str(existing[1]) != statistics_hash:
                    raise ValueError("immutable v4 statistics conflict")
                return
            winner = statistics.get("winner")
            if isinstance(winner, dict) and winner.get("eligible"):
                replication = statistics.get("sina_replication")
                if not (
                    isinstance(replication, dict)
                    and replication.get("artifact_hash")
                    and replication.get("status") == "complete"
                    and replication.get("completeness_rate_bps") == 10_000
                ):
                    raise ValueError("v4 winner requires a persisted Sina replication artifact")
            connection.execute(
                "INSERT INTO v4_study_statistics(study_id,arm_id,statistics_json,statistics_hash) "
                "VALUES(?,'__study__',?,?)",
                (study_id, encoded, statistics_hash),
            )

    def get_v4_study_execution_state(self, *, study_id: str) -> dict[str, object]:
        run = self.get_v4_study_run(study_id)
        if run is None:
            raise ValueError("unknown v4 study")
        manifest = self.get_v4_dataset_manifest(str(run["manifest_hash"]))
        if manifest is None:
            raise ValueError("v4 study manifest is missing")
        arms = self.list_v4_study_arms(study_id)
        completed_dates = {
            str(arm["arm_id"]): tuple(
                str(item["signal_date"])
                for item in self.list_v4_study_days(
                    study_id=study_id,
                    arm_id=str(arm["arm_id"]),
                    after_signal_date=None,
                    limit=100_000,
                )
            )
            for arm in arms
        }
        with self.connect() as connection:
            stats = connection.execute(
                "SELECT statistics_json FROM v4_study_statistics "
                "WHERE study_id=? AND arm_id='__study__'",
                (study_id,),
            ).fetchone()
            artifacts = connection.execute(
                "SELECT artifact_json FROM v4_study_proposal_artifacts "
                "WHERE study_id=? ORDER BY artifact_hash",
                (study_id,),
            ).fetchall()
        statistics = {} if stats is None else json.loads(str(stats[0]))
        replication = statistics.get("sina_replication") if isinstance(statistics, dict) else None
        return {
            "run": run,
            "manifest": manifest,
            "arms": arms,
            "completed_dates": completed_dates,
            "statistics": statistics,
            "sina_replication": replication,
            "proposal_artifacts": tuple(json.loads(str(row[0])) for row in artifacts),
        }

    @staticmethod
    def _require_running_v4_arm(connection: sqlite3.Connection, study_id: str, arm_id: str) -> None:
        row = connection.execute(
            "SELECT r.status FROM v4_study_runs r JOIN v4_study_arms a "
            "ON a.study_id=r.study_id WHERE r.study_id=? AND a.arm_id=?",
            (study_id, arm_id),
        ).fetchone()
        if row is None or str(row[0]) != "running":
            raise ValueError("v4 study arm is not running")

    def requeue_interrupted_v4_studies(self) -> int:
        """Make interrupted research work claimable after a process restart."""

        with self._idempotent_write_connection() as connection:
            cursor = connection.execute(
                "UPDATE v4_study_runs SET status = 'queued', error = NULL WHERE status = 'running'"
            )
            return int(cursor.rowcount)

    def claim_next_v4_study(self) -> dict[str, object] | None:
        """Atomically claim the oldest queued study."""

        with self._idempotent_write_connection() as connection:
            row = connection.execute(
                "SELECT study_id, status FROM v4_study_runs "
                "WHERE status IN ('running', 'queued') "
                "ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, "
                "created_at, study_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            study_id = str(row[0])
            started_at = datetime.now(UTC).isoformat()
            if str(row[1]) == "queued":
                cursor = connection.execute(
                    "UPDATE v4_study_runs SET status = 'running', "
                    "started_at = COALESCE(started_at, ?), error = NULL "
                    "WHERE study_id = ? AND status = 'queued'",
                    (started_at, study_id),
                )
                if cursor.rowcount != 1:
                    return None
            claimed = connection.execute(
                "SELECT study_id, manifest_hash, status, input_hash, result_hash, "
                "report_json, error, created_at, started_at, completed_at "
                "FROM v4_study_runs WHERE study_id = ?",
                (study_id,),
            ).fetchone()
        if claimed is None:
            return None
        return self._v4_study_run_from_row(claimed)

    def save_v4_study_step(self, *, study_id: str, step: dict[str, object]) -> None:
        """Persist one immutable, typed research step."""

        self.save_v4_study_steps(study_id=study_id, steps=(step,))

    def save_v4_study_steps(self, *, study_id: str, steps: Iterable[dict[str, object]]) -> None:
        """Atomically persist all arm results for one signal-day work unit.

        Repeated identical rows are idempotent.  Any invalid or conflicting row
        rolls back the entire batch so a restart cannot observe a partially
        published seven-arm signal day.
        """

        batch = tuple(steps)
        if not batch:
            raise ValueError("v4 study step batch is empty")
        signal_dates = {str(step.get("signal_date", "")) for step in batch}
        arm_ids = {str(step.get("arm_id", "")) for step in batch}
        if len(signal_dates) != 1 or len(arm_ids) != len(batch):
            raise ValueError("v4 study step batch must contain one date and unique arms")
        with self._idempotent_write_connection() as connection:
            run = connection.execute(
                "SELECT status,manifest_hash FROM v4_study_runs WHERE study_id = ?", (study_id,)
            ).fetchone()
            if run is None or str(run[0]) != "running":
                raise ValueError("v4 study is not running")
            for step in batch:
                self._save_v4_study_step(
                    connection,
                    study_id=study_id,
                    manifest_hash=str(run[1]),
                    step=step,
                )

    def _save_v4_study_step(
        self,
        connection: sqlite3.Connection,
        *,
        study_id: str,
        manifest_hash: str,
        step: dict[str, object],
    ) -> None:
        if step.get("kind") != "day":
            raise ValueError("unsupported v4 study step kind")
        arm_id = str(step.get("arm_id", ""))
        signal_date = str(step.get("signal_date", ""))
        result = step.get("result")
        if not arm_id or not signal_date or not isinstance(result, dict):
            raise ValueError("v4 day step is incomplete")
        try:
            date.fromisoformat(signal_date)
        except ValueError as error:
            raise ValueError("v4 signal_date is invalid") from error
        encoded = self._jsonable_json(result)
        result_hash = hashlib.sha256(
            self._json(
                {
                    "schema": "v4-result-v1",
                    "manifest_hash": manifest_hash,
                    "arm_id": arm_id,
                    "signal_date": signal_date,
                    "result": self._jsonable(result),
                }
            ).encode()
        ).hexdigest()
        if (
            connection.execute(
                "SELECT 1 FROM v4_study_arms WHERE study_id = ? AND arm_id = ?",
                (study_id, arm_id),
            ).fetchone()
            is None
        ):
            raise ValueError("unknown v4 study arm")
        existing = connection.execute(
            "SELECT result_json, result_hash FROM v4_study_days "
            "WHERE study_id = ? AND arm_id = ? AND signal_date = ?",
            (study_id, arm_id, signal_date),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != encoded or str(existing[1]) != result_hash:
                raise ValueError("immutable v4 study day conflict")
            return
        connection.execute(
            "INSERT INTO v4_study_days(study_id, arm_id, signal_date, result_json, "
            "result_hash) VALUES (?, ?, ?, ?, ?)",
            (study_id, arm_id, signal_date, encoded, result_hash),
        )
        outcomes = result.get("candidate_outcomes")
        if isinstance(outcomes, dict):
            self._save_v4_candidate_outcomes(
                connection, study_id=study_id, arm_id=arm_id, outcomes=outcomes
            )

    def complete_v4_study(self, *, study_id: str, report: dict[str, object]) -> None:
        """Commit an immutable terminal report for a running study."""

        with self.connect() as connection:
            run_manifest = connection.execute(
                "SELECT m.manifest_json FROM v4_study_runs r JOIN v4_dataset_manifests m "
                "ON m.manifest_hash=r.manifest_hash WHERE r.study_id=?",
                (study_id,),
            ).fetchone()
            arms = int(
                connection.execute(
                    "SELECT COUNT(*) FROM v4_study_arms WHERE study_id=?", (study_id,)
                ).fetchone()[0]
            )
            days = int(
                connection.execute(
                    "SELECT COUNT(*) FROM v4_study_days WHERE study_id=?", (study_id,)
                ).fetchone()[0]
            )
            statistics = connection.execute(
                "SELECT 1 FROM v4_study_statistics WHERE study_id=? AND arm_id='__study__'",
                (study_id,),
            ).fetchone()
        strict_report = report.get("schema") == "v4-statistics-v1" or any(
            key in report
            for key in (
                "completeness_status",
                "outcome_completeness_rate_bps",
                "benchmark_completeness_rate_bps",
            )
        )
        if strict_report and (arms != 7 or days < 7 or statistics is None):
            raise ValueError("v4 completion requires seven-arm calendar and outcome evidence")
        if strict_report and report.get("completeness_status") not in {"complete", "incomplete"}:
            raise ValueError("v4 completion report evidence is invalid")
        if strict_report:
            if run_manifest is None:
                raise ValueError("v4 completion manifest evidence is missing")
            manifest = json.loads(str(run_manifest[0]))
            sessions = manifest.get("sessions")
            if not isinstance(sessions, list) or len(sessions) < 86:
                raise ValueError("v4 completion manifest calendar is incomplete")
            expected = tuple(map(str, sessions[60:-25]))
            with self.connect() as connection:
                arm_rows = connection.execute(
                    "SELECT arm_id FROM v4_study_arms WHERE study_id=? ORDER BY arm_id",
                    (study_id,),
                ).fetchall()
                for (arm_id,) in arm_rows:
                    actual = tuple(
                        str(row[0])
                        for row in connection.execute(
                            "SELECT signal_date FROM v4_study_days "
                            "WHERE study_id=? AND arm_id=? ORDER BY signal_date",
                            (study_id, arm_id),
                        ).fetchall()
                    )
                    if actual != expected:
                        raise ValueError("v4 completion calendar evidence is incomplete")

        encoded = self._jsonable_json(report)
        result_hash = hashlib.sha256(
            self._json({"schema": "v4-statistics-v1", "report": report}).encode()
        ).hexdigest()
        completed_at = datetime.now(UTC).isoformat()
        with self._idempotent_write_connection() as connection:
            row = connection.execute(
                "SELECT status, result_hash, report_json FROM v4_study_runs WHERE study_id = ?",
                (study_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown v4 study")
            if str(row[0]) == "completed":
                if str(row[1]) != result_hash or str(row[2]) != encoded:
                    raise ValueError("immutable v4 study report conflict")
                return
            if str(row[0]) != "running":
                raise ValueError("v4 study is not running")
            connection.execute(
                "UPDATE v4_study_runs SET status = 'completed', result_hash = ?, "
                "report_json = ?, error = NULL, completed_at = ? WHERE study_id = ?",
                (result_hash, encoded, completed_at, study_id),
            )
            connection.execute(
                "UPDATE v4_study_arms SET status = 'completed' WHERE study_id = ?",
                (study_id,),
            )

    def fail_v4_study(self, *, study_id: str, error: str) -> None:
        """Persist a safe terminal failure without overwriting completed work."""

        if not error or len(error) > 512:
            raise ValueError("v4 study failure summary is invalid")
        with self._idempotent_write_connection() as connection:
            row = connection.execute(
                "SELECT status, error FROM v4_study_runs WHERE study_id = ?", (study_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown v4 study")
            if str(row[0]) == "failed":
                if str(row[1]) != error:
                    raise ValueError("immutable v4 study failure conflict")
                return
            if str(row[0]) != "running":
                raise ValueError("v4 study is not running")
            connection.execute(
                "UPDATE v4_study_runs SET status = 'failed', error = ?, completed_at = ? "
                "WHERE study_id = ?",
                (error, datetime.now(UTC).isoformat(), study_id),
            )
            connection.execute(
                "UPDATE v4_study_arms SET status = 'failed' WHERE study_id = ?",
                (study_id,),
            )

    def activate_provider_source(
        self,
        *,
        source: str,
        qualification_id: str,
        capabilities: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, object]:
        normalized_capabilities = tuple(sorted(set(capabilities)))
        if normalized_capabilities != ("backup_price", "enrichment"):
            raise ValueError("both frozen provider capabilities are required")
        operation = "provider:activate"
        request = {
            "source": source,
            "qualification_id": qualification_id,
            "capabilities": list(normalized_capabilities),
        }
        request_hash = self._request_hash(operation, request)
        with self._idempotent_write_connection() as connection:
            cached = self._idempotent_result(connection, operation, idempotency_key, request_hash)
            if cached is not None:
                return dict(cached)
            approval = connection.execute(
                "SELECT capabilities_json, consumed_at FROM provider_source_approvals "
                "WHERE qualification_id = ?",
                (qualification_id,),
            ).fetchone()
            expected = self._json(list(normalized_capabilities))
            if approval is None or approval[0] != expected or approval[1] is not None:
                raise ValueError("unconsumed host provider approval is required")
            report = connection.execute(
                "SELECT source, status, window_hash FROM provider_qualification_reports "
                "WHERE qualification_id = ?",
                (qualification_id,),
            ).fetchone()
            attestation = connection.execute(
                "SELECT window_hash, terms_confirmed FROM "
                "provider_qualification_review_attestations WHERE qualification_id = ?",
                (qualification_id,),
            ).fetchone()
            if (
                report is None
                or report[0] != source
                or report[1] != "qualified_for_manual_approval"
                or attestation != (report[2], 1)
            ):
                raise ValueError("current qualified provider evidence and attestation are required")
            activated_at = datetime.now(UTC).isoformat()
            connection.execute(
                "INSERT INTO provider_source_registry(source, qualification_id, capabilities_json, "
                "activated_at) VALUES (?, ?, ?, ?) ON CONFLICT(source) DO UPDATE SET "
                "qualification_id=excluded.qualification_id, "
                "capabilities_json=excluded.capabilities_json, "
                "activated_at=excluded.activated_at",
                (source, qualification_id, expected, activated_at),
            )
            connection.execute(
                "UPDATE provider_source_approvals SET consumed_at = ? WHERE qualification_id = ?",
                (activated_at, qualification_id),
            )
            result = {
                "source": source,
                "qualification_id": qualification_id,
                "capabilities": list(normalized_capabilities),
                "activated_at": activated_at,
            }
            self._save_idempotent_result(
                connection, operation, idempotency_key, request_hash, result
            )
        return result

    def approve_provider_source_capabilities(
        self, *, qualification_id: str, capabilities: tuple[str, ...]
    ) -> dict[str, object]:
        normalized = tuple(sorted(set(capabilities)))
        if normalized != ("backup_price", "enrichment"):
            raise ValueError("host approval requires both frozen provider capabilities")
        encoded = self._json(list(normalized))
        approved_at = datetime.now(UTC).isoformat()
        with self._idempotent_write_connection() as connection:
            report = connection.execute(
                "SELECT status, window_hash FROM provider_qualification_reports "
                "WHERE qualification_id = ?",
                (qualification_id,),
            ).fetchone()
            attestation = connection.execute(
                "SELECT window_hash, terms_confirmed FROM "
                "provider_qualification_review_attestations WHERE qualification_id = ?",
                (qualification_id,),
            ).fetchone()
            if (
                report is None
                or report[0] != "qualified_for_manual_approval"
                or attestation != (report[1], 1)
            ):
                raise ValueError("qualified and attested provider report is required")
            row = connection.execute(
                "SELECT capabilities_json, approved_at, consumed_at FROM provider_source_approvals "
                "WHERE qualification_id = ?",
                (qualification_id,),
            ).fetchone()
            if row is not None and row[0] != encoded:
                raise ValueError("provider source approval is immutable")
            if row is None:
                connection.execute(
                    "INSERT INTO provider_source_approvals(qualification_id, capabilities_json, "
                    "approved_at) VALUES (?, ?, ?)",
                    (qualification_id, encoded, approved_at),
                )
            else:
                approved_at = str(row[1])
        return {
            "qualification_id": qualification_id,
            "capabilities": list(normalized),
            "approved_at": approved_at,
        }

    def _save_market_snapshot(
        self, connection: sqlite3.Connection, snapshot: MarketSnapshot
    ) -> None:
        self._save_daily_bars(connection, snapshot.bars)
        key = (snapshot.trade_date.isoformat(), snapshot.source)
        values = (
            snapshot.source_timestamp.isoformat(),
            snapshot.advance_ratio_bps,
            snapshot.above_ma20_ratio_bps,
        )
        existing = connection.execute(
            """
            SELECT source_timestamp, advance_ratio_bps, above_ma20_ratio_bps
            FROM market_snapshots WHERE trade_date = ? AND source = ?
            """,
            key,
        ).fetchone()
        if existing is not None and existing != values:
            raise ValueError("market snapshot metadata is immutable")
        security_values = {
            security.symbol: (
                security.name,
                security.exchange,
                security.board,
                security.list_date.isoformat(),
                security.industry,
                int(security.is_st),
            )
            for security in snapshot.securities
        }
        if len(security_values) != len(snapshot.securities):
            raise ValueError("market snapshot securities must be unique")
        existing_securities = {
            str(row[0]): tuple(row[1:])
            for row in connection.execute(
                """
                SELECT symbol, name, exchange, board, list_date, industry, is_st
                FROM snapshot_securities
                WHERE trade_date = ? AND source = ?
                """,
                key,
            ).fetchall()
        }
        if existing is not None and existing_securities != security_values:
            raise ValueError("market snapshot securities are immutable")
        connection.execute(
            """
            INSERT OR IGNORE INTO market_snapshots(
                trade_date, source, source_timestamp,
                advance_ratio_bps, above_ma20_ratio_bps
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (*key, *values),
        )
        for security in snapshot.securities:
            row = (
                *key,
                security.symbol,
                security.name,
                security.exchange,
                security.board,
                security.list_date.isoformat(),
                security.industry,
                int(security.is_st),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO snapshot_securities(
                    trade_date, source, symbol, name, exchange, board,
                    list_date, industry, is_st
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )

    def _save_daily_bars(self, connection: sqlite3.Connection, bars: Iterable[DailyBar]) -> None:
        records = tuple(bars)
        incoming: dict[tuple[str, str, str], tuple[str | int, ...]] = {}
        date_groups: dict[tuple[str, str], set[str]] = {}
        symbol_groups: dict[tuple[str, str], set[str]] = {}
        for bar in records:
            values = self._daily_bar_values(bar)
            key = (str(values[0]), str(values[1]), str(values[9]))
            previous = incoming.get(key)
            if previous is not None and previous != values:
                raise ValueError("daily market bar is immutable")
            incoming[key] = values
            date_groups.setdefault((key[1], key[2]), set()).add(key[0])
            symbol_groups.setdefault((key[0], key[2]), set()).add(key[1])

        stored: dict[tuple[str, str, str], tuple[object, ...]] = {}
        chunk_size = 500
        if len(date_groups) <= len(symbol_groups):
            for (trade_date_value, source), symbols in date_groups.items():
                ordered = sorted(symbols)
                for offset in range(0, len(ordered), chunk_size):
                    chunk = ordered[offset : offset + chunk_size]
                    placeholders = ", ".join("?" for _item in chunk)
                    rows = connection.execute(
                        """
                        SELECT symbol, trade_date, open_1e4, high_1e4, low_1e4, close_1e4,
                               pre_close_1e4, volume_shares, amount_fen, source,
                               source_timestamp
                        FROM daily_bars WHERE trade_date = ? AND source = ?
                        """
                        f" AND symbol IN ({placeholders})",
                        (trade_date_value, source, *chunk),
                    ).fetchall()
                    stored.update(
                        {(str(row[0]), str(row[1]), str(row[9])): tuple(row) for row in rows}
                    )
        else:
            for (symbol, source), trade_dates in symbol_groups.items():
                ordered = sorted(trade_dates)
                for offset in range(0, len(ordered), chunk_size):
                    chunk = ordered[offset : offset + chunk_size]
                    placeholders = ", ".join("?" for _item in chunk)
                    rows = connection.execute(
                        """
                        SELECT symbol, trade_date, open_1e4, high_1e4, low_1e4, close_1e4,
                               pre_close_1e4, volume_shares, amount_fen, source,
                               source_timestamp
                        FROM daily_bars WHERE symbol = ? AND source = ?
                        """
                        f" AND trade_date IN ({placeholders})",
                        (symbol, source, *chunk),
                    ).fetchall()
                    stored.update(
                        {(str(row[0]), str(row[1]), str(row[9])): tuple(row) for row in rows}
                    )
        if any(stored[key] != incoming[key] for key in stored):
            raise ValueError("daily market bar is immutable")

        connection.executemany(
            """
            INSERT OR IGNORE INTO daily_bars (
                symbol, trade_date, open_1e4, high_1e4, low_1e4, close_1e4,
                pre_close_1e4, volume_shares, amount_fen, source, source_timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            incoming.values(),
        )

    def prune_market_data_before(self, cutoff: date) -> None:
        """Apply the three-year market-data retention without touching reviews."""

        with self.connect() as connection:
            connection.execute(
                "DELETE FROM snapshot_securities WHERE trade_date < ?",
                (cutoff.isoformat(),),
            )
            connection.execute(
                "DELETE FROM market_snapshots WHERE trade_date < ?",
                (cutoff.isoformat(),),
            )
            connection.execute(
                "DELETE FROM daily_bars WHERE trade_date < ?",
                (cutoff.isoformat(),),
            )

    def has_market_snapshot(self, target: date, *, source: str = "tushare") -> bool:
        """Return whether an immutable snapshot is present without loading its facts."""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM market_snapshots
                WHERE trade_date = ? AND source = ?
                LIMIT 1
                """,
                (target.isoformat(), source),
            ).fetchone()
        return row is not None

    def load_market_snapshots(
        self, start: date, end: date, *, source: str = "tushare", history_limit: int = 60
    ) -> tuple[MarketSnapshot, ...]:
        if end < start:
            raise ValueError("snapshot replay range is invalid")
        return tuple(
            self.load_market_snapshot(day, source=source, history_limit=history_limit)
            for day in self.load_market_snapshot_dates(start, end, source=source)
        )

    def load_market_snapshot_dates(
        self, start: date, end: date, *, source: str = "tushare"
    ) -> tuple[date, ...]:
        if end < start:
            raise ValueError("snapshot replay range is invalid")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date
                FROM market_snapshots
                WHERE source = ? AND trade_date BETWEEN ? AND ? ORDER BY trade_date
                """,
                (source, start.isoformat(), end.isoformat()),
            ).fetchall()
        return tuple(date.fromisoformat(str(row[0])) for row in rows)

    def load_market_snapshot(
        self, target: date, *, source: str = "tushare", history_limit: int = 60
    ) -> MarketSnapshot:
        if history_limit < 1:
            raise ValueError("snapshot history limit must be positive")
        with self.connect() as connection:
            meta = connection.execute(
                """
                SELECT source_timestamp, advance_ratio_bps, above_ma20_ratio_bps
                FROM market_snapshots WHERE trade_date = ? AND source = ?
                """,
                (target.isoformat(), source),
            ).fetchone()
            if meta is None:
                raise ValueError(f"market snapshot does not exist: {target.isoformat()}")
            security_rows = connection.execute(
                """
                SELECT symbol, name, exchange, board, list_date, industry, is_st
                FROM snapshot_securities
                WHERE trade_date = ? AND source = ? ORDER BY symbol
                """,
                (target.isoformat(), source),
            ).fetchall()
            bar_rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT b.symbol, b.trade_date, b.open_1e4, b.high_1e4, b.low_1e4,
                           b.close_1e4, b.pre_close_1e4, b.volume_shares, b.amount_fen,
                           b.source, b.source_timestamp,
                           ROW_NUMBER() OVER (
                               PARTITION BY b.symbol ORDER BY b.trade_date DESC
                           ) AS history_rank
                    FROM daily_bars AS b
                    JOIN snapshot_securities AS s
                      ON s.symbol = b.symbol AND s.trade_date = ? AND s.source = ?
                    WHERE b.source = ? AND b.trade_date <= ?
                )
                SELECT symbol, trade_date, open_1e4, high_1e4, low_1e4, close_1e4,
                       pre_close_1e4, volume_shares, amount_fen, source, source_timestamp
                FROM ranked WHERE history_rank <= ? ORDER BY trade_date, symbol
                """,
                (target.isoformat(), source, source, target.isoformat(), history_limit),
            ).fetchall()
        securities = tuple(
            Security(
                symbol=row[0],
                name=row[1],
                exchange=row[2],
                board=row[3],
                list_date=date.fromisoformat(row[4]),
                industry=row[5],
                is_st=bool(row[6]),
            )
            for row in security_rows
        )
        return MarketSnapshot(
            trade_date=target,
            source=source,
            source_timestamp=datetime.fromisoformat(str(meta[0])),
            securities=securities,
            bars=tuple(self._daily_bar_from_row(row) for row in bar_rows),
            advance_ratio_bps=int(meta[1]),
            above_ma20_ratio_bps=int(meta[2]),
        )

    def save_expected_trading_days(self, source: str, days: Iterable[date]) -> None:
        """Persist the provider calendar used to judge historical coverage."""

        normalized = tuple(sorted(set(days)))
        if not source or not normalized:
            raise ValueError("expected trading-day coverage requires a source and dates")
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO expected_trading_days(source, trade_date)
                VALUES (?, ?)
                """,
                [(source, day.isoformat()) for day in normalized],
            )

    def load_expected_trading_days(
        self, start: date, end: date, *, source: str
    ) -> tuple[date, ...]:
        if end < start:
            raise ValueError("trading-day coverage range is invalid")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date FROM expected_trading_days
                WHERE source = ? AND trade_date BETWEEN ? AND ?
                ORDER BY trade_date
                """,
                (source, start.isoformat(), end.isoformat()),
            ).fetchall()
        return tuple(date.fromisoformat(str(row[0])) for row in rows)

    def save_strategy_version(self, strategy: StrategyVersion) -> None:
        parameters_json = self._json(strategy.parameters)
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT status, parameters_json FROM strategy_versions WHERE version = ?",
                (strategy.version,),
            ).fetchone()
            if existing is not None:
                if existing != (strategy.status, parameters_json):
                    raise ValueError(f"strategy version {strategy.version!r} is immutable")
                return
            connection.execute(
                "INSERT INTO strategy_versions(version, status, parameters_json) VALUES (?, ?, ?)",
                (strategy.version, strategy.status, parameters_json),
            )

    def save_strategy_proposal_with_relation(
        self, strategy: StrategyVersion, *, predecessor: str
    ) -> None:
        """Atomically persist one immutable proposal and its supersedes relation."""

        if strategy.status != "proposed":
            raise ValueError("only proposed strategy versions can be registered")
        parameters_json = self._json(strategy.parameters)
        with self._idempotent_write_connection() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM strategy_versions WHERE version = ?", (predecessor,)
                ).fetchone()
                is None
            ):
                raise ValueError("strategy relation predecessor does not exist")
            existing = connection.execute(
                "SELECT status, parameters_json FROM strategy_versions WHERE version = ?",
                (strategy.version,),
            ).fetchone()
            if existing is not None and existing != (strategy.status, parameters_json):
                raise ValueError(f"strategy version {strategy.version!r} is immutable")
            relation = connection.execute(
                """
                SELECT relation FROM strategy_version_relations
                WHERE predecessor = ? AND successor = ?
                """,
                (predecessor, strategy.version),
            ).fetchone()
            if relation is not None and relation[0] != "supersedes":
                raise ValueError("strategy version relation is immutable; conflicting relation")
            connection.execute(
                """
                INSERT OR IGNORE INTO strategy_versions(version, status, parameters_json)
                VALUES (?, ?, ?)
                """,
                (strategy.version, strategy.status, parameters_json),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO strategy_version_relations(
                    predecessor, successor, relation, created_at
                ) VALUES (?, ?, 'supersedes', ?)
                """,
                (predecessor, strategy.version, datetime.now(UTC).isoformat()),
            )

    def save_daily_price_limits(
        self, *, trade_date: date, source: str, limits: dict[str, object]
    ) -> int:
        """Persist one immutable, batch-atomic set of derived limit facts."""

        return self._save_v3_fact_batch(
            "daily_price_limits", "fact_json", trade_date, source, limits
        )

    def load_daily_price_limits(self, trade_date: date, *, source: str) -> dict[str, object]:
        return self._load_v3_fact_batch("daily_price_limits", "fact_json", trade_date, source)

    def save_v3_snapshot_features(
        self, *, trade_date: date, source: str, features: dict[str, object]
    ) -> int:
        """Persist an immutable v3 feature batch without touching legacy snapshots."""

        return self._save_v3_fact_batch(
            "v3_snapshot_features", "feature_json", trade_date, source, features
        )

    def load_v3_snapshot_features(self, trade_date: date, *, source: str) -> dict[str, object]:
        return self._load_v3_fact_batch("v3_snapshot_features", "feature_json", trade_date, source)

    def _save_v3_fact_batch(
        self,
        table: str,
        json_column: str,
        trade_date: date,
        source: str,
        facts: dict[str, object],
    ) -> int:
        if table not in {"daily_price_limits", "v3_snapshot_features"}:
            raise ValueError("unsupported v3 fact table")
        canonical = {symbol: self._json(value) for symbol, value in sorted(facts.items())}
        with self._idempotent_write_connection() as connection:
            rows = connection.execute(
                f"SELECT symbol, {json_column} FROM {table} "
                "WHERE trade_date = ? AND source = ? ORDER BY symbol",
                (trade_date.isoformat(), source),
            ).fetchall()
            existing = {str(row[0]): str(row[1]) for row in rows}
            if existing:
                if existing != canonical:
                    raise ValueError(f"{table} facts are immutable; conflicting batch")
                return 0
            connection.executemany(
                f"INSERT INTO {table}(trade_date, source, symbol, {json_column}) "
                "VALUES (?, ?, ?, ?)",
                (
                    (trade_date.isoformat(), source, symbol, encoded)
                    for symbol, encoded in canonical.items()
                ),
            )
        return len(canonical)

    def _load_v3_fact_batch(
        self, table: str, json_column: str, trade_date: date, source: str
    ) -> dict[str, object]:
        if table not in {"daily_price_limits", "v3_snapshot_features"}:
            raise ValueError("unsupported v3 fact table")
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT symbol, {json_column} FROM {table} "
                "WHERE trade_date = ? AND source = ? ORDER BY symbol",
                (trade_date.isoformat(), source),
            ).fetchall()
        return {str(row[0]): json.loads(str(row[1])) for row in rows}

    def load_strategy_version(self, version: str) -> StrategyVersion | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT version, status, parameters_json FROM strategy_versions WHERE version = ?",
                (version,),
            ).fetchone()
        if row is None:
            return None
        return StrategyVersion(version=row[0], status=row[1], parameters=json.loads(row[2]))

    def list_strategy_versions(self) -> tuple[StrategyVersion, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT version, status, parameters_json FROM strategy_versions ORDER BY version"
            ).fetchall()
        return tuple(
            StrategyVersion(version=row[0], status=row[1], parameters=json.loads(row[2]))
            for row in rows
        )

    def create_strategy_replay_job(
        self,
        *,
        strategy_version: str,
        parameters_hash: str,
        source: str,
        start_date: date,
        end_date: date,
        expected_sessions: Iterable[date],
        idempotency_key: str | None = None,
        pipeline_version: str | None = None,
        input_hash: str | None = None,
        warmup_sessions: int = 20,
        input_hash_schema: str | None = None,
        result_hash_schema: str | None = None,
        outcome_hash_schema: str | None = None,
        industry_classification_standard: str | None = None,
        industry_classification_mode: str | None = None,
        industry_classification_as_of: date | None = None,
        industry_mapping_sha256: str | None = None,
    ) -> dict[str, object]:
        """Create one durable replay attempt over an immutable strategy and calendar."""

        strategy = self.load_strategy_version(strategy_version)
        if strategy is None:
            raise ValueError(f"unknown strategy version: {strategy_version}")
        if strategy.status != "proposed":
            raise ValueError("only a proposed strategy version can start a governance replay")
        self._validate_sha256(parameters_hash, "parameters")
        stored_hash = hashlib.sha256(self._json(strategy.parameters).encode("utf-8")).hexdigest()
        if stored_hash != parameters_hash:
            raise ValueError("strategy replay parameters hash does not match the stored version")
        if not source or end_date < start_date:
            raise ValueError("strategy replay range is invalid")
        if input_hash is not None:
            self._validate_sha256(input_hash, "input")
        if warmup_sessions < 0:
            raise ValueError("strategy replay warmup sessions are invalid")
        if industry_mapping_sha256 is not None:
            self._validate_sha256(industry_mapping_sha256, "industry mapping")
        sessions = tuple(expected_sessions)
        if (
            not sessions
            or sessions != tuple(sorted(set(sessions)))
            or sessions[0] < start_date
            or sessions[-1] > end_date
        ):
            raise ValueError("strategy replay expected sessions are invalid")
        operation = "strategy:start_strategy_replay"
        request = {
            "version": strategy_version,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        request_hash = self._request_hash(operation, request)
        context = self._idempotent_write_connection() if idempotency_key else self.connect()
        job_id = f"replay-{uuid4().hex}"
        with context as connection:
            if idempotency_key:
                cached = self._idempotent_result(
                    connection, operation, idempotency_key, request_hash
                )
                if cached is not None:
                    job_id = str(cached["job_id"])
                    job = self.get_strategy_replay_job(job_id)
                    if job is None:  # pragma: no cover - protected by the same database
                        raise RuntimeError("idempotent strategy replay job disappeared")
                    return job
            connection.execute(
                """
                INSERT INTO strategy_replay_jobs(
                    job_id, strategy_version, parameters_hash, source,
                    start_date, end_date, expected_sessions_json, status, created_at,
                    pipeline_version, input_hash, warmup_sessions, input_hash_schema,
                    result_hash_schema, outcome_hash_schema, outcome_status,
                    industry_classification_standard, industry_classification_mode,
                    industry_classification_as_of, industry_mapping_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    strategy_version,
                    parameters_hash,
                    source,
                    start_date.isoformat(),
                    end_date.isoformat(),
                    self._json([session.isoformat() for session in sessions]),
                    datetime.now(UTC).isoformat(),
                    pipeline_version,
                    input_hash,
                    warmup_sessions,
                    input_hash_schema,
                    result_hash_schema,
                    outcome_hash_schema,
                    "queued" if outcome_hash_schema else None,
                    industry_classification_standard,
                    industry_classification_mode,
                    None
                    if industry_classification_as_of is None
                    else industry_classification_as_of.isoformat(),
                    industry_mapping_sha256,
                ),
            )
            if idempotency_key:
                self._save_idempotent_result(
                    connection,
                    operation,
                    idempotency_key,
                    request_hash,
                    {"job_id": job_id},
                )
        job = self.get_strategy_replay_job(job_id)
        if job is None:  # pragma: no cover - the INSERT above is authoritative
            raise RuntimeError("strategy replay job was not persisted")
        return job

    def get_strategy_replay_job(self, job_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT job_id, strategy_version, parameters_hash, source,
                       start_date, end_date, expected_sessions_json, status,
                       dataset_hash, result_hash, summary_json, error,
                       created_at, started_at, completed_at,
                       pipeline_version, input_hash, warmup_sessions,
                       input_hash_schema, result_hash_schema, outcome_hash_schema,
                       outcome_status, outcome_json, outcome_hash,
                       industry_classification_standard, industry_classification_mode,
                       industry_classification_as_of, industry_mapping_sha256
                FROM strategy_replay_jobs WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            completed_dates = {
                str(item[0])
                for item in connection.execute(
                    "SELECT trade_date FROM strategy_replay_days WHERE job_id = ?",
                    (job_id,),
                ).fetchall()
            }
            certified = (
                connection.execute(
                    "SELECT 1 FROM strategy_replay_attestations WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                is not None
            )
        return self._strategy_replay_job(row, completed_dates, certified)

    def bind_strategy_replay_start_idempotency(
        self,
        job_id: str,
        *,
        strategy_version: str,
        start_date: date,
        end_date: date,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Bind a deduplicated replay result to a request key without creating another job."""

        operation = "strategy:start_strategy_replay"
        request = {
            "version": strategy_version,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        request_hash = self._request_hash(operation, request)
        with self._idempotent_write_connection() as connection:
            cached = self._idempotent_result(connection, operation, idempotency_key, request_hash)
            if cached is None:
                if (
                    connection.execute(
                        "SELECT 1 FROM strategy_replay_jobs WHERE job_id = ?", (job_id,)
                    ).fetchone()
                    is None
                ):
                    raise ValueError(f"unknown strategy replay job: {job_id}")
                self._save_idempotent_result(
                    connection,
                    operation,
                    idempotency_key,
                    request_hash,
                    {"job_id": job_id},
                )
            else:
                job_id = str(cached["job_id"])
        job = self.get_strategy_replay_job(job_id)
        if job is None:  # pragma: no cover
            raise RuntimeError("idempotent strategy replay job disappeared")
        return job

    def list_strategy_replay_jobs(
        self, *, version: str | None = None, limit: int = 20
    ) -> tuple[dict[str, object], ...]:
        if not 1 <= limit <= 200:
            raise ValueError("strategy replay list limit is invalid")
        query = "SELECT job_id FROM strategy_replay_jobs"
        values: tuple[object, ...]
        if version is None:
            values = (limit,)
        else:
            query += " WHERE strategy_version = ?"
            values = (version, limit)
        query += " ORDER BY created_at DESC, job_id DESC LIMIT ?"
        with self.connect() as connection:
            rows = connection.execute(query, values).fetchall()
        jobs = tuple(self.get_strategy_replay_job(str(row[0])) for row in rows)
        return tuple(job for job in jobs if job is not None)

    def get_next_runnable_strategy_replay_job(self) -> dict[str, object] | None:
        """Return the running job, or the oldest queued job, without terminal-job starvation."""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT job_id FROM strategy_replay_jobs
                WHERE status IN ('running', 'queued')
                ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END,
                         created_at, job_id
                LIMIT 1
                """
            ).fetchone()
        return None if row is None else self.get_strategy_replay_job(str(row[0]))

    def get_next_pending_strategy_replay_outcome_job(
        self,
    ) -> dict[str, object] | None:
        """Return the oldest completed v3 replay whose outcome work can resume."""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT job_id FROM strategy_replay_jobs
                WHERE status = 'completed'
                  AND input_hash_schema = 'v3-input-v1'
                  AND outcome_hash_schema = 'v3-outcome-v1'
                  AND outcome_hash IS NULL
                  AND outcome_status IN ('queued', 'running')
                ORDER BY completed_at, created_at, job_id
                LIMIT 1
                """
            ).fetchone()
        return None if row is None else self.get_strategy_replay_job(str(row[0]))

    def save_strategy_replay_day(
        self,
        job_id: str,
        *,
        trade_date: date,
        input_hash: str,
        output_hash: str,
        result: object,
    ) -> dict[str, object]:
        self._validate_sha256(input_hash, "input")
        self._validate_sha256(output_hash, "output")
        result_json = self._json(result)
        with self._idempotent_write_connection() as connection:
            job = connection.execute(
                "SELECT expected_sessions_json, status FROM strategy_replay_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise ValueError(f"unknown strategy replay job: {job_id}")
            if job[1] in {"completed", "failed"}:
                raise ValueError("strategy replay job is terminal")
            expected = tuple(json.loads(str(job[0])))
            if trade_date.isoformat() not in expected:
                raise ValueError("strategy replay day is outside the expected calendar")
            existing = connection.execute(
                """
                SELECT input_hash, output_hash, result_json FROM strategy_replay_days
                WHERE job_id = ? AND trade_date = ?
                """,
                (job_id, trade_date.isoformat()),
            ).fetchone()
            values = (input_hash, output_hash, result_json)
            if existing is not None:
                if existing != values:
                    raise ValueError("strategy replay day is immutable; conflicting result")
            else:
                completed = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT trade_date FROM strategy_replay_days WHERE job_id = ?",
                        (job_id,),
                    ).fetchall()
                }
                next_expected = next(
                    (session for session in expected if session not in completed), None
                )
                if trade_date.isoformat() != next_expected:
                    raise ValueError("strategy replay day must be the next expected session")
                connection.execute(
                    """
                    INSERT INTO strategy_replay_days(
                        job_id, trade_date, input_hash, output_hash, result_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (job_id, trade_date.isoformat(), *values),
                )
            connection.execute(
                """
                UPDATE strategy_replay_jobs SET status = 'running',
                    started_at = COALESCE(started_at, ?)
                WHERE job_id = ?
                """,
                (datetime.now(UTC).isoformat(), job_id),
            )
        return {
            "job_id": job_id,
            "trade_date": trade_date,
            "status": "completed",
            "input_hash": input_hash,
            "output_hash": output_hash,
            "result": json.loads(result_json),
        }

    def list_strategy_replay_days(
        self,
        job_id: str,
        *,
        after_trade_date: date | None = None,
        limit: int | None = None,
    ) -> tuple[dict[str, object], ...]:
        if limit is not None and not 1 <= limit <= 200:
            raise ValueError("strategy replay day limit is invalid")
        query = """
            SELECT trade_date, input_hash, output_hash, result_json
            FROM strategy_replay_days WHERE job_id = ?
        """
        values: list[object] = [job_id]
        if after_trade_date is not None:
            query += " AND trade_date > ?"
            values.append(after_trade_date.isoformat())
        query += " ORDER BY trade_date"
        if limit is not None:
            query += " LIMIT ?"
            values.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, tuple(values)).fetchall()
        return tuple(
            {
                "job_id": job_id,
                "trade_date": date.fromisoformat(str(row[0])),
                "status": "completed",
                "input_hash": str(row[1]),
                "output_hash": str(row[2]),
                "result": json.loads(str(row[3])),
            }
            for row in rows
        )

    def complete_strategy_replay(
        self,
        job_id: str,
        *,
        dataset_hash: str,
        result_hash: str,
        summary: object,
        outcome: object | None = None,
        outcome_hash: str | None = None,
    ) -> dict[str, object]:
        self._validate_sha256(dataset_hash, "dataset")
        self._validate_sha256(result_hash, "result")
        if (outcome is None) != (outcome_hash is None):
            raise ValueError("strategy replay outcome and outcome hash must be recorded together")
        if outcome_hash is not None:
            self._validate_sha256(outcome_hash, "outcome")
        summary_json = self._json(summary)
        outcome_json = None if outcome is None else self._json(outcome)
        with self._idempotent_write_connection() as connection:
            row = connection.execute(
                """
                SELECT expected_sessions_json, status, dataset_hash, result_hash, summary_json,
                       outcome_json, outcome_hash
                FROM strategy_replay_jobs WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown strategy replay job: {job_id}")
            if row[1] == "failed":
                raise ValueError("failed strategy replay job is terminal")
            if row[1] == "completed":
                if tuple(row[2:]) != (
                    dataset_hash,
                    result_hash,
                    summary_json,
                    outcome_json,
                    outcome_hash,
                ):
                    raise ValueError("completed strategy replay evidence is immutable")
            else:
                expected = tuple(json.loads(str(row[0])))
                actual = tuple(
                    str(item[0])
                    for item in connection.execute(
                        """
                        SELECT trade_date FROM strategy_replay_days
                        WHERE job_id = ? ORDER BY trade_date
                        """,
                        (job_id,),
                    ).fetchall()
                )
                if actual != expected:
                    raise ValueError("strategy replay is incomplete; expected sessions are missing")
                connection.execute(
                    """
                    UPDATE strategy_replay_jobs SET status = 'completed',
                        dataset_hash = ?, result_hash = ?, summary_json = ?,
                        outcome_json = COALESCE(?, outcome_json),
                        outcome_hash = COALESCE(?, outcome_hash),
                        outcome_status = CASE
                            WHEN ? IS NULL THEN outcome_status ELSE 'completed' END,
                        error = NULL, completed_at = ? WHERE job_id = ?
                    """,
                    (
                        dataset_hash,
                        result_hash,
                        summary_json,
                        outcome_json,
                        outcome_hash,
                        outcome_hash,
                        datetime.now(UTC).isoformat(),
                        job_id,
                    ),
                )
        completed = self.get_strategy_replay_job(job_id)
        if completed is None:  # pragma: no cover
            raise RuntimeError("completed strategy replay disappeared")
        return completed

    def attach_strategy_replay_outcome(
        self, job_id: str, *, outcome: object, outcome_hash: str
    ) -> dict[str, object]:
        """Attach independently computed v3 outcome evidence without rewriting candidate proof."""

        self._validate_sha256(outcome_hash, "outcome")
        outcome_json = self._json(outcome)
        with self._idempotent_write_connection() as connection:
            row = connection.execute(
                """
                SELECT status, outcome_json, outcome_hash
                FROM strategy_replay_jobs WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown strategy replay job: {job_id}")
            if row[0] != "completed":
                raise ValueError("candidate replay must complete before outcome evidence")
            if row[1] is not None and (row[1], row[2]) != (outcome_json, outcome_hash):
                raise ValueError("strategy replay outcome is immutable; conflicting evidence")
            connection.execute(
                """
                UPDATE strategy_replay_jobs
                SET outcome_json = ?, outcome_hash = ?, outcome_status = 'completed'
                WHERE job_id = ?
                """,
                (outcome_json, outcome_hash, job_id),
            )
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """
                INSERT INTO strategy_replay_outcome_runs(
                    job_id, status, outcome_hash, summary_json, error, created_at, completed_at
                ) VALUES (?, 'completed', ?, ?, NULL, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status=excluded.status, outcome_hash=excluded.outcome_hash,
                    summary_json=excluded.summary_json, error=NULL,
                    completed_at=excluded.completed_at
                """,
                (job_id, outcome_hash, outcome_json, now, now),
            )
            if isinstance(outcome, dict):
                for candidate_id, candidate_outcome in sorted(outcome.items()):
                    encoded = self._json(candidate_outcome)
                    existing_candidate = connection.execute(
                        """
                        SELECT outcome_json FROM strategy_replay_candidate_outcomes
                        WHERE job_id = ? AND candidate_id = ?
                        """,
                        (job_id, str(candidate_id)),
                    ).fetchone()
                    if existing_candidate is not None and existing_candidate[0] != encoded:
                        raise ValueError("candidate outcome is immutable; conflicting evidence")
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO strategy_replay_candidate_outcomes(
                            job_id, candidate_id, outcome_json
                        ) VALUES (?, ?, ?)
                        """,
                        (job_id, str(candidate_id), encoded),
                    )
        attached = self.get_strategy_replay_job(job_id)
        if attached is None:  # pragma: no cover
            raise RuntimeError("strategy replay outcome disappeared")
        return attached

    def fail_strategy_replay_outcome(self, job_id: str, *, error: str) -> None:
        if not error:
            raise ValueError("outcome failure requires an error")
        now = datetime.now(UTC).isoformat()
        with self._idempotent_write_connection() as connection:
            connection.execute(
                "UPDATE strategy_replay_jobs SET outcome_status = 'failed' WHERE job_id = ?",
                (job_id,),
            )
            connection.execute(
                """
                INSERT INTO strategy_replay_outcome_runs(
                    job_id, status, error, created_at, completed_at
                ) VALUES (?, 'failed', ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET status='failed', error=excluded.error,
                    completed_at=excluded.completed_at
                """,
                (job_id, error[:1_000], now, now),
            )

    def fail_strategy_replay(self, job_id: str, *, error: str) -> dict[str, object]:
        if not error:
            raise ValueError("strategy replay failure requires an error")
        with self._idempotent_write_connection() as connection:
            row = connection.execute(
                "SELECT status, error FROM strategy_replay_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown strategy replay job: {job_id}")
            if row[0] == "completed":
                raise ValueError("completed strategy replay job is terminal")
            if row[0] == "failed" and row[1] != error:
                raise ValueError("failed strategy replay evidence is immutable")
            connection.execute(
                """
                UPDATE strategy_replay_jobs SET status = 'failed', error = ?, completed_at = ?
                WHERE job_id = ?
                """,
                (error, datetime.now(UTC).isoformat(), job_id),
            )
        failed = self.get_strategy_replay_job(job_id)
        if failed is None:  # pragma: no cover
            raise RuntimeError("failed strategy replay disappeared")
        return failed

    def requeue_interrupted_strategy_replays(self) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE strategy_replay_jobs SET status = 'queued' WHERE status = 'running'"
            )
            return int(cursor.rowcount)

    def certify_strategy_replay(
        self, job_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, object]:
        job = self.get_strategy_replay_job(job_id)
        if job is None:
            raise ValueError(f"unknown strategy replay job: {job_id}")
        sessions = tuple(job["expected_sessions"])
        strategy = self.load_strategy_version(str(job["strategy_version"]))
        is_v3 = strategy is not None and (
            strategy.parameters.get("rule_engine_version") == 3
            or str(job["strategy_version"]).startswith(("v3", "v0.3-"))
        )
        if (
            job["status"] != "completed"
            or not 1_095 <= (job["end_date"] - job["start_date"]).days <= 1_100
            or len(sessions) < 600
            or job["processed_sessions"] != len(sessions)
        ):
            raise ValueError("strategy replay governance coverage is insufficient")
        if is_v3 and (
            job["start_date"] != date(2023, 8, 8)
            or job["end_date"] != date(2026, 8, 7)
            or len(sessions) != 727
        ):
            raise ValueError("v3 strategy replay requires the fixed 727-session dataset")
        if is_v3 and (job.get("outcome_status") != "completed" or job.get("outcome_hash") is None):
            raise ValueError("v3 strategy replay outcome evidence is incomplete")
        values = (
            job["strategy_version"],
            job_id,
            job["parameters_hash"],
            job["dataset_hash"],
            job["result_hash"],
            job["start_date"].isoformat(),
            job["end_date"].isoformat(),
            len(sessions),
            job.get("outcome_hash"),
        )
        operation = "strategy:certify_strategy_replay"
        request = {"replay_id": job_id, "confirmed": True}
        request_hash = self._request_hash(operation, request)
        with self._idempotent_write_connection() as connection:
            if idempotency_key:
                cached = self._idempotent_result(
                    connection, operation, idempotency_key, request_hash
                )
                if cached is not None:
                    strategy_version = str(cached["strategy_version"])
                    attestation = self.get_strategy_replay_attestation(strategy_version)
                    if attestation is None:  # pragma: no cover
                        raise RuntimeError("idempotent replay attestation disappeared")
                    return attestation
            existing = connection.execute(
                """
                SELECT strategy_version, job_id, parameters_hash, dataset_hash,
                       result_hash, start_date, end_date, session_count, outcome_hash
                FROM strategy_replay_attestations WHERE strategy_version = ?
                """,
                (job["strategy_version"],),
            ).fetchone()
            if existing is not None and existing != values:
                raise ValueError("strategy replay attestation is immutable; conflicting proof")
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO strategy_replay_attestations(
                        strategy_version, job_id, parameters_hash, dataset_hash,
                        result_hash, start_date, end_date, session_count, outcome_hash,
                        certified_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*values, datetime.now(UTC).isoformat()),
                )
            if idempotency_key:
                self._save_idempotent_result(
                    connection,
                    operation,
                    idempotency_key,
                    request_hash,
                    {"strategy_version": job["strategy_version"]},
                )
        attestation = self.get_strategy_replay_attestation(str(job["strategy_version"]))
        if attestation is None:  # pragma: no cover
            raise RuntimeError("strategy replay attestation was not persisted")
        return attestation

    def get_strategy_replay_attestation(self, strategy_version: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT strategy_version, job_id, parameters_hash, dataset_hash,
                       result_hash, start_date, end_date, session_count, outcome_hash,
                       certified_at
                FROM strategy_replay_attestations WHERE strategy_version = ?
                """,
                (strategy_version,),
            ).fetchone()
        if row is None:
            return None
        return {
            "strategy_version": str(row[0]),
            "job_id": str(row[1]),
            "parameters_hash": str(row[2]),
            "dataset_hash": str(row[3]),
            "result_hash": str(row[4]),
            "start_date": date.fromisoformat(str(row[5])),
            "end_date": date.fromisoformat(str(row[6])),
            "session_count": int(row[7]),
            "outcome_hash": None if row[8] is None else str(row[8]),
            "certified_at": datetime.fromisoformat(str(row[9])),
        }

    def record_verified_strategy_replay_attestation(
        self,
        *,
        strategy_version: str,
        parameters_hash: str,
        dataset_hash: str,
        result_hash: str,
        outcome_hash: str,
    ) -> dict[str, object]:
        """Record a pre-verified v3 proof for migration/tests without consuming it."""

        for label, value in (
            ("parameters", parameters_hash),
            ("dataset", dataset_hash),
            ("result", result_hash),
            ("outcome", outcome_hash),
        ):
            self._validate_sha256(value, label)
        strategy = self.load_strategy_version(strategy_version)
        if strategy is None:
            raise ValueError(f"unknown strategy version: {strategy_version}")
        stored_hash = hashlib.sha256(self._json(strategy.parameters).encode("utf-8")).hexdigest()
        if stored_hash != parameters_hash:
            raise ValueError("strategy replay parameters hash does not match the stored version")
        job_id = f"verified-{strategy_version}"
        now = datetime.now(UTC).isoformat()
        values = (
            strategy_version,
            job_id,
            parameters_hash,
            dataset_hash,
            result_hash,
            date.today().isoformat(),
            date.today().isoformat(),
            0,
            outcome_hash,
        )
        with self._idempotent_write_connection() as connection:
            existing = connection.execute(
                """
                SELECT strategy_version, job_id, parameters_hash, dataset_hash,
                       result_hash, start_date, end_date, session_count, outcome_hash
                FROM strategy_replay_attestations WHERE strategy_version = ?
                """,
                (strategy_version,),
            ).fetchone()
            if existing is not None and existing != values:
                raise ValueError("strategy replay attestation is immutable; conflicting proof")
            connection.execute(
                """
                INSERT OR IGNORE INTO strategy_replay_jobs(
                    job_id, strategy_version, parameters_hash, source, start_date, end_date,
                    expected_sessions_json, status, dataset_hash, result_hash, outcome_status,
                    outcome_hash, created_at, completed_at
                ) VALUES (?, ?, ?, 'verified-local', ?, ?, '[]', 'completed', ?, ?,
                          'completed', ?, ?, ?)
                """,
                (
                    job_id,
                    strategy_version,
                    parameters_hash,
                    date.today().isoformat(),
                    date.today().isoformat(),
                    dataset_hash,
                    result_hash,
                    outcome_hash,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO strategy_replay_attestations(
                    strategy_version, job_id, parameters_hash, dataset_hash, result_hash,
                    start_date, end_date, session_count, outcome_hash, certified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*values, now),
            )
        proof = self.get_strategy_replay_attestation(strategy_version)
        if proof is None:  # pragma: no cover
            raise RuntimeError("verified replay attestation disappeared")
        return proof

    def set_active_strategy_version(self, version: str) -> None:
        if self.load_strategy_version(version) is None:
            raise ValueError(f"unknown strategy version: {version}")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO active_strategy(singleton, version) VALUES (1, ?)
                ON CONFLICT(singleton) DO UPDATE SET version=excluded.version
                """,
                (version,),
            )

    def approve_strategy_version(self, version: str) -> None:
        strategy = self.load_strategy_version(version)
        if strategy is None:
            raise ValueError(f"unknown strategy version: {version}")
        parameters_hash = hashlib.sha256(
            self._json(strategy.parameters).encode("utf-8")
        ).hexdigest()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO strategy_approvals(version, parameters_hash, approved_at)
                VALUES (?, ?, ?)
                ON CONFLICT(version) DO UPDATE SET
                    parameters_hash=excluded.parameters_hash,
                    approved_at=excluded.approved_at
                """,
                (version, parameters_hash, datetime.now(UTC).isoformat()),
            )

    def consume_strategy_approval(self, version: str) -> bool:
        strategy = self.load_strategy_version(version)
        if strategy is None:
            return False
        expected = hashlib.sha256(self._json(strategy.parameters).encode("utf-8")).hexdigest()
        with self._idempotent_write_connection() as connection:
            row = connection.execute(
                "SELECT parameters_hash FROM strategy_approvals WHERE version = ?",
                (version,),
            ).fetchone()
            if row is None or row[0] != expected:
                return False
            connection.execute("DELETE FROM strategy_approvals WHERE version = ?", (version,))
            return True

    def record_replay_attestation(self, version: str, parameters_hash: str) -> None:
        """Record a legacy non-governance replay marker.

        Activation deliberately rejects this two-field form.  Production uses
        :meth:`record_governance_replay_attestation`, which binds the proof to
        one complete immutable dataset.
        """
        if self.load_strategy_version(version) is None:
            raise ValueError(f"unknown strategy version: {version}")
        if len(parameters_hash) != 64 or any(
            character not in "0123456789abcdef" for character in parameters_hash
        ):
            raise ValueError("replay attestation requires a lowercase SHA-256 hash")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO replay_attestations(version, parameters_hash, recorded_at)
                VALUES (?, ?, ?)
                ON CONFLICT(version) DO UPDATE SET
                    parameters_hash=excluded.parameters_hash,
                    recorded_at=excluded.recorded_at
                """,
                (version, parameters_hash, datetime.now(UTC).isoformat()),
            )

    def record_governance_replay_attestation(
        self,
        version: str,
        parameters_hash: str,
        dataset_hash: str,
        start: date,
        end: date,
        session_count: int,
    ) -> None:
        if self.load_strategy_version(version) is None:
            raise ValueError(f"unknown strategy version: {version}")
        for label, value in (("parameters", parameters_hash), ("dataset", dataset_hash)):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"replay attestation requires a lowercase {label} SHA-256 hash")
        if end < start or session_count < 400:
            raise ValueError("governance replay coverage is insufficient")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO replay_attestations(
                    version, parameters_hash, recorded_at, dataset_hash,
                    start_date, end_date, session_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(version) DO UPDATE SET
                    parameters_hash=excluded.parameters_hash,
                    recorded_at=excluded.recorded_at,
                    dataset_hash=excluded.dataset_hash,
                    start_date=excluded.start_date,
                    end_date=excluded.end_date,
                    session_count=excluded.session_count
                """,
                (
                    version,
                    parameters_hash,
                    datetime.now(UTC).isoformat(),
                    dataset_hash,
                    start.isoformat(),
                    end.isoformat(),
                    session_count,
                ),
            )

    def consume_replay_attestation(self, version: str, parameters_hash: str) -> bool:
        with self._idempotent_write_connection() as connection:
            row = connection.execute(
                """
                SELECT parameters_hash, dataset_hash
                FROM strategy_replay_attestations WHERE strategy_version = ?
                """,
                (version,),
            ).fetchone()
            return not (row is None or row[0] != parameters_hash or row[1] is None)

    def consume_strategy_activation_grants(self, version: str, parameters_hash: str) -> str:
        """Atomically consume approval while retaining the permanent replay proof."""

        with self._idempotent_write_connection() as connection:
            approval = connection.execute(
                "SELECT parameters_hash FROM strategy_approvals WHERE version = ?",
                (version,),
            ).fetchone()
            if approval is None or approval[0] != parameters_hash:
                return "operator_approval_required"
            replay = connection.execute(
                """
                SELECT parameters_hash, dataset_hash
                FROM strategy_replay_attestations WHERE strategy_version = ?
                """,
                (version,),
            ).fetchone()
            if replay is None or replay[0] != parameters_hash or replay[1] is None:
                return "replay_attestation_required"
            connection.execute("DELETE FROM strategy_approvals WHERE version = ?", (version,))
            return "ok"

    def activate_strategy_version_with_grants(self, version: str, parameters_hash: str) -> str:
        """Consume both grants and update the active pointer in one transaction."""

        with self._idempotent_write_connection() as connection:
            lifecycle = connection.execute(
                """
                SELECT event_type FROM strategy_lifecycle_events
                WHERE version = ? ORDER BY event_id DESC LIMIT 1
                """,
                (version,),
            ).fetchone()
            if lifecycle is not None and lifecycle[0] == "superseded":
                raise ValueError("a superseded strategy version cannot be reactivated")
            strategy = connection.execute(
                "SELECT parameters_json FROM strategy_versions WHERE version = ?",
                (version,),
            ).fetchone()
            if strategy is None:
                return "strategy_version_not_found"
            stored_hash = hashlib.sha256(str(strategy[0]).encode("utf-8")).hexdigest()
            if stored_hash != parameters_hash:
                return "strategy_parameters_changed"
            approval = connection.execute(
                "SELECT parameters_hash FROM strategy_approvals WHERE version = ?",
                (version,),
            ).fetchone()
            if approval is None or approval[0] != parameters_hash:
                return "operator_approval_required"
            parameters = json.loads(str(strategy[0]))
            is_v3 = parameters.get("rule_engine_version") == 3 or version.startswith(
                ("v3", "v0.3-")
            )
            replay = connection.execute(
                """
                SELECT a.parameters_hash, a.dataset_hash, a.result_hash, a.outcome_hash,
                       a.job_id, a.start_date, a.end_date, a.session_count,
                       j.status, j.dataset_hash, j.result_hash, j.outcome_hash,
                       j.input_hash_schema,
                       (SELECT COUNT(*) FROM strategy_replay_days d
                        WHERE d.job_id = a.job_id)
                FROM strategy_replay_attestations a
                LEFT JOIN strategy_replay_jobs j ON j.job_id = a.job_id
                WHERE a.strategy_version = ?
                """,
                (version,),
            ).fetchone()
            if replay is None or replay[0] != parameters_hash or replay[1] is None:
                return "replay_attestation_required"
            if is_v3 and replay[3] is None:
                return "replay_outcome_required"
            if is_v3 and (
                replay[5] != "2023-08-08"
                or replay[6] != "2026-08-07"
                or replay[7] != 727
                or replay[8] != "completed"
                or replay[9] != replay[1]
                or replay[10] != replay[2]
                or replay[11] != replay[3]
                or replay[12] != "v3-input-v1"
                or replay[13] != 727
            ):
                return "replay_attestation_required"
            previous = connection.execute(
                "SELECT version FROM active_strategy WHERE singleton = 1"
            ).fetchone()
            connection.execute(
                """
                INSERT INTO active_strategy(singleton, version) VALUES (1, ?)
                ON CONFLICT(singleton) DO UPDATE SET version=excluded.version
                """,
                (version,),
            )
            relation = connection.execute(
                """
                SELECT predecessor FROM strategy_version_relations
                WHERE successor = ? AND relation = 'supersedes'
                ORDER BY predecessor LIMIT 1
                """,
                (version,),
            ).fetchone()
            predecessor = (
                str(relation[0])
                if relation is not None
                else None
                if previous is None
                else str(previous[0])
            )
            if is_v3 and predecessor is not None and predecessor != version:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO strategy_version_relations(
                        predecessor, successor, relation, created_at
                    ) VALUES (?, ?, 'supersedes', ?)
                    """,
                    (predecessor, version, datetime.now(UTC).isoformat()),
                )
                connection.execute(
                    """
                    INSERT INTO strategy_lifecycle_events(
                        version, event_type, occurred_at, detail, related_version
                    ) VALUES (?, 'superseded', ?, 'activated successor', ?)
                    """,
                    (predecessor, datetime.now(UTC).isoformat(), version),
                )
                connection.execute(
                    """
                    INSERT INTO strategy_lifecycle_events(
                        version, event_type, occurred_at, detail, related_version
                    ) VALUES (?, 'active', ?, 'superseded predecessor', ?)
                    """,
                    (version, datetime.now(UTC).isoformat(), predecessor),
                )
            connection.execute("DELETE FROM strategy_approvals WHERE version = ?", (version,))
            return "ok"

    def save_strategy_version_relation(
        self, *, predecessor: str, successor: str, relation: str
    ) -> None:
        if relation not in {"supersedes", "derived_from"}:
            raise ValueError("strategy version relation is unsupported")
        with self._idempotent_write_connection() as connection:
            existing = connection.execute(
                """
                SELECT relation FROM strategy_version_relations
                WHERE predecessor = ? AND successor = ?
                """,
                (predecessor, successor),
            ).fetchone()
            if existing is not None and existing[0] != relation:
                raise ValueError("strategy version relation is immutable; conflicting relation")
            connection.execute(
                """
                INSERT OR IGNORE INTO strategy_version_relations(
                    predecessor, successor, relation, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (predecessor, successor, relation, datetime.now(UTC).isoformat()),
            )

    def list_strategy_version_relations(self, successor: str) -> tuple[dict[str, str], ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT predecessor, successor, relation FROM strategy_version_relations
                WHERE successor = ? ORDER BY predecessor, relation
                """,
                (successor,),
            ).fetchall()
        return tuple(
            {"predecessor": str(row[0]), "successor": str(row[1]), "relation": str(row[2])}
            for row in rows
        )

    def append_strategy_lifecycle_event(
        self,
        *,
        version: str,
        event_type: str,
        occurred_at: datetime,
        detail: str,
        related_version: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO strategy_lifecycle_events(
                    version, event_type, occurred_at, detail, related_version
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (version, event_type, occurred_at.isoformat(), detail, related_version),
            )

    def list_strategy_lifecycle_events(self, version: str) -> tuple[dict[str, object], ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT event_type, occurred_at, detail, related_version
                FROM strategy_lifecycle_events WHERE version = ? ORDER BY event_id
                """,
                (version,),
            ).fetchall()
        return tuple(
            {
                "version": version,
                "event_type": str(row[0]),
                "occurred_at": datetime.fromisoformat(str(row[1])),
                "detail": str(row[2]),
                "related_version": None if row[3] is None else str(row[3]),
            }
            for row in rows
        )

    def get_strategy_lifecycle_state(self, version: str) -> str:
        events = self.list_strategy_lifecycle_events(version)
        if events:
            return str(events[-1]["event_type"])
        strategy = self.load_strategy_version(version)
        if strategy is None:
            raise ValueError(f"unknown strategy version: {version}")
        active = self.get_active_strategy_version()
        if active is not None and active.version == version:
            return "active"
        return strategy.status

    def get_strategy_superseded_by(self, version: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT related_version FROM strategy_lifecycle_events
                WHERE version = ? AND event_type = 'superseded'
                ORDER BY event_id DESC LIMIT 1
                """,
                (version,),
            ).fetchone()
        return None if row is None or row[0] is None else str(row[0])

    def get_active_strategy_version(self) -> StrategyVersion | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT version FROM active_strategy WHERE singleton = 1"
            ).fetchone()
        return None if row is None else self.load_strategy_version(row[0])

    def save_daily_review(self, review: DailyReview) -> None:
        with self.connect() as connection:
            self._save_daily_review(connection, review)

    def _save_daily_review(self, connection: sqlite3.Connection, review: DailyReview) -> None:
        existing = self._load_daily_review(connection, review.trade_date, review.strategy_version)
        if existing is not None:
            if existing != review:
                raise ValueError("daily reviews are immutable once published")
            return
        connection.execute(
            """
            INSERT INTO daily_reviews(
                trade_date, strategy_version, status, source, source_timestamp, market_regime
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                review.trade_date.isoformat(),
                review.strategy_version,
                review.status,
                review.source,
                review.source_timestamp.isoformat(),
                review.market_regime.value,
            ),
        )
        for candidate in review.candidates:
            if candidate.strategy_version != review.strategy_version:
                raise ValueError("candidate strategy version must match its daily review")
            connection.execute(
                """
                INSERT INTO candidates(
                    candidate_id, trade_date, strategy_version, symbol, name, rank, score,
                    setup_type, confirmation_condition, invalidation_condition
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.candidate_id,
                    review.trade_date.isoformat(),
                    review.strategy_version,
                    candidate.symbol,
                    candidate.name,
                    candidate.rank,
                    candidate.score,
                    candidate.setup_type.value,
                    candidate.confirmation_condition,
                    candidate.invalidation_condition,
                ),
            )
            connection.executemany(
                """
                INSERT INTO candidate_evidence(
                    candidate_id, ordinal, metric, value_json, threshold_json, passed,
                    score_contribution
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        candidate.candidate_id,
                        ordinal,
                        evidence.metric,
                        self._json(evidence.value),
                        self._json(evidence.threshold),
                        int(evidence.passed),
                        evidence.score_contribution,
                    )
                    for ordinal, evidence in enumerate(candidate.evidence)
                ],
            )

    def load_daily_review(self, trade_date: date, strategy_version: str) -> DailyReview | None:
        with self.connect() as connection:
            return self._load_daily_review(connection, trade_date, strategy_version)

    def get_daily_review(self, trade_date: date) -> DailyReview | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT strategy_version FROM daily_reviews
                WHERE trade_date = ? AND status IN ('published', 'ready')
                ORDER BY
                    CASE status WHEN 'published' THEN 0 ELSE 1 END,
                    strategy_version DESC
                LIMIT 1
                """,
                (trade_date.isoformat(),),
            ).fetchone()
            if row is None:
                return None
            return self._load_daily_review(connection, trade_date, row[0])

    def save_pipeline_run(self, run: object) -> None:
        """Atomically persist market facts, a published review and run metadata."""

        from .pipeline import PipelineRun

        if not isinstance(run, PipelineRun):
            raise TypeError("run must be a PipelineRun")
        if run.status == "ready" and run.review is None:
            raise ValueError("a ready pipeline run requires a review")
        if run.snapshot is not None and run.snapshot.trade_date != run.trade_date:
            raise ValueError("pipeline snapshot date must match the run")
        if run.review is not None and run.review.trade_date != run.trade_date:
            raise ValueError("pipeline review date must match the run")
        stored_review = None
        if run.review is not None:
            visibility = "published" if run.status == "ready" else "observation"
            stored_review = replace(run.review, status=visibility)
        with self.connect() as connection:
            existing = connection.execute(
                """
                SELECT status, attempts, strategy_version, error
                FROM pipeline_runs WHERE trade_date = ? AND pipeline_version = ?
                """,
                (run.trade_date.isoformat(), run.pipeline_version),
            ).fetchone()
            strategy_version = None if stored_review is None else stored_review.strategy_version
            values = (run.status, run.attempts, strategy_version, run.error)
            if existing is not None and existing[0] != "failed" and existing != values:
                raise ValueError("terminal pipeline runs are immutable")
            if run.snapshot is not None:
                self._save_market_snapshot(connection, run.snapshot)
                self._save_snapshot_price_limits(connection, run.snapshot)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO expected_trading_days(source, trade_date)
                    VALUES (?, ?)
                    """,
                    (run.snapshot.source, run.trade_date.isoformat()),
                )
                cutoff = (run.trade_date - timedelta(days=3 * 366)).isoformat()
                connection.execute(
                    "DELETE FROM snapshot_securities WHERE trade_date < ?", (cutoff,)
                )
                connection.execute("DELETE FROM market_snapshots WHERE trade_date < ?", (cutoff,))
                connection.execute("DELETE FROM daily_bars WHERE trade_date < ?", (cutoff,))
            if stored_review is not None:
                self._save_daily_review(connection, stored_review)
            connection.execute(
                """
                INSERT INTO pipeline_runs(
                    trade_date, pipeline_version, status, attempts, strategy_version, error
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, pipeline_version) DO UPDATE SET
                    status=excluded.status,
                    attempts=excluded.attempts,
                    strategy_version=excluded.strategy_version,
                    error=excluded.error
                """,
                (
                    run.trade_date.isoformat(),
                    run.pipeline_version,
                    *values,
                ),
            )

    def _save_snapshot_price_limits(
        self, connection: sqlite3.Connection, snapshot: MarketSnapshot
    ) -> None:
        """Persist deterministic same-day limit facts with a live normalized snapshot."""

        from .v3 import derive_daily_price_limit

        security_by_symbol = {security.symbol: security for security in snapshot.securities}
        facts: dict[str, str] = {}
        for bar in snapshot.bars:
            if bar.trade_date != snapshot.trade_date or bar.source != snapshot.source:
                continue
            security = security_by_symbol.get(bar.symbol)
            if security is None:
                continue
            fact = derive_daily_price_limit(bar, security)
            facts[bar.symbol] = self._json(
                {
                    "limit_up_1e4": fact.up_limit_1e4,
                    "limit_down_1e4": fact.down_limit_1e4,
                    "touched_up": fact.touched_up,
                    "touched_down": fact.touched_down,
                    "policy_exception": fact.policy_exception,
                    "algorithm": fact.algorithm,
                }
            )
        existing_rows = connection.execute(
            """
            SELECT symbol, fact_json FROM daily_price_limits
            WHERE trade_date = ? AND source = ? ORDER BY symbol
            """,
            (snapshot.trade_date.isoformat(), snapshot.source),
        ).fetchall()
        existing = {str(row[0]): str(row[1]) for row in existing_rows}
        if existing:
            if existing != facts:
                raise ValueError("daily_price_limits facts are immutable; conflicting batch")
            return
        connection.executemany(
            """
            INSERT INTO daily_price_limits(trade_date, source, symbol, fact_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                (snapshot.trade_date.isoformat(), snapshot.source, symbol, encoded)
                for symbol, encoded in sorted(facts.items())
            ),
        )

    def load_pipeline_run(self, trade_date: date, pipeline_version: str) -> object | None:
        from .pipeline import PipelineRun

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT status, attempts, strategy_version, error
                FROM pipeline_runs WHERE trade_date = ? AND pipeline_version = ?
                """,
                (trade_date.isoformat(), pipeline_version),
            ).fetchone()
            if row is None:
                if pipeline_version != "pipeline-v0.1":
                    return None
                review = self.get_daily_review(trade_date)
                if review is None:
                    return None
                return PipelineRun(
                    trade_date=trade_date,
                    pipeline_version=pipeline_version,
                    status="ready",
                    attempts=0,
                    snapshot=None,
                    review=review,
                )
            review = (
                None
                if row[2] is None
                else self._load_daily_review(connection, trade_date, str(row[2]))
            )
        if row[0] == "ready" and review is None:
            raise ValueError("ready pipeline run has no persisted review")
        return PipelineRun(
            trade_date=trade_date,
            pipeline_version=pipeline_version,
            status=str(row[0]),
            attempts=int(row[1]),
            snapshot=None,
            review=review,
            error=None if row[3] is None else str(row[3]),
        )

    def save_schedule_outcome_record(
        self,
        *,
        trade_date: date,
        status: str,
        next_at: datetime | None,
        pipeline_version: str | None,
        error: str | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO schedule_outcomes(
                    trade_date, status, next_at, pipeline_version, error
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    status=excluded.status,
                    next_at=excluded.next_at,
                    pipeline_version=excluded.pipeline_version,
                    error=excluded.error
                """,
                (
                    trade_date.isoformat(),
                    status,
                    None if next_at is None else next_at.isoformat(),
                    pipeline_version,
                    error,
                ),
            )

    def load_schedule_outcome_record(self, trade_date: date) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT status, next_at, pipeline_version, error
                FROM schedule_outcomes WHERE trade_date = ?
                """,
                (trade_date.isoformat(),),
            ).fetchone()
        if row is None:
            return None
        return {
            "status": str(row[0]),
            "next_at": None if row[1] is None else datetime.fromisoformat(str(row[1])),
            "pipeline_version": None if row[2] is None else str(row[2]),
            "error": None if row[3] is None else str(row[3]),
        }

    def get_publication_status(self, trade_date: date) -> dict[str, object] | None:
        record = self.load_schedule_outcome_record(trade_date)
        if record is None:
            with self.connect() as connection:
                row = connection.execute(
                    """
                    SELECT status, error FROM pipeline_runs
                    WHERE trade_date = ? ORDER BY pipeline_version DESC LIMIT 1
                    """,
                    (trade_date.isoformat(),),
                ).fetchone()
            if row is None:
                return None
            record = {
                "status": str(row[0]),
                "next_at": None,
                "pipeline_version": None,
                "error": None if row[1] is None else str(row[1]),
            }
        return {"trade_date": trade_date, **record}

    def count_live_observation_sessions(self, pipeline_version: str) -> int:
        with self.connect() as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM pipeline_runs
                    WHERE pipeline_version = ?
                      AND status = 'degraded_observation'
                      AND strategy_version IS NOT NULL
                    """,
                    (pipeline_version,),
                ).fetchone()[0]
            )

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT c.candidate_id, c.symbol, c.name, c.rank, c.score, c.setup_type,
                       c.strategy_version, c.confirmation_condition, c.invalidation_condition
                FROM candidates c
                JOIN daily_reviews r
                  ON r.trade_date = c.trade_date
                 AND r.strategy_version = c.strategy_version
                WHERE c.candidate_id = ? AND r.status IN ('published', 'ready')
                """,
                (candidate_id,),
            ).fetchone()
            if row is None:
                return None
            return self._candidate_from_row(connection, row)

    def get_candidate_context(self, candidate_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT c.trade_date, c.strategy_version, c.symbol, r.source
                FROM candidates c
                JOIN daily_reviews r
                  ON r.trade_date = c.trade_date
                 AND r.strategy_version = c.strategy_version
                WHERE c.candidate_id = ? AND r.status IN ('published', 'ready')
                """,
                (candidate_id,),
            ).fetchone()
            if row is None:
                return None
            trade_date_value = date.fromisoformat(str(row[0]))
            review = self._load_daily_review(connection, trade_date_value, str(row[1]))
            security = connection.execute(
                """
                SELECT industry FROM snapshot_securities
                WHERE trade_date = ? AND source = ? AND symbol = ?
                """,
                (row[0], row[3], row[2]),
            ).fetchone()
            industry = "" if security is None else str(security[0])
            peer_count = (
                0
                if not industry
                else int(
                    connection.execute(
                        """
                    SELECT COUNT(*) FROM snapshot_securities
                    WHERE trade_date = ? AND source = ? AND industry = ?
                      AND board = 'MAIN' AND is_st = 0
                    """,
                        (row[0], row[3], industry),
                    ).fetchone()[0]
                )
            )
            evidence = connection.execute(
                """
                SELECT value_json FROM candidate_evidence
                WHERE candidate_id = ? AND metric = 'industry_strength_bps'
                ORDER BY ordinal LIMIT 1
                """,
                (candidate_id,),
            ).fetchone()
        if review is None:
            return None
        strength = None if evidence is None else json.loads(evidence[0])
        if not isinstance(strength, int) or isinstance(strength, bool):
            strength = None
        return {
            "review": review,
            "industry_context": {
                "industry": industry,
                "industry_strength_bps": strength,
                "eligible_peer_count": peer_count,
            },
        }

    def list_review_history(self) -> tuple[DailyReview, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date, strategy_version FROM daily_reviews
                WHERE status IN ('published', 'ready')
                ORDER BY trade_date DESC, strategy_version DESC
                """
            ).fetchall()
            return tuple(
                review
                for row in rows
                if (
                    review := self._load_daily_review(
                        connection, date.fromisoformat(row[0]), row[1]
                    )
                )
                is not None
            )

    def list_watchlists(self) -> tuple[str, ...]:
        with self.connect() as connection:
            rows = connection.execute("SELECT name FROM watchlists ORDER BY name").fetchall()
        return tuple(row[0] for row in rows)

    def get_watchlist(self, name: str) -> tuple[str, ...] | None:
        with self.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM watchlists WHERE name = ?", (name,)
            ).fetchone()
            if exists is None:
                return None
            rows = connection.execute(
                """
                SELECT symbol FROM watchlist_items
                WHERE watchlist_name = ? ORDER BY ordinal
                """,
                (name,),
            ).fetchall()
        return tuple(row[0] for row in rows)

    def create_watchlist(self, *, name: str, idempotency_key: str) -> tuple[str, ...]:
        operation = "create_watchlist"
        request_hash = self._request_hash(operation, {"name": name})
        with self._idempotent_write_connection() as connection:
            cached = self._idempotent_result(connection, operation, idempotency_key, request_hash)
            if cached is not None:
                return tuple(cached)
            connection.execute(
                "INSERT OR IGNORE INTO watchlists(name, created_at) VALUES (?, ?)",
                (name, datetime.now(UTC).isoformat()),
            )
            result = self._watchlist_items(connection, name)
            self._save_idempotent_result(
                connection, operation, idempotency_key, request_hash, result
            )
            return result

    def add_watchlist_items(
        self, *, name: str, symbols: tuple[str, ...], idempotency_key: str
    ) -> tuple[str, ...] | None:
        operation = "add_watchlist_items"
        request_hash = self._request_hash(operation, {"name": name, "symbols": symbols})
        with self._idempotent_write_connection() as connection:
            cached = self._idempotent_result(connection, operation, idempotency_key, request_hash)
            if cached is not None:
                return tuple(cached)
            if not self._watchlist_exists(connection, name):
                return None
            result = list(self._watchlist_items(connection, name))
            for symbol in symbols:
                if symbol in result:
                    continue
                result.append(symbol)
                connection.execute(
                    """
                    INSERT INTO watchlist_items(watchlist_name, symbol, ordinal)
                    VALUES (?, ?, ?)
                    """,
                    (name, symbol, len(result)),
                )
                connection.execute(
                    """
                    INSERT INTO watchlist_events(symbol, event_type, occurred_at, detail)
                    VALUES (?, 'added', ?, ?)
                    """,
                    (symbol, datetime.now(UTC).isoformat(), name),
                )
            final = tuple(result)
            self._save_idempotent_result(
                connection, operation, idempotency_key, request_hash, final
            )
            return final

    def remove_watchlist_items(
        self, *, name: str, symbols: tuple[str, ...], idempotency_key: str
    ) -> tuple[str, ...] | None:
        operation = "remove_watchlist_items"
        request_hash = self._request_hash(operation, {"name": name, "symbols": symbols})
        with self._idempotent_write_connection() as connection:
            cached = self._idempotent_result(connection, operation, idempotency_key, request_hash)
            if cached is not None:
                return tuple(cached)
            if not self._watchlist_exists(connection, name):
                return None
            removed = set(symbols)
            current = self._watchlist_items(connection, name)
            for symbol in current:
                if symbol in removed:
                    connection.execute(
                        "DELETE FROM watchlist_items WHERE watchlist_name = ? AND symbol = ?",
                        (name, symbol),
                    )
                    connection.execute(
                        """
                        INSERT INTO watchlist_events(symbol, event_type, occurred_at, detail)
                        VALUES (?, 'removed', ?, ?)
                        """,
                        (symbol, datetime.now(UTC).isoformat(), name),
                    )
            final = tuple(symbol for symbol in current if symbol not in removed)
            for ordinal, symbol in enumerate(final, 1):
                connection.execute(
                    """
                    UPDATE watchlist_items SET ordinal = ?
                    WHERE watchlist_name = ? AND symbol = ?
                    """,
                    (ordinal, name, symbol),
                )
            self._save_idempotent_result(
                connection, operation, idempotency_key, request_hash, final
            )
            return final

    def record_candidate_event(
        self,
        *,
        candidate_id: str,
        status: str | None = None,
        event_date: date | None = None,
        price_1e4: int | None = None,
        reason: str | None = None,
        event_type: str | None = None,
        detail: str | None = None,
        idempotency_key: str,
    ) -> dict[str, object] | None:
        status = status or event_type
        reason = reason or detail
        event_date = event_date or datetime.now(UTC).date()
        if status not in {"watched", "bought", "skipped", "exited", "observed"}:
            raise ValueError("candidate event status is unsupported")
        if not reason:
            raise ValueError("candidate event reason is required")
        if price_1e4 is not None and price_1e4 <= 0:
            raise ValueError("candidate event price must be positive")
        operation = "record_candidate_event"
        request = {
            "candidate_id": candidate_id,
            "status": status,
            "event_date": event_date.isoformat(),
            "price_1e4": price_1e4,
            "reason": reason,
        }
        request_hash = self._request_hash(operation, request)
        with self._idempotent_write_connection() as connection:
            cached = self._idempotent_result(connection, operation, idempotency_key, request_hash)
            if cached is not None:
                return dict(cached)
            if (
                connection.execute(
                    """
                    SELECT 1 FROM candidates c
                    JOIN daily_reviews r
                      ON r.trade_date = c.trade_date
                     AND r.strategy_version = c.strategy_version
                    WHERE c.candidate_id = ? AND r.status IN ('published', 'ready')
                    """,
                    (candidate_id,),
                ).fetchone()
                is None
            ):
                return None
            occurred_at = datetime.now(UTC).isoformat()
            result = {
                "candidate_id": candidate_id,
                "status": status,
                "event_date": event_date.isoformat(),
                "price_1e4": price_1e4,
                "reason": reason,
            }
            connection.execute(
                """
                INSERT INTO candidate_events(
                    candidate_id, event_type, occurred_at, detail, idempotency_key,
                    status, event_date, price_1e4, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    status,
                    occurred_at,
                    reason,
                    idempotency_key,
                    status,
                    event_date.isoformat(),
                    price_1e4,
                    reason,
                ),
            )
            self._save_idempotent_result(
                connection, operation, idempotency_key, request_hash, result
            )
            return result

    def record_review_note(
        self, *, trade_date: date, note: str, idempotency_key: str
    ) -> dict[str, str] | None:
        operation = "record_review_note"
        request_hash = self._request_hash(
            operation,
            {"trade_date": trade_date.isoformat(), "note": note},
        )
        with self._idempotent_write_connection() as connection:
            cached = self._idempotent_result(connection, operation, idempotency_key, request_hash)
            if cached is not None:
                return dict(cached)
            if (
                connection.execute(
                    """
                    SELECT 1 FROM daily_reviews
                    WHERE trade_date = ? AND status IN ('published', 'ready') LIMIT 1
                    """,
                    (trade_date.isoformat(),),
                ).fetchone()
                is None
            ):
                return None
            occurred_at = datetime.now(UTC).isoformat()
            result = {
                "trade_date": trade_date.isoformat(),
                "note": note,
                "occurred_at": occurred_at,
            }
            connection.execute(
                """
                INSERT INTO review_notes(trade_date, note, occurred_at, idempotency_key)
                VALUES (?, ?, ?, ?)
                """,
                (trade_date.isoformat(), note, occurred_at, idempotency_key),
            )
            self._save_idempotent_result(
                connection, operation, idempotency_key, request_hash, result
            )
            return result

    def list_review_notes(self, trade_date: date) -> tuple[dict[str, str], ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date, note, occurred_at FROM review_notes
                WHERE trade_date = ? ORDER BY note_id
                """,
                (trade_date.isoformat(),),
            ).fetchall()
        return tuple({"trade_date": row[0], "note": row[1], "occurred_at": row[2]} for row in rows)

    def append_watchlist_event(
        self, *, symbol: str, event_type: str, occurred_at: datetime, detail: str
    ) -> None:
        self._append_event("watchlist_events", "symbol", symbol, event_type, occurred_at, detail)

    def list_watchlist_events(self, symbol: str) -> tuple[tuple[str, datetime, str], ...]:
        return self._list_events("watchlist_events", "symbol", symbol)

    def append_candidate_event(
        self, *, candidate_id: str, event_type: str, occurred_at: datetime, detail: str
    ) -> None:
        self._append_event(
            "candidate_events", "candidate_id", candidate_id, event_type, occurred_at, detail
        )

    def list_candidate_events(self, candidate_id: str) -> tuple[tuple[str, datetime, str], ...]:
        return self._list_events("candidate_events", "candidate_id", candidate_id)

    def list_candidate_review_events(self, candidate_id: str) -> tuple[dict[str, object], ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT status, event_date, price_1e4, reason
                FROM candidate_events
                WHERE candidate_id = ? AND status IS NOT NULL
                ORDER BY event_id
                """,
                (candidate_id,),
            ).fetchall()
        return tuple(
            {
                "candidate_id": candidate_id,
                "status": str(row[0]),
                "event_date": str(row[1]),
                "price_1e4": None if row[2] is None else int(row[2]),
                "reason": str(row[3]),
            }
            for row in rows
        )

    def backup_to(self, destination: str | Path) -> None:
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(destination_path) as destination_connection:
            source.backup(destination_connection)

    def doctor(self) -> dict[str, str]:
        with self.connect() as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
            foreign_keys = str(connection.execute("PRAGMA foreign_keys").fetchone()[0])
        return {
            "integrity": integrity,
            "journal_mode": journal_mode,
            "foreign_keys": foreign_keys,
        }

    def is_ready(self) -> bool:
        """Return a constant-time schema/readability check for HTTP readiness.

        Full ``PRAGMA integrity_check`` remains part of ``doctor`` and backup
        diagnostics. It is intentionally excluded here because scanning a
        multi-million-row database would block the single MCP event loop on
        every ``/readyz`` request.
        """

        try:
            with self.connect() as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                readable = connection.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return version == SCHEMA_VERSION and readable == (1,)

    def load_idempotent_write(
        self, operation: str, idempotency_key: str, request: object
    ) -> object | None:
        request_hash = self._request_hash(operation, request)
        with self.connect() as connection:
            return self._idempotent_result(connection, operation, idempotency_key, request_hash)

    def save_idempotent_write(
        self, operation: str, idempotency_key: str, request: object, result: object
    ) -> object:
        request_hash = self._request_hash(operation, request)
        with self._idempotent_write_connection() as connection:
            cached = self._idempotent_result(connection, operation, idempotency_key, request_hash)
            if cached is not None:
                return cached
            self._save_idempotent_result(
                connection, operation, idempotency_key, request_hash, result
            )
            return result

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @staticmethod
    def _iso(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, date | datetime):
            return value.isoformat()
        return str(value)

    @staticmethod
    def _mapping(value: object) -> dict[str, object]:
        if isinstance(value, dict):
            return dict(value)
        fields = getattr(value, "__dataclass_fields__", None)
        if fields is None:
            raise TypeError("provider fact must be a mapping or dataclass")
        return {name: getattr(value, name) for name in fields}

    @classmethod
    def _jsonable(cls, value: object) -> object:
        if isinstance(value, dict):
            return {str(key): cls._jsonable(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [cls._jsonable(item) for item in value]
        if isinstance(value, date | datetime):
            return value.isoformat()
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        fields = getattr(value, "__dataclass_fields__", None)
        if fields is not None:
            return {name: cls._jsonable(getattr(value, name)) for name in fields}
        return str(value)

    @classmethod
    def _jsonable_json(cls, value: object) -> str:
        return json.dumps(
            cls._jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    @staticmethod
    def _immutable_insert(
        connection: sqlite3.Connection,
        table: str,
        key_column: str,
        key: object,
        value_columns: tuple[str, ...],
        values: tuple[object, ...],
    ) -> None:
        Database._immutable_composite_insert(
            connection, table, (key_column,), (key,), value_columns, values
        )

    @staticmethod
    def _immutable_composite_insert(
        connection: sqlite3.Connection,
        table: str,
        key_columns: tuple[str, ...],
        keys: tuple[object, ...],
        value_columns: tuple[str, ...],
        values: tuple[object, ...],
    ) -> None:
        where = " AND ".join(f"{name} = ?" for name in key_columns)
        selected = ", ".join(value_columns)
        existing = connection.execute(
            f"SELECT {selected} FROM {table} WHERE {where}", keys
        ).fetchone()
        if existing is not None:
            if tuple(existing) != values:
                raise ValueError(f"{table} fact is immutable; conflicting content")
            return
        columns = (*key_columns, *value_columns)
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO {table}({', '.join(columns)}) VALUES ({placeholders})",
            (*keys, *values),
        )

    @staticmethod
    def _validate_sha256(value: object, label: str) -> None:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"strategy replay {label} hash must be lowercase SHA-256")

    @staticmethod
    def _strategy_replay_job(
        row: tuple[object, ...], completed_dates: set[str], certified: bool
    ) -> dict[str, object]:
        expected = tuple(date.fromisoformat(item) for item in json.loads(str(row[6])))
        next_trade_date = next(
            (session for session in expected if session.isoformat() not in completed_dates),
            None,
        )
        return {
            "job_id": str(row[0]),
            "replay_id": str(row[0]),
            "strategy_version": str(row[1]),
            "version": str(row[1]),
            "parameters_hash": str(row[2]),
            "source": str(row[3]),
            "start_date": date.fromisoformat(str(row[4])),
            "end_date": date.fromisoformat(str(row[5])),
            "expected_sessions": expected,
            "expected_session_count": len(expected),
            "processed_sessions": len(completed_dates),
            "next_trade_date": next_trade_date,
            "status": str(row[7]),
            "dataset_hash": None if row[8] is None else str(row[8]),
            "result_hash": None if row[9] is None else str(row[9]),
            "summary": None if row[10] is None else json.loads(str(row[10])),
            "error": None if row[11] is None else str(row[11]),
            "created_at": datetime.fromisoformat(str(row[12])),
            "started_at": None if row[13] is None else datetime.fromisoformat(str(row[13])),
            "completed_at": None if row[14] is None else datetime.fromisoformat(str(row[14])),
            "pipeline_version": None if row[15] is None else str(row[15]),
            "input_hash": None if row[16] is None else str(row[16]),
            "warmup_sessions": 20 if row[17] is None else int(row[17]),
            "input_hash_schema": None if row[18] is None else str(row[18]),
            "result_hash_schema": None if row[19] is None else str(row[19]),
            "outcome_hash_schema": None if row[20] is None else str(row[20]),
            "outcome_status": None if row[21] is None else str(row[21]),
            "outcome": None if row[22] is None else json.loads(str(row[22])),
            "outcome_hash": None if row[23] is None else str(row[23]),
            "industry_classification_standard": None if row[24] is None else str(row[24]),
            "industry_classification_mode": None if row[25] is None else str(row[25]),
            "industry_classification_as_of": (
                None if row[26] is None else date.fromisoformat(str(row[26]))
            ),
            "industry_mapping_sha256": None if row[27] is None else str(row[27]),
            "certified": certified,
        }

    def register_research_hypotheses(self, hypotheses: Iterable[dict[str, object]]) -> int:
        """Register immutable, versioned research definitions."""

        records = tuple(dict(item) for item in hypotheses)
        with self._idempotent_write_connection() as connection:
            for record in records:
                required = (
                    "hypothesis_id",
                    "family",
                    "title",
                    "mechanism",
                    "formula",
                    "data_requirements",
                    "status",
                    "sample_role",
                    "registered_at",
                )
                if any(record.get(field) in (None, "") for field in required):
                    raise ValueError("research hypothesis definition is incomplete")
                formula_json = self._json(record["formula"])
                requirements_json = self._json(record["data_requirements"])
                canonical = {field: self._jsonable(record.get(field)) for field in required}
                canonical["frozen_after"] = self._iso(record.get("frozen_after"))
                definition_hash = hashlib.sha256(self._json(canonical).encode("utf-8")).hexdigest()
                self._immutable_insert(
                    connection,
                    "research_hypotheses",
                    "hypothesis_id",
                    str(record["hypothesis_id"]),
                    (
                        "family",
                        "title",
                        "mechanism",
                        "formula_json",
                        "data_requirements_json",
                        "status",
                        "sample_role",
                        "frozen_after",
                        "definition_hash",
                        "registered_at",
                    ),
                    (
                        str(record["family"]),
                        str(record["title"]),
                        str(record["mechanism"]),
                        formula_json,
                        requirements_json,
                        str(record["status"]),
                        str(record["sample_role"]),
                        self._iso(record.get("frozen_after")),
                        definition_hash,
                        self._iso(record["registered_at"]),
                    ),
                )
        return len(records)

    def get_research_hypothesis(self, hypothesis_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT hypothesis_id, family, title, mechanism, formula_json,
                       data_requirements_json, status, sample_role, frozen_after,
                       definition_hash, registered_at
                FROM research_hypotheses WHERE hypothesis_id = ?
                """,
                (hypothesis_id,),
            ).fetchone()
        return None if row is None else self._research_hypothesis_from_row(row)

    def list_research_hypotheses(
        self, *, family: str | None = None, status: str | None = None
    ) -> tuple[dict[str, object], ...]:
        conditions: list[str] = []
        parameters: list[object] = []
        if family is not None:
            conditions.append("family = ?")
            parameters.append(family)
        if status is not None:
            conditions.append("status = ?")
            parameters.append(status)
        where = "" if not conditions else " WHERE " + " AND ".join(conditions)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT hypothesis_id, family, title, mechanism, formula_json,
                       data_requirements_json, status, sample_role, frozen_after,
                       definition_hash, registered_at
                FROM research_hypotheses
                """
                + where
                + " ORDER BY hypothesis_id",
                tuple(parameters),
            ).fetchall()
        return tuple(self._research_hypothesis_from_row(row) for row in rows)

    def save_research_trial(self, trial: dict[str, object]) -> None:
        required = (
            "trial_id",
            "hypothesis_id",
            "manifest_hash",
            "sample_role",
            "status",
            "result",
            "result_hash",
            "created_at",
        )
        if any(trial.get(field) in (None, "") for field in required):
            raise ValueError("research trial is incomplete")
        self._validate_research_hash(trial["manifest_hash"], "manifest")
        self._validate_research_hash(trial["result_hash"], "result")
        with self._idempotent_write_connection() as connection:
            self._immutable_insert(
                connection,
                "research_trials",
                "trial_id",
                str(trial["trial_id"]),
                (
                    "hypothesis_id",
                    "manifest_hash",
                    "sample_role",
                    "status",
                    "result_json",
                    "result_hash",
                    "created_at",
                    "completed_at",
                ),
                (
                    str(trial["hypothesis_id"]),
                    str(trial["manifest_hash"]),
                    str(trial["sample_role"]),
                    str(trial["status"]),
                    self._json(trial["result"]),
                    str(trial["result_hash"]),
                    self._iso(trial["created_at"]),
                    self._iso(trial.get("completed_at")),
                ),
            )

    def list_research_trials(
        self, *, hypothesis_id: str | None = None
    ) -> tuple[dict[str, object], ...]:
        query = """
            SELECT trial_id, hypothesis_id, manifest_hash, sample_role, status,
                   result_json, result_hash, created_at, completed_at
            FROM research_trials
        """
        parameters: tuple[object, ...] = ()
        if hypothesis_id is not None:
            query += " WHERE hypothesis_id = ?"
            parameters = (hypothesis_id,)
        query += " ORDER BY created_at, trial_id"
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(
            {
                "trial_id": str(row[0]),
                "hypothesis_id": str(row[1]),
                "manifest_hash": str(row[2]),
                "sample_role": str(row[3]),
                "status": str(row[4]),
                "result": json.loads(str(row[5])),
                "result_hash": str(row[6]),
                "created_at": datetime.fromisoformat(str(row[7])),
                "completed_at": None if row[8] is None else datetime.fromisoformat(str(row[8])),
            }
            for row in rows
        )

    def save_research_forward_observation(self, observation: dict[str, object]) -> None:
        with self._idempotent_write_connection() as connection:
            self._save_research_forward_observation(connection, observation)

    def _save_research_forward_observation(
        self, connection: sqlite3.Connection, observation: dict[str, object]
    ) -> None:
        required = (
            "hypothesis_id",
            "trade_date",
            "symbol",
            "input_hash",
            "result_hash",
            "observation",
            "recorded_at",
        )
        if any(observation.get(field) in (None, "") for field in required):
            raise ValueError("forward research observation is incomplete")
        self._validate_research_hash(observation["input_hash"], "input")
        self._validate_research_hash(observation["result_hash"], "result")
        trade_date = self._iso(observation["trade_date"])
        hypothesis = connection.execute(
            "SELECT frozen_after FROM research_hypotheses WHERE hypothesis_id = ?",
            (str(observation["hypothesis_id"]),),
        ).fetchone()
        if hypothesis is None:
            raise ValueError("forward research hypothesis does not exist")
        if hypothesis[0] is not None and trade_date <= str(hypothesis[0]):
            raise ValueError("forward observation must be after the frozen discovery sample")
        self._immutable_composite_insert(
            connection,
            "research_forward_observations",
            ("hypothesis_id", "trade_date", "symbol"),
            (
                str(observation["hypothesis_id"]),
                trade_date,
                str(observation["symbol"]),
            ),
            ("input_hash", "result_hash", "observation_json", "recorded_at"),
            (
                str(observation["input_hash"]),
                str(observation["result_hash"]),
                self._json(observation["observation"]),
                self._iso(observation["recorded_at"]),
            ),
        )

    def list_research_forward_observations(
        self, *, hypothesis_id: str, symbol: str | None = None
    ) -> tuple[dict[str, object], ...]:
        parameters: tuple[object, ...] = (hypothesis_id,)
        symbol_filter = ""
        if symbol is not None:
            symbol_filter = " AND symbol = ?"
            parameters = (hypothesis_id, symbol)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT hypothesis_id, trade_date, symbol, input_hash, result_hash,
                       observation_json, recorded_at
                FROM research_forward_observations
                WHERE hypothesis_id = ?{symbol_filter}
                ORDER BY trade_date, symbol
                """,  # noqa: S608 - filter is a fixed internal fragment
                parameters,
            ).fetchall()
        return tuple(
            {
                "hypothesis_id": str(row[0]),
                "trade_date": str(row[1]),
                "symbol": str(row[2]),
                "input_hash": str(row[3]),
                "result_hash": str(row[4]),
                "observation": json.loads(str(row[5])),
                "recorded_at": datetime.fromisoformat(str(row[6])),
            }
            for row in rows
        )

    def list_pending_research_forward_observations(
        self,
        *,
        hypothesis_ids: Iterable[str],
        mature_on_or_before: date,
    ) -> tuple[dict[str, object], ...]:
        """Return mature observations that still lack their 20-session outcome."""

        normalized = tuple(dict.fromkeys(str(item) for item in hypothesis_ids if str(item)))
        if not normalized:
            return ()
        placeholders = ",".join("?" for _ in normalized)
        parameters: tuple[object, ...] = (*normalized, mature_on_or_before.isoformat())
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT o.hypothesis_id, o.trade_date, o.symbol, o.input_hash,
                       o.result_hash, o.observation_json, o.recorded_at
                FROM research_forward_observations AS o
                WHERE o.hypothesis_id IN ({placeholders})
                  AND o.trade_date <= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM research_forward_outcomes AS r
                      WHERE r.hypothesis_id = o.hypothesis_id
                        AND r.signal_date = o.trade_date
                        AND r.symbol = o.symbol
                        AND r.horizon_sessions = 20
                  )
                ORDER BY o.trade_date, o.symbol, o.hypothesis_id
                """,  # noqa: S608 - placeholders are generated, values remain bound
                parameters,
            ).fetchall()
        return tuple(
            {
                "hypothesis_id": str(row[0]),
                "trade_date": str(row[1]),
                "symbol": str(row[2]),
                "input_hash": str(row[3]),
                "result_hash": str(row[4]),
                "observation": json.loads(str(row[5])),
                "recorded_at": datetime.fromisoformat(str(row[6])),
            }
            for row in rows
        )

    def save_research_forward_outcome(self, outcome: dict[str, object]) -> None:
        with self._idempotent_write_connection() as connection:
            self._save_research_forward_outcome(connection, outcome)

    def _save_research_forward_outcome(
        self, connection: sqlite3.Connection, outcome: dict[str, object]
    ) -> None:
        required = (
            "hypothesis_id",
            "signal_date",
            "symbol",
            "horizon_sessions",
            "observation_result_hash",
            "outcome",
            "outcome_hash",
            "recorded_at",
        )
        if any(outcome.get(field) in (None, "") for field in required):
            raise ValueError("forward research outcome is incomplete")
        horizon = int(outcome["horizon_sessions"])
        if horizon not in {5, 10, 20}:
            raise ValueError("forward research outcome horizon must be 5, 10, or 20 sessions")
        observation_hash = str(outcome["observation_result_hash"])
        self._validate_research_hash(observation_hash, "observation result")
        self._validate_research_hash(outcome["outcome_hash"], "outcome")
        signal_date = self._iso(outcome["signal_date"])
        symbol = str(outcome["symbol"])
        hypothesis_id = str(outcome["hypothesis_id"])
        observation = connection.execute(
            """
            SELECT result_hash FROM research_forward_observations
            WHERE hypothesis_id = ? AND trade_date = ? AND symbol = ?
            """,
            (hypothesis_id, signal_date, symbol),
        ).fetchone()
        if observation is None:
            raise ValueError("forward research outcome requires its observation")
        if str(observation[0]) != observation_hash:
            raise ValueError("forward research outcome observation hash conflicts")
        self._immutable_composite_insert(
            connection,
            "research_forward_outcomes",
            ("hypothesis_id", "signal_date", "symbol", "horizon_sessions"),
            (hypothesis_id, signal_date, symbol, horizon),
            ("observation_result_hash", "outcome_json", "outcome_hash", "recorded_at"),
            (
                observation_hash,
                self._json(outcome["outcome"]),
                str(outcome["outcome_hash"]),
                self._iso(outcome["recorded_at"]),
            ),
        )

    def save_research_forward_bundle(
        self,
        *,
        observations: Iterable[dict[str, object]],
        outcomes: Iterable[dict[str, object]],
    ) -> dict[str, int]:
        observation_records = tuple(dict(item) for item in observations)
        outcome_records = tuple(dict(item) for item in outcomes)
        with self._idempotent_write_connection() as connection:
            for observation in observation_records:
                self._save_research_forward_observation(connection, observation)
            for outcome in outcome_records:
                self._save_research_forward_outcome(connection, outcome)
        return {"observations": len(observation_records), "outcomes": len(outcome_records)}

    def list_research_forward_outcomes(
        self, *, hypothesis_id: str, symbol: str | None = None
    ) -> tuple[dict[str, object], ...]:
        parameters: tuple[object, ...] = (hypothesis_id,)
        symbol_filter = ""
        if symbol is not None:
            symbol_filter = " AND symbol = ?"
            parameters = (hypothesis_id, symbol)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT hypothesis_id, signal_date, symbol, horizon_sessions,
                       observation_result_hash, outcome_json, outcome_hash, recorded_at
                FROM research_forward_outcomes
                WHERE hypothesis_id = ?{symbol_filter}
                ORDER BY signal_date, symbol, horizon_sessions
                """,  # noqa: S608 - filter is a fixed internal fragment
                parameters,
            ).fetchall()
        return tuple(
            {
                "hypothesis_id": str(row[0]),
                "signal_date": str(row[1]),
                "symbol": str(row[2]),
                "horizon_sessions": int(row[3]),
                "observation_result_hash": str(row[4]),
                "outcome": json.loads(str(row[5])),
                "outcome_hash": str(row[6]),
                "recorded_at": datetime.fromisoformat(str(row[7])),
            }
            for row in rows
        )

    def save_point_in_time_fundamentals(self, facts: Iterable[dict[str, object]]) -> int:
        records = tuple(dict(item) for item in facts)
        with self._idempotent_write_connection() as connection:
            for record in records:
                required = (
                    "symbol",
                    "interface",
                    "report_period",
                    "visible_date",
                    "revision_key",
                    "source",
                    "payload",
                    "payload_hash",
                    "source_timestamp",
                )
                if any(record.get(field) in (None, "") for field in required):
                    raise ValueError("point-in-time fundamental fact is incomplete")
                self._validate_research_hash(record["payload_hash"], "payload")
                self._immutable_composite_insert(
                    connection,
                    "point_in_time_fundamentals",
                    (
                        "symbol",
                        "interface",
                        "report_period",
                        "visible_date",
                        "revision_key",
                        "source",
                    ),
                    (
                        str(record["symbol"]),
                        str(record["interface"]),
                        self._iso(record["report_period"]),
                        self._iso(record["visible_date"]),
                        str(record["revision_key"]),
                        str(record["source"]),
                    ),
                    ("payload_json", "payload_hash", "source_timestamp"),
                    (
                        self._json(record["payload"]),
                        str(record["payload_hash"]),
                        self._iso(record["source_timestamp"]),
                    ),
                )
        return len(records)

    def load_point_in_time_fundamentals(
        self, *, symbol: str, as_of: date, source: str = "tushare"
    ) -> tuple[dict[str, object], ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, interface, report_period, visible_date, revision_key,
                       source, payload_json, payload_hash, source_timestamp
                FROM point_in_time_fundamentals
                WHERE symbol = ? AND source = ? AND visible_date <= ?
                ORDER BY interface, report_period, visible_date DESC, revision_key DESC
                """,
                (symbol, source, as_of.isoformat()),
            ).fetchall()
        latest: dict[tuple[str, str], tuple[object, ...]] = {}
        for row in rows:
            latest.setdefault((str(row[1]), str(row[2])), row)
        return tuple(
            {
                "symbol": str(row[0]),
                "interface": str(row[1]),
                "report_period": date.fromisoformat(str(row[2])),
                "visible_date": date.fromisoformat(str(row[3])),
                "revision_key": str(row[4]),
                "source": str(row[5]),
                "payload": json.loads(str(row[6])),
                "payload_hash": str(row[7]),
                "source_timestamp": datetime.fromisoformat(str(row[8])),
            }
            for row in latest.values()
        )

    @staticmethod
    def _validate_research_hash(value: object, label: str) -> None:
        text = str(value)
        if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
            raise ValueError(f"research {label} hash must be lowercase SHA-256")

    @staticmethod
    def _research_hypothesis_from_row(row: tuple[object, ...]) -> dict[str, object]:
        return {
            "hypothesis_id": str(row[0]),
            "family": str(row[1]),
            "title": str(row[2]),
            "mechanism": str(row[3]),
            "formula": json.loads(str(row[4])),
            "data_requirements": json.loads(str(row[5])),
            "status": str(row[6]),
            "sample_role": str(row[7]),
            "frozen_after": None if row[8] is None else str(row[8]),
            "definition_hash": str(row[9]),
            "registered_at": datetime.fromisoformat(str(row[10])),
        }

    def _migrate(self, connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            message = (
                f"database schema version {version} is newer than supported "
                f"version {SCHEMA_VERSION}"
            )
            raise ValueError(message)
        if version < 1:
            self._ensure_column(connection, "candidate_events", "idempotency_key", "TEXT")
            connection.execute("PRAGMA user_version = 1")
        if version < 2:
            self._ensure_column(
                connection,
                "idempotent_writes",
                "request_hash",
                "TEXT NOT NULL DEFAULT ''",
            )
            connection.execute("PRAGMA user_version = 2")
        if version < 3:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_approvals (
                    version TEXT PRIMARY KEY,
                    parameters_hash TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    FOREIGN KEY (version) REFERENCES strategy_versions(version)
                )
                """
            )
            connection.execute("PRAGMA user_version = 3")
        if version < 4:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    trade_date TEXT NOT NULL, source TEXT NOT NULL,
                    source_timestamp TEXT NOT NULL, advance_ratio_bps INTEGER NOT NULL,
                    above_ma20_ratio_bps INTEGER NOT NULL,
                    PRIMARY KEY (trade_date, source)
                );
                CREATE TABLE IF NOT EXISTS snapshot_securities (
                    trade_date TEXT NOT NULL, source TEXT NOT NULL, symbol TEXT NOT NULL,
                    name TEXT NOT NULL, exchange TEXT NOT NULL, board TEXT NOT NULL,
                    list_date TEXT NOT NULL, industry TEXT NOT NULL,
                    is_st INTEGER NOT NULL CHECK (is_st IN (0, 1)),
                    PRIMARY KEY (trade_date, source, symbol),
                    FOREIGN KEY (trade_date, source)
                        REFERENCES market_snapshots(trade_date, source)
                );
                PRAGMA user_version = 4;
                """
            )
        if version < 5:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS replay_attestations (
                    version TEXT PRIMARY KEY,
                    parameters_hash TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY (version) REFERENCES strategy_versions(version)
                );
                PRAGMA user_version = 5;
                """
            )
        if version < 6:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pipeline_runs (
                    trade_date TEXT NOT NULL,
                    pipeline_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    strategy_version TEXT,
                    error TEXT,
                    PRIMARY KEY (trade_date, pipeline_version),
                    FOREIGN KEY (strategy_version) REFERENCES strategy_versions(version)
                );
                CREATE TABLE IF NOT EXISTS schedule_outcomes (
                    trade_date TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    next_at TEXT,
                    pipeline_version TEXT,
                    error TEXT
                );
                PRAGMA user_version = 6;
                """
            )
        if version < 7:
            self._ensure_column(connection, "candidate_events", "status", "TEXT")
            self._ensure_column(connection, "candidate_events", "event_date", "TEXT")
            self._ensure_column(connection, "candidate_events", "price_1e4", "INTEGER")
            self._ensure_column(connection, "candidate_events", "reason", "TEXT")
            connection.execute("PRAGMA user_version = 7")
        if version < 8:
            self._ensure_column(connection, "replay_attestations", "dataset_hash", "TEXT")
            self._ensure_column(connection, "replay_attestations", "start_date", "TEXT")
            self._ensure_column(connection, "replay_attestations", "end_date", "TEXT")
            self._ensure_column(connection, "replay_attestations", "session_count", "INTEGER")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS expected_trading_days (
                    source TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    PRIMARY KEY (source, trade_date)
                );
                PRAGMA user_version = 8;
                """
            )
        if version < 9:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS strategy_replay_jobs (
                    job_id TEXT PRIMARY KEY,
                    strategy_version TEXT NOT NULL,
                    parameters_hash TEXT NOT NULL,
                    source TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    expected_sessions_json TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('queued', 'running', 'completed', 'failed')),
                    dataset_hash TEXT,
                    result_hash TEXT,
                    summary_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    FOREIGN KEY (strategy_version) REFERENCES strategy_versions(version)
                );
                CREATE TABLE IF NOT EXISTS strategy_replay_days (
                    job_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    output_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    PRIMARY KEY (job_id, trade_date),
                    FOREIGN KEY (job_id) REFERENCES strategy_replay_jobs(job_id)
                );
                CREATE TABLE IF NOT EXISTS strategy_replay_attestations (
                    strategy_version TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE,
                    parameters_hash TEXT NOT NULL,
                    dataset_hash TEXT NOT NULL,
                    result_hash TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    session_count INTEGER NOT NULL,
                    certified_at TEXT NOT NULL,
                    FOREIGN KEY (strategy_version) REFERENCES strategy_versions(version),
                    FOREIGN KEY (job_id) REFERENCES strategy_replay_jobs(job_id)
                );
                PRAGMA user_version = 9;
                """
            )
        if version < 10:
            self._ensure_column(connection, "strategy_replay_jobs", "pipeline_version", "TEXT")
            self._ensure_column(connection, "strategy_replay_jobs", "input_hash", "TEXT")
            self._ensure_column(connection, "strategy_replay_jobs", "input_hash_schema", "TEXT")
            self._ensure_column(connection, "strategy_replay_jobs", "result_hash_schema", "TEXT")
            self._ensure_column(connection, "strategy_replay_jobs", "outcome_hash_schema", "TEXT")
            self._ensure_column(connection, "strategy_replay_jobs", "warmup_sessions", "INTEGER")
            self._ensure_column(connection, "strategy_replay_jobs", "outcome_status", "TEXT")
            self._ensure_column(connection, "strategy_replay_jobs", "outcome_json", "TEXT")
            self._ensure_column(connection, "strategy_replay_jobs", "outcome_hash", "TEXT")
            self._ensure_column(
                connection, "strategy_replay_jobs", "industry_classification_standard", "TEXT"
            )
            self._ensure_column(
                connection, "strategy_replay_jobs", "industry_classification_mode", "TEXT"
            )
            self._ensure_column(
                connection, "strategy_replay_jobs", "industry_classification_as_of", "TEXT"
            )
            self._ensure_column(
                connection, "strategy_replay_jobs", "industry_mapping_sha256", "TEXT"
            )
            self._ensure_column(connection, "strategy_replay_attestations", "outcome_hash", "TEXT")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_price_limits (
                    trade_date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    fact_json TEXT NOT NULL,
                    PRIMARY KEY (trade_date, source, symbol)
                );
                CREATE TABLE IF NOT EXISTS v3_snapshot_features (
                    trade_date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    feature_json TEXT NOT NULL,
                    PRIMARY KEY (trade_date, source, symbol)
                );
                CREATE TABLE IF NOT EXISTS strategy_replay_outcome_runs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    outcome_hash TEXT,
                    summary_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY (job_id) REFERENCES strategy_replay_jobs(job_id)
                );
                CREATE TABLE IF NOT EXISTS strategy_replay_candidate_outcomes (
                    job_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    outcome_json TEXT NOT NULL,
                    PRIMARY KEY (job_id, candidate_id),
                    FOREIGN KEY (job_id) REFERENCES strategy_replay_jobs(job_id)
                );
                CREATE TABLE IF NOT EXISTS strategy_version_relations (
                    predecessor TEXT NOT NULL,
                    successor TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (predecessor, successor),
                    FOREIGN KEY (predecessor) REFERENCES strategy_versions(version),
                    FOREIGN KEY (successor) REFERENCES strategy_versions(version)
                );
                CREATE TABLE IF NOT EXISTS strategy_lifecycle_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    related_version TEXT,
                    FOREIGN KEY (version) REFERENCES strategy_versions(version)
                );
                PRAGMA user_version = 10;
                """
            )
        if version < 11:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS provider_fetch_evidence (
                    fetch_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL, endpoint_kind TEXT NOT NULL,
                    request_key TEXT NOT NULL, trade_date TEXT, http_date TEXT,
                    retrieved_at TEXT NOT NULL, http_status INTEGER,
                    byte_length INTEGER NOT NULL, payload_sha256 TEXT NOT NULL,
                    adapter_version TEXT NOT NULL, status TEXT NOT NULL,
                    error_class TEXT
                );
                CREATE TABLE IF NOT EXISTS provider_backfill_runs (
                    run_id TEXT PRIMARY KEY, source TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL, manifest_json TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS sina_backfill_checkpoints (
                    run_id TEXT NOT NULL, symbol TEXT NOT NULL, status TEXT NOT NULL,
                    checkpoint_json TEXT NOT NULL, PRIMARY KEY(run_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS provider_backfill_checkpoints (
                    run_id TEXT NOT NULL, request_key TEXT NOT NULL, status TEXT NOT NULL,
                    checkpoint_json TEXT NOT NULL, PRIMARY KEY(run_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS share_capital_facts (
                    symbol TEXT NOT NULL, effective_date TEXT NOT NULL, source TEXT NOT NULL,
                    outstanding_shares INTEGER NOT NULL, source_timestamp TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    PRIMARY KEY(symbol, effective_date, source)
                );
                CREATE TABLE IF NOT EXISTS daily_security_status (
                    symbol TEXT NOT NULL, trade_date TEXT NOT NULL, source TEXT NOT NULL,
                    tradestatus TEXT NOT NULL, is_st INTEGER NOT NULL,
                    source_timestamp TEXT NOT NULL, batch_sha256 TEXT NOT NULL,
                    PRIMARY KEY(symbol, trade_date, source)
                );
                CREATE TABLE IF NOT EXISTS provider_daily_metrics (
                    symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
                    price_source TEXT NOT NULL, capital_source TEXT NOT NULL,
                    upstream_market_cap_fen INTEGER, derived_market_cap_fen INTEGER,
                    upstream_turnover_rate TEXT, derived_turnover_rate TEXT,
                    evidence_sha256 TEXT NOT NULL,
                    PRIMARY KEY(symbol, trade_date, price_source, capital_source)
                );
                CREATE TABLE IF NOT EXISTS provider_spot_batches (
                    trade_date TEXT NOT NULL, source TEXT NOT NULL, metrics_json TEXT NOT NULL,
                    PRIMARY KEY(trade_date, source)
                );
                CREATE TABLE IF NOT EXISTS provider_shadow_runs (
                    source TEXT NOT NULL, trade_date TEXT NOT NULL,
                    adapter_version TEXT NOT NULL, run_json TEXT NOT NULL,
                    status TEXT NOT NULL, dataset_hash TEXT NOT NULL,
                    PRIMARY KEY(source, trade_date, adapter_version)
                );
                CREATE TABLE IF NOT EXISTS provider_qualifications (
                    source TEXT NOT NULL, through_date TEXT NOT NULL, status TEXT NOT NULL,
                    dataset_hash TEXT NOT NULL, recorded_at TEXT NOT NULL,
                    report_json TEXT NOT NULL, PRIMARY KEY(source, through_date)
                );
                CREATE TABLE IF NOT EXISTS provider_qualification_reports (
                    qualification_id TEXT PRIMARY KEY, source TEXT NOT NULL,
                    through_date TEXT NOT NULL, status TEXT NOT NULL,
                    window_hash TEXT NOT NULL, report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_attestations (
                    source TEXT NOT NULL, through_date TEXT NOT NULL,
                    dataset_hash TEXT NOT NULL, attested_at TEXT NOT NULL,
                    PRIMARY KEY(source, through_date)
                );
                CREATE TABLE IF NOT EXISTS provider_qualification_review_attestations (
                    qualification_id TEXT PRIMARY KEY, window_hash TEXT NOT NULL,
                    terms_confirmed INTEGER NOT NULL, attested_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_approvals (
                    source TEXT NOT NULL, through_date TEXT NOT NULL,
                    dataset_hash TEXT NOT NULL, approved_at TEXT NOT NULL,
                    PRIMARY KEY(source, through_date)
                );
                CREATE TABLE IF NOT EXISTS provider_source_approvals (
                    qualification_id TEXT PRIMARY KEY, capabilities_json TEXT NOT NULL,
                    approved_at TEXT NOT NULL, consumed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS provider_registry (
                    source TEXT PRIMARY KEY, qualification_id TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL, activated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_source_registry (
                    source TEXT PRIMARY KEY, qualification_id TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL, activated_at TEXT NOT NULL,
                    expires_on_failure INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS v4_dataset_manifests (
                    manifest_hash TEXT PRIMARY KEY, manifest_json TEXT NOT NULL,
                    price_source TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS v4_study_runs (
                    study_id TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL,
                    status TEXT NOT NULL, input_hash TEXT NOT NULL,
                    result_hash TEXT, report_json TEXT, error TEXT,
                    created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS v4_study_arms (
                    study_id TEXT NOT NULL, arm_id TEXT NOT NULL,
                    parameters_json TEXT NOT NULL, parameters_hash TEXT NOT NULL,
                    parent_version TEXT NOT NULL, unique_difference TEXT NOT NULL,
                    status TEXT NOT NULL, PRIMARY KEY(study_id, arm_id)
                );
                CREATE TABLE IF NOT EXISTS v4_study_days (
                    study_id TEXT NOT NULL, arm_id TEXT NOT NULL, signal_date TEXT NOT NULL,
                    result_json TEXT NOT NULL, result_hash TEXT NOT NULL,
                    PRIMARY KEY(study_id, arm_id, signal_date)
                );
                CREATE TABLE IF NOT EXISTS v4_study_candidate_outcomes (
                    study_id TEXT NOT NULL, arm_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL, outcome_json TEXT NOT NULL,
                    outcome_hash TEXT NOT NULL, PRIMARY KEY(study_id, arm_id, candidate_id)
                );
                CREATE TABLE IF NOT EXISTS v4_study_statistics (
                    study_id TEXT NOT NULL, arm_id TEXT NOT NULL,
                    statistics_json TEXT NOT NULL, statistics_hash TEXT NOT NULL,
                    PRIMARY KEY(study_id, arm_id)
                );
                CREATE TABLE IF NOT EXISTS v4_study_proposal_artifacts (
                    artifact_hash TEXT PRIMARY KEY, study_id TEXT NOT NULL,
                    arm_id TEXT NOT NULL, version TEXT NOT NULL,
                    artifact_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                PRAGMA user_version = 11;
                """
            )
        if version < 12:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_hypotheses (
                    hypothesis_id TEXT PRIMARY KEY,
                    family TEXT NOT NULL,
                    title TEXT NOT NULL,
                    mechanism TEXT NOT NULL,
                    formula_json TEXT NOT NULL,
                    data_requirements_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sample_role TEXT NOT NULL,
                    frozen_after TEXT,
                    definition_hash TEXT NOT NULL,
                    registered_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_trials (
                    trial_id TEXT PRIMARY KEY,
                    hypothesis_id TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    sample_role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    result_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY(hypothesis_id) REFERENCES research_hypotheses(hypothesis_id)
                );
                CREATE INDEX IF NOT EXISTS research_trials_hypothesis_idx
                    ON research_trials(hypothesis_id, created_at);
                CREATE TABLE IF NOT EXISTS research_forward_observations (
                    hypothesis_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    result_hash TEXT NOT NULL,
                    observation_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY(hypothesis_id, trade_date),
                    FOREIGN KEY(hypothesis_id) REFERENCES research_hypotheses(hypothesis_id)
                );
                CREATE TABLE IF NOT EXISTS point_in_time_fundamentals (
                    symbol TEXT NOT NULL,
                    interface TEXT NOT NULL,
                    report_period TEXT NOT NULL,
                    visible_date TEXT NOT NULL,
                    revision_key TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    source_timestamp TEXT NOT NULL,
                    PRIMARY KEY(
                        symbol, interface, report_period, visible_date,
                        revision_key, source
                    )
                );
                CREATE INDEX IF NOT EXISTS point_in_time_fundamentals_visible_idx
                    ON point_in_time_fundamentals(symbol, source, visible_date);
                PRAGMA user_version = 12;
                """
            )
        if version < 13:
            connection.executescript(
                """
                ALTER TABLE research_forward_observations
                    RENAME TO research_forward_observations_v12;
                CREATE TABLE research_forward_observations (
                    hypothesis_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    result_hash TEXT NOT NULL,
                    observation_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY(hypothesis_id, trade_date, symbol),
                    FOREIGN KEY(hypothesis_id) REFERENCES research_hypotheses(hypothesis_id)
                );
                INSERT INTO research_forward_observations(
                    hypothesis_id, trade_date, symbol, input_hash, result_hash,
                    observation_json, recorded_at
                )
                SELECT hypothesis_id, trade_date, 'legacy-unspecified', input_hash,
                       result_hash, observation_json, recorded_at
                FROM research_forward_observations_v12;
                DROP TABLE research_forward_observations_v12;
                CREATE TABLE research_forward_outcomes (
                    hypothesis_id TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    horizon_sessions INTEGER NOT NULL CHECK(horizon_sessions IN (5, 10, 20)),
                    observation_result_hash TEXT NOT NULL,
                    outcome_json TEXT NOT NULL,
                    outcome_hash TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY(hypothesis_id, signal_date, symbol, horizon_sessions),
                    FOREIGN KEY(hypothesis_id, signal_date, symbol)
                        REFERENCES research_forward_observations(
                            hypothesis_id, trade_date, symbol
                        )
                );
                PRAGMA user_version = 13;
                """
            )

    @contextmanager
    def _idempotent_write_connection(self) -> Iterable[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    @staticmethod
    def _watchlist_exists(connection: sqlite3.Connection, name: str) -> bool:
        return (
            connection.execute("SELECT 1 FROM watchlists WHERE name = ?", (name,)).fetchone()
            is not None
        )

    @staticmethod
    def _watchlist_items(connection: sqlite3.Connection, name: str) -> tuple[str, ...]:
        rows = connection.execute(
            """
            SELECT symbol FROM watchlist_items
            WHERE watchlist_name = ? ORDER BY ordinal
            """,
            (name,),
        ).fetchall()
        return tuple(row[0] for row in rows)

    @staticmethod
    def _idempotent_result(
        connection: sqlite3.Connection,
        operation: str,
        idempotency_key: str,
        request_hash: str,
    ) -> object | None:
        row = connection.execute(
            """
            SELECT request_hash, result_json FROM idempotent_writes
            WHERE operation = ? AND idempotency_key = ?
            """,
            (operation, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row[0] and row[0] != request_hash:
            raise IdempotencyKeyReuseError(
                "idempotency key cannot be reused for a different request"
            )
        return json.loads(row[1])

    def _save_idempotent_result(
        self,
        connection: sqlite3.Connection,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        result: object,
    ) -> None:
        connection.execute(
            """
            INSERT INTO idempotent_writes(operation, idempotency_key, request_hash, result_json)
            VALUES (?, ?, ?, ?)
            """,
            (operation, idempotency_key, request_hash, self._json(result)),
        )

    def _request_hash(self, operation: str, request: object) -> str:
        canonical_request = self._json({"operation": operation, "request": request})
        return hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()

    def _load_daily_review(
        self, connection: sqlite3.Connection, trade_date: date, strategy_version: str
    ) -> DailyReview | None:
        review_row = connection.execute(
            """
            SELECT status, trade_date, source, source_timestamp, strategy_version, market_regime
            FROM daily_reviews WHERE trade_date = ? AND strategy_version = ?
            """,
            (trade_date.isoformat(), strategy_version),
        ).fetchone()
        if review_row is None:
            return None
        candidate_rows = connection.execute(
            """
            SELECT candidate_id, symbol, name, rank, score, setup_type, strategy_version,
                   confirmation_condition, invalidation_condition
            FROM candidates
            WHERE trade_date = ? AND strategy_version = ?
            ORDER BY rank, symbol, candidate_id
            """,
            (trade_date.isoformat(), strategy_version),
        ).fetchall()
        candidates = tuple(self._candidate_from_row(connection, row) for row in candidate_rows)
        return DailyReview(
            status=review_row[0],
            trade_date=date.fromisoformat(review_row[1]),
            source=review_row[2],
            source_timestamp=datetime.fromisoformat(review_row[3]),
            strategy_version=review_row[4],
            market_regime=MarketRegime(review_row[5]),
            candidates=candidates,
        )

    def _candidate_from_row(
        self, connection: sqlite3.Connection, row: tuple[object, ...]
    ) -> Candidate:
        return Candidate(
            candidate_id=str(row[0]),
            symbol=str(row[1]),
            name=str(row[2]),
            rank=int(row[3]),
            score=int(row[4]),
            setup_type=SetupType(str(row[5])),
            strategy_version=str(row[6]),
            evidence=self._load_evidence(connection, str(row[0])),
            confirmation_condition=str(row[7]),
            invalidation_condition=str(row[8]),
        )

    @staticmethod
    def _load_evidence(connection: sqlite3.Connection, candidate_id: str) -> tuple[Evidence, ...]:
        rows = connection.execute(
            """
            SELECT metric, value_json, threshold_json, passed, score_contribution
            FROM candidate_evidence WHERE candidate_id = ? ORDER BY ordinal
            """,
            (candidate_id,),
        ).fetchall()
        return tuple(
            Evidence(
                metric=row[0],
                value=json.loads(row[1]),
                threshold=json.loads(row[2]),
                passed=bool(row[3]),
                score_contribution=row[4],
            )
            for row in rows
        )

    def _append_event(
        self,
        table: str,
        key_column: str,
        key: str,
        event_type: str,
        occurred_at: datetime,
        detail: str,
    ) -> None:
        statement = (
            f"INSERT INTO {table}({key_column}, event_type, occurred_at, detail) "
            "VALUES (?, ?, ?, ?)"
        )
        with self.connect() as connection:
            connection.execute(
                statement,
                (key, event_type, occurred_at.isoformat(), detail),
            )

    def _list_events(
        self, table: str, key_column: str, key: str
    ) -> tuple[tuple[str, datetime, str], ...]:
        statement = (
            f"SELECT event_type, occurred_at, detail FROM {table} "
            f"WHERE {key_column} = ? ORDER BY event_id"
        )
        with self.connect() as connection:
            rows = connection.execute(
                statement,
                (key,),
            ).fetchall()
        return tuple((row[0], datetime.fromisoformat(row[1]), row[2]) for row in rows)

    @staticmethod
    def _daily_bar_values(bar: DailyBar) -> tuple[str | int, ...]:
        return (
            bar.symbol,
            bar.trade_date.isoformat(),
            bar.open_1e4,
            bar.high_1e4,
            bar.low_1e4,
            bar.close_1e4,
            bar.pre_close_1e4,
            bar.volume_shares,
            bar.amount_fen,
            bar.source,
            bar.source_timestamp.isoformat(),
        )

    @staticmethod
    def _daily_bar_from_row(row: tuple[object, ...]) -> DailyBar:
        return DailyBar(
            symbol=str(row[0]),
            trade_date=date.fromisoformat(str(row[1])),
            open_1e4=int(row[2]),
            high_1e4=int(row[3]),
            low_1e4=int(row[4]),
            close_1e4=int(row[5]),
            pre_close_1e4=int(row[6]),
            volume_shares=int(row[7]),
            amount_fen=int(row[8]),
            source=str(row[9]),
            source_timestamp=datetime.fromisoformat(str(row[10])),
        )

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
