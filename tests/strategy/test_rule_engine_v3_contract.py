from __future__ import annotations

import hashlib
import importlib
import json
import unittest
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from types import ModuleType

from stock_mcp.domain import DailyBar, MarketSnapshot, Security, StrategyVersion
from stock_mcp.review import generate_daily_review

TRADE_DATE = date(2026, 8, 7)
AS_OF = datetime(2026, 8, 7, 8, 30, tzinfo=UTC)
SOURCE = "recorded-tushare"
GOLDEN_PATH = Path(__file__).with_name("fixtures") / "v1_v2_review_golden.json"


def _v3_module(test_case: unittest.TestCase) -> ModuleType:
    try:
        return importlib.import_module("stock_mcp.v3")
    except ModuleNotFoundError as error:
        test_case.fail(f"rule engine v3 public module is required: {error}")


def _v3_domain(test_case: unittest.TestCase) -> ModuleType:
    domain = importlib.import_module("stock_mcp.domain")
    required = (
        "DailyPriceLimit",
        "IndustryClassificationReference",
        "V3BreadthFacts",
        "V3MarketInput",
        "V3SecurityInput",
    )
    missing = tuple(name for name in required if not hasattr(domain, name))
    if missing:
        test_case.fail("rule engine v3 domain contract is required: " + ", ".join(missing))
    return domain


def _bar(
    symbol: str,
    trade_date: date,
    *,
    close: int,
    pre_close: int | None = None,
    high: int | None = None,
    low: int | None = None,
    volume: int = 1_000_000,
    amount: int = 4_000_000_000,
) -> DailyBar:
    previous = close if pre_close is None else pre_close
    return DailyBar(
        symbol=symbol,
        trade_date=trade_date,
        open_1e4=previous,
        high_1e4=max(close, previous) + 500 if high is None else high,
        low_1e4=min(close, previous) - 500 if low is None else low,
        close_1e4=close,
        pre_close_1e4=previous,
        volume_shares=volume,
        amount_fen=amount,
        source=SOURCE,
        source_timestamp=AS_OF,
    )


def _security(symbol: str, *, industry: str = "银行", **changes: object) -> Security:
    values: dict[str, object] = {
        "symbol": symbol,
        "name": symbol,
        "exchange": "SSE",
        "board": "MAIN",
        "list_date": date(2020, 1, 1),
        "industry": industry,
        "is_st": False,
    }
    values.update(changes)
    return Security(**values)


def _prior_bars(
    symbol: str,
    *,
    close: int = 100_000,
    amount: int = 4_000_000_000,
) -> tuple[DailyBar, ...]:
    return tuple(
        _bar(symbol, TRADE_DATE - timedelta(days=60 - index), close=close, amount=amount)
        for index in range(60)
    )


def _breakout_input(test_case: unittest.TestCase, symbol: str = "600301.SH") -> object:
    v3 = _v3_module(test_case)
    domain = _v3_domain(test_case)
    security = _security(symbol)
    prior = _prior_bars(symbol)
    target = _bar(
        symbol,
        TRADE_DATE,
        close=101_000,
        pre_close=100_000,
        high=101_500,
        volume=2_000_000,
        amount=6_000_000_000,
    )
    return domain.V3SecurityInput(
        security=security,
        prior_bars=prior,
        target_bar=target,
        price_limit=v3.derive_daily_price_limit(target, security),
        industry=security.industry,
    )


def _pullback_input(test_case: unittest.TestCase, symbol: str = "600302.SH") -> object:
    v3 = _v3_module(test_case)
    domain = _v3_domain(test_case)
    security = _security(symbol)
    closes = [100_000] * 40 + [100_000 + (index - 40) * 1_000 for index in range(40, 60)]
    prior = tuple(
        _bar(
            symbol,
            TRADE_DATE - timedelta(days=60 - index),
            close=close,
            pre_close=close if index == 0 else closes[index - 1],
        )
        for index, close in enumerate(closes)
    )
    target = _bar(
        symbol,
        TRADE_DATE,
        close=116_000,
        pre_close=closes[-1],
        amount=3_000_000_000,
    )
    return domain.V3SecurityInput(
        security=security,
        prior_bars=prior,
        target_bar=target,
        price_limit=v3.derive_daily_price_limit(target, security),
        industry=security.industry,
    )


