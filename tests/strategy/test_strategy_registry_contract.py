from __future__ import annotations

import unittest
from dataclasses import replace

from stock_mcp.domain import StrategyVersion
from stock_mcp.strategy import StrategyRegistry


def _proposal(*, version: str = "v0.1-proposed", minimum: int = 5_500) -> StrategyVersion:
    return StrategyVersion(
        version=version,
        status="proposed",
        parameters={
            "rule_engine_version": 1,
            "offensive_min_bps": minimum,
            "defensive_max_bps": 4_000,
            "neutral_limit": 2,
            "offensive_limit": 3,
            "min_liquidity_amount_fen": 2_000_000_000,
            "max_consecutive_limit_up_days": 2,
            "strong_pullback_min_prior_gain_bps": 1_000,
            "strong_pullback_max_pullback_bps": 800,
            "volume_breakout_min_volume_ratio_bps": 15_000,
        },
    )


class StrategyRegistryContractTest(unittest.TestCase):
    def test_rule_engine_v2_can_be_proposed_but_unknown_engines_are_rejected(self) -> None:
        registry = StrategyRegistry()
        version_two = _proposal(version="v0.2-proposed")
        version_two = replace(
            version_two,
            parameters={**version_two.parameters, "rule_engine_version": 2},
        )

        stored = registry.propose(version_two)

        self.assertEqual(2, stored.parameters["rule_engine_version"])
        with self.assertRaisesRegex(ValueError, "rule_engine_version.*out of range"):
            registry.propose(
                replace(
                    _proposal(version="v0.3-proposed"),
                    parameters={
                        **_proposal().parameters,
                        "rule_engine_version": 3,
                    },
                )
            )

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

    def test_activation_rejects_each_missing_screening_threshold(self) -> None:
        required_screening_thresholds = (
            "rule_engine_version",
            "min_liquidity_amount_fen",
            "max_consecutive_limit_up_days",
            "strong_pullback_min_prior_gain_bps",
            "strong_pullback_max_pullback_bps",
            "volume_breakout_min_volume_ratio_bps",
        )

        for missing in required_screening_thresholds:
            with self.subTest(missing=missing):
                registry = StrategyRegistry()
                parameters = dict(_proposal().parameters)
                del parameters[missing]
                registry.propose(
                    StrategyVersion(
                        version=f"missing-{missing}",
                        status="proposed",
                        parameters=parameters,
                    )
                )

                with self.assertRaisesRegex(ValueError, missing):
                    registry.activate(f"missing-{missing}", confirmed=True)
