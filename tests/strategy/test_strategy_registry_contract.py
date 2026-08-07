from __future__ import annotations

import unittest

from stock_mcp.domain import StrategyVersion
from stock_mcp.strategy import StrategyRegistry


def _proposal(*, version: str = "v0.1-proposed", minimum: int = 5_500) -> StrategyVersion:
    return StrategyVersion(
        version=version,
        status="proposed",
        parameters={
            "offensive_min_bps": minimum,
            "defensive_max_bps": 4_000,
            "neutral_limit": 2,
            "offensive_limit": 3,
        },
    )


class StrategyRegistryContractTest(unittest.TestCase):
    def test_only_proposed_versions_can_be_registered(self) -> None:
        registry = StrategyRegistry()
        already_active = StrategyVersion(
            version="v0.1",
            status="active",
            parameters=_proposal().parameters,
        )

        with self.assertRaises(ValueError):
            registry.propose(already_active)

    def test_activation_requires_explicit_confirmation_and_records_active_version(self) -> None:
        registry = StrategyRegistry()
        proposal = _proposal()
        registry.propose(proposal)

        with self.assertRaises(ValueError):
            registry.activate(proposal.version, confirmed=False)

        active = registry.activate(proposal.version, confirmed=True)

        self.assertEqual(active.version, proposal.version)
        self.assertEqual(active.status, "active")
        self.assertEqual(registry.active_version, proposal.version)

    def test_version_parameters_are_immutable_after_proposal(self) -> None:
        registry = StrategyRegistry()
        proposal = _proposal()
        registry.propose(proposal)

        with self.assertRaises(ValueError):
            registry.propose(_proposal(minimum=6_000))

        self.assertEqual(registry.get(proposal.version).parameters["offensive_min_bps"], 5_500)

    def test_activating_a_new_version_does_not_rewrite_the_previous_version(self) -> None:
        registry = StrategyRegistry()
        first = _proposal(version="v0.1-proposed")
        second = _proposal(version="v0.2-proposed", minimum=6_000)
        registry.propose(first)
        registry.activate(first.version, confirmed=True)
        registry.propose(second)
        registry.activate(second.version, confirmed=True)

        self.assertEqual(registry.get(first.version).version, first.version)
        self.assertEqual(registry.get(first.version).parameters, first.parameters)
        self.assertEqual(registry.active_version, second.version)

    def test_versions_can_be_listed_deterministically_for_the_public_tool(self) -> None:
        registry = StrategyRegistry()
        registry.propose(_proposal(version="v0.2-proposed", minimum=6_000))
        registry.propose(_proposal(version="v0.1-proposed"))

        self.assertEqual(
            ("v0.1-proposed", "v0.2-proposed"),
            tuple(version.version for version in registry.list_versions()),
        )

    def test_incomplete_or_out_of_range_parameters_cannot_be_activated(self) -> None:
        registry = StrategyRegistry()
        registry.propose(
            StrategyVersion(
                version="incomplete",
                status="proposed",
                parameters={"offensive_limit": 3},
            )
        )
        with self.assertRaises(ValueError):
            registry.propose(
                StrategyVersion(
                    version="invalid",
                    status="proposed",
                    parameters={**_proposal().parameters, "neutral_limit": -1},
                )
            )

        with self.assertRaises(ValueError):
            registry.activate("incomplete", confirmed=True)