def _reference(domain: ModuleType, inputs: tuple[object, ...], *, suffix: str = "") -> object:
    industries = {item.security.symbol: f"{item.industry}{suffix}" for item in inputs}
    encoded = json.dumps(industries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return domain.IndustryClassificationReference(
        classification_standard="recorded-csrc",
        classification_mode="point-in-time",
        classification_as_of=TRADE_DATE,
        classification_mapping_sha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        industries=industries,
    )


def _market(
    test_case: unittest.TestCase,
    inputs: tuple[object, ...],
    *,
    advance_count: int | None = None,
    eligible_count: int | None = None,
    above_ma20_count: int | None = None,
    ma20_eligible_count: int | None = None,
    advance_ratio_bps: int = 10_000,
    above_ma20_ratio_bps: int = 10_000,
    reference_suffix: str = "",
) -> object:
    domain = _v3_domain(test_case)
    total = len(inputs)
    breadth = domain.V3BreadthFacts(
        advance_count=total if advance_count is None else advance_count,
        eligible_count=total if eligible_count is None else eligible_count,
        above_ma20_count=total if above_ma20_count is None else above_ma20_count,
        ma20_eligible_count=total if ma20_eligible_count is None else ma20_eligible_count,
        advance_ratio_bps=advance_ratio_bps,
        above_ma20_ratio_bps=above_ma20_ratio_bps,
    )
    return domain.V3MarketInput(
        trade_date=TRADE_DATE,
        source=SOURCE,
        source_timestamp=AS_OF,
        prior_dates=tuple(TRADE_DATE - timedelta(days=60 - index) for index in range(60)),
        securities=inputs,
        breadth=breadth,
        industry_reference=_reference(domain, inputs, suffix=reference_suffix),
        pipeline_version="pipeline-v0.2",
        input_hash_schema="v3-input-v1",
    )


def _v3_strategy(*, policy: int = 1) -> StrategyVersion:
    return StrategyVersion(
        version=f"v3-policy-{policy}",
        status="proposed",
        parameters={
            "rule_engine_version": 3,
            "regime_policy": policy,
            "offensive_min_bps": 5_500,
            "defensive_max_bps": 4_000,
            "neutral_pullback_limit": 1,
            "neutral_breakout_limit": 1,
            "offensive_pullback_limit": 2,
            "offensive_breakout_limit": 1,
            "min_median_amount_fen": 2_000_000_000,
            "liquidity_lookback_sessions": 20,
            "trend_lookback_sessions": 60,
            "pullback_peak_lookback_sessions": 20,
            "pullback_min_prior_gain_bps": 1_200,
            "pullback_max_drawdown_bps": 350,
            "pullback_max_amount_ratio_bps": 10_000,
            "breakout_lookback_sessions": 60,
            "breakout_amount_lookback_sessions": 20,
            "breakout_min_amount_ratio_bps": 15_000,
            "recent_limit_up_lookback_sessions": 5,
            "required_warmup_sessions": 60,
        },
    )


def _review_json(review: object) -> str:
    return json.dumps(
        asdict(review),
        default=str,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _candidate_projection(review: object) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (item.symbol, item.score, item.rank, item.setup_type) for item in review.candidates
    )


class LegacyGoldenCompatibilityContractTest(unittest.TestCase):
    def test_v1_and_v2_complete_json_and_hashes_remain_frozen(self) -> None:
        fixture = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        securities = (
            _security("600101.SH", name="有行业"),
            _security("600102.SH", name="无行业", industry=""),
        )
        bars = tuple(
            bar
            for security in securities
            for bar in (
                _bar(security.symbol, TRADE_DATE - timedelta(days=2), close=98_000, volume=500_000),
                _bar(
                    security.symbol,
                    TRADE_DATE - timedelta(days=1),
                    close=100_000,
                    volume=500_000,
                ),
                _bar(
                    security.symbol,
                    TRADE_DATE,
                    close=105_000,
                    pre_close=100_000,
                    volume=1_000_000,
                    amount=8_000_000_000,
                ),
            )
        )
        snapshot = MarketSnapshot(
            trade_date=TRADE_DATE,
            source=SOURCE,
            source_timestamp=AS_OF,
            securities=securities,
            bars=bars,
            advance_ratio_bps=6_500,
            above_ma20_ratio_bps=6_500,
        )
        shared = {
            "offensive_min_bps": 5_500,
            "defensive_max_bps": 4_000,
            "neutral_limit": 2,
            "offensive_limit": 3,
            "min_liquidity_amount_fen": 2_000_000_000,
            "max_consecutive_limit_up_days": 2,
            "strong_pullback_min_prior_gain_bps": 1_000,
            "strong_pullback_max_pullback_bps": 800,
            "volume_breakout_min_volume_ratio_bps": 15_000,
        }

        for engine in (1, 2):
            with self.subTest(engine=engine):
                review = generate_daily_review(
                    snapshot,
                    StrategyVersion(
                        version=f"v0.{engine}-golden",
                        status="proposed",
                        parameters={"rule_engine_version": engine, **shared},
                    ),
                )
                actual_json = _review_json(review)
                self.assertEqual(fixture[f"v{engine}"]["json"], actual_json)
                self.assertEqual(
                    fixture[f"v{engine}"]["sha256"],
                    hashlib.sha256(actual_json.encode("utf-8")).hexdigest(),
                )


class RuleEngineV3ParameterContractTest(unittest.TestCase):
    def test_v3_exposes_and_validates_the_frozen_policy_one_and_two_parameters(self) -> None:
        v3 = _v3_module(self)
        expected_names = {
            "rule_engine_version",
            "regime_policy",
            "offensive_min_bps",
            "defensive_max_bps",
            "neutral_pullback_limit",
            "neutral_breakout_limit",
            "offensive_pullback_limit",
            "offensive_breakout_limit",
            "min_median_amount_fen",
            "liquidity_lookback_sessions",
            "trend_lookback_sessions",
            "pullback_peak_lookback_sessions",
            "pullback_min_prior_gain_bps",
            "pullback_max_drawdown_bps",
            "pullback_max_amount_ratio_bps",
            "breakout_lookback_sessions",
            "breakout_amount_lookback_sessions",
            "breakout_min_amount_ratio_bps",
            "recent_limit_up_lookback_sessions",
            "required_warmup_sessions",
        }

        self.assertEqual(expected_names, set(v3.V3_PARAMETER_NAMES))
        self.assertEqual(
            _v3_strategy(policy=1).parameters,
            v3.validate_v3_parameters(_v3_strategy().parameters),
        )
        self.assertEqual(
            _v3_strategy(policy=2).parameters,
            v3.validate_v3_parameters(_v3_strategy(policy=2).parameters),
        )
        invalid = dict(_v3_strategy().parameters)
        invalid["regime_policy"] = 3
        with self.assertRaisesRegex(ValueError, "regime_policy"):
            v3.validate_v3_parameters(invalid)


class RuleEngineV3ArithmeticContractTest(unittest.TestCase):
    def test_close_preclose_chain_uses_exact_fractional_point_in_time_adjustment(self) -> None:
        v3 = _v3_module(self)
        prior = (
            _bar("600311.SH", TRADE_DATE - timedelta(days=2), close=100_000),
            _bar(
                "600311.SH",
                TRADE_DATE - timedelta(days=1),
                close=100_001,
                pre_close=100_000,
            ),
        )
        target = _bar("600311.SH", TRADE_DATE, close=101_000, pre_close=33_333)

        self.assertEqual(
            (Fraction(3_333_300_000, 100_001), Fraction(33_333), Fraction(101_000)),
            v3.adjusted_close_chain(prior, target),
        )

    def test_main_board_ten_percent_limit_is_half_up_and_records_exceptions(self) -> None:
        v3 = _v3_module(self)
        domain = _v3_domain(self)
        security = _security("600312.SH")
        bar = _bar(
            security.symbol,
            TRADE_DATE,
            close=110_100,
            pre_close=100_100,
            high=110_100,
            low=90_100,
        )

        limit = v3.derive_daily_price_limit(bar, security)

        self.assertIsInstance(limit, domain.DailyPriceLimit)
        self.assertEqual(110_100, limit.up_limit_1e4)
        self.assertEqual(90_100, limit.down_limit_1e4)
        self.assertTrue(limit.touched_up)
        self.assertTrue(limit.touched_down)
        self.assertFalse(limit.policy_exception)
        self.assertIn("half-up", limit.algorithm)
        exception = v3.derive_daily_price_limit(bar, _security("300312.SZ", board="GEM"))
        self.assertTrue(exception.policy_exception)

    def test_percentiles_use_mid_ranks_singletons_and_flooring(self) -> None:
        v3 = _v3_module(self)

        self.assertEqual((10_000,), v3.percentile_bps((7,), higher_is_better=True))
        self.assertEqual(
            (0, 1_666, 3_333, 5_000, 6_666, 8_333, 10_000),
            v3.percentile_bps((10, 20, 30, 40, 50, 60, 70), higher_is_better=True),
        )
        self.assertEqual(
            (10_000, 5_000, 5_000, 0),
            v3.percentile_bps((10, 20, 20, 30), higher_is_better=False),
        )


class RuleEngineV3ScreeningContractTest(unittest.TestCase):
    def test_requires_sixty_prior_sessions_plus_target_and_complete_ma20_coverage(self) -> None:
        v3 = _v3_module(self)
        valid = _breakout_input(self)
        review = v3.generate_v3_daily_review(_market(self, (valid,)), _v3_strategy())
        self.assertEqual((valid.security.symbol,), tuple(item.symbol for item in review.candidates))

        too_short = replace(valid, prior_bars=valid.prior_bars[1:])
        self.assertEqual(
            "insufficient_prior_history",
            v3.evaluate_v3_eligibility(too_short, _v3_strategy()).reason,
        )

        missing_ma20 = _market(
            self,
            (valid,),
            advance_count=1,
            eligible_count=1,
            above_ma20_count=0,
            ma20_eligible_count=0,
            advance_ratio_bps=10_000,
            above_ma20_ratio_bps=0,
        )
        with self.assertRaisesRegex(ValueError, "ma20"):
            v3.generate_v3_daily_review(missing_ma20, _v3_strategy())

    def test_eligibility_reasons_follow_the_frozen_first_failure_order(self) -> None:
        v3 = _v3_module(self)
        valid = _breakout_input(self)
        no_target = replace(valid, target_bar=None, price_limit=None)
        exception = replace(valid, price_limit=replace(valid.price_limit, policy_exception=True))
        too_short = replace(valid, prior_bars=valid.prior_bars[1:])
        low_liquidity = replace(
            valid,
            prior_bars=tuple(replace(bar, amount_fen=1) for bar in valid.prior_bars),
        )
        touched_up = replace(valid, price_limit=replace(valid.price_limit, touched_up=True))
        no_setup = replace(
            valid,
            target_bar=_bar(valid.security.symbol, TRADE_DATE, close=100_000),
        )
        cases = (
            (
                replace(valid, security=replace(valid.security, board="GEM", is_st=True)),
                "not_main_board",
            ),
            (replace(valid, security=replace(valid.security, is_st=True)), "st_security"),
            (
                replace(
                    valid,
                    security=replace(
                        valid.security,
                        list_date=TRADE_DATE - timedelta(days=179),
                    ),
                ),
                "listing_age_lt_180_days",
            ),
            (no_target, "missing_target_or_limit_facts"),
            (exception, "limit_policy_exception"),
            (too_short, "insufficient_prior_history"),
            (low_liquidity, "low_median_liquidity"),
            (touched_up, "target_touched_up_limit"),
            (no_setup, "no_eligible_setup"),
        )

        for security_input, reason in cases:
            with self.subTest(reason=reason):
                decision = v3.evaluate_v3_eligibility(security_input, _v3_strategy())
                self.assertFalse(decision.eligible)
                self.assertEqual(reason, decision.reason)

    def test_pullback_and_breakout_are_independent_setups_with_explained_scores(self) -> None:
        v3 = _v3_module(self)
        pullback = _pullback_input(self)
        breakout = _breakout_input(self)

        review = v3.generate_v3_daily_review(_market(self, (pullback, breakout)), _v3_strategy())
        candidates = {candidate.symbol: candidate for candidate in review.candidates}

        self.assertEqual("strong_pullback", candidates[pullback.security.symbol].setup_type)
        self.assertEqual("volume_breakout", candidates[breakout.security.symbol].setup_type)
        for candidate in candidates.values():
            contributions = {
                item.metric: item.score_contribution for item in candidate.evidence
            }
            self.assertEqual(40, contributions["primary_percentile_bps"])
            self.assertEqual(30, contributions["amount_percentile_bps"])
            self.assertEqual(30, contributions["liquidity_percentile_bps"])
            self.assertEqual(0, contributions["recent_limit_up_count"])
            self.assertEqual(
                candidate.score,
                sum(item.score_contribution for item in candidate.evidence),
            )
            self.assertEqual(100, candidate.score)

    def test_policy_one_uses_regime_quotas_policy_two_uses_offensive_quotas(self) -> None:
        v3 = _v3_module(self)
        inputs = (
            _pullback_input(self, "600321.SH"),
            _pullback_input(self, "600322.SH"),
            _breakout_input(self, "600323.SH"),
            _breakout_input(self, "600324.SH"),
        )
        neutral_market = _market(
            self,
            inputs,
            advance_count=11,
            eligible_count=20,
            above_ma20_count=8,
            ma20_eligible_count=20,
            advance_ratio_bps=5_500,
            above_ma20_ratio_bps=4_000,
        )

        policy_one = v3.generate_v3_daily_review(neutral_market, _v3_strategy(policy=1))
        policy_two = v3.generate_v3_daily_review(neutral_market, _v3_strategy(policy=2))

        self.assertEqual("neutral", policy_one.market_regime)
        self.assertEqual("neutral", policy_two.market_regime)
        self.assertEqual(2, len(policy_one.candidates))
        self.assertEqual(3, len(policy_two.candidates))
        self.assertEqual(
            1,
            sum(item.setup_type == "strong_pullback" for item in policy_one.candidates),
        )
        self.assertEqual(
            1,
            sum(item.setup_type == "volume_breakout" for item in policy_one.candidates),
        )
        self.assertEqual(
            2,
            sum(item.setup_type == "strong_pullback" for item in policy_two.candidates),
        )
        self.assertEqual(
            1,
            sum(item.setup_type == "volume_breakout" for item in policy_two.candidates),
        )

    def test_setup_quotas_do_not_backfill_missing_pullbacks_and_output_is_sorted(self) -> None:
        v3 = _v3_module(self)
        inputs = (
            _breakout_input(self, "600331.SH"),
            _breakout_input(self, "600332.SH"),
            _breakout_input(self, "600333.SH"),
        )

        review = v3.generate_v3_daily_review(_market(self, inputs), _v3_strategy(policy=1))

        self.assertEqual(1, len(review.candidates))
        self.assertEqual("volume_breakout", review.candidates[0].setup_type)
        self.assertEqual(
            review.candidates,
            tuple(sorted(review.candidates, key=lambda item: (-item.score, item.symbol))),
        )


class RuleEngineV3IndustryIsolationContractTest(unittest.TestCase):
    def test_industry_tags_and_missing_mapping_do_not_change_candidates_or_scores(self) -> None:
        v3 = _v3_module(self)
        classified = _breakout_input(self, "600341.SH")
        missing = replace(classified, industry="")
        classified_market = _market(self, (classified,))
        missing_market = _market(self, (missing,), reference_suffix="-different")

        classified_review = v3.generate_v3_daily_review(classified_market, _v3_strategy())
        missing_review = v3.generate_v3_daily_review(missing_market, _v3_strategy())
        self.assertEqual(
            _candidate_projection(classified_review),
            _candidate_projection(missing_review),
        )
        for review in (classified_review, missing_review):
            for candidate in review.candidates:
                for evidence in candidate.evidence:
                    if evidence.metric.startswith("industry"):
                        self.assertEqual(0, evidence.score_contribution)

    def test_input_and_result_hashes_bind_industry_mapping_without_changing_review(self) -> None:
        v3 = _v3_module(self)
        security_input = _breakout_input(self, "600351.SH")
        original = _market(self, (security_input,))
        remapped = _market(self, (security_input,), reference_suffix="-reclassified")
        strategy = _v3_strategy()

        original_review = v3.generate_v3_daily_review(original, strategy)
        remapped_review = v3.generate_v3_daily_review(remapped, strategy)

        self.assertNotEqual(
            v3.canonical_v3_market_input_hash(original),
            v3.canonical_v3_market_input_hash(remapped),
        )
        self.assertNotEqual(
            v3.canonical_v3_result_hash(original, strategy, original_review),
            v3.canonical_v3_result_hash(remapped, strategy, remapped_review),
        )
        self.assertEqual(
            tuple((item.symbol, item.score, item.rank) for item in original_review.candidates),
            tuple((item.symbol, item.score, item.rank) for item in remapped_review.candidates),
        )
