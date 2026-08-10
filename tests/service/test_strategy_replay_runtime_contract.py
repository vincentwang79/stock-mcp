"""Offline service contracts for bounded, background strategy replay work."""

from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from stock_mcp.config import Settings

SHANGHAI = ZoneInfo("Asia/Shanghai")


class _Database:
    def initialize(self) -> None:
        return


class _Scheduler:
    def configure(self, *, timezone: str) -> None:
        self.timezone = timezone

    def start(self) -> None:
        self.started = True


class _McpServer:
    runtime_available = True


class _ReplayRunner:
    def __init__(self) -> None:
        self.resumed = 0
        self.background_starts = 0
        self.replay_execution_attempts = 0

    def requeue_interrupted(self) -> None:
        self.resumed += 1

    def start_background(self) -> None:
        self.background_starts += 1

    def run_replay_now(self) -> None:
        self.replay_execution_attempts += 1
        raise AssertionError("runtime construction must never run replay work inline")


class StrategyReplayRuntimeContractTest(unittest.TestCase):
    def test_runtime_requeues_interrupted_work_and_starts_one_background_runner(self) -> None:
        from stock_mcp.service import build_runtime

        with TemporaryDirectory() as temporary:
            runner = _ReplayRunner()
            runtime = build_runtime(
                Settings(root=Path(temporary)),
                dependencies={
                    "database": _Database(),
                    "application": object(),
                    "mcp_server": _McpServer(),
                    "scheduler": _Scheduler(),
                    "replay_runner": runner,
                },
            )

        registered_runner = getattr(runtime, "replay_runner", None)
        self.assertIs(runner, registered_runner)
        if registered_runner is None:
            return
        self.assertEqual(1, runner.resumed)
        self.assertEqual(1, runner.background_starts)
        self.assertEqual(0, runner.replay_execution_attempts)

    def test_replay_blackout_window_is_inclusive_and_uses_china_standard_time(self) -> None:
        import stock_mcp.service as service

        is_strategy_replay_allowed = getattr(service, "is_strategy_replay_allowed", None)
        self.assertTrue(
            callable(is_strategy_replay_allowed), "service must expose replay window policy"
        )
        if not callable(is_strategy_replay_allowed):
            return

        self.assertFalse(
            is_strategy_replay_allowed(datetime(2026, 8, 10, 16, 20, tzinfo=SHANGHAI))
        )
        self.assertFalse(
            is_strategy_replay_allowed(datetime(2026, 8, 10, 18, 10, tzinfo=SHANGHAI))
        )
        self.assertTrue(
            is_strategy_replay_allowed(datetime(2026, 8, 10, 18, 11, tzinfo=SHANGHAI))
        )


if __name__ == "__main__":
    unittest.main()
