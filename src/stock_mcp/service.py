"""Single-process runtime for the private loopback MCP service.

Construction is deliberately separate from serving.  ``build_runtime`` only
initialises the local database and starts the in-process scheduler; it never
contacts a provider or opens a TCP listener.  That separation gives the
Windows service a truthful health surface while an administrator is still
entering its secrets.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from argparse import ArgumentParser
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .application import StockMcpApplication
from .config import Settings
from .mcp_server import create_server
from .production import LazyAKShareQuoteProvider, ProductionPostMarketTask
from .replay_jobs import StrategyReplayCoordinator
from .storage import Database
from .strategy import DatabaseStrategyRegistry

_LOOPBACK_HOST = "127.0.0.1"
_MCP_PATH = "/mcp"
_TIMEZONE = "Asia/Shanghai"


class _UnavailableScheduler:
    """A harmless scheduler substitute when APScheduler is not installed.

    The release package installs APScheduler.  Keeping this explicit fallback
    lets ``stock-mcp doctor`` report configuration/runtime facts without a
    partially configured host repeatedly crashing its Windows service.
    """

    def __init__(self) -> None:
        self.timezone: str | None = None
        self.started = False

    def configure(self, *, timezone: str) -> None:
        self.timezone = timezone

    def start(self) -> None:
        self.started = True


class _UnavailableQuoteProvider:
    """Prevents application construction from accidentally fetching a quote."""

    def fetch_quote(self, _symbol: str) -> dict[str, object]:
        raise RuntimeError("next-day quotes require a configured runtime provider")


@dataclass(slots=True)
class ServiceRuntime:
    """The process-owned dependency graph; no listener is started here."""

    settings: Settings
    database: Any
    application: Any
    mcp_server: Any
    scheduler: Any
    replay_runner: Any | None = None
    listener_started: bool = False


def build_runtime(
    settings: Settings,
    *,
    dependencies: Mapping[str, object] | None = None,
) -> ServiceRuntime:
    """Build the singleton service graph without provider I/O or TCP binding."""

    _validate_endpoint(settings)
    resolved = dict(_default_dependencies(settings) if dependencies is None else dependencies)
    required = ("database", "application", "mcp_server", "scheduler")
    missing = tuple(name for name in required if name not in resolved)
    if missing:
        raise ValueError("service dependencies missing: " + ", ".join(missing))

    database = resolved["database"]
    scheduler = resolved["scheduler"]
    initialize = getattr(database, "initialize", None)
    if not callable(initialize):
        raise TypeError("database dependency must provide initialize()")
    initialize()

    _configure_scheduler(scheduler, settings.timezone)
    replay_runner = resolved.get("replay_runner")
    if replay_runner is not None:
        requeue = getattr(replay_runner, "requeue_interrupted", None)
        start_background = getattr(replay_runner, "start_background", None)
        if not callable(requeue) or not callable(start_background):
            raise TypeError("replay runner must provide recovery and background startup")
        requeue()
        start_background()
    return ServiceRuntime(
        settings=settings,
        database=database,
        application=resolved["application"],
        mcp_server=resolved["mcp_server"],
        scheduler=scheduler,
        replay_runner=replay_runner,
    )


def health(runtime: ServiceRuntime) -> dict[str, object]:
    """Return redacted liveness/readiness facts suitable for localhost probes."""

    database = _database_health(runtime.database)
    missing = runtime.settings.missing_secrets
    if missing:
        readyz = "configuration_required"
    elif not bool(getattr(runtime.mcp_server, "runtime_available", True)):
        readyz = "mcp_runtime_unavailable"
    elif database.get("integrity") != "ok":
        readyz = "database_unavailable"
    else:
        readyz = "ready"
    return {
        "healthz": "healthy",
        "readyz": readyz,
        "missing": missing,
        "host": runtime.settings.host,
        "port": runtime.settings.port,
        "mcp_path": runtime.settings.mcp_path,
        "database": database,
    }


def serve(settings: Settings) -> int:
    """Run MCP when fully ready, otherwise retain a local health-only surface."""

    runtime = build_runtime(settings)
    payload = health(runtime)
    if payload["readyz"] == "ready":
        return _run_mcp(runtime)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return _serve_health_only(runtime)


def main(argv: Sequence[str] | None = None) -> int:
    """Entrypoint used by ``python -m stock_mcp.service --root PATH``."""

    parser = ArgumentParser(prog="stock-mcp-service")
    parser.add_argument("--root", type=Path)
    args = parser.parse_args(argv)
    return serve(Settings.load(root=args.root))


def _default_dependencies(settings: Settings) -> dict[str, object]:
    database = Database(settings.database_path)
    strategy_registry = DatabaseStrategyRegistry(database)
    replay_runner = StrategyReplayCoordinator(
        database,
        strategy_registry,
        allowed=is_strategy_replay_allowed,
    )
    application = StockMcpApplication(
        database,
        LazyAKShareQuoteProvider(),
        strategy_registry,
        replay=replay_runner,
    )

    scheduler = _make_scheduler()
    _register_post_market_job(scheduler, ProductionPostMarketTask(settings, database))
    return {
        "database": database,
        "application": application,
        "mcp_server": create_server(
            application,
            health_provider=lambda: _mcp_route_health(settings, database),
        ),
        "scheduler": scheduler,
        "replay_runner": replay_runner,
    }


def _make_scheduler() -> Any:
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        return _UnavailableScheduler()
    return BackgroundScheduler(timezone=_TIMEZONE)


def _register_post_market_job(scheduler: Any, task: Any, *, now: datetime | None = None) -> None:
    add_job = getattr(scheduler, "add_job", None)
    if not callable(add_job):
        return
    current = now or datetime.now(ZoneInfo(_TIMEZONE))
    options: dict[str, object] = {}
    if (
        current.weekday() < 5
        and (current.hour, current.minute) >= (16, 30)
        and (current.hour, current.minute) <= (18, 0)
    ):
        options["next_run_time"] = current
    add_job(
        task,
        "cron",
        id="stock-mcp-post-market",
        day_of_week="mon-fri",
        hour="16-18",
        minute="0,10,20,30,40,50",
        timezone=_TIMEZONE,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=5_400,
        replace_existing=True,
        **options,
    )


def _configure_scheduler(scheduler: Any, timezone: str) -> None:
    configure = getattr(scheduler, "configure", None)
    if callable(configure):
        configure(timezone=timezone)
    start = getattr(scheduler, "start", None)
    if not callable(start):
        raise TypeError("scheduler dependency must provide start()")
    start()


def is_strategy_replay_allowed(now: datetime) -> bool:
    """Reserve the China post-market publication window for the daily pipeline."""

    local = now.astimezone(ZoneInfo(_TIMEZONE))
    current = (local.hour, local.minute)
    return not ((16, 20) <= current <= (18, 10))


def _validate_endpoint(settings: Settings) -> None:
    if settings.host != _LOOPBACK_HOST:
        raise ValueError("the private MCP service must bind only to 127.0.0.1")
    if settings.mcp_path != _MCP_PATH:
        raise ValueError("the private MCP service must use the /mcp endpoint")
    if settings.timezone != _TIMEZONE:
        raise ValueError("the private MCP service scheduler must use Asia/Shanghai")


def _database_health(database: Any) -> dict[str, object]:
    doctor = getattr(database, "doctor", None)
    if not callable(doctor):
        return {"integrity": "unknown"}
    try:
        result = doctor()
    except Exception as error:  # A health probe must expose, not crash on, a DB fault.
        return {"integrity": "error", "error": str(error)}
    return dict(result) if isinstance(result, Mapping) else {"integrity": "unknown"}


def _mcp_route_health(settings: Settings, database: Any) -> Mapping[str, object]:
    """Return constant-time facts for high-frequency MCP HTTP probes."""

    missing = settings.missing_secrets
    if missing:
        readyz = "configuration_required"
    else:
        is_ready = getattr(database, "is_ready", None)
        try:
            database_ready = bool(is_ready()) if callable(is_ready) else False
        except Exception:  # Readiness must report an unavailable DB, not crash HTTP.
            database_ready = False
        readyz = "ready" if database_ready else "database_unavailable"
    return {"healthz": "healthy", "readyz": readyz}


def _run_mcp(runtime: ServiceRuntime) -> int:
    run = getattr(runtime.mcp_server, "run", None)
    if not callable(run):
        raise RuntimeError("MCP runtime is unavailable; install the pinned mcp SDK and run doctor")
    kwargs = {
        "transport": "streamable-http",
        "host": runtime.settings.host,
        "port": runtime.settings.port,
        "streamable_http_path": runtime.settings.mcp_path,
        "stateless_http": True,
        "json_response": True,
    }
    result = _call_supported(run, kwargs)
    if inspect.isawaitable(result):
        asyncio.run(result)
    return 0


def _call_supported(callable_: Any, kwargs: Mapping[str, object]) -> Any:
    """Adapt only the SDK call signature; exceptions from ``run`` propagate."""

    try:
        signature = inspect.signature(callable_)
    except (TypeError, ValueError):
        return callable_(**kwargs)
    accepts_varkw = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    supported = (
        dict(kwargs)
        if accepts_varkw
        else {name: value for name, value in kwargs.items() if name in signature.parameters}
    )
    signature.bind(**supported)
    return callable_(**supported)


def _serve_health_only(runtime: ServiceRuntime) -> int:
    payload = health(runtime)

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - required HTTPServer hook
            if self.path == "/healthz":
                response, status = {"status": payload["healthz"]}, 200
            elif self.path == "/readyz":
                response = {"status": payload["readyz"], "missing": payload["missing"]}
                status = 200 if payload["readyz"] == "ready" else 503
            else:
                response, status = {"error": "not_found"}, 404
            body = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    with ThreadingHTTPServer(
        (runtime.settings.host, runtime.settings.port), HealthHandler
    ) as server:
        runtime.listener_started = True
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return 0
        finally:
            runtime.listener_started = False


if __name__ == "__main__":
    raise SystemExit(main())
