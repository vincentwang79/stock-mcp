"""Pure qualification contracts for consecutive Sina shadow evidence."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from stock_mcp.provider_qualification import evaluate_provider_qualification


class SinaQualificationContractTest(unittest.TestCase):
    def test_missing_expected_trading_day_cannot_qualify(self) -> None:
        expected = tuple(date(2026, 7, 1) + timedelta(days=index) for index in range(20))
        runs = [_run(day) for day in expected if day != expected[10]]
        runs.append(_run(expected[-1] + timedelta(days=1)))

        result = evaluate_provider_qualification(
            runs,
            adapter_version="sina-adapter-v1",
            configuration_hash="a" * 64,
            windows_validation_complete=True,
            terms_attested=True,
            expected_trading_days=expected,
        )

        self.assertEqual("collecting", result["status"])
        self.assertFalse(result["consecutive_window_complete"])


def _run(day: date) -> dict[str, object]:
    return {
        "trade_date": day.isoformat(),
        "status": "success",
        "dataset_hash": f"{day.toordinal():064x}",
        "same_source_history_ok": True,
        "status_coverage_bps": 10_000,
    }


if __name__ == "__main__":
    unittest.main()
