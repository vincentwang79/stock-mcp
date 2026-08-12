"""Offline contracts for production v4 research execution ownership."""

from __future__ import annotations

import socket
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from stock_mcp.config import Settings


class V4ResearchExecutionRuntimeContractTest(unittest.TestCase):
    def test_default_composition_injects_the_executor_and_research_window_policy(self) -> None:
        import stock_mcp.production as production
        import stock_mcp.service as service

        created: list[_CapturedV4ResearchCoordinator] = []

        def capture(database: object, **kwargs: object) -> _CapturedV4ResearchCoordinator:
            coordinator = _CapturedV4ResearchCoordinator(database, **kwargs)
            created.append(coordinator)
            return coordinator

        with (
            TemporaryDirectory() as temporary,
            patch.object(service, "V4ResearchCoordinator", side_effect=capture),
            patch.object(
                socket.socket,
                "connect",
                autospec=True,
                side_effect=AssertionError("MCP composition must not open a network connection"),
            ) as connect,
        ):
            service._default_dependencies(Settings(root=Path(temporary), tushare_token="fixture"))

        self.assertEqual(1, len(created))
        self.assertTrue(callable(created[0].kwargs.get("step_executor")))
        self.assertIs(production.is_v4_research_allowed, created[0].kwargs.get("allowed"))
        connect.assert_not_called()

    def test_mcp_service_lifecycle_recovers_starts_and_stops_v4_work_without_network_io(
        self,
    ) -> None:
        import stock_mcp.service as service

        database = _Database()
        scheduler = _Scheduler()
        v4_runner = _V4Runner()
        dependencies = {
            "database": database,
            "application": object(),
            "mcp_server": _McpServer(),
            "scheduler": scheduler,
            "v4_research_runner": v4_runner,
        }
        with TemporaryDirectory() as temporary:
            settings = Settings(root=Path(temporary), tushare_token="fixture")
            with (
                patch.object(service, "_default_dependencies", return_value=dependencies),
                patch.object(service, "_run_mcp", return_value=0),
                patch.object(
                    socket.socket,
                    "connect",
                    autospec=True,
                    side_effect=AssertionError("MCP startup must not contact a live endpoint"),
                ) as connect,
            ):
                self.assertEqual(0, service.serve(settings))

        self.assertEqual(1, database.initialized)
        self.assertEqual(1, scheduler.started)
        self.assertEqual(1, v4_runner.recovered)
        self.assertEqual(1, v4_runner.started)
        self.assertEqual(1, v4_runner.stopped)
        connect.assert_not_called()


class _CapturedV4ResearchCoordinator:
    def __init__(self, database: object, **kwargs: object) -> None:
        self.database = database
        self.kwargs = kwargs


class _Database:
    def __init__(self) -> None:
        self.initialized = 0

    def initialize(self) -> None:
        self.initialized += 1

    def doctor(self) -> dict[str, str]:
        return {"integrity": "ok"}


class _Scheduler:
    def __init__(self) -> None:
        self.started = 0

    def configure(self, *, timezone: str) -> None:
        self.timezone = timezone

    def start(self) -> None:
        self.started += 1


class _McpServer:
    runtime_available = True


class _V4Runner:
    def __init__(self) -> None:
        self.recovered = 0
        self.started = 0
        self.stopped = 0

    def requeue_interrupted(self) -> None:
        self.recovered += 1

    def start_background(self) -> None:
        self.started += 1

    def stop_background(self) -> None:
        self.stopped += 1


if __name__ == "__main__":
    unittest.main()
