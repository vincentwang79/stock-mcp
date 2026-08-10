"""Recorded, deterministic industry-classification references for v3 facts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class RecordedIndustryReference:
    """A local industry map and the classification metadata that identifies it."""

    standard: str
    mode: str
    as_of: date | None
    mapping_sha256: str
    industries: Mapping[str, str]


def load_industry_reference(path: Path | str) -> RecordedIndustryReference:
    """Load either the recorded metadata/stocks document or a simple symbol map."""

    document = _load_json(Path(path))
    formal_reference = "stocks" in document
    if formal_reference:
        industries = _stocks_industries(document["stocks"])
        metadata = _metadata(document)
        standard = _text(metadata, "standard", "industry_standard", "classification_standard")
        mode = _text(metadata, "mode", "industry_mode", "classification_mode")
        as_of = _date(
            metadata,
            "as_of",
            "industry_as_of",
            "classification_as_of",
            "industry_retrieved_at",
            "retrieved_at",
        )
    else:
        industries = _symbol_map_industries(document)
        standard = None
        mode = None
        as_of = None
    canonical = json.dumps(industries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    mapping_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return RecordedIndustryReference(
        standard=standard or "recorded-symbol-map-v1",
        mode=mode or (
            "retrospective_current_mapping" if formal_reference else "recorded"
        ),
        as_of=as_of,
        mapping_sha256=mapping_sha256,
        industries=industries,
    )


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"recorded industry JSON does not exist: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError("recorded industry JSON is invalid") from error
    if not isinstance(value, Mapping):
        raise ValueError("recorded industry JSON must be an object")
    return value


def _metadata(document: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = document.get("metadata")
    if nested is None:
        return document
    if not isinstance(nested, Mapping):
        raise ValueError("recorded industry metadata must be an object")
    return {**document, **nested}


def _stocks_industries(value: object) -> dict[str, str]:
    if not isinstance(value, list):
        raise ValueError("recorded industry stocks must be a list")
    industries: dict[str, str] = {}
    for row in value:
        if not isinstance(row, Mapping):
            raise ValueError("recorded industry stock must be an object")
        symbol = _symbol_from_code_exchange(row.get("code"), row.get("exchange"))
        industry = _required_industry(row.get("industry")) or "unavailable"
        _put_industry(industries, symbol, industry)
    return industries


def _symbol_map_industries(value: Mapping[str, Any]) -> dict[str, str]:
    industries: dict[str, str] = {}
    for raw_symbol, raw_industry in value.items():
        if not isinstance(raw_symbol, str):
            raise ValueError("recorded industry symbol must be a string")
        industry = _required_industry(raw_industry)
        if industry is not None:
            _put_industry(industries, _symbol_from_symbol(raw_symbol), industry)
    return industries


def _put_industry(industries: dict[str, str], symbol: str, industry: str) -> None:
    existing = industries.get(symbol)
    if existing is not None and existing != industry:
        raise ValueError(f"recorded industry mapping conflicts for {symbol}")
    industries[symbol] = industry


def _required_industry(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("recorded industry label must be a string")
    return value.strip() or None


def _symbol_from_code_exchange(code: object, exchange: object) -> str:
    if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
        raise ValueError("recorded industry code must be a six-digit string")
    suffix = {"SSE": "SH", "SH": "SH", "SZSE": "SZ", "SZ": "SZ"}.get(
        str(exchange).strip().upper()
    )
    if suffix is None:
        raise ValueError("recorded industry exchange must be SSE or SZSE")
    return f"{code}.{suffix}"


def _symbol_from_symbol(value: str) -> str:
    code, separator, suffix = value.strip().upper().partition(".")
    if separator != "." or len(code) != 6 or not code.isdigit() or suffix not in {"SH", "SZ"}:
        raise ValueError("recorded industry symbol must use the 600000.SH form")
    return f"{code}.{suffix}"


def _text(metadata: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = metadata.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"recorded industry {name} must be a non-empty string")
        return value.strip()
    return None


def _date(metadata: Mapping[str, Any], *names: str) -> date | None:
    value = _text(metadata, *names)
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError as error:
            raise ValueError("recorded industry as-of value must be ISO-8601") from error


def _mapping_sha256(metadata: Mapping[str, Any], document: Mapping[str, Any]) -> str | None:
    value = _text(
        metadata,
        "mapping_sha256",
        "industry_mapping_sha256",
        "classification_mapping_sha256",
        "normalized_mapping_sha256",
    )
    if value is None:
        source_hashes = {
            str(source["normalized_mapping_sha256"]).strip()
            for source in document.get("sources", ())
            if isinstance(source, Mapping) and source.get("normalized_mapping_sha256")
        }
        if len(source_hashes) == 1:
            value = source_hashes.pop()
        elif len(source_hashes) > 1:
            raise ValueError("recorded industry sources disagree on mapping hash")
    if value is None:
        return None
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("recorded industry mapping hash must be lowercase SHA-256")
    return value
