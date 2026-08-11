"""Strict, side-effect-free decoders for recorded Sina payloads."""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from fractions import Fraction
from typing import Any


class SinaDecodeError(ValueError):
    """The provider payload did not match the frozen Sina adapter contract."""


def parse_jsonp_assignment(payload: bytes, *, assignment: str) -> Any:
    try:
        text = payload.decode("utf-8-sig").strip()
    except UnicodeDecodeError as error:
        raise SinaDecodeError("JSONP payload is not UTF-8") from error
    pattern = rf"(?:var\s+)?{re.escape(assignment)}\s*=\s*(.+?)\s*;?\s*\Z"
    match = re.fullmatch(pattern, text, flags=re.DOTALL)
    if match is None:
        raise SinaDecodeError("JSONP assignment or trailing payload is invalid")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise SinaDecodeError("JSONP assignment value is not strict JSON") from error


def decode_spot_json(payload: bytes) -> tuple[dict[str, Any], ...]:
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SinaDecodeError("spot JSON payload is invalid") from error
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise SinaDecodeError("spot JSON payload must be an array of objects")
    return tuple(dict(row) for row in value)


def decode_klc2(payload: bytes) -> tuple[dict[str, Any], ...]:
    """Decode strict JSON recordings or Sina's inert compressed KLC2 stream.

    The compressed branch is a native bit-stream decoder.  It never evaluates
    the assignment, executes JavaScript, downloads a decoder, or imports an
    AKShare implementation detail.
    """

    try:
        text = payload.decode("utf-8-sig").strip()
    except UnicodeDecodeError as error:
        raise SinaDecodeError("KLC payload is not UTF-8") from error
    value: Any
    if text.startswith("["):
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise SinaDecodeError("KLC JSON payload cannot be decoded") from error
    else:
        compressed = re.fullmatch(
            r'(?:var\s+)?[A-Za-z0-9_]+\s*=\s*("(?:[^"\\]|\\.)*")\s*;?\s*'
            r"(?:/\*[A-Za-z0-9+/=\s]*\*/\s*)?\Z",
            text,
            re.DOTALL,
        )
        if compressed is not None:
            try:
                encoded = json.loads(compressed.group(1))
            except json.JSONDecodeError as error:
                raise SinaDecodeError("KLC compressed string is invalid") from error
            return _Klc2Decoder(encoded).decode()
        match = re.fullmatch(r"(?:var\s+)?([A-Za-z0-9_]+)\s*=\s*(.+?)\s*;?\s*\Z", text, re.S)
        if match is None:
            raise SinaDecodeError("KLC payload has trailing or executable content")
        try:
            value = json.loads(match.group(2))
        except json.JSONDecodeError as error:
            raise SinaDecodeError("KLC payload is not a supported inert recording") from error
        if isinstance(value, str):
            return _Klc2Decoder(value).decode()
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise SinaDecodeError("KLC decoded payload must be an array of records")
    return tuple(dict(row) for row in value)


_BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_KLC_EPOCH = date(1970, 1, 1) + timedelta(days=7657)


