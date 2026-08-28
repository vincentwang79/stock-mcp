"""MCP transport factory.

The catalog is usable in tests without the optional MCP runtime installed.
Production startup must explicitly reject :class:`CatalogBackedServer` and
surface a doctor failure rather than pretending an HTTP MCP server is running.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from inspect import Parameter, Signature
from typing import Any

from .mcp_tools import ToolDefinition, ToolResult, build_tool_catalog

_SERVER_TITLE = "家庭 A 股盘后助手"
_SERVER_DESCRIPTION = "面向非技术用户的沪深主板 A 股盘后研究、候选解释、观察列表和复盘记录服务。"
_SERVER_INSTRUCTIONS = """\
用户可以只用自然语言交流；不要要求用户记忆或提供工具名、API 名、候选 ID、幂等键、哈希或数据库字段。
没有指定日期而询问今天、最近或最新盘后结果时，使用最新盘后结果工具，不得猜测交易日期。
只能解释服务端返回的证据，不得自行增加候选、改变分数或排序，也不得把分数解释为上涨概率。
写入观察列表、决策记录、笔记或治理状态前，必须先复述内容并取得用户明确确认。
不得执行交易、连接券商账户、决定仓位、承诺收益、持续盘中监控或主动提醒。
数据不完整、尚在观察、降级或失败时必须如实说明，不得拼接其他来源或展示较早成功结果冒充最新结果。
"""


@dataclass(frozen=True, slots=True)
class CatalogBackedServer:
    """Dependency-free fallback exposed only for catalog/contract inspection."""

    catalog: tuple[ToolDefinition, ...]
    runtime_available: bool = False


def create_server(
    service: Any,
    *,
    health_provider: Callable[[], Mapping[str, object]] | None = None,
) -> Any:
    """Create the SDK server when available, otherwise a transparent fallback.

    The MCP SDK v2 registration API is intentionally kept in this small branch
    so an SDK upgrade cannot affect the public catalog or application service.
    """

    catalog = build_tool_catalog(service)
    try:
        from mcp.server import MCPServer  # type: ignore[import-not-found]
        from mcp.types import ToolAnnotations  # type: ignore[import-not-found]
    except ImportError:
        return CatalogBackedServer(catalog)

    server = MCPServer(
        "stock-mcp",
        title=_SERVER_TITLE,
        description=_SERVER_DESCRIPTION,
        instructions=_SERVER_INSTRUCTIONS,
    )
    for definition in catalog:
        _register_tool(server, definition, ToolAnnotations)
    _register_health_routes(server, health_provider)
    server.stock_mcp_catalog = catalog
    return server


def _register_health_routes(
    server: Any,
    health_provider: Callable[[], Mapping[str, object]] | None,
) -> None:
    try:
        from starlette.requests import Request
        from starlette.responses import JSONResponse, Response
    except ImportError:
        return

    def status_payload() -> Mapping[str, object]:
        if health_provider is None:
            return {"healthz": "healthy", "readyz": "ready"}
        return health_provider()

    async def healthz(_request: Request) -> Response:
        payload = status_payload()
        return JSONResponse({"status": payload.get("healthz", "healthy")})

    async def readyz(_request: Request) -> Response:
        payload = status_payload()
        status = str(payload.get("readyz", "unavailable"))
        return JSONResponse({"status": status}, status_code=200 if status == "ready" else 503)

    server.custom_route("/healthz", methods=["GET"])(healthz)
    server.custom_route("/readyz", methods=["GET"])(readyz)


def _register_tool(server: Any, definition: ToolDefinition, annotations_type: Any) -> None:
    """Register one catalog item through MCP SDK v2's documented decorator API.

    ``MCPServer.tool`` derives its input JSON schema from a function signature.
    The catalog owns Pydantic models instead, so the small adapter below exposes
    the fields as a synthetic public signature while retaining one validation
    path in :mod:`stock_mcp.mcp_tools`.
    """

    def invoke(**arguments: Any) -> ToolResult:
        return definition.handler(**arguments)  # type: ignore[return-value]

    invoke.__name__ = definition.name
    invoke.__qualname__ = definition.name
    invoke.__doc__ = definition.description
    invoke.__signature__ = _signature_for(definition)
    annotations = annotations_type(
        title=definition.title,
        read_only_hint=definition.annotations["readOnlyHint"],
        destructive_hint=definition.annotations["destructiveHint"],
        idempotent_hint=definition.annotations["idempotentHint"],
        open_world_hint=definition.annotations["openWorldHint"],
    )
    server.tool(name=definition.name, annotations=annotations)(invoke)
    # MCPServer v2 builds an intermediate argument model from the synthetic
    # signature.  Replace only that validation model with the catalog's strict
    # DTO so Field constraints and ``extra=forbid`` survive the real transport.
    manager = getattr(server, "_tool_manager", None)
    registered = getattr(manager, "_tools", {}).get(definition.name)
    if registered is not None:
        registered.parameters = definition.input_model.model_json_schema()
        registered.fn_metadata.arg_model = definition.input_model


def _signature_for(definition: ToolDefinition) -> Signature:
    parameters: list[Parameter] = []
    for name, model_field in definition.input_model.model_fields.items():
        if model_field.is_required():
            default = Parameter.empty
        else:
            default = model_field.get_default(call_default_factory=True)
        parameters.append(
            Parameter(
                name,
                kind=Parameter.KEYWORD_ONLY,
                default=default,
                annotation=model_field.annotation,
            )
        )
    return Signature(parameters=parameters, return_annotation=definition.output_model)
