"""Small proxy-free HTTP readiness probe used by the Windows deployment scripts."""

from __future__ import annotations

from argparse import ArgumentParser
from collections.abc import Sequence
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


def probe(url: str, *, timeout_seconds: float = 5.0) -> bool:
    """Read a loopback readiness response fully and return its 2xx outcome.

    ``HttpWebRequest`` can leave Windows loopback Uvicorn connections in
    ``CloseWait`` even after the server has logged a 200 response.  The
    application runtime's standard-library client is deliberately used here
    with proxies disabled and an explicit close, so deployment validates the
    actual response lifecycle rather than only a listening TCP port.
    """

    request = Request(url, method="GET", headers={"Connection": "close"})
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            response.read()
            return 200 <= response.status < 300
    except (HTTPError, URLError, OSError, ValueError):
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(prog="stock-mcp-loopback-probe")
    parser.add_argument("url")
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    return 0 if probe(args.url, timeout_seconds=args.timeout_seconds) else 1


if __name__ == "__main__":
    raise SystemExit(main())
