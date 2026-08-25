"""Read-only validation for the latest live v3 post-market observation."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


def _row(connection: sqlite3.Connection, sql: str, values: tuple[object, ...] = ()) -> Any:
    return connection.execute(sql, values).fetchone()


def _count(connection: sqlite3.Connection, sql: str, values: tuple[object, ...] = ()) -> int:
    return int(connection.execute(sql, values).fetchone()[0])


def _report(connection: sqlite3.Connection, *, after: str) -> dict[str, object]:
    connection.row_factory = sqlite3.Row
    active = _row(connection, "SELECT version FROM active_strategy WHERE singleton = 1")
    schedule = _row(
        connection,
        "SELECT trade_date,status,next_at,pipeline_version,error FROM schedule_outcomes "
        "WHERE trade_date>? ORDER BY trade_date DESC LIMIT 1",
        (after,),
    )
    if schedule is None:
        return {
            "ok": False,
            "after": after,
            "error": "no schedule outcome after cutoff",
            "validation": {
                "status": "fail",
                "failures": ["no_post_cutoff_schedule_outcome"],
            },
        }

    trade_date = str(schedule["trade_date"])
    pipeline = _row(
        connection,
        "SELECT trade_date,pipeline_version,status,attempts,strategy_version,error "
        "FROM pipeline_runs WHERE trade_date=? AND pipeline_version='pipeline-v0.1'",
        (trade_date,),
    )
    strategy_version = None if pipeline is None else pipeline["strategy_version"]
    review = None
    candidate_count = 0
    if strategy_version:
        review = _row(
            connection,
            "SELECT trade_date,strategy_version,status,source,source_timestamp,market_regime "
            "FROM daily_reviews WHERE trade_date=? AND strategy_version=?",
            (trade_date, strategy_version),
        )
        candidate_count = _count(
            connection,
            "SELECT COUNT(*) FROM candidates WHERE trade_date=? AND strategy_version=?",
            (trade_date, strategy_version),
        )
    source = None if review is None else str(review["source"])

    def fact_count(table: str) -> int:
        if source is None:
            return 0
        return _count(
            connection,
            f"SELECT COUNT(*) FROM {table} WHERE trade_date=? AND source=?",
            (trade_date, source),
        )

    target_bars = fact_count("daily_bars")
    limits = fact_count("daily_price_limits")
    features = fact_count("v3_snapshot_features")
    live_sessions = 0
    if strategy_version:
        live_sessions = _count(
            connection,
            "SELECT COUNT(*) FROM pipeline_runs WHERE pipeline_version='pipeline-v0.1' "
            "AND status='degraded_observation' AND strategy_version=?",
            (strategy_version,),
        )
    historical_simulation_sessions = 0
    if strategy_version:
        try:
            bootstrap = _row(
                connection,
                "SELECT session_count FROM historical_observation_bootstrap_runs "
                "WHERE pipeline_version='pipeline-v0.1' AND strategy_version=? "
                "AND source='tushare' "
                "AND policy_version='historical-production-simulation-v1' "
                "AND session_count=20 AND end_date>=date(?, '-35 days') AND end_date<? "
                "ORDER BY end_date DESC, recorded_at DESC LIMIT 1",
                (strategy_version, trade_date, trade_date),
            )
            historical_simulation_sessions = 0 if bootstrap is None else int(bootstrap[0])
        except sqlite3.OperationalError:
            # Older installations have no bootstrap tables; report no evidence rather
            # than treating a schema upgrade as a successful simulation.
            historical_simulation_sessions = 0
    required_live_observation_sessions = 3 if historical_simulation_sessions == 20 else 20
    forward_observations = _count(connection, "SELECT COUNT(*) FROM research_forward_observations")

    failures: list[str] = []
    active_version = None if active is None else str(active["version"])
    schedule_status = str(schedule["status"])
    pipeline_status = None if pipeline is None else str(pipeline["status"])
    review_status = None if review is None else str(review["status"])
    error_text = " ".join(
        str(value or "")
        for value in (
            schedule["error"],
            None if pipeline is None else pipeline["error"],
        )
    ).lower()
    if active_version != "v0.3-policy-1":
        failures.append("active_strategy_is_not_v0.3-policy-1")
    if schedule_status not in {"degraded_observation", "ready"}:
        failures.append("schedule_is_not_a_successful_observation_or_publication")
    if pipeline_status != schedule_status:
        failures.append("pipeline_status_does_not_match_schedule")
    if pipeline is None or strategy_version != active_version:
        failures.append("pipeline_strategy_does_not_match_active_strategy")
    expected_review_status = (
        "observation" if schedule_status == "degraded_observation" else "published"
    )
    if review_status != expected_review_status:
        failures.append("daily_review_status_is_unexpected")
    if source != "tushare":
        failures.append("daily_review_source_is_not_tushare")
    if not 0 <= candidate_count <= 3:
        failures.append("candidate_count_is_outside_0_to_3")
    if target_bars <= 0 or not (target_bars == limits == features):
        failures.append("target_bar_limit_feature_counts_do_not_match")
    if schedule_status == "degraded_observation" and live_sessions < 1:
        failures.append("no_successful_live_observation_session")
    if "missing without a recorded suspension" in error_text:
        failures.append("unresolved_unrecorded_history_gap")
    if "insufficient history" in error_text:
        failures.append("unresolved_insufficient_history")

    return {
        "ok": not failures,
        "trade_date": trade_date,
        "active_strategy": active_version,
        "schedule": dict(schedule),
        "pipeline_run": None if pipeline is None else dict(pipeline),
        "daily_review": None if review is None else dict(review),
        "candidate_count": candidate_count,
        "target_bar_count": target_bars,
        "price_limit_count": limits,
        "v3_feature_count": features,
        "live_observation_sessions": live_sessions,
        "historical_simulation_sessions": historical_simulation_sessions,
        "required_live_observation_sessions": required_live_observation_sessions,
        "forward_observation_count": forward_observations,
        "validation": {"status": "pass" if not failures else "fail", "failures": failures},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--after", required=True)
    args = parser.parse_args()
    database = args.database.resolve()
    if not database.is_file():
        parser.error(f"database does not exist: {database}")
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
        report = _report(connection, after=args.after)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
