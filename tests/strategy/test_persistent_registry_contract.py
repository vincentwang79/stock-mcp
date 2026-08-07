from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stock_mcp.domain import StrategyVersion
from stock_mcp.storage import Database
from stock_mcp.strategy import DatabaseStrategyRegistry


def _proposal(version: str = "v0.1-proposed") -> StrategyVersion:
    return StrategyVersion(
        version=version,
        status="proposed",
        parameters={
            "offensive_limit": 3,
            "neutral_limit": 2,
            "offensive_min_bps": 5_500,
            "defensive_max_bps": 4_000,
        },
    )


class PersistentStrategyRegistryContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = Database(Path(self.temp_dir.name) / "stock.sqlite3")
        self.database.initialize()

    def test_proposal_and_explicit_activation_survive_process_restart(self) -> None:
        registry = DatabaseStrategyRegistry(self.database)
        proposal = registry.propose(_proposal())

        with self.assertRaises(ValueError):
            registry.activate(proposal.version, confirmed=False)
        with self.assertRaisesRegex(ValueError, "operator approval"):
            registry.activate(proposal.version, confirmed=True)
        self.database.approve_strategy_version(proposal.version)
        active = registry.activate(proposal.version, confirmed=True)
        reopened = DatabaseStrategyRegistry(Database(self.database.path))

        self.assertEqual("active", active.status)
        self.assertEqual(proposal.version, reopened.active_version)
        self.assertEqual("active", reopened.get(proposal.version).status)
        self.assertEqual(
            (proposal.version,), tuple(item.version for item in reopened.list_versions())
        )
        self.assertEqual(
            "proposed",
            self.database.load_strategy_version(proposal.version).status,
            "activation is a pointer and must not rewrite the immutable proposal",
        )
        replayed = registry.activate(proposal.version, confirmed=True)
        self.assertEqual("active", replayed.status, "a lost activation response is retry-safe")

    def test_duplicate_version_with_changed_parameters_is_rejected(self) -> None:
        registry = DatabaseStrategyRegistry(self.database)
        registry.propose(_proposal())
        changed = StrategyVersion(
            version="v0.1-proposed",
            status="proposed",
            parameters={"offensive_limit": 2},
        )

        with self.assertRaises(ValueError):
            registry.propose(changed)


if __name__ == "__main__":
    unittest.main()