class _Klc2Decoder:
    """Native decoder for the daily K2 stream emitted by ``klc_kl.js``."""

    def __init__(self, encoded: str) -> None:
        if not encoded or any(character not in _BASE64 for character in encoded):
            raise SinaDecodeError("KLC compressed stream is not canonical base64")
        self.values = tuple(_BASE64.index(character) for character in encoded)
        self.index = 0
        self.offset = 0
        self.state: dict[str, Any] = {}

    def decode(self) -> tuple[dict[str, Any], ...]:
        kind, variant = self.read_many((12, 6))
        if kind != 3466 or (63 ^ variant) != 0:
            raise SinaDecodeError("KLC compressed stream is not daily K2 data")
        self.state = {
            "b_avp": 1,
            "b_ph": 0,
            "b_phx": 0,
            "b_sep": 0,
            "p_p": 6,
            "p_v": 0,
            "p_a": 0,
            "p_e": 0,
            "p_t": 0,
            "l_o": 3,
            "l_h": 3,
            "l_l": 3,
            "l_c": 3,
            "l_v": 5,
            "l_a": 5,
            "l_e": 3,
            "l_t": 0,
            "u_p": 0,
            "u_v": 0,
            "u_a": 0,
            "wd": 62,
            "d": 0,
        }
        rows: list[dict[str, Any]] = []
        while not self.exhausted:
            control: dict[str, Any] = {"d": 1, "c": 0}
            if self.bit():
                if self.bit():
                    if self.bit():
                        self._read_parameter_change(control, rows[-1] if rows else None)
                    if self.bit():
                        self._read_length_change(control)
                    if self.bit():
                        self._read_prevclose(control)
                    if not control["c"]:
                        break
                else:
                    self._read_compact_control(control)
            rows.append(self._read_row(control))
        if not rows:
            raise SinaDecodeError("KLC compressed stream contains no daily rows")
        return tuple(rows)

    @property
    def exhausted(self) -> bool:
        return self.index >= len(self.values)

    def bit(self) -> bool:
        if self.exhausted:
            return False
        value = bool(self.values[self.index] & (1 << self.offset))
        self.offset += 1
        if self.offset >= 6:
            self.offset = 0
            self.index += 1
        return value

    def read_many(
        self,
        widths: tuple[int, ...],
        signed: tuple[bool, ...] | None = None,
        keep_pair: tuple[bool, ...] | None = None,
    ) -> tuple[Any, ...]:
        signs = signed or (False,) * len(widths)
        pairs = keep_pair or (False,) * len(widths)
        return tuple(
            self.read(width, signed=signs[index], keep_pair=pairs[index])
            for index, width in enumerate(widths)
        )

    def read(self, width: int, *, signed: bool = False, keep_pair: bool = False) -> Any:
        if width <= 0:
            return 0
        if self.exhausted:
            raise SinaDecodeError("KLC compressed stream ended inside a field")
        if width > 30:
            low = self.read(30)
            high = self.read(width - 30, signed=signed)
            return (low, high) if keep_pair else low + high * (1 << 30)
        remaining = width
        shift = 0
        result = 0
        while remaining:
            if self.exhausted:
                raise SinaDecodeError("KLC compressed stream ended inside a field")
            take = min(6 - self.offset, remaining)
            result |= ((self.values[self.index] >> self.offset) & ((1 << take) - 1)) << shift
            self.offset += take
            if self.offset >= 6:
                self.offset = 0
                self.index += 1
            shift += take
            remaining -= take
        if signed and result >= 1 << (width - 1):
            result -= 1 << width
        return result

    def delta_length(self) -> int:
        positive = self.bit()
        magnitude = 1
        while self.bit():
            magnitude += 1
        return magnitude if positive else -magnitude

    def _read_parameter_change(
        self, control: dict[str, Any], previous: dict[str, Any] | None
    ) -> None:
        control["c"] += 1
        control["a"] = self.state["b_avp"]
        if self.bit():
            self.state["b_avp"] ^= int(self.bit())
            self.state["b_ph"] ^= int(self.bit())
            self.state["b_phx"] ^= int(self.bit())
            control["s"] = self.state["b_sep"]
            self.state["b_sep"] ^= int(self.bit())
            if self.bit():
                self.state["wd"] = self.read(7)
            if control["s"] != self.state["b_sep"]:
                if control["s"]:
                    self.state["u_p"] = self.state.get("u_c", 0)
                else:
                    base = self.state["u_p"]
                    for field in "ohlc":
                        self.state[f"u_{field}"] = base
        for index in range(3 + 2 * self.state["b_ph"]):
            if not self.bit():
                continue
            field = "pvaet"[index]
            previous_precision = self.state[f"p_{field}"]
            self.state[f"p_{field}"] += self.delta_length()
            self.state[f"u_{field}"] = _scale_number(
                self.state.get(f"u_{field}", 0),
                previous_precision,
                self.state[f"p_{field}"],
            )
            if self.state["b_sep"] and index == 0:
                for price_field in "ohlc":
                    self.state[f"u_{price_field}"] = _scale_number(
                        self.state.get(f"u_{price_field}", 0),
                        previous_precision,
                        self.state["p_p"],
                    )
        if not self.state["b_avp"] and control["a"]:
            self.state["u_a"] = _scale_number(
                0 if previous is None else int(previous.get("amount", 0)),
                0,
                self.state["p_a"],
            )

    def _read_length_change(self, control: dict[str, Any]) -> None:
        control["c"] += 1
        count = 7 + self.state["b_ph"] + self.state["b_phx"]
        for index in range(count):
            if not self.bit():
                continue
            if index == 6:
                control["d"] = self._date_delta()
            else:
                self.state[f"l_{'ohlcva*et'[index]}"] += self.delta_length()

    def _read_prevclose(self, control: dict[str, Any]) -> None:
        control["c"] += 1
        width = self.state["l_o"] + (self.delta_length() if self.bit() else 0)
        delta = self.read(3 * width, signed=True)
        if self.state["b_sep"]:
            control["p"] = self.state.get("u_c", 0) + delta
        else:
            self.state["u_p"] += delta
            control["p"] = self.state["u_p"]

    def _read_compact_control(self, control: dict[str, Any]) -> None:
        if self.bit():
            if self.bit():
                if self.bit():
                    control["d"] = self._date_delta()
                else:
                    self.state["l_v"] += self.delta_length()
            elif self.state["b_ph"] and self.bit():
                field = "et"[int(bool(self.state["b_phx"] and self.bit()))]
                self.state[f"l_{field}"] += self.delta_length()
            else:
                self.state["l_a"] += self.delta_length()
        else:
            self.state[f"l_{'ohlc'[self.read(2)]}"] += self.delta_length()

    def _date_delta(self) -> int:
        value = self.read(3)
        if value == 1:
            self.state["d"] = self.read(18, signed=True)
            return 0
        return self.read(6) if value == 0 else value

    def _next_trading_date(self, increment: int) -> date:
        mask = int(self.state.get("wd", 62))
        for _ in range(increment):
            while True:
                self.state["d"] += 1
                weekday_bit = (self.state["d"] % 7 + 10) % 7
                if mask & (1 << weekday_bit):
                    break
        return _KLC_EPOCH + timedelta(days=self.state["d"])

    def _read_row(self, control: dict[str, Any]) -> dict[str, Any]:
        values: dict[str, int] = {}
        count = 6 + self.state["b_ph"] + self.state["b_phx"]
        for index in range(count):
            field = "ohlcvaet"[index]
            signed = bool(((191 if self.state["b_sep"] else 185) >> index) & 1)
            values[field] = self.read(3 * self.state[f"l_{field}"], signed=signed)
        row: dict[str, Any] = {"date": self._next_trading_date(int(control["d"])).isoformat()}
        if "p" in control:
            row["prevclose"] = _scaled_output(control["p"], self.state["p_p"])
        if self.state["b_sep"]:
            for field in "ohlc":
                self.state[f"u_{field}"] = self.state.get(f"u_{field}", 0) + values[field]
                row[{"o": "open", "h": "high", "l": "low", "c": "close"}[field]] = _scaled_output(
                    self.state[f"u_{field}"], self.state["p_p"]
                )
        else:
            opening = self.state["u_p"] + values["o"]
            row["open"] = _scaled_output(opening, self.state["p_p"])
            row["high"] = _scaled_output(opening + values["h"], self.state["p_p"])
            row["low"] = _scaled_output(opening - values["l"], self.state["p_p"])
            self.state["u_p"] = opening + values["c"]
            row["close"] = _scaled_output(self.state["u_p"], self.state["p_p"])
        self.state["u_v"] += values["v"]
        row["volume"] = _scaled_output(self.state["u_v"], self.state["p_v"])
        if self.state["b_avp"]:
            price_pair = _split_exponent(self.state["p_p"])
            volume_pair = _split_exponent(self.state["p_v"])
            if self.state["b_sep"]:
                average_price = sum(self.state[f"u_{field}"] for field in "ohlc") / 4
            else:
                average_price = opening + (values["h"] - values["l"] + values["c"]) / 4
            raw_amount = int(average_price * self.state["u_v"] + 0.5)
            combined = (price_pair[0] + volume_pair[0], price_pair[1] + volume_pair[1])
            amount_base = _scale_number(raw_amount, combined, self.state["p_a"])
            row["amount"] = _scaled_output(amount_base + values["a"], self.state["p_a"])
        else:
            self.state["u_a"] += values["a"]
            row["amount"] = _scaled_output(self.state["u_a"], self.state["p_a"])
        if self.state["b_ph"]:
            row["postVol"] = _scaled_output(values["e"], self.state["p_e"])
            extra = (
                _scaled_output(values.get("t", 0), self.state["p_t"]) if self.state["b_phx"] else 0
            )
            row["postAmt"] = int(float(row["postVol"]) * float(row["close"]) + float(extra) + 0.5)
        return row


