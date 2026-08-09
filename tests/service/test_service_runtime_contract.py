"""Contracts for the single-process MCP service runtime.

These tests exercise dependency-injected fakes only.  Starting the real HTTP
transport or contacting a market-data provider is deliberately out of scope:
the Windows service needs a deterministic local readiness surface even while
configuration is incomplete.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from stock_mcp.config import Settings


class _Database:
    def __init__(self) -> None:
        self.initialized = 0
        self.readiness_checks = 0
        self.full_integrity_checks = 0

    def initialize(self) -> None:
        self.initialized += 1

    def doctor(self) -> dict[str, str]:
        self.full_integrity_checks += 1
        return {"integrity": "ok"}

    def is_ready(self) -> bool:
        self.readiness_checks += 1
        return True


class _Scheduler:
    def __init__(self) -> None:
        self.timezone: str | None = None
        self.started = 0
        self.jobs: list[tuple[object, str, dict[str, object]]] = []

    def configure(self, *, timezone: str) -> None:
        self.timezone = timezone

    def start(self) -> None:
        self.started += 1

    def add_job(self, function: object, trigger: str, **kwargs: object) -> None:
        self.jobs.append((function, trigger, kwargs))


class _McpServer:
    def __init__(self, *, runtime_available: bool) -> None:
        self.runtime_available = runtime_available


class ServiceRuntimeContractTest(unittest.TestCase):
    def _settings(self, root: Path, *, configured: bool = False) -> Settings:
        if configured:
            return Settings(
                root=root,
                tushare_token="test-token",
                tunnel_id="test-tunnel",
                tunnel_api_key="test-key",
            )
        return Settings(root=root)

    def _dependencies(
        self,
        *,
        runtime_available: bool = True,
    ) -> tuple[dict[str, object], _Database, _Scheduler]:
        database = _Database()
        scheduler = _Scheduler()
        return (
            {
                "database": database,
                "application": object(),
                "mcp_server": _McpServer(runtime_available=runtime_available),
                "scheduler": scheduler,
            },
            database,
            scheduler,
        )

    def test_missing_production_configuration_is_healthy_but_not_ready(self) -> None:
        from stock_mcp.service import build_runtime, health

        with TemporaryDirectory() as temporary:
            dependencies, database, scheduler = self._dependencies()
            runtime = build_runtime(self._settings(Path(temporary)), dependencies=dependencies)

        payload = health(runtime)

        self.assertEqual("healthy", payload["healthz"])
        self.assertEqual("configuration_required", payload["readyz"])
        self.assertEqual(("TUSHARE_TOKEN",), payload["missing"])
        self.assertEqual("127.0.0.1", payload["host"])
        self.assertEqual("/mcp", payload["mcp_path"])
        self.assertEqual(1, database.initialized)
        self.assertEqual("Asia/Shanghai", scheduler.timezone)
        self.assertEqual(1, scheduler.started)

    def test_configured_service_reports_unavailable_mcp_runtime_without_crashing(self) -> None:
        from stock_mcp.service import build_runtime, health

        with TemporaryDirectory() as temporary:
            dependencies, database, _scheduler = self._dependencies(runtime_available=False)
            runtime = build_runtime(
                self._settings(Path(temporary), configured=True), dependencies=dependencies
            )

        payload = health(runtime)

        self.assertEqual("healthy", payload["healthz"])
        self.assertEqual("mcp_runtime_unavailable", payload["readyz"])
        self.assertEqual("ok", payload["database"]["integrity"])
        self.assertEqual(1, database.initialized)

    def test_injected_runtime_never_constructs_providers_or_opens_a_listener(self) -> None:
        from stock_mcp.service import build_runtime, health

        with TemporaryDirectory() as temporary:
            dependencies, _database, _scheduler = self._dependencies()
            runtime = build_runtime(self._settings(Path(temporary)), dependencies=dependencies)

        self.assertFalse(getattr(runtime, "listener_started", False))
        self.assertEqual("configuration_required", health(runtime)["readyz"])

    def test_runtime_rejects_non_loopback_or_nonstandard_mcp_endpoint(self) -> None:
        from stock_mcp.service import build_runtime

        with TemporaryDirectory() as temporary:
            dependencies, _database, _scheduler = self._dependencies()
            unsafe = Settings(root=Path(temporary), host="0.0.0.0")

            with self.assertRaisesRegex(ValueError, "127\\.0\\.0\\.1"):
                build_runtime(unsafe, dependencies=dependencies)

            unsafe_path = Settings(root=Path(temporary), mcp_path="/private")
            with self.assertRaisesRegex(ValueError, "/mcp"):
                build_runtime(unsafe_path, dependencies=dependencies)

    def test_mcp_readiness_route_does_not_run_a_full_database_integrity_scan(self) -> None:
        try:
            from stock_mcp.service import _mcp_route_health
        except ImportError as error:
            self.fail(f"constant-time MCP readiness is not implemented: {error}")

        with TemporaryDirectory() as temporary:
            database = _Database()
            payload = _mcp_route_health(
                self._settings(Path(temporary), configured=True), database
            )

        self.assertEqual({"healthz": "healthy", "readyz": "ready"}, payload)
        self.assertEqual(1, database.readiness_checks)
        self.assertEqual(0, database.full_integrity_checks)

    def test_production_scheduler_registers_one_coalesced_singleton_job(self) -> None:
        from stock_mcp.service import _register_post_market_job

        scheduler = _Scheduler()
        task = object()
        restart = datetime(2026, 8, 7, 17, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
        _register_post_market_job(scheduler, task, now=restart)

        self.assertEqual(1, len(scheduler.jobs))
        function, trigger, options = scheduler.jobs[0]
        self.assertIs(task, function)
        self.assertEqual("cron", trigger)
        self.assertEqual("stock-mcp-post-market", options["id"])
        self.assertEqual(1, options["max_instances"])
        self.assertTrue(options["coalesce"])
        self.assertEqual("Asia/Shanghai", options["timezone"])
        self.assertEqual(restart, options["next_run_time"])


if __name__ == "__main__":
    unittest.main()
