from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from stock_mcp import production
from stock_mcp.domain import DailyBar, MarketSnapshot, Security, StrategyVersion
from stock_mcp.storage import Database
from stock_mcp.v3 import v3_proposal_parameters

TARGET = date(2026, 9, 3)
RECORDED_AT = datetime(2026, 9, 4, 1, 45, tzinfo=UTC)


class LateReconciledPublicationServiceContractTest(unittest.TestCase):
    def test_publishes_from_recorded_facts_without_rewriting_retry_audit(self) -> None:
        runner = getattr(production, "publish_late_reconciled_v3_daily_review", None)
        self.assertTrue(
            callable(runner),
            "late reconciled formal publication must have a dedicated offline service path",
        )
        if not callable(runner):
            return

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "current").mkdir()
            (root / "current" / "a_share_mainboard_code_name.json").write_text(
                json.dumps(
                    {
                        "metadata": {
                            "standard": "fixture-industry-v1",
                            "mode": "retrospective_current_mapping",
                            "as_of": "2026-08-10",
                        },
                        "stocks": [{"code": "600001", "exchange": "SSE", "industry": "测试行业"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            database = Database(root / "data" / "stock-mcp.sqlite3")
            database.initialize()
            strategy = StrategyVersion(
                version="v0.3-policy-1",
                status="active",
                parameters=v3_proposal_parameters(1),
            )
            database.save_strategy_version(strategy)
            database.set_active_strategy_version(strategy.version)
            security = Security(
                "600001.SH", "测试证券", "SSE", "MAIN", date(2020, 1, 1), "测试行业", False
            )
            sessions = tuple(TARGET - timedelta(days=60 - index) for index in range(61))
            timestamp = datetime(2026, 9, 3, 16, 31, tzinfo=UTC)
            bars = tuple(
                DailyBar(
                    security.symbol,
                    session,
                    100_000 + index,
                    101_000 + index,
                    99_000 + index,
                    100_000 + index,
                    99_900 + index,
                    1_000_000,
                    10_000_000_000,
                    "tushare",
                    timestamp,
                )
                for index, session in enumerate(sessions)
            )
            database.save_expected_trading_days("tushare", sessions)
            database.save_market_snapshot(
                MarketSnapshot(
                    TARGET,
                    "tushare",
                    timestamp,
                    (security,),
                    bars,
                    5_000,
                    5_000,
                )
            )
            database.save_daily_security_statuses(
                {
                    "symbol": security.symbol,
                    "trade_date": session,
                    "source": "baostock",
                    "tradestatus": "1",
                    "is_st": False,
                    "source_timestamp": timestamp,
                    "batch_sha256": "fixture-status",
                }
                for session in sessions
            )
            database.save_schedule_outcome_record(
                trade_date=TARGET,
                status="retry_scheduled",
                next_at=datetime(2026, 9, 3, 17, 10, tzinfo=UTC),
                pipeline_version="pipeline-v0.1",
                error="fixture incomplete status batch",
            )

            result = runner(
                database=database,
                root=root,
                strategy=strategy,
                trade_date=TARGET,
                recorded_at=RECORDED_AT,
                idempotency_key="publish-2026-09-03-fixture-1",
            )

            self.assertEqual("late_reconciled", result["publication_class"])
            self.assertEqual("retry_scheduled", result["original_schedule_status"])
            self.assertEqual("published", database.get_daily_review(TARGET).status)
            self.assertEqual("retry_scheduled", database.get_publication_status(TARGET)["status"])
            self.assertEqual(1, len(database.load_v3_snapshot_features(TARGET, source="tushare")))
