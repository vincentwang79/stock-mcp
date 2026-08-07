"""Stable, SDK-independent definition of the public MCP tool catalog.

The application service owns persistence and business rules.  This module is
deliberately thin: it validates the public boundary then dispatches to the
equally-named service method.  Keeping the catalog separate from the transport
makes it possible to exercise every tool without a network server or MCP SDK.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


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
    event_type: str = Field(min_length=1, max_length=80)
    detail: str = Field(min_length=1, max_length=2_000)


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
        result = getattr(service, method_name)(
            **validated.model_dump(mode="python", exclude_none=True)
        )
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
        ("create_strategy_proposal", CreateStrategyProposalInput, False, False, False),
        ("activate_strategy_version", ActivateStrategyVersionInput, False, True, False),
    )
    return tuple(
        ToolDefinition(
            name=name,
            input_model=input_model,
            output_model=ToolResult,
            handler=_handler(service, name, input_model),
            annotations=_annotations(
                read_only=read_only,
                destructive=destructive,
                open_world=open_world,
            ),
        )
        for name, input_model, read_only, destructive, open_world in definitions
    )
