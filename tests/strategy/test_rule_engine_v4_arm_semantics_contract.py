"""Offline behavioral contracts for preregistered v4 arm semantics."""

from __future__ import annotations

import unittest
from dataclasses import replace

from stock_mcp import v3, v4
from stock_mcp.v4_research import _restrict_v4_market
from tests.strategy.test_rule_engine_v3_contract import (
    _breakout_input,
    _market,
    _pullback_input,
    _v3_domain,
    _v3_strategy,
)


def _generate_v4_review(
    *,
    market: object,
    strategy: object,
    prior_four_breadth: tuple[object, ...],
    features: dict[str, dict[str, object]],
    arm_id: str,
) -> object:
    """Use the v4 public seam, preserving an observable legacy RED fallback."""

    generate = getattr(v4, "generate_v4_daily_review", None)
    if callable(generate):
        return generate(
            market=market,
            strategy=strategy,
            prior_four_breadth=prior_four_breadth,
            features=features,
            arm_id=arm_id,
        )
    return v3.generate_v3_daily_review(market, strategy)


class V4ArmSemanticsContractTest(unittest.TestCase):
    def test_current_breadth_uses_the_same_mainboard_non_st_listing_eligibility(self) -> None:
        eligible = _breakout_input(self, "600501.SH")
        st_inputs = tuple(
            replace(
                _pullback_input(self, f"60050{index}.SH"),
                security=replace(
                    _pullback_input(self, f"60050{index}.SH").security,
                    is_st=True,
                ),
            )
            for index in range(2, 6)
        )
        market = _market(self, (eligible, *st_inputs))

        restricted = _restrict_v4_market(
            market, {item.security.symbol for item in market.securities}
        )

        self.assertEqual(1, restricted.breadth.eligible_count)
        self.assertEqual(10_000, restricted.breadth.advance_ratio_bps)

    def test_trend_challenger_replenishes_filtered_top_breakout_from_full_qualified_pool(
        self,
    ) -> None:
        inputs = tuple(_breakout_input(self, f"60051{index}.SH") for index in range(1, 5))
        market = _market(
            self,
            inputs,
            advance_count=2,
            eligible_count=4,
            above_ma20_count=2,
            ma20_eligible_count=4,
            advance_ratio_bps=5_000,
            above_ma20_ratio_bps=5_000,
        )
        features = {
            item.security.symbol: {
                "return_20d_bps": 1_000,
                "ma20_rising_5d": item.security.symbol != "600511.SH",
            }
            for item in inputs
        }

        review = _generate_v4_review(
            market=market,
            strategy=_v3_strategy(),
            prior_four_breadth=(market.breadth,) * 4,
            features=features,
            arm_id="v4-trend-quality",
        )

        self.assertEqual(("600512.SH",), tuple(item.symbol for item in review.candidates))

    def test_breadth_median_challenger_uses_five_days_to_recompute_regime_and_quotas(
        self,
    ) -> None:
        inputs = (
            _pullback_input(self, "600521.SH"),
            _pullback_input(self, "600522.SH"),
            _breakout_input(self, "600523.SH"),
            _breakout_input(self, "600524.SH"),
        )
        market = _market(self, inputs)
        domain = _v3_domain(self)
        defensive_breadth = domain.V3BreadthFacts(
            advance_count=3,
            eligible_count=10,
            above_ma20_count=3,
            ma20_eligible_count=10,
            advance_ratio_bps=3_000,
            above_ma20_ratio_bps=3_000,
        )

        review = _generate_v4_review(
            market=market,
            strategy=_v3_strategy(),
            prior_four_breadth=(defensive_breadth,) * 4,
            features={},
            arm_id="v4-breadth-five-day-median",
        )

        self.assertEqual("defensive", review.market_regime)
        self.assertEqual((), tuple(item.symbol for item in review.candidates))

    def test_signal_quality_challenger_reranks_full_qualified_pool_before_neutral_quota(
        self,
    ) -> None:
        inputs = tuple(_pullback_input(self, f"60053{index}.SH") for index in range(1, 5))
        market = _market(
            self,
            inputs,
            advance_count=2,
            eligible_count=4,
            above_ma20_count=2,
            ma20_eligible_count=4,
            advance_ratio_bps=5_000,
            above_ma20_ratio_bps=5_000,
        )
        features = {
            item.security.symbol: {
                "primary_percentile_bps": 10_000 if item.security.symbol == "600534.SH" else 0,
                "amount_percentile_bps": 10_000 if item.security.symbol == "600534.SH" else 0,
            }
            for item in inputs
        }

        review = _generate_v4_review(
            market=market,
            strategy=_v3_strategy(),
            prior_four_breadth=(market.breadth,) * 4,
            features=features,
            arm_id="v4-signal-quality-rank",
        )

        self.assertEqual(("600534.SH",), tuple(item.symbol for item in review.candidates))


if __name__ == "__main__":
    unittest.main()
