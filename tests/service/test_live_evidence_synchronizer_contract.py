from __future__ import annotations

import importlib.util
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from stock_mcp.live_evidence import (
    LiveEvidenceSyncError,
    LiveEvidenceSynchronizer,
    synchronize_production_live_evidence,
)


class _Calendar:
    def __init__(self, sessions: tuple[date, ...]) -> None:
        self.sessions = sessions

    def prior_trading_days(self, target: date, count: int) -> tuple[date, ...]:
        prior = tuple(day for day in self.sessions if day < target)
        if len(prior) < count:
            raise ValueError("insufficient fixture calendar")
        return prior[-count:]


class _Database:
    def __init__(self, prices: set[date], statuses: set[date]) -> None:
        self.prices = prices
        self.statuses = statuses

    def has_complete_market_snapshot(
        self, target: date, *, source: str, minimum_main_board_count: int
    ) -> bool:
        return source == "tushare" and minimum_main_board_count > 0 and target in self.prices

    def has_complete_daily_security_status(
        self,
        target: date,
        *,
        source: str,
        expected_symbols: frozenset[str],
        minimum_count: int,
    ) -> bool:
        return (
            source == "baostock"
            and bool(expected_symbols)
            and minimum_count > 0
            and target in self.statuses
        )


class LiveEvidenceSynchronizerContractTest(unittest.TestCase):
    def test_incomplete_window_error_keeps_the_full_serializable_gap_report(self) -> None:
        sessions = tuple(date(2026, 5, 1) + timedelta(days=index) for index in range(61))
        target = sessions[-1]
        missing = sessions[-2]
        database = _Database(set(sessions) - {missing}, set(sessions))
        synchronizer = LiveEvidenceSynchronizer(
            database,
            price_sync=lambda _days: None,
            status_sync=lambda _days: None,
            minimum_price_count=1,
            minimum_status_count=1,
        )

        with self.assertRaises(LiveEvidenceSyncError) as raised:
            synchronizer.sync(
                target,
                calendar=_Calendar(sessions),
                expected_symbols=frozenset({"600001.SH"}),
            )

        self.assertEqual((missing.isoformat(),), raised.exception.report["remaining_price_dates"])
        self.assertIn(missing.isoformat(), str(raised.exception))

    def test_production_price_repair_uses_the_full_snapshot_coverage_floor(self) -> None:
        sessions = tuple(date(2026, 5, 1) + timedelta(days=index) for index in range(61))
        target = sessions[-1]
        database = _Database(set(), set())

        def repair_prices(*_args, **kwargs):
            database.prices.update(sessions[:-1])
            self.assertEqual(7, kwargs["minimum_main_board_count"])
            return SimpleNamespace(incomplete_dates=())

        def repair_statuses(_database, days, **_kwargs):
            database.statuses.update(days)

        with (
            patch("stock_mcp.backfill.run_production_backfill", side_effect=repair_prices),
            patch(
                "stock_mcp.live_evidence._sync_baostock_status_days",
                side_effect=repair_statuses,
            ),
        ):
            report = synchronize_production_live_evidence(
                SimpleNamespace(),
                database,
                target=target,
                calendar=_Calendar(sessions),
                expected_symbols=frozenset({"600001.SH"}),
                minimum_price_count=7,
                minimum_status_count=1,
            )

        self.assertEqual("ready", report["status"])

    def test_shared_live_evidence_synchronizer_is_available(self) -> None:
        spec = importlib.util.find_spec("stock_mcp.live_evidence")
        self.assertIsNotNone(
            spec, "live evidence synchronization must be shared by CLI and service"
        )

    def test_missing_window_days_are_repaired_once_and_second_run_is_network_free(self) -> None:
        sessions = tuple(date(2026, 4, 1) + timedelta(days=index) for index in range(81))
        target = sessions[-1]
        database = _Database(set(sessions) - {sessions[-3]}, set(sessions) - {sessions[-2]})
        price_calls: list[tuple[date, ...]] = []
        status_calls: list[tuple[date, ...]] = []

        def repair_prices(days: tuple[date, ...]) -> None:
            price_calls.append(days)
            database.prices.update(days)

        def repair_statuses(days: tuple[date, ...]) -> None:
            status_calls.append(days)
            database.statuses.update(days)

        synchronizer = LiveEvidenceSynchronizer(
            database,
            price_sync=repair_prices,
            status_sync=repair_statuses,
            minimum_price_count=1,
            minimum_status_count=1,
        )
        first = synchronizer.sync(
            target,
            calendar=_Calendar(sessions),
            expected_symbols=frozenset({"600001.SH"}),
            include_target_price=True,
            simulation_sessions=20,
        )
        second = synchronizer.sync(
            target,
            calendar=_Calendar(sessions),
            expected_symbols=frozenset({"600001.SH"}),
            include_target_price=True,
            simulation_sessions=20,
        )

        self.assertEqual("ready", first["status"])
        self.assertEqual([sessions[-3]], list(first["repaired_price_dates"]))
        self.assertEqual([sessions[-2]], list(first["repaired_status_dates"]))
        self.assertEqual("ready", second["status"])
        self.assertEqual(1, len(price_calls))
        self.assertEqual(1, len(status_calls))
        self.assertEqual(0, second["price_gap_days_after"])
        self.assertEqual(0, second["status_gap_days_after"])

    def test_failed_repair_reports_all_remaining_days_and_can_resume(self) -> None:
        sessions = tuple(date(2026, 5, 1) + timedelta(days=index) for index in range(62))
        target = sessions[-1]
        missing = {sessions[-3], sessions[-2]}
        database = _Database(set(sessions) - missing, set(sessions))
        attempts = 0

        def repair_prices(days: tuple[date, ...]) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                database.prices.add(days[0])
                raise ConnectionError("fixture interruption")
            database.prices.update(days)

        synchronizer = LiveEvidenceSynchronizer(
            database,
            price_sync=repair_prices,
            status_sync=lambda days: database.statuses.update(days),
            minimum_price_count=1,
            minimum_status_count=1,
        )

        with self.assertRaises(LiveEvidenceSyncError) as raised:
            synchronizer.sync(
                target,
                calendar=_Calendar(sessions),
                expected_symbols=frozenset({"600001.SH"}),
                include_target_price=True,
            )
        self.assertEqual(
            ({"stage": "price_sync", "error_class": "ConnectionError"},),
            raised.exception.report["repair_errors"],
        )
        self.assertEqual(
            (sessions[-2].isoformat(),),
            raised.exception.report["remaining_price_dates"],
        )
        resumed = synchronizer.sync(
            target,
            calendar=_Calendar(sessions),
            expected_symbols=frozenset({"600001.SH"}),
            include_target_price=True,
        )

        self.assertEqual("ready", resumed["status"])
        self.assertEqual((sessions[-2],), resumed["repaired_price_dates"])

    def test_failed_repairs_keep_safe_nested_provider_reasons(self) -> None:
        sessions = tuple(date(2026, 5, 1) + timedelta(days=index) for index in range(61))
        target = sessions[-1]
        database = _Database(set(sessions[:-1]), set(sessions[:-1]))

        def fail_prices(_days: tuple[date, ...]) -> None:
            raise LiveEvidenceSyncError(
                {
                    "schema": "live-evidence-window-v1",
                    "status": "incomplete",
                    "remaining_price_dates": (target.isoformat(),),
                    "price_failures": (
                        {
                            "trade_date": target.isoformat(),
                            "error_class": "ProviderRuntimeError",
                            "message": "Tushare returned no bars for the configured universe",
                        },
                    ),
                }
            )

        def fail_statuses(_days: tuple[date, ...]) -> None:
            raise ValueError("BaoStock status day main-board coverage is incomplete")

        synchronizer = LiveEvidenceSynchronizer(
            database,
            price_sync=fail_prices,
            status_sync=fail_statuses,
            minimum_price_count=1,
            minimum_status_count=1,
        )

        with self.assertRaises(LiveEvidenceSyncError) as raised:
            synchronizer.sync(
                target,
                calendar=_Calendar(sessions),
                expected_symbols=frozenset({"600001.SH"}),
                include_target_price=True,
            )

        self.assertEqual(
            (
                {
                    "stage": "price_sync",
                    "error_class": "LiveEvidenceSyncError",
                    "details": {
                        "schema": "live-evidence-window-v1",
                        "status": "incomplete",
                        "remaining_price_dates": (target.isoformat(),),
                        "price_failures": (
                            {
                                "trade_date": target.isoformat(),
                                "error_class": "ProviderRuntimeError",
                                "message": "Tushare returned no bars for the configured universe",
                            },
                        ),
                    },
                },
                {
                    "stage": "status_sync",
                    "error_class": "ValueError",
                    "message": "BaoStock status day main-board coverage is incomplete",
                },
            ),
            raised.exception.report["repair_errors"],
        )


if __name__ == "__main__":
    unittest.main()
