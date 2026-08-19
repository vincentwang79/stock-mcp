"""Fixed SQLite proof for the restart-safe post-market forward batch."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from stock_mcp.cli import main
from stock_mcp.domain import (
    Candidate,
    DailyBar,
    DailyReview,
    Evidence,
    MarketRegime,
    MarketSnapshot,
    Security,
    SetupType,
    StrategyVersion,
)
from stock_mcp.storage import Database


class ResearchForwardBatchE2ETest(unittest.TestCase):
    def test_batch_rejects_discovery_sample_dates_before_loading_a_review(self) -> None:
        from stock_mcp import research_program

        runner = getattr(research_program, "run_stored_price_research_batch", None)
        self.assertTrue(callable(runner))
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "stock-mcp.sqlite3")
            database.initialize()

            with self.assertRaisesRegex(ValueError, "forward sample begins"):
                runner(
                    database,
                    trade_date=date(2026, 8, 7),
                    source="tushare",
                    recorded_at=datetime(2026, 8, 7, 10, tzinfo=UTC),
                )

    def test_daily_candidates_are_observed_then_matured_once_after_twenty_sessions(self) -> None:
        from stock_mcp import research_program

        run_stored_price_research_batch = getattr(
            research_program, "run_stored_price_research_batch", None
        )
        self.assertTrue(
            callable(run_stored_price_research_batch),
            "post-market research requires a restart-safe stored-price batch",
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(0, main(("initialize-research-program", "--root", str(root))))
            database = Database(root / "data" / "stock-mcp.sqlite3")
            sessions = tuple(date(2026, 8, 10) + timedelta(days=index) for index in range(26))
            database.save_expected_trading_days("tushare", sessions)
            securities = (
                Security("600001.SH", "Alpha", "SSE", "MAIN", date(2000, 1, 1), "A", False),
                Security("600002.SH", "Beta", "SSE", "MAIN", date(2000, 1, 1), "B", False),
            )
            for index, session in enumerate(sessions):
                timestamp = datetime.combine(session, datetime.min.time(), tzinfo=UTC)
                bars = (
                    _bar("600001.SH", session, 100_000 + index * 1_000, timestamp),
                    _bar("600002.SH", session, 200_000 + index * 500, timestamp),
                )
                database.save_market_snapshot(
                    MarketSnapshot(session, "tushare", timestamp, securities, bars, 5_000, 5_000)
                )
                database.save_daily_price_limits(
                    trade_date=session,
                    source="tushare",
                    limits={
                        symbol: {
                            "limit_up_1e4": bar.close_1e4 + 10_000,
                            "limit_down_1e4": bar.close_1e4 - 10_000,
                            "touched_up": symbol == "600002.SH" and index == 4,
                            "touched_down": False,
                            "policy_exception": False,
                            "algorithm": "mainboard-10pct-round-half-up-v1",
                        }
                        for symbol, bar in zip(("600001.SH", "600002.SH"), bars, strict=True)
                    },
                )

            signal_date = sessions[5]
            _save_strategy(database)
            database.save_daily_review(
                _review(signal_date, securities, candidate_symbols=securities)
            )
            observed = run_stored_price_research_batch(
                database,
                trade_date=signal_date,
                source="tushare",
                recorded_at=datetime(2026, 8, 15, 10, tzinfo=UTC),
            )

            self.assertEqual(2, observed["candidate_count"])
            self.assertEqual(4, observed["observations_recorded"])
            self.assertEqual(0, observed["outcomes_recorded"])
            self.assertEqual(0, observed["blocked_observations"])

            maturity_date = sessions[25]
            database.save_daily_review(_review(maturity_date, securities, candidate_symbols=()))
            matured = run_stored_price_research_batch(
                database,
                trade_date=maturity_date,
                source="tushare",
                recorded_at=datetime(2026, 9, 30, 10, tzinfo=UTC),
            )
            repeated = run_stored_price_research_batch(
                database,
                trade_date=maturity_date,
                source="tushare",
                recorded_at=datetime(2026, 10, 1, 10, tzinfo=UTC),
            )

            self.assertEqual(4, matured["matured_observations"])
            self.assertEqual(12, matured["outcomes_recorded"])
            self.assertEqual(0, matured["blocked_observations"])
            self.assertEqual(0, repeated["observations_recorded"])
            self.assertEqual(0, repeated["outcomes_recorded"])
            for hypothesis_id in (
                "no-recent-limit-up-v1",
                "overnight-intraday-separation-v1",
            ):
                observations = database.list_research_forward_observations(
                    hypothesis_id=hypothesis_id
                )
                outcomes = database.list_research_forward_outcomes(hypothesis_id=hypothesis_id)
                self.assertEqual(2, len(observations))
                self.assertEqual(6, len(outcomes))

            completed = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "stock_mcp.cli",
                    "run-research-forward-batch",
                    "--root",
                    str(root),
                    "--trade-date",
                    maturity_date.isoformat(),
                ),
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(0, json.loads(completed.stdout)["outcomes_recorded"])

    def test_one_missing_future_symbol_stays_pending_without_blocking_other_outcomes(self) -> None:
        from stock_mcp import research_program

        run_stored_price_research_batch = getattr(
            research_program, "run_stored_price_research_batch", None
        )
        self.assertTrue(
            callable(run_stored_price_research_batch),
            "post-market research requires a restart-safe stored-price batch",
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(0, main(("initialize-research-program", "--root", str(root))))
            database = Database(root / "data" / "stock-mcp.sqlite3")
            sessions = tuple(date(2026, 8, 10) + timedelta(days=index) for index in range(26))
            database.save_expected_trading_days("tushare", sessions)
            securities = (
                Security("600001.SH", "Alpha", "SSE", "MAIN", date(2000, 1, 1), "A", False),
                Security("600002.SH", "Beta", "SSE", "MAIN", date(2000, 1, 1), "B", False),
            )
            for index, session in enumerate(sessions):
                timestamp = datetime.combine(session, datetime.min.time(), tzinfo=UTC)
                bars = [_bar("600001.SH", session, 100_000 + index * 1_000, timestamp)]
                if index != 12:
                    bars.append(_bar("600002.SH", session, 200_000 + index * 500, timestamp))
                database.save_market_snapshot(
                    MarketSnapshot(
                        session, "tushare", timestamp, securities, tuple(bars), 5_000, 5_000
                    )
                )
                database.save_daily_price_limits(
                    trade_date=session,
                    source="tushare",
                    limits={
                        bar.symbol: {
                            "limit_up_1e4": bar.close_1e4 + 10_000,
                            "limit_down_1e4": bar.close_1e4 - 10_000,
                            "touched_up": False,
                            "touched_down": False,
                            "policy_exception": False,
                            "algorithm": "mainboard-10pct-round-half-up-v1",
                        }
                        for bar in bars
                    },
                )
            signal_date = sessions[5]
            _save_strategy(database)
            database.save_daily_review(
                _review(signal_date, securities, candidate_symbols=securities)
            )
            run_stored_price_research_batch(
                database,
                trade_date=signal_date,
                source="tushare",
                recorded_at=datetime(2026, 8, 15, 10, tzinfo=UTC),
            )
            database.save_daily_review(_review(sessions[-1], securities, candidate_symbols=()))

            report = run_stored_price_research_batch(
                database,
                trade_date=sessions[-1],
                source="tushare",
                recorded_at=datetime(2026, 9, 30, 10, tzinfo=UTC),
            )

            self.assertEqual(2, report["matured_observations"])
            self.assertEqual(6, report["outcomes_recorded"])
            self.assertEqual(2, report["blocked_observations"])


def _bar(symbol: str, session: date, close: int, timestamp: datetime) -> DailyBar:
    return DailyBar(
        symbol=symbol,
        trade_date=session,
        open_1e4=close - 100,
        high_1e4=close + 100,
        low_1e4=close - 200,
        close_1e4=close,
        pre_close_1e4=close - 500,
        volume_shares=1_000,
        amount_fen=1_000_000,
        source="tushare",
        source_timestamp=timestamp,
    )


def _review(
    trade_date: date,
    securities: tuple[Security, ...],
    *,
    candidate_symbols: tuple[Security, ...],
) -> DailyReview:
    timestamp = datetime.combine(trade_date, datetime.min.time(), tzinfo=UTC)
    candidates = tuple(
        Candidate(
            candidate_id=f"{trade_date.isoformat()}-{security.symbol}",
            symbol=security.symbol,
            name=security.name,
            rank=index,
            score=80 - index,
            setup_type=SetupType.STRONG_PULLBACK,
            strategy_version="v0.3-policy-1",
            evidence=(Evidence("fixture", 1, 1, True, 0),),
            confirmation_condition="fixture",
            invalidation_condition="fixture",
        )
        for index, security in enumerate(candidate_symbols, start=1)
    )
    return DailyReview(
        status="published",
        trade_date=trade_date,
        source="tushare",
        source_timestamp=timestamp,
        strategy_version="v0.3-policy-1",
        market_regime=MarketRegime.NEUTRAL,
        candidates=candidates,
    )


def _save_strategy(database: Database) -> None:
    database.save_strategy_version(
        StrategyVersion("v0.3-policy-1", "proposed", {"rule_engine_version": 3})
    )


if __name__ == "__main__":
    unittest.main()
