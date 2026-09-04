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

_PARAMETER_DOCS: dict[str, tuple[str, list[Any]]] = {
    "after_signal_date": (
        "Return signal days strictly after this date; omit it for the first page.",
        ["2026-01-30"],
    ),
    "after_trade_date": (
        "Return replay days strictly after this trading date; omit it for the first page.",
        ["2026-01-30"],
    ),
    "arm_id": (
        "Exact research arm identifier returned by the study arm list.",
        ["v4-no-recent-limit-up"],
    ),
    "candidate_id": (
        "Exact candidate identifier returned by a daily review or earlier candidate result; "
        "do not invent it.",
        ["2026-08-27:v0.3-policy-1:600000.SH"],
    ),
    "capabilities": (
        "The complete approved Sina capability set; both enrichment and backup_price are required.",
        [["enrichment", "backup_price"]],
    ),
    "confirmed": (
        "True only after the user explicitly confirms this write action in the conversation.",
        [True],
    ),
    "description": (
        "Optional plain-language purpose of the personal observation list.",
        ["等待盘后确认的股票"],
    ),
    "end": ("Inclusive final trading date of the read-only comparison.", ["2026-08-07"]),
    "end_date": ("Inclusive final trading date of the governance replay.", ["2026-08-07"]),
    "event_date": ("Calendar date on which the user made the recorded decision.", ["2026-08-28"]),
    "family": (
        "Optional exact research-family filter; omit it to include every registered family.",
        ["entry-quality"],
    ),
    "horizon_sessions": (
        "Forward outcome horizon in trading sessions; only 5, 10, or 20 is allowed.",
        [20],
    ),
    "hypothesis_id": (
        "Exact immutable hypothesis identifier returned by the research hypothesis list.",
        ["overnight-intraday-separation-v1"],
    ),
    "idempotency_key": (
        "Caller-generated stable key for this exact write request; reuse it only when "
        "retrying the same request.",
        ["family-assistant-20260828-001"],
    ),
    "left_version": (
        "Existing strategy version used as the left side of a read-only comparison.",
        ["v0.3-policy-1"],
    ),
    "limit": ("Maximum number of records to return in this page, within the schema limits.", [20]),
    "manifest_hash": (
        "Exact 64-character SHA-256 hash of the frozen v4 research manifest.",
        ["bae4147c631b78be7012b81f1ec6993c63a3f0f835302ba4d2b1859ba4a4ce1a"],
    ),
    "name": ("Exact user-visible name of the personal observation list.", ["等待确认"]),
    "note": (
        "Personal review note to store verbatim after the user confirms it.",
        ["没有追高，等待确认条件"],
    ),
    "parameters": (
        "Complete immutable strategy parameter object approved by the user; never infer "
        "omitted values.",
        [{"rule_engine_version": 3}],
    ),
    "price_1e4": (
        "Optional observed price in 1e-4 yuan units; for example 123400 represents ¥12.3400.",
        [123400],
    ),
    "qualification_id": (
        "Exact provider qualification identifier from the qualification report.",
        ["sina-qualification-20260828"],
    ),
    "rationale": (
        "User-approved plain-language reason for creating this immutable strategy proposal.",
        ["独立样本验证后再进入治理回放"],
    ),
    "reason": (
        "User-provided reason for the recorded candidate decision; record it without "
        "adding an inferred motive.",
        ["确认条件未满足，所以跳过"],
    ),
    "replay_id": (
        "Exact replay identifier returned when the governance replay was started.",
        ["replay-be37e9065a5c485e9a3c39dfa4537aab"],
    ),
    "right_version": (
        "Existing strategy version used as the right side of a read-only comparison; "
        "it must differ from left_version.",
        ["v0.4-proposed"],
    ),
    "source": (
        "Supported provider source constrained by this tool; currently only sina.",
        ["sina"],
    ),
    "start": ("Inclusive first trading date of the read-only comparison.", ["2023-08-08"]),
    "start_date": ("Inclusive first trading date of the governance replay.", ["2023-08-08"]),
    "status": (
        "Optional status value or constrained candidate event state accepted by this tool.",
        ["watched"],
    ),
    "study_id": (
        "Exact v4 study identifier returned when the research job was started.",
        ["v4-study-555cdc72b3c14f549f91f04f28d9e0cf"],
    ),
    "supersedes_version": (
        "Optional existing strategy version this proposal is intended to supersede after "
        "governance activation.",
        ["v0.2-proposed"],
    ),
    "symbols": (
        "One or more explicit沪深主板 symbols in exchange-qualified form; preserve the "
        "user's requested order.",
        [["600000.SH", "000001.SZ"]],
    ),
    "trade_date": (
        "Trading date in YYYY-MM-DD form; do not guess when the user did not provide one.",
        ["2026-08-27"],
    ),
    "version": (
        "Exact immutable strategy version identifier, usually obtained from the strategy "
        "version list.",
        ["v0.3-policy-1"],
    ),
}

