"""Stable, SDK-independent definition of the public MCP tool catalog.

The application service owns persistence and business rules.  This module is
deliberately thin: it validates the public boundary then dispatches to the
equally-named service method.  Keeping the catalog separate from the transport
makes it possible to exercise every tool without a network server or MCP SDK.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .storage import IdempotencyKeyReuseError


class _Dto(BaseModel):
    """Public input/output DTO base: unknown fields are never silently kept."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    def model_dump_one_level(self) -> dict[str, Any]:
        """MCP SDK argument-model hook that retains nested Pydantic values."""

        return {
            (field.alias or name): getattr(self, name)
            for name, field in self.__class__.model_fields.items()
        }


class _IdempotentWrite(_Dto):
    idempotency_key: str = Field(min_length=1, max_length=128)


class GetDailyReviewInput(_Dto):
    trade_date: date


class GetCandidateInput(_Dto):
    candidate_id: str = Field(min_length=1, max_length=200)


class CheckNextDayInput(_Dto):
    candidate_id: str = Field(min_length=1, max_length=200)


class ListWatchlistsInput(_Dto):
    pass


class GetWatchlistInput(_Dto):
    name: str = Field(min_length=1, max_length=80)


class CreateWatchlistInput(_IdempotentWrite):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)


class AddWatchlistItemsInput(_IdempotentWrite):
    name: str = Field(min_length=1, max_length=80)
    symbols: list[str] = Field(min_length=1, max_length=200)


class RemoveWatchlistItemsInput(_IdempotentWrite):
    name: str = Field(min_length=1, max_length=80)
    symbols: list[str] = Field(min_length=1, max_length=200)


class RecordCandidateEventInput(_IdempotentWrite):
    candidate_id: str = Field(min_length=1, max_length=200)
    status: Literal["watched", "bought", "skipped", "exited"]
    event_date: date
    price_1e4: int | None = Field(default=None, gt=0)
    reason: str = Field(min_length=1, max_length=2_000)


class RecordReviewNoteInput(_IdempotentWrite):
    trade_date: date
    note: str = Field(min_length=1, max_length=4_000)


class GetReviewHistoryInput(_Dto):
    candidate_id: str | None = Field(default=None, min_length=1, max_length=200)
    limit: int = Field(default=50, ge=1, le=200)


class ListStrategyVersionsInput(_Dto):
    pass


class CompareStrategyVersionsInput(_Dto):
    left_version: str = Field(min_length=1, max_length=80)
    right_version: str = Field(min_length=1, max_length=80)
    start: date
    end: date


class StartStrategyReplayInput(_IdempotentWrite):
    version: str = Field(min_length=1, max_length=80)
    start_date: date
    end_date: date


class GetStrategyReplayInput(_Dto):
    replay_id: str = Field(min_length=1, max_length=100)


class ListStrategyReplaysInput(_Dto):
    version: str | None = Field(default=None, min_length=1, max_length=80)
    limit: int = Field(default=20, ge=1, le=200)


class GetStrategyReplayDaysInput(_Dto):
    replay_id: str = Field(min_length=1, max_length=100)
    after_trade_date: date | None = None
    limit: int = Field(default=20, ge=1, le=50)


class CertifyStrategyReplayInput(_IdempotentWrite):
    replay_id: str = Field(min_length=1, max_length=100)
    confirmed: bool


class CreateStrategyProposalInput(_IdempotentWrite):
    version: str = Field(min_length=1, max_length=80)
    parameters: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=4_000)


class ActivateStrategyVersionInput(_IdempotentWrite):
    version: str = Field(min_length=1, max_length=80)
    confirmed: bool


class ToolError(_Dto):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1_000)


class ToolResult(_Dto):
    """The common envelope for both successful and business-error results."""

    ok: bool
    data: dict[str, Any] | None = None
    error: ToolError | None = None


class EvidenceOutput(_Dto):
    metric: str
    value: int | str
    threshold: int | str
    passed: bool
    score_contribution: int


class IndustryContextOutput(_Dto):
    industry: str
    industry_strength_bps: int | None
    eligible_peer_count: int


class CandidateOutput(_Dto):
    candidate_id: str
    symbol: str
    name: str
    rank: int
    score: int
    setup_type: str
    strategy_version: str
    evidence: list[EvidenceOutput]
    confirmation_condition: str
    invalidation_condition: str
    source: str | None = None
    source_timestamp: datetime | None = None
    market_regime: str | None = None
    industry_context: IndustryContextOutput | None = None


class ReviewNoteOutput(_Dto):
    trade_date: date
    note: str
    occurred_at: datetime | None = None


class CandidateEventOutput(_Dto):
    candidate_id: str
    status: Literal["watched", "bought", "skipped", "exited"]
    event_date: date
    price_1e4: int | None = None
    reason: str


