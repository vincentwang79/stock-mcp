"""Bounded synchronization and validation of the live v3 evidence window."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Protocol


class TradingCalendar(Protocol):
    def prior_trading_days(self, target: date, count: int) -> tuple[date, ...]: ...


class LiveEvidenceSyncError(ValueError):
    def __init__(self, report: dict[str, object]) -> None:
        self.report = report
        super().__init__(
            "live evidence synchronization is incomplete: "
            + json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=lambda value: value.isoformat() if isinstance(value, date) else str(value),
            )
        )


class LiveEvidenceSynchronizer:
    """Synchronize only the recorded sessions needed by one live v3 evaluation."""

    def __init__(
        self,
        database: Any,
        *,
        price_sync: Callable[[tuple[date, ...]], None],
        status_sync: Callable[[tuple[date, ...]], None],
        minimum_price_count: int,
        minimum_status_count: int,
    ) -> None:
        if minimum_price_count < 1 or minimum_status_count < 1:
            raise ValueError("minimum evidence coverage counts must be positive")
        self._database = database
        self._price_sync = price_sync
        self._status_sync = status_sync
        self._minimum_price_count = minimum_price_count
        self._minimum_status_count = minimum_status_count

    def sync(
        self,
        target: date,
        *,
        calendar: TradingCalendar,
        expected_symbols: frozenset[str],
        include_target_price: bool = False,
        simulation_sessions: int = 1,
    ) -> dict[str, object]:
        if simulation_sessions < 1 or simulation_sessions > 20:
            raise ValueError("live evidence simulation sessions must be between one and twenty")
        if not expected_symbols:
            raise ValueError("live evidence synchronization requires an expected universe")
        prior_count = 60 + simulation_sessions - 1
        prior = calendar.prior_trading_days(target, prior_count)
        required = (*prior, target)
        price_required = required if include_target_price else prior
        missing_prices = self._missing_price_dates(price_required)
        missing_statuses = self._missing_status_dates(required, expected_symbols)
        repair_errors: list[dict[str, str]] = []
        if missing_prices:
            try:
                self._price_sync(missing_prices)
            except Exception as error:
                repair_errors.append({"stage": "price_sync", "error_class": type(error).__name__})
        if missing_statuses:
            try:
                self._status_sync(missing_statuses)
            except Exception as error:
                repair_errors.append({"stage": "status_sync", "error_class": type(error).__name__})
        remaining_prices = self._missing_price_dates(price_required)
        remaining_statuses = self._missing_status_dates(required, expected_symbols)
        report: dict[str, object] = {
            "schema": "live-evidence-window-v1",
            "status": (
                "ready"
                if not remaining_prices and not remaining_statuses and not repair_errors
                else "incomplete"
            ),
            "trade_date": target.isoformat(),
            "window_start": required[0].isoformat(),
            "window_end": target.isoformat(),
            "window_session_count": len(required),
            "repaired_price_dates": missing_prices,
            "repaired_status_dates": missing_statuses,
            "price_gap_days_after": len(remaining_prices),
            "status_gap_days_after": len(remaining_statuses),
            "remaining_price_dates": tuple(day.isoformat() for day in remaining_prices),
            "remaining_status_dates": tuple(day.isoformat() for day in remaining_statuses),
            "repair_errors": tuple(repair_errors),
        }
        if report["status"] != "ready":
            raise LiveEvidenceSyncError(report)
        return report

    def _missing_price_dates(self, days: tuple[date, ...]) -> tuple[date, ...]:
        return tuple(
            day
            for day in days
            if not self._database.has_complete_market_snapshot(
                day,
                source="tushare",
                minimum_main_board_count=self._minimum_price_count,
            )
        )

    def _missing_status_dates(
        self, days: tuple[date, ...], expected_symbols: frozenset[str]
    ) -> tuple[date, ...]:
        return tuple(
            day
            for day in days
            if not self._database.has_complete_daily_security_status(
                day,
                source="baostock",
                expected_symbols=expected_symbols,
                minimum_count=self._minimum_status_count,
            )
        )


def synchronize_production_live_evidence(
    settings: Any,
    database: Any,
    *,
    target: date,
    calendar: TradingCalendar,
    expected_symbols: frozenset[str],
    minimum_price_count: int,
    minimum_status_count: int,
    include_target_price: bool = False,
    simulation_sessions: int = 1,
) -> dict[str, object]:
    """Synchronize a bounded production window using only formal providers."""

    def sync_prices(days: tuple[date, ...]) -> None:
        if not days:
            return
        from .backfill import run_production_backfill

        result = run_production_backfill(
            settings,
            database,
            min(days),
            max(days),
            minimum_main_board_count=minimum_price_count,
        )
        unresolved = tuple(day for day in days if day in result.incomplete_dates)
        if unresolved:
            raise LiveEvidenceSyncError(
                {
                    "schema": "live-evidence-window-v1",
                    "status": "incomplete",
                    "remaining_price_dates": tuple(day.isoformat() for day in unresolved),
                }
            )

    def sync_statuses(days: tuple[date, ...]) -> None:
        if not days:
            return
        _sync_baostock_status_days(
            database,
            days,
            minimum_main_board_count=minimum_status_count,
        )

    return LiveEvidenceSynchronizer(
        database,
        price_sync=sync_prices,
        status_sync=sync_statuses,
        minimum_price_count=minimum_price_count,
        minimum_status_count=minimum_status_count,
    ).sync(
        target,
        calendar=calendar,
        expected_symbols=expected_symbols,
        include_target_price=include_target_price,
        simulation_sessions=simulation_sessions,
    )


def _sync_baostock_status_days(
    database: Any,
    sessions: tuple[date, ...],
    *,
    minimum_main_board_count: int,
) -> None:
    import baostock  # type: ignore[import-not-found]

    from .backfill import backfill_baostock_daily_statuses

    login = getattr(baostock, "login", None)
    logout = getattr(baostock, "logout", None)
    if not callable(login) or not callable(logout):
        raise ValueError("BaoStock client does not provide login/logout")
    original_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(30.0)
    result = login()
    if str(getattr(result, "error_code", "0")) != "0":
        socket.setdefaulttimeout(original_timeout)
        raise RuntimeError("BaoStock login failed")
    try:
        backfill_baostock_daily_statuses(
            database=database,
            client=baostock,
            sessions=sessions,
            source_timestamp=datetime.now(UTC).isoformat(),
            login=login,
            logout=logout,
            minimum_main_board_count=minimum_main_board_count,
        )
    finally:
        try:
            logout()
        finally:
            socket.setdefaulttimeout(original_timeout)
