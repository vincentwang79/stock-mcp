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

SCHEMA_VERSION = 8


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

    def initialize(self) -> None:
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
                """
            )
            self._migrate(connection)

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
        groups: dict[tuple[str, str], set[str]] = {}
        for bar in records:
            values = self._daily_bar_values(bar)
            key = (str(values[0]), str(values[1]), str(values[9]))
            previous = incoming.get(key)
            if previous is not None and previous != values:
                raise ValueError("daily market bar is immutable")
            incoming[key] = values
            groups.setdefault((key[1], key[2]), set()).add(key[0])

        for (trade_date_value, source), symbols in groups.items():
            stored = {
                (str(row[0]), str(row[1]), str(row[9])): tuple(row)
                for row in connection.execute(
                    """
                    SELECT symbol, trade_date, open_1e4, high_1e4, low_1e4, close_1e4,
                           pre_close_1e4, volume_shares, amount_fen, source, source_timestamp
                    FROM daily_bars WHERE trade_date = ? AND source = ?
                    """,
                    (trade_date_value, source),
                ).fetchall()
                if str(row[0]) in symbols
            }
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
        with self.connect() as connection:
            meta = connection.execute(
                """
                SELECT trade_date, source_timestamp, advance_ratio_bps, above_ma20_ratio_bps
                FROM market_snapshots
                WHERE source = ? AND trade_date BETWEEN ? AND ? ORDER BY trade_date
                """,
                (source, start.isoformat(), end.isoformat()),
            ).fetchall()
            snapshots: list[MarketSnapshot] = []
            for row in meta:
                target = date.fromisoformat(row[0])
                security_rows = connection.execute(
                    """
                    SELECT symbol, name, exchange, board, list_date, industry, is_st
                    FROM snapshot_securities
                    WHERE trade_date = ? AND source = ? ORDER BY symbol
                    """,
                    (row[0], source),
                ).fetchall()
                securities = tuple(
                    Security(
                        symbol=item[0],
                        name=item[1],
                        exchange=item[2],
                        board=item[3],
                        list_date=date.fromisoformat(item[4]),
                        industry=item[5],
                        is_st=bool(item[6]),
                    )
                    for item in security_rows
                )
                bars = tuple(
                    bar
                    for security in securities
                    for bar in self.load_symbol_history(
                        security.symbol, end_date=target, source=source, limit=history_limit
                    )
                )
                snapshots.append(
                    MarketSnapshot(
                        trade_date=target,
                        source=source,
                        source_timestamp=datetime.fromisoformat(row[1]),
                        securities=securities,
                        bars=bars,
                        advance_ratio_bps=int(row[2]),
                        above_ma20_ratio_bps=int(row[3]),
                    )
                )
        return tuple(snapshots)

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
                "SELECT parameters_hash, dataset_hash FROM replay_attestations WHERE version = ?",
                (version,),
            ).fetchone()
            if row is None or row[0] != parameters_hash or row[1] is None:
                return False
            connection.execute("DELETE FROM replay_attestations WHERE version = ?", (version,))
            return True

    def consume_strategy_activation_grants(self, version: str, parameters_hash: str) -> str:
        """Atomically consume the replay proof and operator approval."""

        with self._idempotent_write_connection() as connection:
            approval = connection.execute(
                "SELECT parameters_hash FROM strategy_approvals WHERE version = ?",
                (version,),
            ).fetchone()
            if approval is None or approval[0] != parameters_hash:
                return "operator_approval_required"
            replay = connection.execute(
                "SELECT parameters_hash, dataset_hash FROM replay_attestations WHERE version = ?",
                (version,),
            ).fetchone()
            if replay is None or replay[0] != parameters_hash or replay[1] is None:
                return "replay_attestation_required"
            connection.execute("DELETE FROM replay_attestations WHERE version = ?", (version,))
            connection.execute("DELETE FROM strategy_approvals WHERE version = ?", (version,))
            return "ok"

    def activate_strategy_version_with_grants(self, version: str, parameters_hash: str) -> str:
        """Consume both grants and update the active pointer in one transaction."""

        with self._idempotent_write_connection() as connection:
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
            replay = connection.execute(
                """
                SELECT parameters_hash, dataset_hash FROM replay_attestations
                WHERE version = ?
                """,
                (version,),
            ).fetchone()
            if replay is None or replay[0] != parameters_hash or replay[1] is None:
                return "replay_attestation_required"
            connection.execute(
                """
                INSERT INTO active_strategy(singleton, version) VALUES (1, ?)
                ON CONFLICT(singleton) DO UPDATE SET version=excluded.version
                """,
                (version,),
            )
            connection.execute("DELETE FROM replay_attestations WHERE version = ?", (version,))
            connection.execute("DELETE FROM strategy_approvals WHERE version = ?", (version,))
            return "ok"

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
