"""Deterministic units and point-in-time normalization for Sina facts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from ..domain import DailyBar, ShareCapitalFact

SINA_SOURCE = "sina"


class SinaNormalizationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SinaSpotRecord:
    symbol: str
    name: str
    trade_date: date
    open_1e4: int
    high_1e4: int
    low_1e4: int
    close_1e4: int
    pre_close_1e4: int
    volume_shares: int
    amount_fen: int
    ticktime: str
    upstream_market_cap_fen: int | None
    upstream_circulating_market_cap_fen: int | None
    upstream_turnover_rate: Decimal | None
    source_timestamp: datetime


@dataclass(frozen=True, slots=True)
class SinaShareMetrics:
    market_cap_fen: int
    turnover_rate: Decimal


def normalize_sina_history(
    rows: Sequence[Mapping[str, object]], *, symbol: str, source_timestamp: datetime
) -> tuple[DailyBar, ...]:
    _symbol(symbol)
    _aware(source_timestamp)
    normalized: list[DailyBar] = []
    prior_close: int | None = None
    seen: set[date] = set()
    for raw in rows:
        target = _date(raw.get("date"), "date")
        if target in seen:
            raise SinaNormalizationError("duplicate history date")
        if normalized and target <= normalized[-1].trade_date:
            raise SinaNormalizationError("history dates must be strictly increasing")
        seen.add(target)
        close = _price(raw.get("close"), "close")
        supplied = _optional_price(raw.get("prevclose"), "prevclose")
        if supplied is None:
            if prior_close is None:
                raise SinaNormalizationError("first history row requires prevclose")
            supplied = prior_close
        bar = DailyBar(
            symbol=symbol,
            trade_date=target,
            open_1e4=_price(raw.get("open"), "open"),
            high_1e4=_price(raw.get("high"), "high"),
            low_1e4=_price(raw.get("low"), "low"),
            close_1e4=close,
            pre_close_1e4=supplied,
            volume_shares=_nonnegative_int(raw.get("volume"), "volume"),
            amount_fen=_decimal_int(raw.get("amount"), Decimal(100), "amount"),
            source=SINA_SOURCE,
            source_timestamp=source_timestamp,
        )
        if not (bar.low_1e4 <= min(bar.open_1e4, bar.close_1e4) <= bar.high_1e4):
            raise SinaNormalizationError("history OHLC is invalid")
        normalized.append(bar)
        prior_close = close
    return tuple(normalized)


def normalize_sina_share_capital(
    rows: Sequence[Sequence[object] | Mapping[str, object]] | None,
    *,
    symbol: str,
    source_timestamp: datetime,
    payload_sha256: str | None = None,
    required_from: date | None = None,
) -> tuple[ShareCapitalFact, ...]:
    _symbol(symbol)
    _aware(source_timestamp)
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise SinaNormalizationError("share-capital array is unavailable")
    digest = payload_sha256 or _canonical_hash(rows)
    parsed: list[tuple[date, int]] = []
    counts: dict[date, int] = {}
    for row in rows:
        if isinstance(row, Mapping):
            effective_value = row.get("date")
            amount_value = row.get("amount")
        else:
            if len(row) < 2:
                raise SinaNormalizationError("share-capital row is incomplete")
            effective_value = row[0]
            amount_value = row[1]
        if effective_value in (None, "") or amount_value in (None, ""):
            raise SinaNormalizationError("share-capital row is incomplete")
        effective = _date(effective_value, "effective_date")
        shares = _decimal_int(amount_value, Decimal(10_000), "outstanding_share")
        parsed.append((effective, shares))
        counts[effective] = counts.get(effective, 0) + 1

    anomalous_dates = {
        effective for effective, shares in parsed if shares <= 0 or counts[effective] > 1
    }
    if required_from is None:
        if anomalous_dates:
            if any(shares <= 0 for _effective, shares in parsed):
                raise SinaNormalizationError("outstanding shares must be positive")
            raise SinaNormalizationError("share-capital dates must be unique and increasing")
        retained = parsed
    else:
        if any(effective >= required_from for effective in anomalous_dates):
            raise SinaNormalizationError(
                "share-capital window contains a nonpositive or duplicate fact"
            )
        cutoff = max(anomalous_dates) if anomalous_dates else None
        retained = [
            (effective, shares)
            for effective, shares in parsed
            if shares > 0 and (cutoff is None or effective > cutoff)
        ]
        if not any(effective <= required_from for effective, _shares in retained):
            raise SinaNormalizationError("share-capital window has no positive opening fact")

    facts: list[ShareCapitalFact] = []
    seen: set[date] = set()
    for effective, shares in retained:
        if effective in seen or (facts and effective <= facts[-1].effective_date):
            raise SinaNormalizationError("share-capital dates must be unique and increasing")
        seen.add(effective)
        facts.append(
            ShareCapitalFact(symbol, effective, SINA_SOURCE, shares, source_timestamp, digest)
        )
    return tuple(facts)


def outstanding_shares_on(facts: Iterable[ShareCapitalFact], *, trade_date: date) -> int | None:
    eligible = [fact for fact in facts if fact.effective_date <= trade_date]
    if not eligible:
        return None
    return max(eligible, key=lambda fact: fact.effective_date).outstanding_shares


def normalize_sina_spot(
    rows: Sequence[Mapping[str, object]], *, trade_date: date, source_timestamp: datetime
) -> tuple[SinaSpotRecord, ...]:
    _aware(source_timestamp)
    result: list[SinaSpotRecord] = []
    seen: set[str] = set()
    for raw in rows:
        symbol = _sina_symbol(raw.get("symbol"))
        if symbol in seen:
            raise SinaNormalizationError("duplicate spot symbol")
        seen.add(symbol)
        record = SinaSpotRecord(
            symbol=symbol,
            name=str(raw.get("name") or "").strip(),
            trade_date=trade_date,
            open_1e4=_price(raw.get("open"), "open"),
            high_1e4=_price(raw.get("high"), "high"),
            low_1e4=_price(raw.get("low"), "low"),
            close_1e4=_price(raw.get("trade"), "trade"),
            pre_close_1e4=_price(raw.get("settlement"), "settlement"),
            volume_shares=_nonnegative_int(raw.get("volume"), "volume"),
            amount_fen=_decimal_int(raw.get("amount"), Decimal(100), "amount"),
            ticktime=str(raw.get("ticktime") or ""),
            upstream_market_cap_fen=_optional_scaled(raw.get("mktcap"), Decimal(1_000_000)),
            upstream_circulating_market_cap_fen=_optional_scaled(
                raw.get("nmc"), Decimal(1_000_000)
            ),
            upstream_turnover_rate=_optional_decimal(raw.get("turnoverratio")),
            source_timestamp=source_timestamp,
        )
        if not (record.low_1e4 <= min(record.open_1e4, record.close_1e4) <= record.high_1e4):
            raise SinaNormalizationError("spot OHLC is invalid")
        result.append(record)
    return tuple(result)


def derive_sina_share_metrics(
    *, close_1e4: int, volume_shares: int, outstanding_shares: int
) -> SinaShareMetrics:
    if close_1e4 <= 0 or volume_shares < 0 or outstanding_shares <= 0:
        raise SinaNormalizationError("share metrics require valid positive facts")
    cap = (Decimal(close_1e4) * Decimal(outstanding_shares) / Decimal(100)).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )
    return SinaShareMetrics(int(cap), Decimal(volume_shares) / Decimal(outstanding_shares))


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as error:
        raise SinaNormalizationError(f"{field} is not numeric") from error
    if not result.is_finite():
        raise SinaNormalizationError(f"{field} must be finite")
    return result


def _decimal_int(value: object, multiplier: Decimal, field: str) -> int:
    result = (_decimal(value, field) * multiplier).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    if result < 0:
        raise SinaNormalizationError(f"{field} cannot be negative")
    return int(result)


def _price(value: object, field: str) -> int:
    result = _decimal_int(value, Decimal(10_000), field)
    if result <= 0:
        raise SinaNormalizationError(f"{field} must be positive")
    return result


def _optional_price(value: object, field: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return _price(value, field)


def _nonnegative_int(value: object, field: str) -> int:
    return _decimal_int(value, Decimal(1), field)


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    return _decimal(value, "provider metric")


def _optional_scaled(value: object, multiplier: Decimal) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return _decimal_int(value, multiplier, "provider market cap")


def _date(value: object, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise SinaNormalizationError(f"{field} must be an ISO date") from error


def _symbol(value: str) -> str:
    if not (value.endswith(".SH") or value.endswith(".SZ")) or len(value) != 9:
        raise SinaNormalizationError("symbol is not a supported main-board code")
    return value


def _sina_symbol(value: object) -> str:
    raw = str(value).lower()
    if re_match := __import__("re").fullmatch(r"(sh|sz)(\d{6})", raw):
        return f"{re_match.group(2)}.{re_match.group(1).upper()}"
    raise SinaNormalizationError("Sina symbol is invalid")


def _aware(value: datetime) -> None:
    if value.utcoffset() is None:
        raise SinaNormalizationError("source timestamp must be timezone-aware")
