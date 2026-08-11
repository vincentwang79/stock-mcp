"""Pure qualification rules for the optional Sina provider."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import date

QUALIFICATION_STATES = frozenset(
    {"collecting", "qualified_for_manual_approval", "failed", "expired"}
)


def evaluate_provider_qualification(
    runs: Sequence[Mapping[str, object]],
    *,
    adapter_version: str,
    configuration_hash: str,
    windows_validation_complete: bool,
    terms_attested: bool,
    expected_trading_days: Sequence[date] | None = None,
) -> dict[str, object]:
    ordered = sorted(runs, key=lambda item: str(item.get("trade_date", "")))
    last_twenty = ordered[-20:]
    expected_window = tuple(expected_trading_days or ())[-20:]
    recorded_window = tuple(str(run.get("trade_date", "")) for run in last_twenty)
    consecutive_window_complete = len(expected_window) == 20 and recorded_window == tuple(
        day.isoformat() for day in expected_window
    )
    failure = next(
        (
            run
            for run in last_twenty
            if run.get("status") not in {"success", "completed"}
            or int(run.get("missing_count", 0)) != 0
            or int(run.get("duplicate_count", 0)) != 0
            or int(run.get("invalid_count", 0)) != 0
            or run.get("same_source_history_ok", run.get("same_source_history")) is not True
            or int(run.get("status_coverage_bps", 10_000)) != 10_000
        ),
        None,
    )
    if failure is not None:
        status = "failed"
    elif (
        len(last_twenty) < 20
        or not consecutive_window_complete
        or not windows_validation_complete
        or not terms_attested
    ):
        status = "collecting"
    else:
        status = "qualified_for_manual_approval"
    window_payload = "|".join(
        f"{item.get('trade_date')}:{item.get('dataset_hash')}" for item in last_twenty
    )
    window_hash = hashlib.sha256(window_payload.encode()).hexdigest()
    through_date = str(last_twenty[-1].get("trade_date", "")) if last_twenty else ""
    return {
        "qualification_id": f"sina:{through_date}:{window_hash[:16]}",
        "source": "sina",
        "status": status,
        "adapter_version": adapter_version,
        "configuration_hash": configuration_hash,
        "window_days": len(last_twenty),
        "consecutive_window_complete": consecutive_window_complete,
        "window_hash": window_hash,
        "dataset_hash": window_hash,
        "through_date": through_date,
        "manual_approval_required": True,
        "source_active": False,
    }


def qualification_is_current(
    qualification: Mapping[str, object],
    *,
    adapter_version: str,
    configuration_hash: str,
    latest_run_successful: bool,
) -> bool:
    return (
        qualification.get("status") == "qualified_for_manual_approval"
        and qualification.get("adapter_version") == adapter_version
        and qualification.get("configuration_hash") == configuration_hash
        and latest_run_successful
    )
