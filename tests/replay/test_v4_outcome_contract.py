"""Offline RED contracts for immutable v4 replay manifests and outcome-v2."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from stock_mcp import outcomes, replay


class V4OutcomeContractTest(unittest.TestCase):
    def test_capital_exclusions_must_cover_every_recorded_missing_symbol(self) -> None:
        load = getattr(replay, "load_v4_capital_exclusions", None)
        self.assertTrue(callable(load))
        if not callable(load):
            return
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "exclusions.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "v4-capital-exclusions-v1",
                        "reason": "sina_share_capital_unavailable",
                        "symbols": ["600002.SH", "600003.SH"],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                ("600002.SH", "600003.SH"),
                load(path, ("600002.SH",)),
            )
            with self.assertRaisesRegex(ValueError, "exactly|missing|exclusion"):
                load(path, ("600004.SH",))

    def test_v4_universe_is_loaded_from_a_hash_verified_sina_backfill_manifest(self) -> None:
        load = getattr(replay, "load_v4_sina_backfill_universe", None)
        self.assertTrue(callable(load))
        if not callable(load):
            return
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sina-manifest.json"
            payload = {
                "schema": "sina-backfill-manifest-v1",
                "run_id": "sina-backfill-2023-08-08-2026-08-07",
                "symbols": ["600001.SH", "600002.SH"],
                "start": "2023-08-08",
                "end": "2026-08-07",
                "adapter_version": "sina-adapter-v1",
            }
            payload["manifest_hash"] = replay.canonical_json_sha256(payload)
            path.write_text(json.dumps(payload), encoding="utf-8")
            symbols, manifest_hash = load(
                path, date(2023, 8, 8), date(2026, 8, 7)
            )
            self.assertEqual(("600001.SH", "600002.SH"), symbols)
            self.assertEqual(payload["manifest_hash"], manifest_hash)
            payload["symbols"].append("600003.SH")
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash|manifest"):
                load(path, date(2023, 8, 8), date(2026, 8, 7))

    def test_checked_in_capital_exclusion_record_contains_the_approved_41(self) -> None:
        path = (
            Path(__file__).resolve().parents[2]
            / "deploy"
            / "windows"
            / "v4-sina-capital-exclusions-20260812.json"
        )
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("v4-capital-exclusions-v1", document["schema"])
        self.assertEqual("sina_share_capital_unavailable", document["reason"])
        self.assertEqual(41, len(document["symbols"]))
        self.assertEqual(document["symbols"], sorted(set(document["symbols"])))

    def test_manifest_rejects_a_sparse_or_mismatched_daily_price_universe(self) -> None:
        validate = getattr(replay, "validate_v4_manifest_universe", None)
        self.assertTrue(callable(validate))
        if not callable(validate):
            return

        validate(
            expected_session_count=727,
            price_day_count=727,
            snapshot_day_count=727,
            missing_price_rows=0,
            orphan_price_rows=0,
        )
        for changed in (
            {"snapshot_day_count": 726},
            {"missing_price_rows": 1},
            {"orphan_price_rows": 1},
        ):
            values = {
                "expected_session_count": 727,
                "price_day_count": 727,
                "snapshot_day_count": 727,
                "missing_price_rows": 0,
                "orphan_price_rows": 0,
                **changed,
            }
            with self.assertRaisesRegex(ValueError, "universe|complete"):
                validate(**values)

    def test_manifest_reserves_final_25_sessions_for_outcomes_not_new_signals(self) -> None:
        build_manifest = getattr(replay, "build_v4_replay_manifest", None)
        self.assertTrue(
            callable(build_manifest),
            "v4 replay needs a manifest builder that records its immutable evidence boundary",
        )
        if not callable(build_manifest):
            return

        sessions = tuple(date(2026, 1, 1) + timedelta(days=index) for index in range(90))
        manifest = build_manifest(
            source="tushare",
            sessions=sessions,
            bar_start=sessions[0],
            signal_start=sessions[60],
            signal_end=sessions[-26],
            outcome_through=sessions[-1],
            prices_hash="a" * 64,
            statuses_hash="b" * 64,
            share_capital_hash="c" * 64,
            industry_mapping_hash="d" * 64,
            universe_symbols=("600001.SH", "600002.SH", "600003.SH"),
            excluded_symbols=("600003.SH",),
            exclusion_reason="sina_share_capital_unavailable",
            universe_source_manifest_hash="e" * 64,
        )

        self.assertEqual("v4-manifest-v1", manifest["schema"])
        self.assertEqual(60, manifest["warmup_sessions"])
        self.assertEqual(5, manifest["confirmation_window_sessions"])
        self.assertEqual(20, manifest["outcome_horizon_sessions"])
        self.assertEqual(sessions[-26].isoformat(), manifest["signal_end"])
        self.assertEqual(sessions[-1].isoformat(), manifest["outcome_through"])
        self.assertRegex(str(manifest["manifest_hash"]), r"^[0-9a-f]{64}$")
        self.assertEqual(["600001.SH", "600002.SH"], manifest["included_symbols"])
        self.assertEqual(["600003.SH"], manifest["excluded_symbols"])
        self.assertEqual(3, manifest["universe_symbol_count"])
        self.assertEqual(2, manifest["included_symbol_count"])
        self.assertEqual(1, manifest["excluded_symbol_count"])
        self.assertEqual(6666, manifest["capital_coverage_bps"])
        self.assertEqual(
            "sina_share_capital_unavailable", manifest["exclusion_reason"]
        )
        self.assertRegex(str(manifest["universe_symbols_hash"]), r"^[0-9a-f]{64}$")
        self.assertRegex(str(manifest["included_symbols_hash"]), r"^[0-9a-f]{64}$")
        self.assertRegex(str(manifest["excluded_symbols_hash"]), r"^[0-9a-f]{64}$")
        self.assertEqual("e" * 64, manifest["universe_source_manifest_hash"])

        changed_exclusion = build_manifest(
            source="tushare",
            sessions=sessions,
            bar_start=sessions[0],
            signal_start=sessions[60],
            signal_end=sessions[-26],
            outcome_through=sessions[-1],
            prices_hash="a" * 64,
            statuses_hash="b" * 64,
            share_capital_hash="c" * 64,
            industry_mapping_hash="d" * 64,
            universe_symbols=("600001.SH", "600002.SH", "600003.SH"),
            excluded_symbols=("600002.SH",),
            exclusion_reason="sina_share_capital_unavailable",
            universe_source_manifest_hash="e" * 64,
        )
        self.assertNotEqual(manifest["manifest_hash"], changed_exclusion["manifest_hash"])

        with self.assertRaisesRegex(ValueError, "excluded|universe|symbols"):
            build_manifest(
                source="tushare",
                sessions=sessions,
                bar_start=sessions[0],
                signal_start=sessions[60],
                signal_end=sessions[-26],
                outcome_through=sessions[-1],
                prices_hash="a" * 64,
                statuses_hash="b" * 64,
                share_capital_hash="c" * 64,
                industry_mapping_hash="d" * 64,
                universe_symbols=("600001.SH", "600002.SH"),
                excluded_symbols=("600003.SH",),
                exclusion_reason="sina_share_capital_unavailable",
                universe_source_manifest_hash="e" * 64,
            )

        with self.assertRaisesRegex(ValueError, "Tushare|source"):
            build_manifest(
                source="sina",
                sessions=sessions,
                bar_start=sessions[0],
                signal_start=sessions[60],
                signal_end=sessions[-26],
                outcome_through=sessions[-1],
                prices_hash="a" * 64,
                statuses_hash="b" * 64,
                share_capital_hash="c" * 64,
                industry_mapping_hash="d" * 64,
                universe_symbols=("600001.SH",),
                excluded_symbols=(),
                exclusion_reason="sina_share_capital_unavailable",
                universe_source_manifest_hash="e" * 64,
            )

    def test_outcome_v2_uses_earliest_event_then_next_executable_open_with_cost_paths(self) -> None:
        evaluate = getattr(outcomes, "evaluate_v4_candidate_outcomes", None)
        self.assertTrue(
            callable(evaluate),
            "v4-outcome-v2 needs an event-ordered, executable-entry evaluator",
        )
        if not callable(evaluate):
            return

        result = evaluate(
            candidates=(
                {
                    "candidate_id": "candidate-1",
                    "symbol": "600001.SH",
                    "trade_date": date(2026, 1, 1),
                    "confirmation_condition": "close >= 101000",
                    "invalidation_condition": "close <= 98000",
                },
            ),
            bars_by_symbol={
                "600001.SH": (
                    _bar("2026-01-02", 100_000, 101_500, 99_500, 101_000),
                    _bar("2026-01-05", 101_000, 103_000, 100_500, 102_000),
                    _bar("2026-01-06", 102_000, 104_000, 101_000, 103_000),
                )
            },
            status_by_symbol={"600001.SH": {"2026-01-02": 1, "2026-01-05": 1, "2026-01-06": 1}},
            mainboard_bars=(
                _bar("2026-01-02", 100_000, 101_000, 99_000, 100_500),
                _bar("2026-01-05", 100_500, 101_500, 100_000, 101_000),
                _bar("2026-01-06", 101_000, 102_000, 100_500, 101_500),
            ),
            source="sina",
            as_of=date(2026, 1, 6),
        )["candidate-1"]

        self.assertEqual("confirmed", result["confirmed_next_open_path"]["status"])
        self.assertEqual("2026-01-05", result["confirmed_next_open_path"]["entry_date"])
        self.assertIn("gross_return_5d_bps", result["next_open_path"])
        self.assertEqual({10, 25, 50}, set(result["next_open_path"]["net_return_bps_by_cost"]))
        self.assertIn("mfe_20d_bps", result["next_open_path"])
        self.assertIn("mae_20d_bps", result["next_open_path"])

    def test_incomplete_calendar_never_claims_a_complete_outcome(self) -> None:
        result = outcomes.evaluate_v4_candidate_outcomes(
            candidates=(
                {
                    "candidate_id": "candidate-gap",
                    "symbol": "600001.SH",
                    "trade_date": date(2026, 1, 1),
                    "confirmation_condition": "close >= 101000",
                    "invalidation_condition": "close <= 98000",
                },
            ),
            bars_by_symbol={"600001.SH": (_bar("2026-01-02", 100_000, 101_500, 99_500, 101_000),)},
            status_by_symbol={"600001.SH": {"2026-01-02": 1}},
            mainboard_bars=(
                {**_bar("2026-01-02", 100_000, 101_000, 99_000, 100_500), "symbol": "600001.SH"},
                {**_bar("2026-01-05", 100_500, 101_500, 100_000, 101_000), "symbol": "600001.SH"},
            ),
            source="sina",
            as_of=date(2026, 1, 5),
        )["candidate-gap"]

        self.assertFalse(result["calendar_complete"])
        self.assertIn(result["next_open_path"]["status"], {"partial", "unavailable"})
        self.assertEqual("incomplete", result["completeness_status"])

    def test_v4_certification_rejects_incomplete_outcome_or_benchmark_evidence(self) -> None:
        validate = getattr(replay, "validate_v4_replay_certification", None)
        self.assertTrue(
            callable(validate),
            "v4 certification needs an explicit complete-outcome and benchmark gate",
        )
        if not callable(validate):
            return

        with self.assertRaisesRegex(ValueError, "outcome|benchmark|complete"):
            validate(
                {
                    "source": "sina",
                    "manifest_hash": "e" * 64,
                    "outcomes": {"candidate-1": {"status": "unavailable"}},
                    "benchmark_completeness": "insufficient",
                }
            )

    def test_complete_outcome_includes_all_mainboard_and_market_cap_decile_benchmarks(self) -> None:
        signal_date = date(2026, 1, 1)
        sessions = tuple(signal_date + timedelta(days=index) for index in range(1, 21))
        candidate_bars = tuple(
            {
                **_bar(day.isoformat(), 100_000, 102_000, 99_000, 101_000 + index * 1_000),
                "symbol": "600005.SH",
            }
            for index, day in enumerate(sessions)
        )
        mainboard = tuple(
            {
                **_bar(day.isoformat(), 100_000, 105_000, 99_000, 100_000 + symbol_index * 100),
                "symbol": f"6000{symbol_index:02d}.SH",
                "market_cap_fen": (symbol_index + 1) * 100,
            }
            for day in sessions
            for symbol_index in range(10)
        )
        result = outcomes.evaluate_v4_candidate_outcomes(
            candidates=(
                {
                    "candidate_id": "candidate-benchmark",
                    "symbol": "600005.SH",
                    "trade_date": signal_date,
                    "market_cap_fen": 600,
                    "confirmation_condition": "close >= 100000",
                    "invalidation_condition": "close <= 90000",
                },
            ),
            bars_by_symbol={"600005.SH": candidate_bars},
            status_by_symbol={"600005.SH": {day.isoformat(): 1 for day in sessions}},
            mainboard_bars=mainboard,
            source="sina",
            as_of=sessions[-1],
        )["candidate-benchmark"]

        benchmark = result["next_open_path"]["benchmark"]
        self.assertEqual("v4-benchmark-v1", benchmark["schema"])
        self.assertEqual(10_000, benchmark["completeness_rate_bps"])
        self.assertEqual({5, 10, 20}, set(benchmark["all_mainboard_return_bps"]))
        self.assertEqual({5, 10, 20}, set(benchmark["market_cap_decile_return_bps"]))
        self.assertEqual({5, 10, 20}, set(benchmark["market_cap_matched_excess_bps"]))
        self.assertEqual("complete", result["completeness_status"])


def _bar(
    trade_date: str, open_1e4: int, high_1e4: int, low_1e4: int, close_1e4: int
) -> dict[str, object]:
    return {
        "trade_date": trade_date,
        "open_1e4": open_1e4,
        "high_1e4": high_1e4,
        "low_1e4": low_1e4,
        "close_1e4": close_1e4,
        "pre_close_1e4": open_1e4,
        "source": "sina",
    }


if __name__ == "__main__":
    unittest.main()