class DailyReviewData(_Dto):
    status: str
    trade_date: date
    source: str | None = None
    source_timestamp: datetime | None = None
    strategy_version: str | None = None
    market_regime: str | None = None
    candidates: list[CandidateOutput] = Field(default_factory=list)
    notes: list[ReviewNoteOutput] = Field(default_factory=list)
    next_at: datetime | None = None
    pipeline_version: str | None = None
    error: str | None = None


class GetDailyReviewResult(_Dto):
    ok: bool
    data: DailyReviewData | None = None
    error: ToolError | None = None


class GetCandidateResult(_Dto):
    ok: bool
    data: CandidateOutput | None = None
    error: ToolError | None = None


class ReviewHistoryData(_Dto):
    reviews: list[DailyReviewData]
    events: list[CandidateEventOutput] = Field(default_factory=list)


class GetReviewHistoryResult(_Dto):
    ok: bool
    data: ReviewHistoryData | None = None
    error: ToolError | None = None


class StrategyVersionOutput(_Dto):
    version: str
    status: str
    parameters: dict[str, int]


class StrategyVersionsData(_Dto):
    versions: list[StrategyVersionOutput]


class ListStrategyVersionsResult(_Dto):
    ok: bool
    data: StrategyVersionsData | None = None
    error: ToolError | None = None


class NextDayData(_Dto):
    candidate_id: str
    symbol: str
    close_1e4: int
    source: str
    as_of: datetime
    status: Literal["confirmed", "invalidated", "pending"]


class CheckNextDayResult(_Dto):
    ok: bool
    data: NextDayData | None = None
    error: ToolError | None = None


class WatchlistNamesData(_Dto):
    names: list[str]


class WatchlistNamesResult(_Dto):
    ok: bool
    data: WatchlistNamesData | None = None
    error: ToolError | None = None


class WatchlistData(_Dto):
    name: str
    symbols: list[str]


class WatchlistResult(_Dto):
    ok: bool
    data: WatchlistData | None = None
    error: ToolError | None = None


class CandidateEventResult(_Dto):
    ok: bool
    data: CandidateEventOutput | None = None
    error: ToolError | None = None


class ReviewNoteResult(_Dto):
    ok: bool
    data: ReviewNoteOutput | None = None
    error: ToolError | None = None


class ReplayCandidateOutput(_Dto):
    candidate_id: str
    symbol: str
    score: int
    evidence: list[EvidenceOutput]


class ReplayReviewOutput(_Dto):
    market_regime: str
    candidates: list[ReplayCandidateOutput]


class ReplayDayOutput(_Dto):
    trade_date: date
    left: ReplayReviewOutput
    right: ReplayReviewOutput


class StrategyComparisonData(_Dto):
    left_version: str
    right_version: str
    start: date
    end: date
    days_compared: int
    left_candidate_count: int
    right_candidate_count: int
    daily: list[ReplayDayOutput]


class StrategyComparisonResult(_Dto):
    ok: bool
    data: StrategyComparisonData | None = None
    error: ToolError | None = None


ReplayStatus = Literal["queued", "running", "completed", "failed"]


class StrategyReplaySummaryOutput(_Dto):
    sessions: int | None = Field(default=None, ge=0)
    reviewed_sessions: int | None = Field(default=None, ge=0)
    total_candidates: int | None = Field(default=None, ge=0)
    zero_candidate_days: int | None = Field(default=None, ge=0)
    max_candidates_per_day: int | None = Field(default=None, ge=0)


class StrategyReplayOutput(_Dto):
    replay_id: str
    version: str
    start_date: date
    end_date: date
    status: ReplayStatus
    certified: bool = False
    source: str | None = None
    parameters_hash: str | None = None
    dataset_hash: str | None = None
    result_hash: str | None = None
    expected_session_count: int | None = Field(default=None, ge=0)
    processed_sessions: int | None = Field(default=None, ge=0)
    next_trade_date: date | None = None
    summary: StrategyReplaySummaryOutput | None = None
    error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class StrategyReplayResult(_Dto):
    ok: bool
    data: StrategyReplayOutput | None = None
    error: ToolError | None = None


class StrategyReplayListData(_Dto):
    replays: list[StrategyReplayOutput]


class StrategyReplayListResult(_Dto):
    ok: bool
    data: StrategyReplayListData | None = None
    error: ToolError | None = None


class StrategyReplayDayOutput(_Dto):
    trade_date: date
    status: Literal["completed"]
    warmup: bool = False
    input_hash: str | None = None
    output_hash: str | None = None
    market_regime: str | None = None
    candidates: list[ReplayCandidateOutput] = Field(default_factory=list)


class StrategyReplayDaysData(_Dto):
    replay_id: str
    days: list[StrategyReplayDayOutput]


class StrategyReplayDaysResult(_Dto):
    ok: bool
    data: StrategyReplayDaysData | None = None
    error: ToolError | None = None


class StrategyVersionResult(_Dto):
    ok: bool
    data: StrategyVersionOutput | None = None
    error: ToolError | None = None


