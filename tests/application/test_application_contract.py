"""Contract tests for the MCP-facing application service.

These tests intentionally exercise only the application boundary.  The MCP
adapter owns JSON Schema and annotations, while SQLite owns durable storage.
The fake repository below documents the narrow protocol required by this
layer, so no test needs a database or a network connection.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

from stock_mcp.domain import (
    Candidate,
    DailyReview,
    Evidence,
    MarketRegime,
    SetupType,
    StrategyVersion,
)
from stock_mcp.strategy import StrategyRegistry

AS_OF = datetime(2026, 8, 7, 9, 31, tzinfo=UTC)
TRADE_DATE = date(2026, 8, 6)


def _candidate(*, candidate_id: str = "candidate-1") -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        symbol="600000.SH",
        name="浦发银行",
        rank=1,
        score=87,
        setup_type=SetupType.STRONG_PULLBACK,
        strategy_version="v0.1-proposed",
        evidence=(
            Evidence(
                metric="relative_strength_bps",
                value=1_250,
                threshold=800,
                passed=True,
                score_contribution=45,
            ),
        ),
        confirmation_condition="close >= 120000",
        invalidation_condition="close < 110000",
    )


def _review() -> DailyReview:
    return DailyReview(
        status="published",
        trade_date=TRADE_DATE,
        source="tushare",
        source_timestamp=AS_OF,
        strategy_version="v0.1-proposed",
        market_regime=MarketRegime.OFFENSIVE,
        candidates=(_candidate(),),
    )


class FakeRepository:
    """Minimal persistence protocol expected by ``StockMcpApplication``.

    A production repository must provide the same named methods.  Write
    methods receive an idempotency key and return their persisted object;
    calling the same method/key again returns the original object without
    appending another event.
    """

    def __init__(self) -> None:
        review = _review()
        self.reviews = {review.trade_date: review}
        self.candidates = {candidate.candidate_id: candidate for candidate in review.candidates}
        self.watchlists: dict[str, set[str]] = {}
        self.candidate_events: list[dict[str, object]] = []
        self.review_notes: list[dict[str, str]] = []
        self._idempotent_writes: dict[tuple[str, str], object] = {}
        self.write_counts: dict[str, int] = {}

    def get_daily_review(self, trade_date: date) -> DailyReview | None:
        return self.reviews.get(trade_date)

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        return self.candidates.get(candidate_id)

    def list_review_history(self) -> tuple[DailyReview, ...]:
        return tuple(
            sorted(self.reviews.values(), key=lambda review: review.trade_date, reverse=True)
        )

    def list_watchlists(self) -> tuple[str, ...]:
        return tuple(sorted(self.watchlists))

    def get_watchlist(self, name: str) -> tuple[str, ...] | None:
        symbols = self.watchlists.get(name)
        return None if symbols is None else tuple(sorted(symbols))

    def create_watchlist(self, *, name: str, idempotency_key: str) -> tuple[str, ...]:
        return self._write("create_watchlist", idempotency_key, lambda: self._create(name))

    def add_watchlist_items(
        self, *, name: str, symbols: tuple[str, ...], idempotency_key: str
    ) -> tuple[str, ...] | None:
        return self._write(
            "add_watchlist_items",
            idempotency_key,
            lambda: self._add_items(name, symbols),
        )

    def remove_watchlist_items(
        self, *, name: str, symbols: tuple[str, ...], idempotency_key: str
    ) -> tuple[str, ...] | None:
        return self._write(
            "remove_watchlist_items",
            idempotency_key,
            lambda: self._remove_items(name, symbols),
        )

    def record_candidate_event(
        self,
        *,
        candidate_id: str,
        status: str,
        event_date: date,
        price_1e4: int | None,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, object] | None:
        return self._write(
            "record_candidate_event",
            idempotency_key,
            lambda: self._candidate_event(candidate_id, status, event_date, price_1e4, reason),
        )

    def record_review_note(
        self, *, trade_date: date, note: str, idempotency_key: str
    ) -> dict[str, str] | None:
        return self._write(
            "record_review_note",
            idempotency_key,
            lambda: self._review_note(trade_date, note),
        )

    def list_review_notes(self, trade_date: date) -> tuple[dict[str, str], ...]:
        return tuple(
            note for note in self.review_notes if note["trade_date"] == trade_date.isoformat()
        )

    def _write(self, operation: str, key: str, action):  # type: ignore[no-untyped-def]
        cache_key = (operation, key)
        if cache_key not in self._idempotent_writes:
            self.write_counts[operation] = self.write_counts.get(operation, 0) + 1
            self._idempotent_writes[cache_key] = action()
        return self._idempotent_writes[cache_key]

    def _create(self, name: str) -> tuple[str, ...]:
        self.watchlists.setdefault(name, set())
        return ()

    def _add_items(self, name: str, symbols: tuple[str, ...]) -> tuple[str, ...] | None:
        if name not in self.watchlists:
            return None
        self.watchlists[name].update(symbols)
        return tuple(sorted(self.watchlists[name]))

    def _remove_items(self, name: str, symbols: tuple[str, ...]) -> tuple[str, ...] | None:
        if name not in self.watchlists:
            return None
        self.watchlists[name].difference_update(symbols)
        return tuple(sorted(self.watchlists[name]))

    def _candidate_event(
        self,
        candidate_id: str,
        status: str,
        event_date: date,
        price_1e4: int | None,
        reason: str,
    ) -> dict[str, object] | None:
        if candidate_id not in self.candidates:
            return None
        event = {
            "candidate_id": candidate_id,
            "status": status,
            "event_date": event_date.isoformat(),
            "price_1e4": price_1e4,
            "reason": reason,
        }
        self.candidate_events.append(event)
        return event

    def _review_note(self, trade_date: date, note: str) -> dict[str, str] | None:
        if trade_date not in self.reviews:
            return None
        entry = {"trade_date": trade_date.isoformat(), "note": note}
        self.review_notes.append(entry)
        return entry


@dataclass
class FakeQuoteProvider:
    close_1e4: int = 120_000
    fetches: list[str] | None = None

    def __post_init__(self) -> None:
        if self.fetches is None:
            self.fetches = []

    def fetch_quote(self, symbol: str) -> dict[str, object]:
        assert self.fetches is not None
        self.fetches.append(symbol)
        return {"symbol": symbol, "close_1e4": self.close_1e4, "source": "akshare", "as_of": AS_OF}


class FakeStrategyRegistry:
    def __init__(self) -> None:
        self.versions = {
            "v0.1-proposed": StrategyVersion(
                version="v0.1-proposed", status="proposed", parameters={"offensive_limit": 3}
            ),
            "v0.0-active": StrategyVersion(
                version="v0.0-active", status="active", parameters={"offensive_limit": 2}
            ),
        }
        self.active_version = "v0.0-active"
        self.propose_calls = 0
        self.activate_calls = 0

    def list_versions(self) -> tuple[StrategyVersion, ...]:
        return tuple(self.versions.values())

    def get(self, version: str) -> StrategyVersion | None:
        return self.versions.get(version)

    def propose(self, strategy: StrategyVersion) -> StrategyVersion:
        self.propose_calls += 1
        self.versions[strategy.version] = strategy
        return strategy

    def activate(self, version: str, *, confirmed: bool) -> StrategyVersion:
        self.activate_calls += 1
        if not confirmed:
            raise ValueError("confirmation required")
        proposal = self.versions[version]
        active = StrategyVersion(
            version=proposal.version, status="active", parameters=proposal.parameters
        )
        self.versions[version] = active
        self.active_version = version
        return active


class FakeReplay:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, date, date]] = []

    def compare(self, left: str, right: str, start: date, end: date) -> dict[str, object]:
        self.calls.append((left, right, start, end))
        return {"left_version": left, "right_version": right, "days_compared": 3}


class StockMcpApplicationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        from stock_mcp.application import StockMcpApplication

        self.repository = FakeRepository()
        self.quote_provider = FakeQuoteProvider()
        self.registry = FakeStrategyRegistry()
        self.replay = FakeReplay()
        self.application = StockMcpApplication(
            self.repository,
            self.quote_provider,
            self.registry,
            replay=self.replay,
        )

    def test_reading_a_published_review_and_candidate_preserves_rank_and_evidence(self) -> None:
        review = self.application.get_daily_review(trade_date=TRADE_DATE)
        self.assertTrue(review["ok"])
        self.assertEqual(1, review["data"]["candidates"][0]["rank"])
        self.assertEqual(
            "relative_strength_bps", review["data"]["candidates"][0]["evidence"][0]["metric"]
        )

        candidate = self.application.get_candidate(candidate_id="candidate-1")
        self.assertTrue(candidate["ok"])
        self.assertEqual(87, candidate["data"]["score"])
        self.assertEqual(1, candidate["data"]["rank"])
        self.assertEqual("v0.1-proposed", candidate["data"]["strategy_version"])

    def test_missing_published_records_are_business_errors(self) -> None:
        missing_review = self.application.get_daily_review(trade_date=date(2099, 1, 1))
        self.assertEqual(
            {
                "ok": False,
                "error": {"code": "daily_review_not_found", "message": "no published review"},
            },
            missing_review,
        )
        missing_candidate = self.application.get_candidate(candidate_id="missing")
        self.assertEqual("candidate_not_found", missing_candidate["error"]["code"])

    def test_next_day_quote_is_explicit_and_reports_source_time_and_confirmation(self) -> None:
        self.application.get_daily_review(trade_date=TRADE_DATE)
        self.application.list_watchlists()
        self.assertEqual([], self.quote_provider.fetches, "reads must never start a quote poll")

        confirmed = self.application.check_next_day(candidate_id="candidate-1")
        self.assertTrue(confirmed["ok"])
        self.assertEqual(["600000.SH"], self.quote_provider.fetches)
        self.assertEqual("confirmed", confirmed["data"]["status"])
        self.assertEqual("akshare", confirmed["data"]["source"])
        self.assertEqual(AS_OF, confirmed["data"]["as_of"])

        invalidating_quotes = FakeQuoteProvider(close_1e4=109_999)
        invalidating_app = type(self.application)(
            self.repository, invalidating_quotes, self.registry, replay=self.replay
        )
        invalidated = invalidating_app.check_next_day(candidate_id="candidate-1")
        self.assertEqual("invalidated", invalidated["data"]["status"])

    def test_next_day_condition_preserves_strict_comparison_operators(self) -> None:
        strict = replace(
            _candidate(),
            confirmation_condition="close > 120000",
            invalidation_condition="close <= 110000",
        )
        self.repository.candidates[strict.candidate_id] = strict
        equal_confirmation = type(self.application)(
            self.repository, FakeQuoteProvider(close_1e4=120_000), self.registry
        ).check_next_day(candidate_id=strict.candidate_id)
        equal_invalidation = type(self.application)(
            self.repository, FakeQuoteProvider(close_1e4=110_000), self.registry
        ).check_next_day(candidate_id=strict.candidate_id)

        self.assertEqual("pending", equal_confirmation["data"]["status"])
        self.assertEqual("invalidated", equal_invalidation["data"]["status"])

    def test_watchlists_are_named_and_write_operations_are_idempotent(self) -> None:
        created = self.application.create_watchlist(name="focus", idempotency_key="create-1")
        created_again = self.application.create_watchlist(name="focus", idempotency_key="create-1")
        self.assertEqual(created, created_again)
        self.assertEqual(1, self.repository.write_counts["create_watchlist"])

        added = self.application.add_watchlist_items(
            name="focus", symbols=("600000.SH", "000001.SZ"), idempotency_key="add-1"
        )
        self.assertTrue(added["ok"])
        self.assertEqual(["600000.SH", "000001.SZ"], added["data"]["symbols"])
        self.assertEqual(["focus"], self.application.list_watchlists()["data"]["names"])
        self.assertEqual(
            ["600000.SH", "000001.SZ"],
            self.application.get_watchlist(name="focus")["data"]["symbols"],
        )

        removed = self.application.remove_watchlist_items(
            name="focus", symbols=("000001.SZ",), idempotency_key="remove-1"
        )
        self.assertEqual(["600000.SH"], removed["data"]["symbols"])
        self.assertEqual(
            "watchlist_not_found", self.application.get_watchlist(name="missing")["error"]["code"]
        )

    def test_events_and_notes_append_once_and_are_available_in_review_history(self) -> None:
        event = self.application.record_candidate_event(
            candidate_id="candidate-1",
            status="watched",
            event_date=TRADE_DATE,
            price_1e4=120_000,
            reason="close held above confirmation",
            idempotency_key="event-1",
        )
        same_event = self.application.record_candidate_event(
            candidate_id="candidate-1",
            status="watched",
            event_date=TRADE_DATE,
            price_1e4=120_000,
            reason="close held above confirmation",
            idempotency_key="event-1",
        )
        self.assertEqual(event, same_event)
        self.assertEqual(1, len(self.repository.candidate_events))

        note = self.application.record_review_note(
            trade_date=TRADE_DATE, note="wait for volume", idempotency_key="note-1"
        )
        self.assertTrue(note["ok"])
        history = self.application.get_review_history()
        self.assertTrue(history["ok"])
        self.assertEqual(TRADE_DATE.isoformat(), history["data"]["reviews"][0]["trade_date"])
        self.assertEqual("wait for volume", history["data"]["reviews"][0]["notes"][0]["note"])

    def test_strategy_proposals_and_activation_confirmation(
        self,
    ) -> None:
        listed = self.application.list_strategy_versions()
        self.assertTrue(listed["ok"])
        self.assertEqual(
            ["v0.0-active", "v0.1-proposed"],
            sorted(version["version"] for version in listed["data"]["versions"]),
        )
        proposal = self.application.create_strategy_proposal(
            version="v0.2-proposed",
            parameters={"offensive_limit": 3},
            idempotency_key="proposal-1",
        )
        self.assertTrue(proposal["ok"])
        self.assertEqual("proposed", proposal["data"]["status"])

        comparison = self.application.compare_strategy_versions(
            left_version="v0.0-active",
            right_version="v0.1-proposed",
            start=date(2026, 8, 3),
            end=TRADE_DATE,
        )
        self.assertTrue(comparison["ok"])
        self.assertEqual(1, len(self.replay.calls), "comparison must invoke an explicit replay")

        refused = self.application.activate_strategy_version(
            version="v0.1-proposed", confirmed=False, idempotency_key="activate-1"
        )
        self.assertEqual("confirmation_required", refused["error"]["code"])
        self.assertEqual("v0.0-active", self.registry.active_version)
        active = self.application.activate_strategy_version(
            version="v0.1-proposed", confirmed=True, idempotency_key="activate-2"
        )
        self.assertEqual("active", active["data"]["status"])
        self.assertEqual("v0.1-proposed", self.registry.active_version)
        self.assertEqual(
            "v0.1-proposed", self.repository.get_daily_review(TRADE_DATE).strategy_version
        )

    def test_strategy_proposal_rejects_unknown_or_non_integer_parameters(self) -> None:
        unknown = self.application.create_strategy_proposal(
            version="v-secret",
            parameters={"offensive_limit": 3, "broker_token": "must-not-persist"},
            idempotency_key="proposal-secret",
        )
        invalid = self.application.create_strategy_proposal(
            version="v-invalid",
            parameters={"offensive_limit": -1},
            idempotency_key="proposal-invalid",
        )

        self.assertEqual("strategy_proposal_rejected", unknown["error"]["code"])
        self.assertEqual("strategy_proposal_rejected", invalid["error"]["code"])

    def test_official_v03_names_cannot_be_used_with_legacy_parameters(self) -> None:
        result = self.application.create_strategy_proposal(
            version="v0.3-policy-1",
            parameters={"offensive_limit": 3},
            idempotency_key="reserved-v03-name",
        )

        self.assertEqual("strategy_proposal_rejected", result["error"]["code"])
        self.assertIn("frozen v3 policy", result["error"]["message"])

    def test_application_methods_do_not_accept_trading_or_account_arguments(self) -> None:
        with self.assertRaises(TypeError):
            self.application.create_watchlist(  # type: ignore[call-arg]
                name="focus", idempotency_key="bad-input", balance=100_000
            )
        with self.assertRaises(TypeError):
            self.application.check_next_day(  # type: ignore[call-arg]
                candidate_id="candidate-1", order={"side": "buy"}
            )

    def test_real_strategy_registry_maps_unknown_versions_to_business_errors(self) -> None:
        registry = StrategyRegistry()
        registry.propose(
            StrategyVersion(
                version="v0.1-proposed",
                status="proposed",
                parameters={"offensive_limit": 3},
            )
        )
        application = type(self.application)(
            self.repository,
            self.quote_provider,
            registry,
            replay=self.replay,
        )

        listed = application.list_strategy_versions()
        compared = application.compare_strategy_versions(
            left_version="missing",
            right_version="v0.1-proposed",
            start=date(2026, 8, 3),
            end=TRADE_DATE,
        )
        activated = application.activate_strategy_version(
            version="missing", confirmed=True, idempotency_key="missing-activation"
        )

        self.assertEqual("v0.1-proposed", listed["data"]["versions"][0]["version"])
        self.assertEqual("strategy_version_not_found", compared["error"]["code"])
        self.assertEqual("strategy_version_not_found", activated["error"]["code"])


if __name__ == "__main__":
    unittest.main()
