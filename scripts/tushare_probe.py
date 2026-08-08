"""Make one safe, standalone Tushare ``daily`` request for diagnostics.

Set ``TUSHARE_TOKEN`` in the process environment or pass the protected host
``secrets.env`` path.  The script never accepts a token as an argument and
never prints its value.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from argparse import ArgumentParser
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _write(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _read_secrets_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"invalid secrets file line {line_number}")
        values[key.strip()] = value.strip()
    return values


def _daily_row_count(*, endpoint: str, token: str, trade_date: str) -> int:
    payload = json.dumps(
        {
            "api_name": "daily",
            "token": token,
            "params": {"trade_date": trade_date},
            "fields": "ts_code",
        }
    ).encode("utf-8")
    request = Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed Tushare endpoint by default
        result = json.loads(response.read().decode("utf-8"))
    if result.get("code") != 0:
        raise RuntimeError(str(result.get("msg") or "Tushare returned an unknown API error"))
    items = result.get("data", {}).get("items", [])
    if not isinstance(items, list):
        raise RuntimeError("Tushare response data.items is not a list")
    return len(items)


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--secrets-file", type=Path)
    args = parser.parse_args(argv)
    try:
        secret_values = _read_secrets_file(args.secrets_file) if args.secrets_file else {}
    except (OSError, UnicodeError, ValueError) as error:
        _write({"status": "configuration_error", "error": str(error)})
        return 2
    token = secret_values.get("TUSHARE_TOKEN", os.environ.get("TUSHARE_TOKEN", "")).strip()
    if not token:
        _write({"status": "configuration_error", "error": "TUSHARE_TOKEN is not set"})
        return 2

    trade_date = os.environ.get("TUSHARE_TRADE_DATE", "20260807").strip()
    try:
        date.fromisoformat(f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}")
    except ValueError:
        _write({"status": "configuration_error", "error": "TUSHARE_TRADE_DATE must be YYYYMMDD"})
        return 2

    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    try:
        count = _daily_row_count(
            endpoint=os.environ.get("TUSHARE_ENDPOINT", "https://api.tushare.pro").strip(),
            token=token,
            trade_date=trade_date,
        )
    except (HTTPError, URLError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        _write(
            {
                "status": "request_error",
                "error_type": type(error).__name__,
                "message": str(error),
                "token_fingerprint": fingerprint,
                "token_length": len(token),
                "trade_date": trade_date,
            }
        )
        return 1

    _write(
        {
            "status": "ok",
            "rows": count,
            "token_fingerprint": fingerprint,
            "token_length": len(token),
            "trade_date": trade_date,
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