ToolHandler = Callable[..., Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A transport-neutral public tool definition."""

    name: str
    input_model: type[_Dto]
    output_model: type[_Dto]
    handler: ToolHandler
    annotations: Mapping[str, bool]


def _annotations(
    *, read_only: bool, destructive: bool = False, open_world: bool = False
) -> dict[str, bool]:
    return {
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "idempotentHint": True,
        "openWorldHint": open_world,
    }


def _result_mapping(value: Any) -> dict[str, Any]:
    """Normalize application results without turning business errors into exceptions."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        return {
            "ok": False,
            "error": {
                "code": "invalid_service_result",
                "message": "application service returned an invalid result",
            },
        }
    try:
        return ToolResult.model_validate(value).model_dump(mode="json", exclude_none=True)
    except ValidationError:
        return {
            "ok": False,
            "error": {
                "code": "invalid_service_result",
                "message": "application service returned an invalid result",
            },
        }


def _handler(service: Any, method_name: str, input_model: type[_Dto]) -> ToolHandler:
    def dispatch(**arguments: Any) -> Mapping[str, Any]:
        try:
            validated = input_model.model_validate(arguments)
        except ValidationError as error:
            return {
                "ok": False,
                "error": {
                    "code": "invalid_input",
                    "message": error.errors(include_url=False)[0]["msg"],
                },
            }
        try:
            result = getattr(service, method_name)(
                **validated.model_dump(mode="python", exclude_none=True)
            )
        except IdempotencyKeyReuseError:
            # Storage owns durable idempotency.  A key reused for a different
            # request is an expected caller conflict, not a transport crash.
            return {
                "ok": False,
                "error": {
                    "code": "idempotency_key_conflict",
                    "message": "idempotency key was already used for another request",
                },
            }
        return _result_mapping(result)

    return dispatch


def build_tool_catalog(service: Any) -> tuple[ToolDefinition, ...]:
    """Build the fixed public catalog without reading data or fetching quotes."""

    definitions: tuple[tuple[str, type[_Dto], bool, bool, bool], ...] = (
        ("get_daily_review", GetDailyReviewInput, True, False, False),
        ("get_candidate", GetCandidateInput, True, False, False),
        ("check_next_day", CheckNextDayInput, True, False, True),
        ("list_watchlists", ListWatchlistsInput, True, False, False),
        ("get_watchlist", GetWatchlistInput, True, False, False),
        ("create_watchlist", CreateWatchlistInput, False, False, False),
        ("add_watchlist_items", AddWatchlistItemsInput, False, False, False),
        ("remove_watchlist_items", RemoveWatchlistItemsInput, False, True, False),
        ("record_candidate_event", RecordCandidateEventInput, False, False, False),
        ("record_review_note", RecordReviewNoteInput, False, False, False),
        ("get_review_history", GetReviewHistoryInput, True, False, False),
        ("list_strategy_versions", ListStrategyVersionsInput, True, False, False),
        ("compare_strategy_versions", CompareStrategyVersionsInput, True, False, False),
        ("start_strategy_replay", StartStrategyReplayInput, False, False, False),
        ("get_strategy_replay", GetStrategyReplayInput, True, False, False),
        ("list_strategy_replays", ListStrategyReplaysInput, True, False, False),
        ("get_strategy_replay_days", GetStrategyReplayDaysInput, True, False, False),
        ("certify_strategy_replay", CertifyStrategyReplayInput, False, False, False),
        ("create_strategy_proposal", CreateStrategyProposalInput, False, False, False),
        ("activate_strategy_version", ActivateStrategyVersionInput, False, True, False),
    )
    output_models: dict[str, type[_Dto]] = {
        "get_daily_review": GetDailyReviewResult,
        "get_candidate": GetCandidateResult,
        "check_next_day": CheckNextDayResult,
        "list_watchlists": WatchlistNamesResult,
        "get_watchlist": WatchlistResult,
        "create_watchlist": WatchlistResult,
        "add_watchlist_items": WatchlistResult,
        "remove_watchlist_items": WatchlistResult,
        "record_candidate_event": CandidateEventResult,
        "record_review_note": ReviewNoteResult,
        "get_review_history": GetReviewHistoryResult,
        "list_strategy_versions": ListStrategyVersionsResult,
        "compare_strategy_versions": StrategyComparisonResult,
        "start_strategy_replay": StrategyReplayResult,
        "get_strategy_replay": StrategyReplayResult,
        "list_strategy_replays": StrategyReplayListResult,
        "get_strategy_replay_days": StrategyReplayDaysResult,
        "certify_strategy_replay": StrategyReplayResult,
        "create_strategy_proposal": StrategyVersionResult,
        "activate_strategy_version": StrategyVersionResult,
    }
    return tuple(
        ToolDefinition(
            name=name,
            input_model=input_model,
            output_model=output_models[name],
            handler=_handler(service, name, input_model),
            annotations=_annotations(
                read_only=read_only,
                destructive=destructive,
                open_world=open_world,
            ),
        )
        for name, input_model, read_only, destructive, open_world in definitions
    )
