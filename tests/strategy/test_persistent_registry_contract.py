from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from hashlib import sha256
from pathlib import Path

from stock_mcp.domain import StrategyVersion
from stock_mcp.storage import Database
from stock_mcp.strategy import DatabaseStrategyRegistry


def _proposal(version: str = "v0.1-proposed") -> StrategyVersion:
    return StrategyVersion(
        version=version,
        status="proposed",
        parameters={
            "rule_engine_version": 1,
            "offensive_limit": 3,
            "neutral_limit": 2,
            "offensive_min_bps": 5_500,
            "defensive_max_bps": 4_000,
            "min_liquidity_amount_fen": 2_000_000_000,
            "max_consecutive_limit_up_days": 2,
            "strong_pullback_min_prior_gain_bps": 1_000,
            "strong_pullback_max_pullback_bps": 800,
            "volume_breakout_min_volume_ratio_bps": 15_000,
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
        record_attestation = getattr(self.database, "record_governance_replay_attestation", None)
        self.assertTrue(callable(record_attestation))
        record_attestation(
            proposal.version,
            _parameters_hash(dict(proposal.parameters)),
            "0" * 64,
            date(2023, 1, 1),
            date(2025, 1, 1),
            400,
        )
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

    def test_activation_requires_a_replay_attestation(self) -> None:
        database = _AttestableStrategyRepository()
        registry = DatabaseStrategyRegistry(database)
        proposal = registry.propose(_proposal())
        database.approve_strategy_version(proposal.version)

        with self.assertRaisesRegex(ValueError, "replay attestation"):
            registry.activate(proposal.version, confirmed=True)

    def test_activation_rejects_an_attestation_for_a_different_parameter_hash(self) -> None:
        database = _AttestableStrategyRepository()
        registry = DatabaseStrategyRegistry(database)
        proposal = registry.propose(_proposal())
        database.approve_strategy_version(proposal.version)
        replayed_parameters = dict(proposal.parameters)
        replayed_parameters["offensive_limit"] = 2
        database.record_replay_attestation(proposal.version, _parameters_hash(replayed_parameters))

        with self.assertRaisesRegex(ValueError, "replay attestation"):
            registry.activate(proposal.version, confirmed=True)

    def test_activation_pointer_and_grant_consumption_are_one_transaction(self) -> None:
        registry = DatabaseStrategyRegistry(self.database)
        proposal = registry.propose(_proposal())
        parameters_hash = _parameters_hash(dict(proposal.parameters))
        self.database.approve_strategy_version(proposal.version)
        self.database.record_governance_replay_attestation(
            proposal.version,
            parameters_hash,
            "0" * 64,
            date(2023, 1, 1),
            date(2025, 1, 1),
            400,
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_active_strategy BEFORE INSERT ON active_strategy
                BEGIN SELECT RAISE(ABORT, 'simulated active pointer failure'); END
                """
            )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "simulated active pointer failure"):
            registry.activate(proposal.version, confirmed=True)

        with self.database.connect() as connection:
            connection.execute("DROP TRIGGER fail_active_strategy")
        active = registry.activate(proposal.version, confirmed=True)
        self.assertEqual("active", active.status)
        self.assertEqual(proposal.version, registry.active_version)


class _AttestableStrategyRepository:
    """Minimal durable-registry seam, kept offline for activation governance tests."""

    def __init__(self) -> None:
        self._versions: dict[str, StrategyVersion] = {}
        self._active_version: str | None = None
        self._operator_approvals: set[str] = set()
        self._replay_attestations: dict[str, str] = {}

    def save_strategy_version(self, strategy: StrategyVersion) -> None:
        existing = self._versions.get(strategy.version)
        if existing is not None and existing != strategy:
            raise ValueError("strategy version is immutable")
        self._versions[strategy.version] = strategy

    def load_strategy_version(self, version: str) -> StrategyVersion | None:
        return self._versions.get(version)

    def list_strategy_versions(self) -> tuple[StrategyVersion, ...]:
        return tuple(self._versions.values())

    def get_active_strategy_version(self) -> StrategyVersion | None:
        return None if self._active_version is None else self._versions[self._active_version]

    def set_active_strategy_version(self, version: str) -> None:
        self._active_version = version

    def approve_strategy_version(self, version: str) -> None:
        self._operator_approvals.add(version)

    def consume_strategy_approval(self, version: str) -> bool:
        return version in self._operator_approvals

    def record_replay_attestation(self, version: str, parameters_hash: str) -> None:
        self._replay_attestations[version] = parameters_hash

    def consume_replay_attestation(self, version: str, parameters_hash: str) -> bool:
        return self._replay_attestations.get(version) == parameters_hash


def _parameters_hash(parameters: dict[str, int]) -> str:
    """The activation seam must bind attestations to canonical version parameters."""

    encoded = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()
