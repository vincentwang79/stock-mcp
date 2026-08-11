"""Injected, bounded HTTP adapter for Sina market facts."""

from __future__ import annotations

import hashlib
import math
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from .sina_decode import decode_klc2, decode_spot_json, parse_jsonp_assignment
from .sina_normalization import normalize_sina_history, normalize_sina_share_capital

ADAPTER_VERSION = "sina-adapter-v1"
SPOT_COUNT_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount"
SPOT_PAGE_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
HISTORY_URL = "https://finance.sina.com.cn/realstock/company/{symbol}/hisdata_klc2/klc_kl.js"
CAPITAL_URL = "https://stock.finance.sina.com.cn/stock/api/jsonp.php/var%20KKE_ShareAmount_{symbol}=/StockService.getAmountBySymbol"


class SinaProviderError(RuntimeError):
    def __init__(self, message: str, *, evidence: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.evidence = None if evidence is None else dict(evidence)


class HttpClient(Protocol):
    def get(self, url: str, *, params: dict[str, object], timeout: tuple[float, float]) -> Any: ...


@dataclass(frozen=True, slots=True)
class FetchEvidence:
    source: str
    endpoint_kind: str
    request_key: str
    http_date: str | None
    retrieved_at: datetime
    http_status: int
    byte_length: int
    payload_sha256: str
    adapter_version: str
    status: str = "success"
    error_class: str | None = None


@dataclass(frozen=True, slots=True)
class FetchResult:
    payload: bytes
    evidence: FetchEvidence


@dataclass(frozen=True, slots=True)
class SpotBatch:
    trade_date: date
    rows: tuple[dict[str, Any], ...]
    expected_count: int
    actual_count: int
    evidence: tuple[FetchEvidence, ...]


class SinaHttpTransport:
    def __init__(
        self,
        *,
        client: HttpClient,
        clock: Callable[[], datetime],
        rate_limiter: Any,
        sleeper: Callable[[float], None],
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 20.0,
        max_retries: int = 2,
        jitter_seconds: Callable[[int], float] | None = None,
    ) -> None:
        if connect_timeout_seconds <= 0 or read_timeout_seconds <= 0 or max_retries < 0:
            raise ValueError("Sina HTTP limits are invalid")
        self._client = client
        self._clock = clock
        self._rate_limiter = rate_limiter
        self._sleeper = sleeper
        self._timeout = (connect_timeout_seconds, read_timeout_seconds)
        self._max_retries = max_retries
        self._jitter = jitter_seconds or (lambda _attempt: 0.0)

    def get(
        self,
        *,
        endpoint_kind: str,
        url: str,
        request_key: str,
        params: Mapping[str, object] | None = None,
    ) -> FetchResult:
        for attempt in range(self._max_retries + 1):
            self._rate_limiter.acquire()
            try:
                response = self._client.get(url, params=dict(params or {}), timeout=self._timeout)
            except (ConnectionError, TimeoutError, urllib.error.URLError) as error:
                if attempt >= self._max_retries:
                    message = f"retry limit reached: {type(error).__name__}"
                    raise SinaProviderError(
                        message,
                        evidence=_failure_evidence(
                            endpoint_kind=endpoint_kind,
                            request_key=request_key,
                            retrieved_at=self._clock(),
                            http_status=None,
                            payload=b"",
                            error_class=type(error).__name__,
                        ),
                    ) from error
                self._sleeper(2.0**attempt + self._jitter(attempt))
                continue
            status = int(getattr(response, "status", getattr(response, "status_code", 0)))
            payload = bytes(getattr(response, "body", getattr(response, "content", b"")))
            headers = getattr(response, "headers", {}) or {}
            if 200 <= status < 300:
                evidence = FetchEvidence(
                    source="sina",
                    endpoint_kind=endpoint_kind,
                    request_key=request_key,
                    http_date=headers.get("Date"),
                    retrieved_at=self._clock(),
                    http_status=status,
                    byte_length=len(payload),
                    payload_sha256=hashlib.sha256(payload).hexdigest(),
                    adapter_version=ADAPTER_VERSION,
                )
                return FetchResult(payload, evidence)
            retryable = status == 429 or 500 <= status <= 599
            if not retryable:
                raise SinaProviderError(
                    f"non-retriable HTTP {status}",
                    evidence=_failure_evidence(
                        endpoint_kind=endpoint_kind,
                        request_key=request_key,
                        retrieved_at=self._clock(),
                        http_status=status,
                        payload=payload,
                        error_class=f"HTTP{status}",
                    ),
                )
            if attempt >= self._max_retries:
                raise SinaProviderError(
                    f"HTTP {status} retry limit reached",
                    evidence=_failure_evidence(
                        endpoint_kind=endpoint_kind,
                        request_key=request_key,
                        retrieved_at=self._clock(),
                        http_status=status,
                        payload=payload,
                        error_class=f"HTTP{status}",
                    ),
                )
            retry_after = _retry_after(headers.get("Retry-After"), now=self._clock())
            delay = retry_after if retry_after is not None else 2.0**attempt + self._jitter(attempt)
            self._sleeper(delay)
        raise AssertionError("bounded Sina request loop must return or raise")


class SinaSpotProvider:
    def __init__(self, *, transport: SinaHttpTransport, clock: Callable[[], datetime]) -> None:
        self._transport = transport
        self._clock = clock

    def fetch_pages(self, trade_date: date) -> SpotBatch:
        count_result = self._transport.get(
            endpoint_kind="spot_count",
            url=SPOT_COUNT_URL,
            request_key=trade_date.isoformat(),
            params={"node": "hs_a"},
        )
        try:
            expected = int(count_result.payload.decode().strip().strip('"'))
        except ValueError as error:
            raise SinaProviderError("spot count payload is invalid") from error
        if expected < 0:
            raise SinaProviderError("spot count cannot be negative")
        rows: list[dict[str, Any]] = []
        evidence = [count_result.evidence]
        page = 1
        maximum_pages = max(1, math.ceil(expected / 80)) + 1
        while len(rows) < expected and page <= maximum_pages:
            result = self._transport.get(
                endpoint_kind="spot_page",
                url=SPOT_PAGE_URL,
                request_key=f"{trade_date.isoformat()}:page={page}:num=80",
                params={"node": "hs_a", "num": 80, "sort": "symbol", "asc": 1, "page": page},
            )
            evidence.append(result.evidence)
            rows.extend(decode_spot_json(result.payload))
            page += 1
        symbols = [str(row.get("symbol", "")) for row in rows]
        if len(rows) != expected:
            raise SinaProviderError("spot pagination count does not match count endpoint")
        if len(symbols) != len(set(symbols)):
            raise SinaProviderError("spot pagination contains duplicate symbols")
        return SpotBatch(trade_date, tuple(rows), expected, len(rows), tuple(evidence))


class FixedIntervalRateLimiter:
    def __init__(
        self,
        requests_per_second: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("rate limit must be positive")
        self._interval = 1.0 / requests_per_second
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._last: float | None = None
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self._monotonic()
            if self._last is not None:
                remaining = self._interval - (now - self._last)
                if remaining > 0:
                    self._sleeper(remaining)
            self._last = self._monotonic()


@dataclass(frozen=True, slots=True)
class _UrlResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class UrllibHttpClient:
    """Small synchronous client honoring environment proxy and explicit timeouts."""

    def __init__(self, *, proxy_url: str | None = None, custom_ca_file: str | None = None) -> None:
        handlers: list[urllib.request.BaseHandler] = []
        if proxy_url:
            handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
        if custom_ca_file:
            handlers.append(
                urllib.request.HTTPSHandler(
                    context=ssl.create_default_context(cafile=custom_ca_file)
                )
            )
        self._opener = urllib.request.build_opener(*handlers)

    def get(
        self, url: str, *, params: dict[str, object], timeout: tuple[float, float]
    ) -> _UrlResponse:
        query = urllib.parse.urlencode(params)
        target = url if not query else f"{url}?{query}"
        request = urllib.request.Request(
            target, headers={"User-Agent": "stock-mcp/sina-adapter-v1"}
        )
        try:
            with self._opener.open(request, timeout=max(timeout)) as response:
                return _UrlResponse(
                    int(response.status), response.read(), dict(response.headers.items())
                )
        except urllib.error.HTTPError as error:
            return _UrlResponse(int(error.code), error.read(), dict(error.headers.items()))


class SinaProvider:
    source = "sina"
    adapter_version = ADAPTER_VERSION

    def __init__(self, *, transport: SinaHttpTransport, clock: Callable[[], datetime]) -> None:
        self._transport = transport
        self._clock = clock

    def fetch_history(
        self, symbol: str, *, start: date, end: date
    ) -> tuple[dict[str, object], ...]:
        rows, _evidence = self.fetch_history_with_evidence(symbol, start=start, end=end)
        return rows

    def fetch_history_with_evidence(
        self, symbol: str, *, start: date, end: date
    ) -> tuple[tuple[dict[str, object], ...], dict[str, object]]:
        wire = _wire_symbol(symbol)
        result = self._transport.get(
            endpoint_kind="history", url=HISTORY_URL.format(symbol=wire), request_key=wire
        )
        try:
            rows = decode_klc2(result.payload)
            rows = _history_rows_for_range(rows, start=start, end=end)
            bars = normalize_sina_history(
                rows, symbol=symbol, source_timestamp=result.evidence.retrieved_at
            )
        except (TypeError, ValueError) as error:
            raise _payload_failure(result.evidence, error) from error
        normalized = tuple(
            {**asdict(bar), "payload_sha256": result.evidence.payload_sha256}
            for bar in bars
        )
        return normalized, _evidence_record(result.evidence)

    def fetch_share_capital(self, symbol: str) -> tuple[dict[str, object], ...]:
        rows, _evidence = self.fetch_share_capital_with_evidence(symbol)
        return rows

    def fetch_share_capital_with_evidence(
        self, symbol: str
    ) -> tuple[tuple[dict[str, object], ...], dict[str, object]]:
        wire = _wire_symbol(symbol)
        result = self._transport.get(
            endpoint_kind="share_capital",
            url=CAPITAL_URL.format(symbol=wire),
            request_key=wire,
            params={"_": 20, "symbol": wire},
        )
        try:
            rows = parse_jsonp_assignment(result.payload, assignment=f"KKE_ShareAmount_{wire}")
            facts = normalize_sina_share_capital(
                rows,
                symbol=symbol,
                source_timestamp=result.evidence.retrieved_at,
                payload_sha256=result.evidence.payload_sha256,
            )
        except (TypeError, ValueError) as error:
            raise _payload_failure(result.evidence, error) from error
        return tuple(asdict(fact) for fact in facts), _evidence_record(result.evidence)


def _wire_symbol(symbol: str) -> str:
    if symbol.endswith(".SH"):
        return "sh" + symbol[:6]
    if symbol.endswith(".SZ"):
        return "sz" + symbol[:6]
    raise ValueError("unsupported Sina symbol")


def _history_rows_for_range(
    rows: tuple[dict[str, Any], ...], *, start: date, end: date
) -> tuple[dict[str, Any], ...]:
    """Crop before normalization and seed the first retained pre-close from the same series."""

    selected: list[dict[str, Any]] = []
    prior_close: object | None = None
    for raw in rows:
        target = date.fromisoformat(str(raw.get("date")))
        close = raw.get("close")
        if target < start:
            prior_close = close
            continue
        if target > end:
            break
        item = dict(raw)
        supplied = item.get("prevclose")
        if supplied in (None, "", 0, 0.0, "0", "0.0"):
            if prior_close in (None, "", 0, 0.0, "0", "0.0"):
                prior_close = close
                continue
            item["prevclose"] = prior_close
        selected.append(item)
        prior_close = close
    return tuple(selected)


def _retry_after(value: object, *, now: datetime | None = None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(str(value))
        except (TypeError, ValueError):
            return None
        current = now or datetime.now(UTC)
        if target.utcoffset() is None:
            target = target.replace(tzinfo=UTC)
        seconds = (target - current.astimezone(UTC)).total_seconds()
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, 60.0)


def _evidence_record(evidence: FetchEvidence) -> dict[str, object]:
    event_hash = hashlib.sha256(
        f"{evidence.request_key}|{evidence.retrieved_at.isoformat()}|"
        f"{evidence.payload_sha256}".encode()
    ).hexdigest()
    fetch_key = f"{evidence.source}:{evidence.endpoint_kind}:{event_hash[:24]}"
    return {
        "fetch_id": fetch_key,
        **asdict(evidence),
    }


def _payload_failure(evidence: FetchEvidence, error: Exception) -> SinaProviderError:
    failed = replace(evidence, status="failed", error_class=type(error).__name__)
    return SinaProviderError(
        f"Sina payload validation failed ({type(error).__name__})",
        evidence=_evidence_record(failed),
    )


def _failure_evidence(
    *,
    endpoint_kind: str,
    request_key: str,
    retrieved_at: datetime,
    http_status: int | None,
    payload: bytes,
    error_class: str,
) -> dict[str, object]:
    payload_hash = hashlib.sha256(payload).hexdigest()
    event_hash = hashlib.sha256(
        f"{request_key}|{retrieved_at.isoformat()}|{error_class}|{payload_hash}".encode()
    ).hexdigest()
    return {
        "fetch_id": f"sina:{endpoint_kind}:failed:{event_hash[:24]}",
        "source": "sina",
        "endpoint_kind": endpoint_kind,
        "request_key": request_key,
        "http_date": None,
        "retrieved_at": retrieved_at,
        "http_status": http_status,
        "byte_length": len(payload),
        "payload_sha256": payload_hash,
        "adapter_version": ADAPTER_VERSION,
        "status": "failed",
        "error_class": error_class,
    }
