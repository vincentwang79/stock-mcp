from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta

from stock_mcp.domain import DailyBar, MarketSnapshot, Security
from stock_mcp.industry import RecordedIndustryReference
from stock_mcp.v3_facts import build_live_v3_market_input


class V3LiveInputContractTest(unittest.TestCase):
    def test_target_day_missing_prices_use_recorded_status_in_the_same_aggregate_audit(
        self,
    ) -> None:
        target = date(2026, 8, 27)
        sessions = tuple(target - timedelta(days=60 - index) for index in range(60))
        timestamp = datetime(2026, 8, 27, 9, tzinfo=UTC)
        securities = (
            Security("600001.SH", "正常样本", "SSE", "MAIN", date(2020, 1, 1), "银行", False),
            Security("600002.SH", "合法停牌", "SSE", "MAIN", date(2020, 1, 1), "银行", False),
            Security("600003.SH", "可交易缺价", "SSE", "MAIN", date(2020, 1, 1), "银行", False),
        )
        bars = tuple(
            _bar(security.symbol, session, timestamp)
            for security in securities
            for session in (*sessions, target)
            if session != target or security.symbol == "600001.SH"
        )
        snapshot = MarketSnapshot(target, "tushare", timestamp, securities, bars, 5_000, 5_000)
        reference = RecordedIndustryReference(
            standard="fixture",
            mode="retrospective_current_mapping",
            as_of=date(2026, 8, 10),
            mapping_sha256="d" * 64,
            industries={security.symbol: "银行" for security in securities},
        )

        with self.assertRaises(ValueError) as raised:
            build_live_v3_market_input(
                snapshot,
                prior_dates=sessions,
                industry_reference=reference,
                trading_statuses={
                    ("600002.SH", target): "0",
                    ("600003.SH", target): "1",
                },
            )

        report = raised.exception.report
        self.assertEqual(1, report["recorded_suspension_count"])
        self.assertEqual(1, report["tradable_price_gap_count"])
        self.assertEqual(target.isoformat(), report["tradable_price_gap_dates"][0]["trade_date"])

    def test_twenty_two_rolling_sessions_survive_suspension_resume_and_repair(self) -> None:
        sessions = tuple(date(2026, 4, 1) + timedelta(days=index) for index in range(82))
        timestamp = datetime(2026, 8, 27, 9, tzinfo=UTC)
        suspension_day = sessions[65]
        missing_day = sessions[69]
        securities = (
            Security("600001.SH", "稳定样本", "SSE", "MAIN", date(2020, 1, 1), "银行", False),
            Security("600002.SH", "停复牌样本", "SSE", "MAIN", date(2020, 1, 1), "制造", False),
            Security("600003.SH", "ST样本", "SSE", "MAIN", date(2020, 1, 1), "制造", True),
            Security("600004.SH", "新上市样本", "SSE", "MAIN", sessions[70], "制造", False),
        )
        reference = RecordedIndustryReference(
            standard="fixture",
            mode="retrospective_current_mapping",
            as_of=date(2026, 8, 10),
            mapping_sha256="c" * 64,
            industries={security.symbol: security.industry for security in securities},
        )
        completed = 0
        repaired = False
        for target_index in range(60, 82):
            target = sessions[target_index]
            prior = sessions[target_index - 60 : target_index]
            omit_whole_day = target_index == 72 and not repaired
            bars = tuple(
                _bar(security.symbol, session, timestamp)
                for security in securities
                for session in (*prior, target)
                if session >= security.list_date
                and not (security.symbol == "600002.SH" and session == suspension_day)
                and not (
                    omit_whole_day and security.symbol == "600001.SH" and session == missing_day
                )
            )
            snapshot = MarketSnapshot(
                target,
                "tushare",
                timestamp,
                securities,
                bars,
                5_000,
                5_000,
            )
            statuses = {("600002.SH", suspension_day): "0"}
            if omit_whole_day:
                statuses[("600001.SH", missing_day)] = "1"
                with self.assertRaisesRegex(ValueError, "missing.*recorded suspension"):
                    build_live_v3_market_input(
                        snapshot,
                        prior_dates=prior,
                        industry_reference=reference,
                        trading_statuses=statuses,
                    )
                repaired = True
                bars = (*bars, _bar("600001.SH", missing_day, timestamp))
                snapshot = MarketSnapshot(
                    target,
                    "tushare",
                    timestamp,
                    securities,
                    bars,
                    5_000,
                    5_000,
                )
            market, _features = build_live_v3_market_input(
                snapshot,
                prior_dates=prior,
                industry_reference=reference,
                trading_statuses=statuses,
            )
            self.assertGreaterEqual(market.breadth.eligible_count, 1)
            completed += 1

        self.assertTrue(repaired)
        self.assertEqual(22, completed)

    def test_missing_tradable_history_fails_instead_of_silently_excluding_security(self) -> None:
        target = date(2026, 8, 20)
        sessions = tuple(target - timedelta(days=60 - index) for index in range(60))
        missing = sessions[-5]
        timestamp = datetime(2026, 8, 20, 9, tzinfo=UTC)
        security = Security(
            "600001.SH", "缺失可交易日", "SSE", "MAIN", date(2020, 1, 1), "银行", False
        )
        bars = tuple(
            _bar(security.symbol, session, timestamp)
            for session in (*sessions, target)
            if session != missing
        )
        snapshot = MarketSnapshot(target, "tushare", timestamp, (security,), bars, 5_000, 5_000)
        reference = RecordedIndustryReference(
            standard="fixture",
            mode="retrospective_current_mapping",
            as_of=date(2026, 8, 10),
            mapping_sha256="b" * 64,
            industries={security.symbol: "银行"},
        )

        with self.assertRaisesRegex(ValueError, "missing.*recorded suspension"):
            build_live_v3_market_input(
                snapshot,
                prior_dates=sessions,
                industry_reference=reference,
                trading_statuses={(security.symbol, missing): "1"},
            )

    def test_all_missing_status_and_tradable_price_gaps_are_reported_together(self) -> None:
        target = date(2026, 8, 27)
        sessions = tuple(target - timedelta(days=60 - index) for index in range(60))
        timestamp = datetime(2026, 8, 27, 9, tzinfo=UTC)
        securities = (
            Security("600001.SH", "状态缺失", "SSE", "MAIN", date(2020, 1, 1), "银行", False),
            Security("600002.SH", "可交易缺价", "SSE", "MAIN", date(2020, 1, 1), "银行", False),
        )
        missing_status_date = sessions[-2]
        tradable_gap_date = sessions[-1]
        bars = tuple(
            _bar(security.symbol, session, timestamp)
            for security in securities
            for session in (*sessions, target)
            if not (
                (security.symbol == "600001.SH" and session == missing_status_date)
                or (security.symbol == "600002.SH" and session == tradable_gap_date)
            )
        )
        snapshot = MarketSnapshot(target, "tushare", timestamp, securities, bars, 5_000, 5_000)
        reference = RecordedIndustryReference(
            standard="fixture",
            mode="retrospective_current_mapping",
            as_of=date(2026, 8, 10),
            mapping_sha256="b" * 64,
            industries={security.symbol: "银行" for security in securities},
        )

        with self.assertRaises(ValueError) as raised:
            build_live_v3_market_input(
                snapshot,
                prior_dates=sessions,
                industry_reference=reference,
                trading_statuses={("600002.SH", tradable_gap_date): "1"},
            )

        report = getattr(raised.exception, "report", None)
        self.assertIsInstance(report, dict)
        self.assertEqual("live-v3-evidence-audit-v1", report["schema"])
        self.assertEqual(1, report["missing_status_count"])
        self.assertEqual(1, report["tradable_price_gap_count"])
        self.assertEqual(
            missing_status_date.isoformat(), report["missing_status_dates"][0]["trade_date"]
        )
        self.assertEqual(
            tradable_gap_date.isoformat(), report["tradable_price_gap_dates"][0]["trade_date"]
        )


def _bar(symbol: str, trade_date: date, timestamp: datetime) -> DailyBar:
    return DailyBar(
        symbol,
        trade_date,
        100_000,
        101_000,
        99_000,
        100_000,
        100_000,
        1_000_000,
        10_000_000_000,
        "tushare",
        timestamp,
    )


if __name__ == "__main__":
    unittest.main()
