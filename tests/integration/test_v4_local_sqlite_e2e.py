"""Offline, fixed-fixture v4 execution proof through the production SQLite seam."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from stock_mcp.application import StockMcpApplication
from stock_mcp.domain import DailyBar, MarketSnapshot, Security
from stock_mcp.mcp_tools import build_tool_catalog
from stock_mcp.replay import build_v4_replay_manifest
from stock_mcp.storage import SCHEMA_VERSION, Database
from stock_mcp.v3_facts import build_v3_facts
from stock_mcp.v4_research import (
    SQLiteV4StudyDataLoader,
    V4ResearchCoordinator,
    V4StudyExecutor,
)

_ARMS = (
    "v0.3-policy-1",
    "v4-trend-quality",
    "v4-breakout-overextension-cap",
    "v4-no-recent-limit-up",
    "v4-breadth-five-day-median",
    "v4-size-bottom-30pct-filter",
    "v4-signal-quality-rank",
)
_SOURCE_TIMESTAMP = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
_SYMBOLS = ("600001.SH", "600002.SH", "600003.SH")
_EXCLUDED_SNAPSHOT_SYMBOL = "600999.SH"


class V4LocalSqliteE2ETest(unittest.TestCase):
    """A real v11 database: no provider, network, clock, or repository doubles."""

    def test_fixed_v11_evidence_completes_the_single_signal_seven_arm_study_after_restart(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database, manifest_hash, signal_dates = _seed_fixed_v11_database(Path(directory))
            admitted = {"value": False}
            coordinator = V4ResearchCoordinator(
                database,
                step_executor=V4StudyExecutor(database),
                allowed=lambda _now: admitted["value"],
                clock=lambda: _SOURCE_TIMESTAMP,
            )
            study = coordinator.start_v4_research(
                manifest_hash=manifest_hash,
                idempotency_key="fixed-local-v4-study",
            )
            study_id = str(study["study_id"])
            coordinator.stop_background()
            admitted["value"] = True

            with (
                patch.object(
                    database,
                    "load_share_capital_fact",
                    side_effect=AssertionError("v4 execution must bulk-load share capital"),
                ),
                patch.object(
                    database,
                    "load_daily_price_limits",
                    wraps=database.load_daily_price_limits,
                ) as price_limit_load,
                patch.object(
                    database,
                    "list_v4_study_days",
                    side_effect=AssertionError(
                        "a running step must use the compact persisted progress cursor"
                    ),
                ),
            ):
                self.assertTrue(coordinator.run_next_step())
                self.assertLessEqual(
                    price_limit_load.call_count,
                    30,
                    "one signal day must load each price-limit day once, not once per symbol",
                )
            first_run = database.get_v4_study_run(study_id)
            assert first_run is not None
            self.assertEqual("running", first_run["status"], first_run["error"])
            self.assertEqual(1, database.requeue_interrupted_v4_studies())

            restarted = V4ResearchCoordinator(
                database,
                step_executor=V4StudyExecutor(database),
                allowed=lambda _now: True,
                clock=lambda: _SOURCE_TIMESTAMP,
            )
            with patch.object(
                database,
                "load_share_capital_fact",
                side_effect=AssertionError("v4 execution must bulk-load share capital"),
            ):
                for _ in range(len(_ARMS) * len(signal_dates)):
                    self.assertTrue(restarted.run_next_step())

            run = database.get_v4_study_run(study_id)
            assert run is not None
            self.assertEqual("completed", run["status"])
            state = database.get_v4_study_execution_state(study_id=study_id)
            expected_dates = tuple(item.isoformat() for item in signal_dates)
            self.assertEqual(expected_dates, tuple(state["completed_dates"][_ARMS[0]]))
            self.assertEqual(
                {arm_id: expected_dates for arm_id in _ARMS},
                state["completed_dates"],
            )

            baseline_outcomes = database.list_v4_study_candidate_outcomes(
                study_id=study_id, arm_id="v0.3-policy-1"
            )
            self.assertTrue(baseline_outcomes)
            for outcome in baseline_outcomes.values():
                self.assertEqual("complete", outcome["completeness_status"])
                benchmark = outcome["next_open_path"]["benchmark"]
                self.assertEqual(10_000, benchmark["completeness_rate_bps"])
                # JSON object keys are strings after the durable SQLite round trip.
                self.assertIn("20", benchmark["market_cap_decile_return_bps"])

            report = run["report"]
            assert isinstance(report, dict)
            self.assertEqual("complete", report["completeness_status"])
            self.assertEqual(10_000, report["outcome_completeness_rate_bps"])
            self.assertEqual(10_000, report["benchmark_completeness_rate_bps"])
            self.assertFalse(report["sina_replication_complete"])
            self.assertEqual(
                {"eligible": False, "arm_id": None, "decision": "retain_baseline"},
                report["winner"],
            )
            self.assertEqual("v0.3-policy-1", report["retain_version"])
            self.assertEqual([], report["proposals"])

            application = StockMcpApplication(
                database,
                _OfflineQuoteProvider(),
                object(),
                v4_research=restarted,
            )
            tools = {tool.name: tool for tool in build_tool_catalog(application)}
            readback = tools["get_v4_research_report"].handler(study_id=study_id)
            self.assertTrue(readback["ok"])
            self.assertEqual(report, readback["data"])

            second_database, second_manifest_hash, _ = _seed_fixed_v11_database(
                Path(directory) / "repeat"
            )
            repeated = _complete_study(second_database, second_manifest_hash)
            self.assertEqual(run["result_hash"], repeated["result_hash"])
            self.assertEqual(report, repeated["report"])

            polluted_database, polluted_manifest_hash, _ = _seed_fixed_v11_database(
                Path(directory) / "excluded-pollution", excluded_breadth_bps=0
            )
            polluted = _complete_study(polluted_database, polluted_manifest_hash)
            self.assertEqual(manifest_hash, polluted_manifest_hash)
            self.assertEqual(run["result_hash"], polluted["result_hash"])
            self.assertEqual(report, polluted["report"])

    def test_recorded_suspension_without_a_bar_is_kept_as_a_zero_return_peer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database, manifest_hash, _ = _seed_fixed_v11_database(
                Path(directory), suspended_outcome=True
            )
            run = _complete_study(database, manifest_hash)
            self.assertEqual("completed", run["status"], run["error"])
            self.assertEqual("complete", run["report"]["completeness_status"])

    def test_missing_price_and_missing_status_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database, manifest_hash, _ = _seed_fixed_v11_database(Path(directory))
            with database.connect() as connection:
                connection.execute(
                    "DELETE FROM daily_bars WHERE source='tushare' AND symbol=? AND trade_date=?",
                    ("600003.SH", date(2026, 3, 13).isoformat()),
                )
                connection.execute(
                    "DELETE FROM daily_security_status WHERE source='baostock' "
                    "AND symbol=? AND trade_date=?",
                    ("600003.SH", date(2026, 3, 13).isoformat()),
                )
            run = _complete_study(database, manifest_hash)
            self.assertEqual("failed", run["status"])

    def test_manifest_hash_binds_security_metadata_and_price_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database, _, _ = _seed_fixed_v11_database(Path(directory))
            before = database.compute_v4_evidence_hashes(
                start=date(2026, 1, 2),
                end=date(2026, 3, 30),
                included_symbols=_SYMBOLS,
            )
            with database.connect() as connection:
                connection.execute(
                    "UPDATE snapshot_securities SET is_st=1 WHERE source='tushare' "
                    "AND symbol='600001.SH' AND trade_date='2026-03-03'"
                )
            after = database.compute_v4_evidence_hashes(
                start=date(2026, 1, 2),
                end=date(2026, 3, 30),
                included_symbols=_SYMBOLS,
            )
            self.assertNotEqual(before, after)

    def test_manifest_hash_binds_snapshot_source_timestamp_not_raw_breadth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database, _, _ = _seed_fixed_v11_database(Path(directory))
            before = database.compute_v4_evidence_hashes(
                start=date(2026, 1, 2),
                end=date(2026, 3, 30),
                included_symbols=_SYMBOLS,
            )
            with database.connect() as connection:
                connection.execute(
                    "UPDATE market_snapshots SET source_timestamp=? "
                    "WHERE source='tushare' AND trade_date='2026-03-03'",
                    ("2026-03-03T17:00:00+08:00",),
                )
            after = database.compute_v4_evidence_hashes(
                start=date(2026, 1, 2),
                end=date(2026, 3, 30),
                included_symbols=_SYMBOLS,
            )
            self.assertNotEqual(before, after)

    def test_manifest_hash_binds_included_industry_feature(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database, _, _ = _seed_fixed_v11_database(Path(directory))
            before = database.compute_v4_evidence_hashes(
                start=date(2026, 1, 2),
                end=date(2026, 3, 30),
                included_symbols=_SYMBOLS,
            )
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT feature_json FROM v3_snapshot_features WHERE source='tushare' "
                    "AND symbol='600001.SH' AND trade_date='2026-03-03'"
                ).fetchone()
                feature = json.loads(str(row[0]))
                feature["industry"] = "changed-industry"
                connection.execute(
                    "UPDATE v3_snapshot_features SET feature_json=? WHERE source='tushare' "
                    "AND symbol='600001.SH' AND trade_date='2026-03-03'",
                    (json.dumps(feature, sort_keys=True, separators=(",", ":")),),
                )
            after = database.compute_v4_evidence_hashes(
                start=date(2026, 1, 2),
                end=date(2026, 3, 30),
                included_symbols=_SYMBOLS,
            )
            self.assertNotEqual(before, after)

    def test_each_study_step_revalidates_manifest_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database, manifest_hash, signal_dates = _seed_fixed_v11_database(Path(directory))
            loader = SQLiteV4StudyDataLoader(database)
            loader.load_v4_signal_evidence(
                manifest_hash=manifest_hash,
                signal_date=signal_dates[0],
                arm_id="baseline",
            )
            database.save_share_capital_facts(
                (
                    {
                        "symbol": "600001.SH",
                        "effective_date": date(2026, 2, 1),
                        "source": "sina",
                        "outstanding_shares": 1_100_000,
                        "source_timestamp": "2026-02-01T16:00:00+08:00",
                        "payload_sha256": "9" * 64,
                    },
                )
            )
            with self.assertRaisesRegex(ValueError, "evidence hashes"):
                loader.load_v4_signal_evidence(
                    manifest_hash=manifest_hash,
                    signal_date=signal_dates[0],
                    arm_id="trend-quality",
                )

    def test_excluded_symbol_missing_v3_fact_does_not_control_included_study(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database, manifest_hash, signal_dates = _seed_fixed_v11_database(Path(directory))
            with database.connect() as connection:
                connection.execute(
                    "DELETE FROM daily_price_limits WHERE source='tushare' "
                    "AND symbol='600999.SH' AND trade_date=?",
                    (signal_dates[0].isoformat(),),
                )
            run = _complete_study(database, manifest_hash)
            self.assertEqual("completed", run["status"], run["error"])

    def test_excluded_industry_metadata_does_not_control_included_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database, _, signal_dates = _seed_fixed_v11_database(Path(directory))
            before = database.compute_v4_evidence_hashes(
                start=date(2026, 1, 2),
                end=date(2026, 3, 30),
                included_symbols=_SYMBOLS,
            )
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT feature_json FROM v3_snapshot_features WHERE source='tushare' "
                    "AND symbol='600999.SH' AND trade_date=?",
                    (signal_dates[0].isoformat(),),
                ).fetchone()
                feature = json.loads(str(row[0]))
                feature["industry_mapping_sha256"] = "f" * 64
                connection.execute(
                    "UPDATE v3_snapshot_features SET feature_json=? WHERE source='tushare' "
                    "AND symbol='600099.SH' AND trade_date=?",
                    (
                        json.dumps(feature, sort_keys=True, separators=(",", ":")),
                        signal_dates[0].isoformat(),
                    ),
                )
            after = database.compute_v4_evidence_hashes(
                start=date(2026, 1, 2),
                end=date(2026, 3, 30),
                included_symbols=_SYMBOLS,
            )
            self.assertEqual(before, after)

    def test_manifest_identity_and_calendar_cannot_be_changed_after_freezing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database, manifest_hash, signal_dates = _seed_fixed_v11_database(Path(directory))
            loader = SQLiteV4StudyDataLoader(database)
            loader.load_v4_signal_evidence(
                manifest_hash=manifest_hash,
                signal_date=signal_dates[0],
                arm_id="baseline",
            )
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT manifest_json FROM v4_dataset_manifests WHERE manifest_hash=?",
                    (manifest_hash,),
                ).fetchone()
                changed = json.loads(str(row[0]))
                changed["included_symbols"] = ["600001.SH"]
                connection.execute(
                    "UPDATE v4_dataset_manifests SET manifest_json=? WHERE manifest_hash=?",
                    (json.dumps(changed, sort_keys=True, separators=(",", ":")), manifest_hash),
                )
            with self.assertRaisesRegex(ValueError, "manifest hash"):
                loader.load_v4_signal_evidence(
                    manifest_hash=manifest_hash,
                    signal_date=signal_dates[0],
                    arm_id="trend-quality",
                )

            database, manifest_hash, signal_dates = _seed_fixed_v11_database(
                Path(directory) / "calendar"
            )
            loader = SQLiteV4StudyDataLoader(database)
            loader.load_v4_signal_evidence(
                manifest_hash=manifest_hash,
                signal_date=signal_dates[0],
                arm_id="baseline",
            )
            with database.connect() as connection:
                prior_session = connection.execute(
                    "SELECT trade_date FROM expected_trading_days WHERE source='tushare' "
                    "AND trade_date<? ORDER BY trade_date DESC LIMIT 1",
                    (signal_dates[0].isoformat(),),
                ).fetchone()[0]
                connection.execute(
                    "DELETE FROM expected_trading_days WHERE source='tushare' AND trade_date=?",
                    (prior_session,),
                )
            with self.assertRaisesRegex(ValueError, "provider calendar"):
                loader.load_v4_signal_evidence(
                    manifest_hash=manifest_hash,
                    signal_date=signal_dates[0],
                    arm_id="trend-quality",
                )

    def test_manifest_sessions_must_equal_the_recorded_provider_calendar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database, manifest_hash, signal_dates = _seed_fixed_v11_database(Path(directory))
            original = database.get_v4_dataset_manifest(manifest_hash)
            assert original is not None
            changed = dict(original)
            changed.pop("manifest_hash")
            sessions = list(changed["sessions"])
            sessions[0] = "2026-01-01"
            changed["sessions"] = sessions
            changed["bar_start"] = sessions[0]
            changed["calendar_hash"] = hashlib.sha256("|".join(sessions).encode()).hexdigest()
            changed["manifest_hash"] = hashlib.sha256(
                json.dumps(changed, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            database.save_v4_dataset_manifest(changed)
            loader = SQLiteV4StudyDataLoader(database)
            with self.assertRaisesRegex(ValueError, "provider calendar"):
                loader.load_v4_signal_evidence(
                    manifest_hash=str(changed["manifest_hash"]),
                    signal_date=signal_dates[0],
                    arm_id="baseline",
                )


class _OfflineQuoteProvider:
    def fetch_quote(self, _symbol: str) -> object:
        raise AssertionError("the fixed local v4 study must not request a live quote")


def _complete_study(database: Database, manifest_hash: str) -> dict[str, object]:
    coordinator = V4ResearchCoordinator(
        database,
        step_executor=V4StudyExecutor(database),
        allowed=lambda _now: True,
        clock=lambda: _SOURCE_TIMESTAMP,
    )
    study = coordinator.start_v4_research(
        manifest_hash=manifest_hash,
        idempotency_key="fixed-local-v4-repeat",
    )
    coordinator.stop_background()
    while True:
        run = database.get_v4_study_run(str(study["study_id"]))
        assert run is not None
        if run["status"] in {"completed", "failed"}:
            return run
        coordinator.run_next_step()


def _seed_fixed_v11_database(
    directory: Path,
    *,
    suspended_outcome: bool = False,
    excluded_breadth_bps: int | None = None,
) -> tuple[Database, str, tuple[date, ...]]:
    directory.mkdir(parents=True, exist_ok=True)
    database = Database(directory / "fixed-v11.sqlite3")
    database.initialize()
    with database.connect() as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION == 11

    sessions = tuple(date(2026, 1, 2) + timedelta(days=index) for index in range(88))
    database.save_expected_trading_days("tushare", sessions)
    snapshot_symbols = (*_SYMBOLS, _EXCLUDED_SNAPSHOT_SYMBOL)
    securities = tuple(
        Security(
            symbol=symbol,
            name=f"fixed-{symbol}",
            exchange="SSE",
            board="MAIN",
            list_date=date(2020, 1, 1),
            industry="fixture-industry",
            is_st=False,
        )
        for symbol in snapshot_symbols
    )
    for index, session in enumerate(sessions):
        bars = tuple(_bar(symbol, session, index) for symbol in snapshot_symbols)
        database.save_market_snapshot(
            MarketSnapshot(
                trade_date=session,
                source="tushare",
                source_timestamp=_SOURCE_TIMESTAMP,
                securities=securities,
                bars=bars,
                advance_ratio_bps=(
                    excluded_breadth_bps
                    if excluded_breadth_bps is not None
                    else 10_000
                    if index == 60
                    else 6_666
                ),
                above_ma20_ratio_bps=(
                    excluded_breadth_bps
                    if excluded_breadth_bps is not None
                    else 10_000
                    if index == 60
                    else 6_666
                ),
            )
        )

    database.save_share_capital_facts(
        {
            "symbol": symbol,
            "effective_date": sessions[0],
            "source": "sina",
            "outstanding_shares": 1_000_000_000 + position * 100_000_000,
            "source_timestamp": _SOURCE_TIMESTAMP,
            "payload_sha256": f"{position + 1:064x}",
        }
        for position, symbol in enumerate(_SYMBOLS)
    )
    database.save_daily_security_statuses(
        {
            "symbol": symbol,
            "trade_date": session,
            "source": "baostock",
            "tradestatus": (
                "0" if suspended_outcome and symbol == "600003.SH" and session_index == 70 else "1"
            ),
            "is_st": False,
            "source_timestamp": _SOURCE_TIMESTAMP,
            "batch_sha256": f"{session.toordinal() + position:064x}",
        }
        for session_index, session in enumerate(sessions[61:], start=61)
        for position, symbol in enumerate(_SYMBOLS)
    )

    industry_path = directory / "fixed-industries.json"
    industry_path.write_text(
        json.dumps(
            {
                "metadata": {
                    "standard": "fixture-industry-standard",
                    "mode": "retrospective_current_mapping",
                    "as_of": "2026-08-12",
                },
                "stocks": [
                    {"code": symbol[:6], "exchange": "SSE", "industry": "fixture-industry"}
                    for symbol in snapshot_symbols
                ],
            }
        ),
        encoding="utf-8",
    )
    build_v3_facts(
        database=database,
        industry_json_path=industry_path,
        source="tushare",
        start=sessions[0],
        end=sessions[-1],
    )
    if suspended_outcome:
        with database.connect() as connection:
            connection.execute(
                "DELETE FROM daily_bars WHERE source='tushare' AND symbol=? AND trade_date=?",
                ("600003.SH", sessions[70].isoformat()),
            )
    evidence_hashes = database.compute_v4_evidence_hashes(
        start=sessions[0], end=sessions[-1], included_symbols=_SYMBOLS
    )
    manifest = build_v4_replay_manifest(
        source="tushare",
        sessions=sessions,
        bar_start=sessions[0],
        signal_start=sessions[60],
        signal_end=sessions[-26],
        outcome_through=sessions[-1],
        prices_hash=evidence_hashes["prices_hash"],
        statuses_hash=evidence_hashes["statuses_hash"],
        share_capital_hash=evidence_hashes["share_capital_hash"],
        industry_mapping_hash=evidence_hashes["industry_mapping_hash"],
        universe_symbols=_SYMBOLS,
        universe_source_manifest_hash="5" * 64,
    )
    database.save_v4_dataset_manifest(manifest)
    return database, str(manifest["manifest_hash"]), sessions[60:-25]


def _bar(symbol: str, session: date, index: int) -> DailyBar:
    is_signal = 60 <= index <= 62
    is_outcome = 61 <= index <= 82
    if symbol == "600001.SH":
        close = 105_000 if is_signal else 107_000 + (index - 61) * 100 if is_outcome else 100_000
        amount = 20_000_000_000 if is_signal else 8_000_000_000
        high = 106_000 if is_signal else close + 1_000
        low = 99_000 if is_signal else close - 1_000
    else:
        close = 101_000 if is_signal else 100_500 + (index - 61) * 50 if is_outcome else 100_000
        amount = 8_000_000_000
        high = close + 1_000
        low = close - 1_000
    if index <= 60:
        pre_close = 100_000
    elif symbol == "600001.SH":
        pre_close = 106_900 + (index - 62) * 100
    else:
        pre_close = 100_500 + (index - 62) * 50
    return DailyBar(
        symbol=symbol,
        trade_date=session,
        open_1e4=pre_close if not is_outcome else pre_close + 50,
        high_1e4=high,
        low_1e4=low,
        close_1e4=close,
        pre_close_1e4=pre_close,
        volume_shares=1_000_000,
        amount_fen=amount,
        source="tushare",
        source_timestamp=_SOURCE_TIMESTAMP,
    )


if __name__ == "__main__":
    unittest.main()