_PARAMETER_DOC_OVERRIDES: dict[tuple[str, str], tuple[str, list[Any]]] = {
    ("ListResearchHypothesesInput", "status"): (
        "Optional exact research lifecycle-status filter; omit it to include all statuses.",
        ["forward_observation"],
    ),
    ("RecordCandidateEventInput", "status"): (
        "User-confirmed decision record: watched, bought, skipped, or exited; this never "
        "executes a trade.",
        ["watched"],
    ),
}


class _Dto(BaseModel):
    """Public input/output DTO base: unknown fields are never silently kept."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> dict[str, Any]:
        schema = handler(core_schema)
        for name, property_schema in schema.get("properties", {}).items():
            docs = _PARAMETER_DOC_OVERRIDES.get((cls.__name__, name), _PARAMETER_DOCS.get(name))
            if docs is not None:
                property_schema.setdefault("description", docs[0])
                property_schema.setdefault("examples", docs[1])
        return schema

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


class GetLatestDailyReviewInput(_Dto):
    pass


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
    supersedes_version: str | None = Field(default=None, min_length=1, max_length=80)


class ActivateStrategyVersionInput(_IdempotentWrite):
    version: str = Field(min_length=1, max_length=80)
    confirmed: bool


class StartV4ResearchInput(_IdempotentWrite):
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class GetV4ResearchInput(_Dto):
    study_id: str = Field(min_length=1, max_length=100)


class GetV4ResearchArmsInput(GetV4ResearchInput):
    pass


class GetV4ResearchDaysInput(GetV4ResearchInput):
    arm_id: str = Field(min_length=1, max_length=100)
    after_signal_date: date | None = None
    limit: int = Field(default=20, ge=1, le=50)


class GetV4ResearchReportInput(GetV4ResearchInput):
    pass


class GetV4ResearchDiagnosticsInput(GetV4ResearchInput):
    pass


class GetProviderQualificationInput(_Dto):
    source: Literal["sina"]


class ActivateProviderSourceInput(_IdempotentWrite):
    source: Literal["sina"]
    qualification_id: str = Field(min_length=1, max_length=200)
    capabilities: list[Literal["enrichment", "backup_price"]] = Field(min_length=2, max_length=2)
    confirmed: bool


class ListResearchHypothesesInput(_Dto):
    family: str | None = Field(default=None, min_length=1, max_length=100)
    status: str | None = Field(default=None, min_length=1, max_length=100)


class GetResearchHypothesisInput(_Dto):
    hypothesis_id: str = Field(min_length=1, max_length=160)


class GetResearchForwardReportInput(GetResearchHypothesisInput):
    horizon_sessions: Literal[5, 10, 20] = 20


class ToolError(_Dto):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1_000)


class ToolResult(_Dto):
    """The common envelope for both successful and business-error results."""

    ok: bool
    data: dict[str, Any] | None = None
    error: ToolError | None = None


class V4ResearchOutput(_Dto):
    study_id: str | None = None
    replay_id: str | None = None
    status: str
    manifest_hash: str | None = None
    outcome_hash_schema: str | None = "v4-outcome-v2"
    outcome_through: date | None = None
    bootstrap_method: str | None = None
    multiple_testing_method: str | None = None
    winner: dict[str, Any] | None = None
    completeness_status: str | None = None
    certified: bool = False
    active: bool = False


class V4ResearchResult(_Dto):
    ok: bool
    data: V4ResearchOutput | dict[str, Any] | None = None
    error: ToolError | None = None


class ProviderQualificationResult(_Dto):
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
    publication_class: str | None = None
    reconciled_at: datetime | None = None
    original_schedule_status: str | None = None
    publication_hash: str | None = None


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
    lifecycle: str | None = None
    superseded_by: str | None = None


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


class IndustryEvidenceOutput(_Dto):
    standard: str | None = None
    bucket: str | None = None
    industry: str | None = None
    industry_strength_bps: int | str | None = None
    classification_mode: str | None = None
    classification_as_of: date | None = None
    classification_mapping_sha256: str | None = None


class CandidateOutcomeOutput(_Dto):
    availability: Literal["complete", "partial", "unavailable"]
    path_status: Literal["confirmed", "invalidated", "pending", "unavailable"]
    return_5d_bps: int | None = None
    return_10d_bps: int | None = None
    return_20d_bps: int | None = None
    benchmark_return_5d_bps: int | None = None
    benchmark_return_10d_bps: int | None = None
    benchmark_return_20d_bps: int | None = None
    excess_return_5d_bps: int | None = None
    excess_return_10d_bps: int | None = None
    excess_return_20d_bps: int | None = None
    mfe_20d_bps: int | None = None
    mae_20d_bps: int | None = None
    first_confirmation_date: date | None = None
    first_invalidation_date: date | None = None


class CandidateOutcomeRecordOutput(CandidateOutcomeOutput):
    candidate_id: str


class StrategyReplayOutcomeOutput(_Dto):
    candidates: list[CandidateOutcomeRecordOutput]


class ReplayCandidateOutput(_Dto):
    candidate_id: str
    symbol: str
    score: int
    evidence: list[EvidenceOutput]
    setup_type: str | None = None
    confirmation_condition: str | None = None
    invalidation_condition: str | None = None
    industry_evidence: IndustryEvidenceOutput | None = None
    outcome: CandidateOutcomeOutput | None = None


class ReplayReviewOutput(_Dto):
    warmup: bool = False
    market_regime: str | None = None
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
    result_hash_schema: str | None = None


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
    pipeline_version: str | None = None
    input_hash: str | None = None
    input_hash_schema: str | None = None
    result_hash_schema: str | None = None
    outcome_hash_schema: str | None = None
    warmup_sessions: int | None = Field(default=None, ge=0)
    outcome_status: ReplayStatus | None = None
    outcome: StrategyReplayOutcomeOutput | None = None
    outcome_hash: str | None = None
    industry_classification_standard: str | None = None
    industry_classification_mode: str | None = None
    industry_classification_as_of: date | None = None
    industry_mapping_sha256: str | None = None


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
    pipeline_version: str | None = None
    input_hash_schema: str | None = None
    result_hash_schema: str | None = None
    industry_classification_standard: str | None = None
    industry_classification_mode: str | None = None
    industry_classification_as_of: date | None = None
    industry_mapping_sha256: str | None = None


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
    title: str
    description: str
    input_model: type[_Dto]
    output_model: type[_Dto]
    handler: ToolHandler
    annotations: Mapping[str, bool]


_TOOL_METADATA: dict[str, tuple[str, str]] = {
    "get_latest_daily_review": (
        "查看最近盘后结果",
        "Use this when the user asks in natural language about today, the latest close, "
        "the most recent market review, or whether there are stocks worth watching without "
        "naming a date. It returns the newest recorded trading-day status, including observation, "
        "degraded, or failed states. Do not use for intraday quotes, arbitrary stock picking, or "
        "to hide a missing formal review.",
    ),
    "get_daily_review": (
        "查看指定日期盘后结果",
        "Use this when the user asks about the market review, formal candidates, or system status "
        "for a specific trading date, for example ‘8月27日有什么值得关注’. Do not use when no date "
        "was supplied (use the latest-review tool), for intraday prices, or to invent candidates.",
    ),
    "get_candidate": (
        "解释候选股票",
        "Use this when the user asks why a previously returned candidate was selected, what its "
        "evidence means, or what confirmation and invalidation conditions apply. Do not use for "
        "arbitrary symbol research, price prediction, reranking, or creating a new candidate.",
    ),
    "check_next_day": (
        "检查候选确认状态",
        "Use this when the user explicitly asks whether a known candidate is currently confirmed, "
        "invalidated, or still pending; this performs one external quote check. Do not use for "
        "continuous monitoring, alerts, automatic trading, or candidates not returned by "
        "Stock MCP.",
    ),
    "list_watchlists": (
        "列出观察列表",
        "Use this when the user asks which personal stock observation lists they have. Do not use "
        "for formal strategy candidates, account positions, broker holdings, or market screening.",
    ),
    "get_watchlist": (
        "查看观察列表",
        "Use this when the user asks to see the stocks saved in a named personal observation list. "
        "Do not use for broker positions, formal candidate rankings, or an unnamed list.",
    ),
    "create_watchlist": (
        "创建观察列表",
        "Use this when the user explicitly asks to create a named personal observation "
        "list and has confirmed what will be stored. Do not use to create portfolios, "
        "positions, orders, or lists "
        "the user did not request.",
    ),
    "add_watchlist_items": (
        "加入观察股票",
        "Use this when the user explicitly asks to save one or more沪深主板 symbols to an "
        "existing observation list and has confirmed the write. Do not use to place "
        "trades, change rankings, "
        "or add symbols merely mentioned in conversation.",
    ),
    "remove_watchlist_items": (
        "移出观察股票",
        "Use this when the user explicitly asks to remove specified symbols from a named "
        "observation list and has confirmed the removal. Do not use to sell securities, "
        "delete a whole list, or "
        "infer removal from negative commentary.",
    ),
    "record_candidate_event": (
        "记录候选决策",
        "Use this when the user explicitly asks to record that they watched, bought, skipped, or "
        "exited a known candidate, together with a reason, and confirms the record. Do not use to "
        "execute an order, store position size, infer a decision, or give transaction advice.",
    ),
    "record_review_note": (
        "记录盘后笔记",
        "Use this when the user explicitly asks to attach a personal note to a specific published "
        "daily review and confirms the text. Do not use to alter the review, candidates, "
        "scores, or "
        "strategy evidence.",
    ),
    "get_review_history": (
        "回顾历史记录",
        "Use this when the user asks to review past published daily reports, personal "
        "notes, or the "
        "recorded history of a known candidate. Do not use for live quotes, new screening, or to "
        "treat historical observations as future predictions.",
    ),
    "list_strategy_versions": (
        "查看策略版本",
        "Use this when the user asks which immutable strategy versions exist or which "
        "lifecycle state "
        "each version has. Do not use to create, certify, approve, or activate a strategy.",
    ),
    "compare_strategy_versions": (
        "比较策略版本",
        "Use this when the user explicitly asks for a read-only comparison of two "
        "different existing "
        "strategy versions over a date range with completed evidence. Do not use for same-version "
        "comparison, synchronous recomputation, automatic optimization, or activation.",
    ),
    "start_strategy_replay": (
        "启动策略治理回放",
        "Use this when the user explicitly requests a governance replay for an existing proposed "
        "strategy version and confirms the exact approved date range. Do not use during "
        "ordinary stock "
        "analysis, automatically after proposal creation, or as certification or activation.",
    ),
    "get_strategy_replay": (
        "查看策略回放状态",
        "Use this when the user asks for the status, progress, hashes, summary, or failure "
        "of a known "
        "strategy replay. Do not use to start, rerun, certify, or activate the replay.",
    ),
    "list_strategy_replays": (
        "列出策略回放",
        "Use this when the user asks which governance replay jobs exist, optionally for "
        "one strategy "
        "version. Do not use to start jobs, poll continuously, certify, or activate strategies.",
    ),
    "get_strategy_replay_days": (
        "审阅逐日回放",
        "Use this when the user asks to page through the persisted daily evidence of a "
        "known replay, "
        "including warmup, candidates, hashes, and outcomes. Do not use to recompute days, change "
        "results, or perform continuous polling.",
    ),
    "certify_strategy_replay": (
        "认证策略回放",
        "Use this when the user explicitly asks to create the permanent governance certificate "
        "for a completed replay, confirms the action, and has reviewed its evidence. Do not use as "
        "host approval, strategy activation, routine analysis, or an inferred next step.",
    ),
    "create_strategy_proposal": (
        "创建策略提案",
        "Use this when the user explicitly asks to persist a fully specified immutable "
        "proposed strategy version, rationale, and governance relation after confirming "
        "the complete payload. Do not use for casual what-if questions, "
        "automatic parameter tuning, activation, or silently inferred parameters.",
    ),
    "activate_strategy_version": (
        "激活策略版本",
        "Use this when the user explicitly requests activation, confirms the destructive change, "
        "and the server can consume a matching host approval and permanent replay "
        "certificate. Do not use for ordinary analysis, proposal creation, certification, "
        "or without explicit confirmation.",
    ),
    "start_v4_research": (
        "启动 v4 研究",
        "Use this when the user explicitly asks to start the frozen seven-arm v4 research "
        "for a known immutable manifest hash and confirms that exact manifest. Do not use "
        "for daily analysis, online data collection, strategy "
        "activation, or automatic repeated experimentation.",
    ),
    "get_v4_research": (
        "查看 v4 研究状态",
        "Use this when the user asks for the current state or failure of a known v4 "
        "research study. "
        "Do not use to start, rerun, certify, activate, or continuously poll a study.",
    ),
    "get_v4_research_arms": (
        "查看 v4 研究臂",
        "Use this when the user asks which baseline and challenger arms belong to a known v4 study "
        "and their persisted status. Do not use to add arms, combine factors, rerun "
        "research, or select "
        "a winner outside the stored statistical gate.",
    ),
    "get_v4_research_days": (
        "审阅 v4 逐日结果",
        "Use this when the user asks to page through persisted signal-day evidence for one "
        "arm of a known v4 study. Do not use to recompute outcomes, mutate results, or "
        "query an unspecified arm.",
    ),
    "get_v4_research_report": (
        "解读 v4 研究报告",
        "Use this when the user asks for the persisted statistical report, completeness "
        "gates, winner decision, or proposal artifacts of a known v4 study. Do not use to "
        "invent missing statistics, "
        "override eligibility, or activate a strategy.",
    ),
    "get_v4_research_diagnostics": (
        "查看 v4 研究诊断",
        "Use this when the user asks why challengers did not advance, how the baseline behaved, or "
        "which statistical and replication gates failed for a known v4 study. Do not use to change "
        "the study, claim significance, or create a proposal.",
    ),
    "get_provider_qualification": (
        "查看数据源资格",
        "Use this when the user asks whether the Sina provider is collecting, qualified, failed, "
        "expired, or approved for specific capabilities. Do not use to activate the "
        "provider, fetch "
        "market data, or imply qualification from service reachability alone.",
    ),
    "activate_provider_source": (
        "激活备用数据源能力",
        "Use this when the user explicitly asks to activate both approved Sina enrichment and "
        "backup-price capabilities, confirms the destructive change, and a matching host approval "
        "exists. Do not use for routine analysis, qualification review, or partial capabilities.",
    ),
    "list_research_hypotheses": (
        "列出研究假设",
        "Use this when the user asks what frozen research hypotheses are registered, optionally by "
        "family or status, and how many trials exist. Do not use to create hypotheses, "
        "start research, "
        "change strategy parameters, or promote a result.",
    ),
    "get_research_hypothesis": (
        "查看研究假设证据",
        "Use this when the user asks for the definition, trials, forward observations, and "
        "outcomes of "
        "a known frozen research hypothesis. Do not use to modify the hypothesis, add evidence, or "
        "claim promotion eligibility.",
    ),
    "get_research_forward_report": (
        "查看前向观察报告",
        "Use this when the user asks for the descriptive forward-evidence report of a "
        "known research hypothesis at a 5, 10, or 20-session horizon. Do not use to "
        "create observations, promote a "
        "strategy, promise returns, or treat descriptive evidence as trading advice.",
    ),
}


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
        ("get_latest_daily_review", GetLatestDailyReviewInput, True, False, False),
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
        ("start_v4_research", StartV4ResearchInput, False, False, False),
        ("get_v4_research", GetV4ResearchInput, True, False, False),
        ("get_v4_research_arms", GetV4ResearchArmsInput, True, False, False),
        ("get_v4_research_days", GetV4ResearchDaysInput, True, False, False),
        ("get_v4_research_report", GetV4ResearchReportInput, True, False, False),
        (
            "get_v4_research_diagnostics",
            GetV4ResearchDiagnosticsInput,
            True,
            False,
            False,
        ),
        ("get_provider_qualification", GetProviderQualificationInput, True, False, False),
        ("activate_provider_source", ActivateProviderSourceInput, False, True, False),
        ("list_research_hypotheses", ListResearchHypothesesInput, True, False, False),
        ("get_research_hypothesis", GetResearchHypothesisInput, True, False, False),
        ("get_research_forward_report", GetResearchForwardReportInput, True, False, False),
    )
    output_models: dict[str, type[_Dto]] = {
        "get_latest_daily_review": GetDailyReviewResult,
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
        "start_v4_research": V4ResearchResult,
        "get_v4_research": V4ResearchResult,
        "get_v4_research_arms": V4ResearchResult,
        "get_v4_research_days": V4ResearchResult,
        "get_v4_research_report": V4ResearchResult,
        "get_v4_research_diagnostics": V4ResearchResult,
        "get_provider_qualification": ProviderQualificationResult,
        "activate_provider_source": ProviderQualificationResult,
        "list_research_hypotheses": V4ResearchResult,
        "get_research_hypothesis": V4ResearchResult,
        "get_research_forward_report": V4ResearchResult,
    }
    return tuple(
        ToolDefinition(
            name=name,
            title=_TOOL_METADATA[name][0],
            description=_TOOL_METADATA[name][1],
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
