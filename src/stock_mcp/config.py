"""Host-level configuration with redacted secret handling."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

_APPLICATION_REQUIRED_SECRETS = ("TUSHARE_TOKEN",)


@dataclass(frozen=True, slots=True)
class Settings:
    root: Path
    host: str = "127.0.0.1"
    port: int = 8765
    mcp_path: str = "/mcp"
    timezone: str = "Asia/Shanghai"
    tushare_token: str | None = field(default=None, repr=False)
    tunnel_id: str | None = field(default=None, repr=False)
    tunnel_api_key: str | None = field(default=None, repr=False)
    https_proxy: str | None = None
    custom_ca_file: Path | None = None
    sina_shadow_enabled: bool = False
    sina_connect_timeout_seconds: float = 5.0
    sina_read_timeout_seconds: float = 20.0
    sina_max_retries: int = 2
    sina_history_rate_per_second: float = 0.5
    sina_spot_rate_per_second: float = 1.0

    def __post_init__(self) -> None:
        if self.sina_connect_timeout_seconds <= 0 or self.sina_read_timeout_seconds <= 0:
            raise ValueError("Sina HTTP timeouts must be positive")
        if not 0 <= self.sina_max_retries <= 2:
            raise ValueError("Sina HTTP is limited to three total attempts")
        if not 0 < self.sina_history_rate_per_second <= 0.5:
            raise ValueError("Sina history rate cannot exceed 0.5 requests per second")
        if not 0 < self.sina_spot_rate_per_second <= 1.0:
            raise ValueError("Sina spot rate cannot exceed 1 request per second")

    @classmethod
    def load(
        cls,
        *,
        root: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Settings:
        values = dict(os.environ if environ is None else environ)
        resolved_root = Path(root or values.get("STOCK_MCP_HOME") or _default_root())
        secret_values = _read_env_file(resolved_root / "config" / "secrets.env")
        app_values = _read_toml(resolved_root / "config" / "app.toml")
        sina_values = app_values.get("sina", {}) if isinstance(app_values, dict) else {}
        service_values = app_values.get("service", {}) if isinstance(app_values, dict) else {}

        host = values.get(
            "STOCK_MCP_HOST",
            str(service_values.get("bind_host", "127.0.0.1")),
        )
        if host != "127.0.0.1":
            raise ValueError("the private MCP service must bind only to 127.0.0.1")
        port = int(values.get("STOCK_MCP_PORT", str(service_values.get("bind_port", 8765))))
        if not 1 <= port <= 65535:
            raise ValueError("STOCK_MCP_PORT must be between 1 and 65535")
        mcp_path = values.get("STOCK_MCP_PATH", str(service_values.get("mcp_path", "/mcp")))
        if not mcp_path.startswith("/"):
            raise ValueError("STOCK_MCP_PATH must start with '/'")

        custom_ca = values.get("STOCK_MCP_CA_FILE") or secret_values.get("STOCK_MCP_CA_FILE")
        return cls(
            root=resolved_root,
            host=host,
            port=port,
            mcp_path=mcp_path,
            timezone=values.get(
                "STOCK_MCP_TIMEZONE", str(service_values.get("timezone", "Asia/Shanghai"))
            ),
            tushare_token=_nonblank(secret_values.get("TUSHARE_TOKEN")),
            tunnel_id=_nonblank(secret_values.get("TUNNEL_ID")),
            tunnel_api_key=_nonblank(secret_values.get("TUNNEL_API_KEY")),
            https_proxy=_nonblank(values.get("HTTPS_PROXY") or secret_values.get("HTTPS_PROXY")),
            custom_ca_file=Path(custom_ca) if custom_ca else None,
            sina_shadow_enabled=_boolean(
                values.get(
                    "STOCK_MCP_SINA_SHADOW_ENABLED", str(sina_values.get("shadow_enabled", False))
                )
            ),
            sina_connect_timeout_seconds=float(
                values.get(
                    "STOCK_MCP_SINA_CONNECT_TIMEOUT_SECONDS",
                    str(sina_values.get("connect_timeout_seconds", 5)),
                )
            ),
            sina_read_timeout_seconds=float(
                values.get(
                    "STOCK_MCP_SINA_READ_TIMEOUT_SECONDS",
                    str(sina_values.get("read_timeout_seconds", 20)),
                )
            ),
            sina_max_retries=int(
                values.get("STOCK_MCP_SINA_MAX_RETRIES", str(sina_values.get("max_retries", 2)))
            ),
            sina_history_rate_per_second=float(
                values.get(
                    "STOCK_MCP_SINA_HISTORY_RATE_PER_SECOND",
                    str(sina_values.get("history_rate_per_second", 0.5)),
                )
            ),
            sina_spot_rate_per_second=float(
                values.get(
                    "STOCK_MCP_SINA_SPOT_RATE_PER_SECOND",
                    str(sina_values.get("spot_rate_per_second", 1.0)),
                )
            ),
        )

    @property
    def database_path(self) -> Path:
        return self.root / "data" / "stock-mcp.sqlite3"

    @property
    def missing_secrets(self) -> tuple[str, ...]:
        present = {
            "TUSHARE_TOKEN": self.tushare_token,
            "TUNNEL_ID": self.tunnel_id,
            "TUNNEL_API_KEY": self.tunnel_api_key,
        }
        return tuple(name for name in _APPLICATION_REQUIRED_SECRETS if not present[name])


def _default_root() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "StockMcp"
    return Path.home() / ".local" / "share" / "StockMcp"


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"invalid secrets.env line {line_number}")
        values[key.strip()] = value.strip()
    return values


def _nonblank(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("boolean configuration must be true or false")


def _read_toml(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    with path.open("rb") as stream:
        value = tomllib.load(stream)
    return dict(value)
