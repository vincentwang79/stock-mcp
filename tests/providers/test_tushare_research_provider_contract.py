"""Offline contracts for point-in-time Tushare research collection."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime

from stock_mcp.providers import runtime


class _Frame:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, *, orient):
        if orient != "records":
            raise AssertionError(orient)
        return [dict(row) for row in self._rows]


class _Client:
    def __init__(self, *, duplicate: bool = False) -> None:
        daily = {"ts_code": "600001.SH", "trade_date": "20260814", "pb": 1.1}
        self.daily_rows = [daily, daily] if duplicate else [daily]
        self.fina_rows = [
            {
                "ts_code": "600001.SH",
                "ann_date": "20260814",
                "end_date": "20260630",
                "roe": 10.5,
                "update_flag": "1",
            }
        ]
        self.calls = []

    def daily_basic(self, **arguments):
        self.calls.append(("daily_basic", arguments))
        return _Frame(self.daily_rows)

    def fina_indicator_vip(self, **arguments):
        self.calls.append(("fina_indicator_vip", arguments))
        return _Frame(self.fina_rows)


class _Repository:
    def __init__(self) -> None:
        self.facts = ()

    def save_point_in_time_fundamentals(self, facts):
        self.facts = tuple(facts)
        return len(self.facts)


class TushareResearchProviderContractTest(unittest.TestCase):
    def test_injected_client_collects_one_atomic_point_in_time_day(self) -> None:
        provider_type = getattr(runtime, "TushareResearchFactProvider", None)
        collect = getattr(runtime, "collect_tushare_research_day", None)
        self.assertTrue(callable(provider_type), "research collection needs an injected adapter")
        self.assertTrue(callable(collect), "research collection needs one atomic coordinator")
        client = _Client()
        timestamp = datetime(2026, 8, 14, 9, tzinfo=UTC)
        provider = provider_type(client=client, clock=lambda: timestamp)
        repository = _Repository()
        report = collect(repository, provider=provider, as_of=date(2026, 8, 14))
        self.assertEqual(
            [
                ("daily_basic", {"trade_date": "20260814"}),
                ("fina_indicator_vip", {"ann_date": "20260814"}),
            ],
            client.calls,
        )
        self.assertEqual({"daily_basic": 1, "fina_indicator": 1, "saved": 2}, report)
        self.assertEqual(2, len(repository.facts))
        self.assertTrue(all(item["source"] == "tushare" for item in repository.facts))

    def test_optional_financial_update_flag_may_be_absent(self) -> None:
        client = _Client()
        client.fina_rows[0].pop("update_flag")
        provider = runtime.TushareResearchFactProvider(
            client=client,
            clock=lambda: datetime(2026, 8, 14, 9, tzinfo=UTC),
        )
        repository = _Repository()
        report = runtime.collect_tushare_research_day(
            repository, provider=provider, as_of=date(2026, 8, 14)
        )
        self.assertEqual(2, report["saved"])
        self.assertEqual("20260814|0", repository.facts[1]["revision_key"])

    def test_duplicate_provider_rows_fail_before_repository_write(self) -> None:
        provider_type = getattr(runtime, "TushareResearchFactProvider", None)
        collect = getattr(runtime, "collect_tushare_research_day", None)
        self.assertTrue(callable(provider_type))
        self.assertTrue(callable(collect))
        provider = provider_type(
            client=_Client(duplicate=True),
            clock=lambda: datetime(2026, 8, 14, 9, tzinfo=UTC),
        )
        repository = _Repository()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            collect(repository, provider=provider, as_of=date(2026, 8, 14))
        self.assertEqual((), repository.facts)


if __name__ == "__main__":
    unittest.main()