def _split_exponent(value: int) -> tuple[int, int]:
    if value == 0:
        return (0, 0)
    remainder = value % 3
    base = (value - remainder) // 3
    result = [base, base]
    if remainder:
        result[remainder - 1] += 1
    return result[0], result[1]


def _scale_number(value: int, source: int | tuple[int, int], target: int) -> int:
    source_pair = _split_exponent(source) if isinstance(source, int) else source
    target_pair = _split_exponent(target)
    result = Fraction(value, 1)
    for factor, difference in zip(
        (2, 5), (target_pair[0] - source_pair[0], target_pair[1] - source_pair[1]), strict=True
    ):
        result = (
            result * factor**difference if difference >= 0 else result / factor ** (-difference)
        )
    quotient, remainder = divmod(result.numerator, result.denominator)
    doubled = remainder * 2
    if doubled > result.denominator or (doubled == result.denominator and quotient % 2):
        quotient += 1
    return quotient


def _scaled_output(value: int, precision: int) -> int | float:
    pair = _split_exponent(precision)
    result = Fraction(value, 1)
    for factor, exponent in zip((2, 5), pair, strict=True):
        result = result / factor**exponent if exponent >= 0 else result * factor ** (-exponent)
    return result.numerator if result.denominator == 1 else float(result)
